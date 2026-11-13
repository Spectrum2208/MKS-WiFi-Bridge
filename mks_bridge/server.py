"""HTTP-сервер: доступ (только этот компьютер или PIN), разбор запросов и раздача панели.
Сами действия — в api.py (панель) и octoprint.py (совместимость с OctoPrint для Orca)."""
import hmac
import html
import json
import os
import re
import secrets
import socket
import socketserver
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .api import PanelApi
from .config import CFG, cfg_save, log
from .i18n import strings, tr
from .octoprint import OctoPrintApi


class QuietServer(ThreadingHTTPServer):
    """Браузер часто обрывает соединение (закрыли вкладку, обновили страницу): это не ошибка, трассировку не печатаем."""
    # На Windows SO_REUSEADDR (по умолчанию у HTTPServer) пускает второй процесс на тот же порт: две копии моста
    # работали бы одновременно. Там нужен эксклюзивный порт — тогда второй запуск получит ошибку и откроет панель первого.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, *args, **kw):
        self.conns = set()
        super().__init__(*args, **kw)

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Стандартный HTTPServer здесь спрашивает у DNS полное имя компьютера (getfqdn). Через VPN или при медленном DNS
        # это висит десятки секунд, и мост не стартует. Имя серверу не нужно — только открываем порт.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, BrokenPipeError)):
            return
        # у exe и ярлыка консоли нет: без записи в журнал ошибка в обработчике пропала бы бесследно
        log("ошибка при обработке запроса от %s:\n%s" % (client_address[0], traceback.format_exc()))

    # Открытые соединения помним, чтобы закрыть их при смене адреса (см. app.rebind): пока они живы,
    # Windows не отдаёт порт новому серверу.
    def get_request(self):
        req = super().get_request()
        self.conns.add(req[0])
        return req

    def shutdown_request(self, request):
        self.conns.discard(request)
        super().shutdown_request(request)

    def drop_clients(self):
        for s in list(self.conns):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
FAILS = {}
MAX_BODY = 600 * 1024 * 1024

# HTML/CSS/JS панели. Читаются при каждом запросе: правка панели не требует перезапуска моста
WEB_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "mks_bridge", "web")
STATIC = {"panel.css": "text/css", "panel.js": "application/javascript", "icon.png": "image/png"}


def web_file(name):
    if name.endswith(".png"):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            return f.read()
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
        return f.read()


def page(name):
    """HTML-страница на текущем языке: {{ключ}} в файле заменяется текстом из языкового файла."""
    texts = strings()
    return re.sub(r"\{\{([\w.]+)\}\}", lambda m: html.escape(texts.get(m.group(1), m.group(1))), web_file(name))


class Handler(OctoPrintApi, PanelApi, BaseHTTPRequestHandler):
    """Обработчик запросов. Принтер подставляет make_server."""
    printer = None
    protocol_version = "HTTP/1.1"
    _raw = b""

    def log_message(self, fmt, *args):
        pass

    # --- ответы ---
    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype.startswith("image/") else ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        return self._raw  # тело читается один раз в do_POST, иначе остаток портит соединение

    def _jbody(self):
        try:
            return json.loads(self._body() or b"{}")
        except ValueError:
            return {}

    # --- доступ ---
    def _local(self):
        return self.client_address[0] in LOCAL_ADDRS

    def _cookie_ok(self):
        # В cookie — длинный случайный ключ, а не сам PIN: PIN из шести цифр перебором cookie подбирался за минуты
        # (счётчик попыток есть только у формы входа), а такой ключ не подобрать.
        m = re.search(r"mks_auth=([\w\-]+)", self.headers.get("Cookie", ""))
        tok = CFG.get("token") or ""
        return bool(m and tok and CFG["remote"] and CFG["pin"] and hmac.compare_digest(m.group(1).encode(), tok.encode()))

    def _gate(self, page=False):
        if self._local():
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            if host not in LOCAL_HOSTS:
                self._send(403, "Bad Host", "text/plain")
                return False
            return True
        if not (CFG["remote"] and CFG["pin"]):
            self._send(403, tr("err.remote_off"), "text/plain")
            return False
        if self._cookie_ok():
            return True
        if page:
            self._send(200, page("login.html"), "text/html")
        else:
            self._json(401, {"error": "auth"})
        return False

    def _csrf_ok(self):
        if self.headers.get("X-Requested-With") == "mks":
            return True
        self._json(403, {"error": "missing X-Requested-With"})
        return False

    def _guard_printer(self):
        if self.printer.transferring:
            self._json(409, {"error": tr("err.uploading")})
            return False
        if self.printer.homing:
            self._json(409, {"error": tr("err.homing")})
            return False
        return True

    # --- маршруты ---
    GET = {"/api/version": "_octo_version", "/api/server": "_octo_version", "/api/printer": "_octo_printer",
           "/api/files": "_octo_files", "/api/status": "_get_status", "/api/list": "_list",
           "/api/settings": "_get_settings", "/api/outline": "_outline", "/api/limits": "_limits",
           "/api/log": "_log_download"}
    POST = {"/api/upload": "_panel_upload", "/api/delete": "_delete", "/api/print": "_print", "/api/move": "_move",
            "/api/goto": "_goto", "/api/extrude": "_extrude", "/api/fan": "_fan", "/api/console": "_console",
            "/api/settings": "_save_settings", "/api/reconnect": "_reconnect", "/api/discover": "_discover",
            "/api/quit": "_quit", "/api/logdir": "_open_logdir"}

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            if self._gate(page=True):
                self._send(200, page("panel.html"), "text/html")
            return
        if not self._gate():
            return
        name = p[len("/static/"):] if p.startswith("/static/") else None
        if name == "lang.js":   # тексты панели на текущем языке (см. i18n.py)
            self._send(200, "const LANG=%s;" % json.dumps(strings(), ensure_ascii=False), "application/javascript")
        elif name in STATIC:
            self._send(200, web_file(name), STATIC[name])
        elif p in self.GET:
            getattr(self, self.GET[p])()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            self._json(400, {"error": tr("err.bad_length")})
            return
        if n > MAX_BODY:
            self.close_connection = True
            self._json(413, {"error": tr("err.too_big")})
            return
        # Тело ВСЕГДА дочитываем (иначе при keep-alive остаток склеится со следующим запросом),
        # кроме запросов от неавторизованных: им соединение просто закрываем.
        if (p == "/login" and n <= 1024) or (p != "/login" and (self._local() or self._cookie_ok())):
            self._raw = self.rfile.read(n) if n else b""
        else:
            self._raw = b""
            self.close_connection = True
        if p == "/login":
            self._login()
            return
        if not self._gate():
            return
        if p == "/api/files/local":
            self._orca_upload()
            return
        if not self._csrf_ok():
            return
        if p in self.POST:
            getattr(self, self.POST[p])()
        else:
            self._json(404, {"error": "not found"})

    def _login(self):
        ip = self.client_address[0]
        if len(FAILS) > 200:   # чтобы список не рос без конца
            for k in [k for k, v in list(FAILS.items()) if time.time() - v[1] > 300]:
                FAILS.pop(k, None)
        n, t0 = FAILS.get(ip, (0, time.time()))
        if time.time() - t0 > 300:
            n, t0 = 0, time.time()
        if n >= 8:
            self._send(429, tr("login.too_many"), "text/plain")
            return
        pin = parse_qs(self._body().decode(errors="ignore")).get("pin", [""])[0].strip()
        # сравнение байтами: str-версия compare_digest падает на не-ASCII вводе
        if CFG["remote"] and CFG["pin"] and hmac.compare_digest(pin.encode(), CFG["pin"].encode()):
            FAILS.pop(ip, None)
            if not CFG.get("token"):   # один ключ на все телефоны; сбрасывается при смене PIN
                CFG["token"] = secrets.token_urlsafe(24)
                cfg_save()
            self._send(302, "", "text/plain", {"Location": "/", "Set-Cookie":
                       "mks_auth=%s; Path=/; Max-Age=31536000; SameSite=Strict; HttpOnly" % CFG["token"]})   # JS cookie не читает
        else:
            FAILS[ip] = (n + 1, t0)
            self._send(200, page("login.html").replace("<!--err-->", "<p class=e>%s</p>" % html.escape(tr("login.wrong"))), "text/html")


def bind_host():
    """Пока доступ с телефона выключен, порт открыт только этому компьютеру: из сети его не видно,
    и брандмауэр Windows не спрашивает разрешения."""
    return "0.0.0.0" if CFG["remote"] and CFG["pin"] else "127.0.0.1"


def make_server(printer, port, host=None):
    srv = QuietServer((host or bind_host(), port), type("H", (Handler,), {"printer": printer}))
    srv.daemon_threads = True
    return srv
