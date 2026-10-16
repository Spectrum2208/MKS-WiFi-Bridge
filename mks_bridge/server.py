"""HTTP-сервер: доступ (только этот компьютер или PIN), разбор запросов и раздача панели.
Сами действия — в api.py (панель) и octoprint.py (совместимость с OctoPrint для Orca)."""
import hmac
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .api import PanelApi
from .config import CFG
from .octoprint import OctoPrintApi


class QuietServer(ThreadingHTTPServer):
    """Браузер часто обрывает соединение (закрыли вкладку, обновили страницу): это не ошибка, трассировку не печатаем."""
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


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
        m = re.search(r"mks_pin=(\d+)", self.headers.get("Cookie", ""))
        return bool(m and CFG["remote"] and CFG["pin"] and hmac.compare_digest(m.group(1), CFG["pin"]))

    def _gate(self, page=False):
        if self._local():
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            if host not in LOCAL_HOSTS:
                self._send(403, "Bad Host", "text/plain")
                return False
            return True
        if not (CFG["remote"] and CFG["pin"]):
            self._send(403, "Удалённый доступ выключен в настройках панели.", "text/plain")
            return False
        if self._cookie_ok():
            return True
        if page:
            self._send(200, web_file("login.html"), "text/html")
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
            self._json(409, {"error": "Идёт загрузка файла, подожди."})
            return False
        if self.printer.homing:
            self._json(409, {"error": "Принтер паркуется, подожди: команды станут доступны, когда он закончит."})
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
                self._send(200, web_file("panel.html"), "text/html")
            return
        if not self._gate():
            return
        name = p[len("/static/"):] if p.startswith("/static/") else None
        if name in STATIC:
            self._send(200, web_file(name), STATIC[name])
        elif p in self.GET:
            getattr(self, self.GET[p])()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self.close_connection = True
            self._json(413, {"error": "Слишком большой запрос"})
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
        n, t0 = FAILS.get(ip, (0, time.time()))
        if time.time() - t0 > 300:
            n, t0 = 0, time.time()
        if n >= 8:
            self._send(429, "Слишком много попыток, подожди 5 минут.", "text/plain")
            return
        pin = parse_qs(self._body().decode(errors="ignore")).get("pin", [""])[0].strip()
        if CFG["remote"] and CFG["pin"] and hmac.compare_digest(pin, CFG["pin"]):
            FAILS.pop(ip, None)
            self._send(302, "", "text/plain", {"Location": "/", "Set-Cookie":
                       "mks_pin=%s; Path=/; Max-Age=31536000; SameSite=Strict" % pin})
        else:
            FAILS[ip] = (n + 1, t0)
            self._send(200, web_file("login.html").replace("<!--err-->", "<p class=e>Неверный PIN</p>"), "text/html")


def make_server(printer, port):
    srv = QuietServer(("0.0.0.0", port), type("H", (Handler,), {"printer": printer}))
    srv.daemon_threads = True
    return srv
