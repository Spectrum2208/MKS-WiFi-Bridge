#!/usr/bin/env python3
"""
MKS WiFi Bridge v2 - панель управления для принтеров с модулем MKS-WIFI (Flying Bear Ghost 4S и др.)

Запуск:  python mks_bridge_v2.py        (или переименуй в .pyw, чтобы не было консоли)
Панель:  http://127.0.0.1:5000          (во вкладке «Принтер» в Orca, в браузере, на телефоне)
Orca:    хост-тип Octo/Klipper, адрес http://127.0.0.1:5000, API-ключ любой непустой.

Зависимости: только стандартная библиотека. Для иконки в трее: pip install pystray pillow
Настройки и логи лежат в %APPDATA%\\MKSBridge\\

Протокол MKS-WIFI: файл -> HTTP POST http://IP/upload?X-Filename=имя; команды -> TCP IP:8080.
Прошивка держит одного TCP-клиента, поэтому во время заливки мы к принтеру по TCP не обращаемся.
"""
import argparse
import bisect
import hmac
import http.client
import itertools
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from email.policy import HTTP as HTTP_POLICY
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class QuietServer(ThreadingHTTPServer):
    """Браузер часто обрывает соединение (закрыли вкладку, обновили страницу): это не ошибка, трассировку не печатаем."""
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

from urllib.parse import quote, urlparse, parse_qs

VERSION = "2.0"

# ------------------------------------------------------------ настройки/логи ---

CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "MKSBridge")
os.makedirs(CFG_DIR, exist_ok=True)
CFG_PATH = os.path.join(CFG_DIR, "config.json")
LAYERS_PATH = os.path.join(CFG_DIR, "layers.json")
LOG_PATH = os.path.join(CFG_DIR, "mks_bridge.log")

DEFAULTS = {"printer_ip": "", "port": 5000, "max_name": 30, "ellipsis": "~", "overwrite": True,
            "remote": False, "pin": "", "tcp_port": 8080, "http_port": 80, "background_poll": True,
            "bed_x": 255, "bed_y": 210, "bed_z": 210, "z_bed": True}
CFG = dict(DEFAULTS)
_cfg_lock = threading.Lock()


def cfg_load():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            CFG.update(json.load(f))
    except (OSError, ValueError):
        pass


def cfg_save():
    with _cfg_lock:
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(CFG, f, ensure_ascii=False, indent=1)
        except OSError:
            pass


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


NOTIFY = [lambda title, text: None]


def notify(title, text):
    try:
        NOTIFY[0](title, text)
    except Exception:
        pass


# ----------------------------------------------- слои и оценка времени (из g-code) ---

LAYERS = {}   # имя файла на принтере (lower) -> {"size", "offsets", "est"}
STATE = {"last_name": None}


def layers_load():
    try:
        with open(LAYERS_PATH, encoding="utf-8") as f:
            LAYERS.update(json.load(f))
    except (OSError, ValueError):
        pass


def layers_save():
    data = {k: LAYERS[k] for k in list(LAYERS)[-30:]}
    try:
        with open(LAYERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def parse_duration(txt):
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)\s*([dhms])", txt)) or None


def index_gcode(path):
    """Начало каждого слоя (смещение в файле и высота Z) + оценка времени печати."""
    with open(path, "rb") as f:
        data = f.read()
    offs, zs, last = [], [], 0.0
    for m in re.finditer(rb";LAYER_CHANGE|;LAYER:\d+", data):
        chunk = data[m.end(): m.end() + 800]
        zm = re.search(rb";Z:([\d.]+)", chunk) or re.search(rb"[\r\n]G[01]\s[^\r\n;]*Z([\d.]+)", chunk)
        if zm:
            last = float(zm.group(1))
        offs.append(m.start())
        zs.append(round(last, 3))
    est = None
    m = re.search(rb"estimated printing time[^=\r\n]*=\s*([^\r\n]+)", data)
    if m:
        est = parse_duration(m.group(1).decode(errors="ignore"))
    else:
        m = re.search(rb";TIME:(\d+)", data)
        if m:
            est = int(m.group(1))
    return {"size": len(data), "offsets": offs, "zs": zs, "est": est}


def layer_info(file, progress, z):
    """Слой точно по высоте Z (из g-code); процент выполнения страхует от подъёмов Z при перемещениях."""
    idx = next((LAYERS[k.lower()] for k in (file, STATE["last_name"]) if k and k.lower() in LAYERS), None)
    if not idx or not idx["offsets"]:
        return None
    n, zs = len(idx["offsets"]), idx.get("zs") or []
    lp = None if progress is None else bisect.bisect_right(idx["offsets"], idx["size"] * progress / 100.0)
    layer, exact = lp, False
    if z is not None and len(zs) == n:
        win = None if lp is None else 1 + -(-n // 100)  # допуск: точность процента + опережение буфера
        near = lambda c: win is None or abs(c - lp) <= win
        hit = [i + 1 for i, v in enumerate(zs) if abs(v - z) < 0.011 and near(i + 1)]
        cand = hit or [i + 1 for i, v in enumerate(zs) if z - 2.0 < v <= z + 0.011 and near(i + 1)]
        if cand:
            ref = lp if lp is not None else max(cand)
            layer, exact = min(cand, key=lambda c: abs(c - ref)), len(hit) == 1
    if layer is None:
        return None
    eta = None if not idx["est"] or progress is None else int(idx["est"] * (100 - progress) / 100.0)
    return {"layer": layer, "layers": n, "eta": eta, "layer_exact": exact}


# ----------------------------------------------------------- поиск принтера ---

def local_ips():
    """Адреса этого компьютера в локальных сетях (основной маршрут первым)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # пакет не отправляется, только выбирается маршрут
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    out = []
    for ip in ips:
        if not ip.startswith(("127.", "169.254.", "0.")) and ip not in out:
            out.append(ip)
    return out


def local_ip():
    ips = local_ips()
    return ips[0] if ips else "127.0.0.1"


def discover(port=8080, hint_ip=""):
    """Ищет в подсетях хосты, которые на порту 8080 отвечают на M105 как принтер.
    Возвращает (найденные адреса, проверенные подсети)."""
    bases = []
    if os.environ.get("MKS_SCAN_BASE"):
        bases = [os.environ["MKS_SCAN_BASE"]]
    else:
        cand = local_ips()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", hint_ip or ""):
            cand.append(hint_ip)
        for ip in cand:
            b = ip.rsplit(".", 1)[0]
            if b not in bases:
                bases.append(b)
    found = []

    def probe(addr):
        try:
            with socket.create_connection((addr, port), timeout=1.0) as s:
                s.sendall(b"M105\r\n")
                s.settimeout(0.5)
                buf, end = b"", time.time() + 2.5
                while b"T:" not in buf and time.time() < end:
                    try:
                        d = s.recv(512)
                    except socket.timeout:
                        continue
                    if not d:
                        break
                    buf += d
                if b"T:" in buf:
                    found.append(addr)
        except OSError:
            pass

    targets = ["%s.%d" % (b, i) for b in bases for i in range(1, 255)]
    with ThreadPoolExecutor(96) as ex:
        list(ex.map(probe, targets))
    log("сканирование %s: найдено %r" % (bases, found))
    return sorted(set(found)), bases


# ----------------------------------------------------------------- принтер ---

_CYR = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n",
     "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y",
     "", "e", "yu", "ya"]))


def sanitize_name(name, max_len, ellipsis="~"):
    """Имя для экрана принтера: латиница, <= max_len; у длинных вырезается середина."""
    base = os.path.basename(name)
    stem, ext = os.path.splitext(base)
    if ext.lower() not in (".gcode", ".gco", ".g"):
        stem, ext = base, ".gcode"
    out = []
    for ch in stem:
        low = ch.lower()
        if low in _CYR:
            t = _CYR[low]
            out.append(t.capitalize() if ch != low else t)
        else:
            out.append(ch)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", "".join(out)).strip("_.") or "model"
    budget = max(len(ellipsis) + 2, max_len - len(ext))
    if len(stem) > budget:
        keep = budget - len(ellipsis)
        head = (keep + 1) // 2
        stem = stem[:head] + ellipsis + stem[len(stem) - (keep - head):]
    return stem + ext


PRINT_STATES = ("PRINTING", "PAUSE")


def is_printing(st):
    return any(x in (st.get("state") or "") for x in PRINT_STATES)


class Printer:
    def __init__(self, ip, http_port=80, tcp_port=8080, max_name=30, chunk=4096):
        self.ip, self.http_port, self.tcp_port = ip, http_port, tcp_port
        self.max_name, self.chunk = max_name, chunk
        self.overwrite, self.ellipsis = True, "~"
        self.on_ip_change = lambda ip: None
        self.transferring = False
        self.homing = False      # идёт парковка (G28): соединение занято ожиданием её конца
        self._home_sock = None
        self.fan = None          # последняя заданная скорость обдува, % (принтер её не сообщает)
        self.history = deque(maxlen=3000)
        self._tcp_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._cache = (0.0, None)
        self._gen = 0            # растёт после каждой команды, меняющей состояние: опрос, начатый раньше, уже устарел
        self._last_scan = 0.0
        self.last_raw = ""
        self.limits = CFG.get("limits")   # максимальные скорости из прошивки (M203), мм/с; читаются только по кнопке и запоминаются
        self.fails = 0           # подряд неудачных опросов
        self.fail_until = 0.0    # до этого момента принтер не дёргаем (пауза после сбоя)
        self.busy_until = 0.0    # принтер занят движением: не засыпаем его запросами
        self.last_err = ""

    # --- TCP ---
    def _connect(self):
        """Модуль принтера держит одного клиента: при «отказе»/сбросе соединения пробуем ещё раз, а не считаем принтер пропавшим."""
        last = None
        for i in range(3):
            try:
                return socket.create_connection((self.ip, self.tcp_port), timeout=4)
            except (ConnectionRefusedError, ConnectionResetError) as e:
                last = e
                time.sleep(0.6)
        raise last

    def _cmd_once(self, commands, wait, until):
        with self._connect() as s:
            s.sendall("".join(c + "\r\n" for c in commands).encode())
            s.settimeout(0.4)
            buf, deadline = b"", time.time() + wait
            while time.time() < deadline:
                try:
                    d = s.recv(4096)
                except socket.timeout:
                    if buf:
                        break
                    continue
                if not d:
                    break
                buf += d
                if until and until.lower().encode() in buf.lower():
                    break
        return buf.decode(errors="replace")

    def invalidate(self):
        self._cache = (0.0, self._cache[1])

    def cmd(self, commands, wait=1.5, until=None, rediscover=True):
        changes = not all(c.split()[0].upper() in ("M105", "M997", "M27", "M992", "M994", "M114", "M20") for c in commands if c.split())
        if changes:
            self.invalidate()  # после команд движения/нагрева следующий статус должен быть свежим
        toks = [c.split()[0].upper() for c in commands if c.split()]
        if "G28" in toks:
            self.busy_until = time.time() + 25     # хоуминг: в это время принтер лучше не опрашивать
        elif any(t in ("G0", "G1") for t in toks):
            self.busy_until = time.time() + 4
        with self._tcp_lock:
            try:
                r = self._cmd_once(commands, wait, until)
            except OSError:
                if not rediscover or not self.rediscover():
                    raise
                r = self._cmd_once(commands, wait, until)
            if changes:
                # Опрос, который держал соединение до нас, прочитал состояние ДО команды и сейчас положит его в кэш
                # как свежее. Новое поколение не даст ему этого: см. status_cached.
                self._gen += 1
                self.invalidate()
        self._recovered()
        return r

    def _recovered(self):
        if self.fails:
            log("связь с принтером восстановлена после %d неудачных опросов" % self.fails)
        self.fails, self.fail_until, self.last_err = 0, 0.0, ""

    def reset_link(self):
        """Кнопка «Переподключиться»: снять паузу после сбоя и попробовать заново."""
        self.fails, self.fail_until, self.busy_until, self.last_err = 0, 0.0, 0.0, ""
        self.invalidate()

    def rediscover(self, force=False):
        """Принтер не отвечает: возможно, роутер выдал ему другой IP."""
        if not force and time.time() - self._last_scan < 30:
            return False
        self._last_scan = time.time()
        found, _ = discover(self.tcp_port, self.ip)
        log("поиск принтера в сети: %r" % (found,))
        if len(found) == 1 and found[0] != self.ip:
            log("принтер переехал: %s -> %s" % (self.ip, found[0]))
            self.ip = found[0]
            self.on_ip_change(self.ip)
            notify("MKS WiFi Bridge", "Принтер нашёлся по новому адресу: " + self.ip)
            return True
        return False

    def home(self, command):
        """Парковка. Marlin не отвечает на запросы, пока паркуется, но команды из того же соединения ставит в очередь:
        шлём G28 и сразу M114 и держим соединение — ответ на M114 придёт ровно в момент окончания парковки.
        Пока ждём, прошивка каждые 2 с присылает «busy: processing»; тишина 15 с — значит связь пропала."""
        if self.homing:
            raise RuntimeError("Принтер уже паркуется.")
        self.homing = True
        started, err = threading.Event(), []

        def run():
            t0, buf, done = time.time(), b"", False
            try:
                with self._tcp_lock:
                    try:
                        s = self._connect()
                    except OSError as e:
                        err.append(e)
                        return
                    self._home_sock = s
                    started.set()
                    try:
                        s.sendall((command + "\r\nM114\r\n").encode())
                        s.settimeout(1.0)
                        last = time.time()
                        while time.time() - t0 < 240 and time.time() - last < 15:
                            try:
                                d = s.recv(4096)
                            except socket.timeout:
                                continue
                            if not d:
                                break
                            buf, last = buf + d, time.time()
                            if b"Count" in buf:
                                done = True
                                break
                    except OSError as e:
                        log("парковка: связь оборвалась: %r" % (e,))
                    finally:
                        self._home_sock = None
                        s.close()
                log("парковка %s: %s за %.1f с" % (command, "готово" if done else "конца не дождались", time.time() - t0))
            finally:
                self.homing = False
                started.set()
                self.invalidate()
            if done:
                try:
                    self.status_cached(max_age=0, quiet=True)   # панель сразу получает новые координаты
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()
        started.wait(20)
        if err:
            raise err[0]

    def emergency(self):
        """M112 во время парковки: в то же соединение, другого модуль не примет."""
        s = self._home_sock
        if s is None:
            return False
        s.sendall(b"M112\r\n")
        return True

    def files(self):
        text = self.cmd(["M20"], wait=2.5, until="End file list")
        self.last_raw = text
        names = []
        for line in text.splitlines():
            line = line.strip().lstrip("/")
            if re.search(r"\.(gcode|gco|gc|g)$", line, re.I):
                names.append(line)
        return names

    def status(self, quiet=False):
        cached = self._cache[1] or {}
        cmds = ["M105", "M997", "M114"]
        if is_printing(cached) or not cached:
            cmds = ["M105", "M997", "M27", "M992", "M994", "M114"]   # подробности нужны только во время печати
        text = self.cmd(cmds, wait=1.5, rediscover=not quiet)
        st = {"state": "unknown", "progress": None, "nozzle": None, "bed": None,
              "elapsed": None, "file": None, "pos": None}
        m = re.search(r"M997\s+([A-Z ]+)", text)
        if m:
            st["state"] = m.group(1).strip()
        m = re.search(r"M27\s+(\d+)", text)
        if m:
            st["progress"] = int(m.group(1))
        m = re.search(r"T:\s*([\d.]+)\s*/\s*([\d.]+)\s+B:\s*([\d.]+)\s*/\s*([\d.]+)", text)
        if m:
            st["nozzle"] = (float(m.group(1)), float(m.group(2)))
            st["bed"] = (float(m.group(3)), float(m.group(4)))
        m = re.search(r"M992\s+(\d+):(\d+):(\d+)", text)
        if m:
            st["elapsed"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        m = re.search(r"M994\s+/?([^\r\n;]+\.(?:gcode|gco|gc|g))", text, re.I)
        if m:
            st["file"] = m.group(1).strip()
        m = re.search(r"X:\s*(-?[\d.]+)\s+Y:\s*(-?[\d.]+)\s+Z:\s*(-?[\d.]+)(?:\s+E:\s*(-?[\d.]+))?", text)
        if m:
            st["pos"] = {"x": float(m.group(1)), "y": float(m.group(2)), "z": float(m.group(3)),
                         "e": float(m.group(4)) if m.group(4) else None}
        st["t"] = time.time()
        return st

    def get_limits(self, refresh=False):
        """Максимальные скорости и ускорения из настроек платы (M503): нужны, чтобы анимация в панели ехала с реальной скоростью."""
        if self.limits is not None and not refresh:
            return self.limits
        text = self.cmd(["M503"], wait=3.0)
        lim = {}
        m = re.search(r"M203\s+X([\d.]+)\s+Y([\d.]+)\s+Z([\d.]+)(?:\s+E([\d.]+))?", text)
        if m:
            lim["vmax"] = {"x": float(m.group(1)), "y": float(m.group(2)), "z": float(m.group(3)),
                           "e": float(m.group(4)) if m.group(4) else None}
        m = re.search(r"M204\s+P([\d.]+)(?:\s+R([\d.]+))?(?:\s+T([\d.]+))?", text)
        if m:
            lim["accel"] = {"print": float(m.group(1)), "retract": float(m.group(2)) if m.group(2) else None,
                            "travel": float(m.group(3)) if m.group(3) else None}
        self.limits = lim if lim else {}
        if lim:
            CFG["limits"] = lim
            cfg_save()
        log("лимиты из прошивки: %r" % (self.limits,))
        return self.limits

    def _merge(self, st, prev):
        """Принтер занят (хоуминг, длинный переезд) и не ответил на часть запросов: показываем последнее известное, помечая как устаревшее."""
        now = time.time()
        got = st["state"] != "unknown" or st["nozzle"] or st["pos"]
        if got:
            st["good_t"] = now
        if not prev:
            return st
        if not got:
            old = dict(prev)
            old["stale"] = True
            return old
        st["good_t"] = now
        for k in ("pos", "nozzle", "bed"):
            if st[k] is None and prev.get(k) is not None and now - prev.get("good_t", 0) < 90:
                st[k] = prev[k]
                st["stale"] = True
                if k == "pos":
                    st["pos_stale"] = True
        return st

    def mark_gap(self):
        """Принтер недоступен: пустая точка разрывает линию на графике."""
        last = self.history[-1] if self.history else None
        if last is None or last[1] is not None or last[2] is not None:
            self.history.append((int(time.time()), None, None, None, None))

    def status_cached(self, max_age=2.5, quiet=False, allow_stale=False):
        """Для панели: во время заливки к принтеру не обращаемся, одновременные запросы схлопываем."""
        if self.transferring or self.homing:
            st = dict(self._cache[1] or {"state": "unknown", "progress": None, "nozzle": None, "bed": None,
                                         "elapsed": None, "file": None, "pos": None})
            if self.transferring:
                st["state"] = "ЗАГРУЗКА ФАЙЛА"
            else:
                st["homing"] = st["stale"] = True
            return st
        with self._status_lock:
            if self._cache[1] and time.time() - self._cache[0] < max_age:
                return self._cache[1]
            now = time.time()
            if allow_stale and self._cache[1] and now < self.busy_until:      # панели и фоновому опросу: принтер занят движением, отдаём последнее известное
                old = dict(self._cache[1])
                old["stale"] = True
                return old
            if now < self.fail_until:                           # после сбоя не долбимся в модуль, ждём
                raise RuntimeError(self.last_err or "нет связи с принтером")
            gen = self._gen
            try:
                raw = self.status(quiet)
            except Exception as e:
                self.fails += 1
                self.last_err = str(e)
                self.fail_until = time.time() + min(60, 4 * 2 ** (self.fails - 1))
                if self.fails in (1, 3, 10):
                    log("опрос принтера не удался (%d подряд): %s; следующая попытка через %d с" % (self.fails, e, self.fail_until - time.time()))
                raise
            n, b = raw["nozzle"], raw["bed"]   # на график идут только реальные измерения
            self.history.append((int(time.time()), n[0] if n else None, b[0] if b else None,
                                 n[1] if n else None, b[1] if b else None))
            st = self._merge(raw, self._cache[1])
            if self._gen != gen:   # пока мы спрашивали, ушла команда движения/нагрева: ответ уже не отражает принтер
                st = dict(st, stale=True, pos_stale=True)
                self._cache = (0.0, st)
            else:
                self._cache = (time.time(), st)
            return st

    def delete(self, name, _checked=False, listed=None):
        if not _checked and is_printing(self.status()):
            raise RuntimeError("Принтер сейчас печатает, удалять файлы нельзя.")
        listed = listed if listed is not None else self.files()
        actual = next((n for n in listed if n.lower() == name.lower()), None)
        if actual is None:
            raise RuntimeError("Файла %r нет на принтере." % name)
        for form in ("M30 %s", "M30 /%s", "M30 0:/%s"):
            self.cmd([form % actual], wait=1.0)
            time.sleep(0.8)
            if actual.lower() not in [n.lower() for n in self.files()]:
                log("удалён %s командой %r" % (actual, form % "<имя>"))
                return form % "<имя>"
        raise RuntimeError("Принтер не удалил файл ни одним из вариантов M30.")

    def start_print(self, name):
        self.cmd(["M23 " + name], wait=1.0)
        time.sleep(0.5)
        self.cmd(["M24"], wait=1.0)

    # --- HTTP-заливка ---
    def upload(self, path, name, progress=lambda sent, total: None):
        with self._tcp_lock:  # на время заливки никто другой не говорит с принтером по TCP
            self.transferring = True
            try:
                self._upload_unlocked(path, name, progress)
            finally:
                self.transferring = False

    def _upload_unlocked(self, path, name, progress):
        size = os.path.getsize(path)
        conn = http.client.HTTPConnection(self.ip, self.http_port, timeout=60)
        try:
            conn.putrequest("POST", "/upload?X-Filename=" + quote(name))
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.putheader("Connection", "keep-alive")
            conn.endheaders()
            sent = 0
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(self.chunk)
                    if not chunk:
                        break
                    try:
                        conn.send(chunk)
                    except OSError as e:
                        raise RuntimeError("соединение оборвано на %d из %d байт (%d%%): %r"
                                           % (sent, size, sent * 100 // max(1, size), e))
                    sent += len(chunk)
                    progress(sent, size)
            try:
                resp = conn.getresponse()
                body = resp.read()
                log("ответ принтера на заливку: HTTP %d %r" % (resp.status, body[:200]))
                if resp.status >= 400:
                    raise RuntimeError("принтер ответил HTTP %d" % resp.status)
            except (http.client.HTTPException, ConnectionError, socket.timeout) as e:
                log("принтер не прислал нормальный ответ после заливки: %r" % (e,))
        finally:
            conn.close()


# ------------------------------------------------------------------ задачи ---

JOBS = deque(maxlen=12)
_job_lock = threading.Lock()
_job_ids = itertools.count(int(time.time() * 1000))   # два файла, брошенные разом, не должны получить один id


class Job:
    def __init__(self, name):
        self.id = next(_job_ids)
        self.name, self.state, self.sent, self.total = name, "working", 0, 0
        self.note = "В очереди…"
        JOBS.append(self)

    def post(self, m):
        k = m[0]
        if k in ("start", "title"):
            self.name = m[1]
        elif k == "progress":
            self.sent, self.total = m[1], m[2]
        elif k == "note":
            self.note = m[1]
        elif k == "done":
            self.state, self.note = "done", m[1]
            notify("Загрузка завершена", m[1])
        elif k == "error":
            self.state, self.note = "error", m[1]
            notify("Ошибка загрузки", m[1])

    def as_dict(self):
        return {"id": self.id, "name": self.name, "state": self.state,
                "sent": self.sent, "total": self.total, "note": self.note}


def run_job(printer, path, orig_name, start, ui):
    with _job_lock:
        name = sanitize_name(orig_name, printer.max_name, printer.ellipsis)
        orig_base = os.path.basename(orig_name)
        renamed = name.lower() != orig_base.lower()
        ui.post(("start", name))
        try:
            try:
                if is_printing(printer.status()):
                    raise RuntimeError("Принтер сейчас печатает, заливка файла во время печати невозможна.")
            except OSError as e:
                raise RuntimeError("Нет связи с принтером %s (%s). Открой настройки панели и нажми «Найти принтер»."
                                   % (printer.ip, e))
            idx = index_gcode(path)

            existing = {n.lower() for n in printer.files()}
            stem, ext = os.path.splitext(name)
            if name.lower() in existing and printer.overwrite:
                ui.post(("note", "Заменяю прежний файл с тем же именем…"))
                try:
                    printer.delete(name, _checked=True)
                    existing.discard(name.lower())
                except Exception as e:
                    log("не удалось убрать старый %s (%s), добавлю суффикс" % (name, e))
            i = 0
            while name.lower() in existing:
                i += 1
                suffix = "_%d" % i
                name = stem[: max(1, printer.max_name - len(ext) - len(suffix))] + suffix + ext
            ui.post(("title", name))
            ui.post(("note", "Передача на принтер…"))
            log("заливка %s (%s)" % (name, orig_name))

            for attempt in (1, 2, 3):
                try:
                    printer.upload(path, name, lambda s, t: ui.post(("progress", s, t)))
                    break
                except Exception as e:
                    log("попытка %d: %s" % (attempt, e))
                    if attempt == 3:
                        try:
                            if name.lower() in [n.lower() for n in printer.files()]:
                                printer.delete(name, _checked=True)
                                log("недокачанный %s удалён с принтера" % name)
                        except Exception as ce:
                            log("не удалось убрать недокачанный %s: %r" % (name, ce))
                        raise RuntimeError("Не удалось залить файл после 3 попыток: %s" % e)
                    ui.post(("note", "Сбой (%s), повтор %d/3…" % (e, attempt)))
                    time.sleep(2)

            confirmed, last = False, []
            for _ in range(15):  # принтер дописывает файл на карту уже после передачи
                ui.post(("note", "Принтер сохраняет файл…"))
                time.sleep(3)
                try:
                    last = printer.files()
                except OSError:
                    continue
                if name.lower() in [n.lower() for n in last]:
                    confirmed = True
                    break
            if not confirmed:
                log("в списке M20 нет %r. Сырой ответ: %r" % (name, printer.last_raw))
                raise RuntimeError("Все байты переданы, но файл не появился в списке принтера за 45 с. "
                                   "Проверь экран принтера. Печать автоматически не запускаю.")
            log("OK: %s залит и подтверждён" % name)
            LAYERS[name.lower()] = idx
            layers_save()
            STATE["last_name"] = name

            tail = " (исходное имя: %s)" % orig_base if renamed else ""
            if start:
                ui.post(("note", "Запускаю печать…"))
                printer.start_print(name)
                ui.post(("done", "Загружено, печать запущена: " + name + tail))
            else:
                ui.post(("done", "Загружено: " + name + tail))
        except Exception as e:
            log(traceback.format_exc())
            ui.post(("error", str(e)))


# ----------------------------------------------------------------- автозапуск ---

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_get():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "MKSBridge")
            return True
    except OSError:
        return False


def autostart_set(on):
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            exe = sys.executable
            if getattr(sys, "frozen", False):
                cmd = '"%s" --no-browser' % exe
            else:
                pw = exe.replace("python.exe", "pythonw.exe")
                cmd = '"%s" "%s" --no-browser' % (pw if os.path.exists(pw) else exe, os.path.abspath(__file__))
            winreg.SetValueEx(k, "MKSBridge", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "MKSBridge")
            except OSError:
                pass


# ------------------------------------------------------------------ сервер ---

SAFE_WHILE_PRINTING = {"M105", "M114", "M115", "M27", "M25", "M24", "M26", "M220", "M221", "M104",
                       "M140", "M106", "M107", "M117", "M503", "M991", "M992", "M994", "M997"}
LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
FAILS = {}
ACTIVITY = {"t": 0.0}   # когда последний раз открывали панель
MAX_BODY = 600 * 1024 * 1024


def make_handler(printer):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # --- ответы ---
        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        _raw = b""

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
                self._send(200, LOGIN_HTML, "text/html")
            else:
                self._json(401, {"error": "auth"})
            return False

        def _csrf_ok(self):
            if self.headers.get("X-Requested-With") == "mks":
                return True
            self._json(403, {"error": "missing X-Requested-With"})
            return False

        def _guard_printer(self):
            if printer.transferring:
                self._json(409, {"error": "Идёт загрузка файла, подожди."})
                return False
            if printer.homing:
                self._json(409, {"error": "Принтер паркуется, подожди: команды станут доступны, когда он закончит."})
                return False
            return True

        # --- GET ---
        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                if self._gate(page=True):
                    self._send(200, PANEL_HTML, "text/html")
                return
            if not self._gate():
                return
            if p in ("/api/version", "/api/server"):
                self._json(200, {"api": "0.1", "server": "1.9.0", "text": "OctoPrint 1.9.0 (MKS bridge)"})
            elif p == "/api/printer":
                self._json(200, {"state": {"text": "Operational", "flags": {"operational": True}}})
            elif p == "/api/files":
                self._json(200, {"files": []})
            elif p == "/api/status":
                self._json(200, self._status())
            elif p == "/api/list":
                if self._guard_printer():
                    try:
                        self._json(200, {"files": printer.files()})
                    except Exception as e:
                        self._json(502, {"error": str(e)})
            elif p == "/api/settings":
                self._json(200, self._settings())
            elif p == "/api/limits":
                if self._guard_printer():
                    try:
                        refresh = parse_qs(urlparse(self.path).query).get("refresh", ["0"])[0] == "1"
                        if printer.limits is None or refresh:
                            if printer.transferring or is_printing(printer.status_cached()):
                                self._json(200, {"limits": printer.limits or {}, "note": "печать идёт, настройки платы не запрашивались"})
                                return
                        self._json(200, {"limits": printer.get_limits(refresh)})
                    except Exception as e:
                        self._json(502, {"error": str(e)})
            else:
                self._json(404, {"error": "not found"})

        def _status(self):
            ACTIVITY["t"] = time.time()
            q = parse_qs(urlparse(self.path).query)
            try:
                win = max(60, min(7200, int(q.get("win", ["900"])[0])))
            except ValueError:
                win = 900
            now = int(time.time())
            bg = CFG.get("background_poll", True)
            out = {"ip": printer.ip, "online": False, "now": now, "jobs": [j.as_dict() for j in list(JOBS)]}
            if not printer.ip:
                out["error"] = "IP принтера не задан: открой настройки и нажми «Найти принтер»."
                out["history"] = []
                return out
            try:
                st = printer.status_cached(max_age=12 if bg else 2.5, quiet=bg, allow_stale=True)
                out.update(st)
                out["online"] = True
                if is_printing(st):
                    li = layer_info(st.get("file"), st.get("progress"), (st.get("pos") or {}).get("z"))
                    if li:
                        out.update(li)
            except Exception as e:
                out["error"] = str(e)
            out["ip"] = printer.ip  # адрес мог поменяться при автопоиске прямо во время запроса
            out["fan"] = printer.fan
            if out.get("stale") and out.get("good_t"):
                out["stale_age"] = round(time.time() - out["good_t"], 1)
            hist = [p for p in list(printer.history) if p[0] >= now - win]
            step = max(1, -(-len(hist) // 400))
            out["history"] = hist[::step]
            if hist and out["history"][-1] is not hist[-1]:
                out["history"].append(hist[-1])
            return out

        def _settings(self):
            port = CFG["port"]
            urls = ["http://%s:%d" % (socket.gethostname(), port), "http://%s:%d" % (local_ip(), port)]
            return {"printer_ip": printer.ip, "max_name": CFG["max_name"], "overwrite": CFG["overwrite"],
                    "background_poll": CFG.get("background_poll", True),
                    "geom": {"bx": CFG["bed_x"], "by": CFG["bed_y"], "bz": CFG["bed_z"], "z_bed": CFG["z_bed"]}, "remote": CFG["remote"], "pin": CFG["pin"] if self._local() else "",
                    "urls": urls, "autostart": autostart_get(), "version": VERSION, "limits": printer.limits or {},
                    "can_autostart": sys.platform == "win32"}

        # --- POST ---
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
            if p == "/api/upload":
                self._panel_upload()
            elif p == "/api/delete":
                self._delete()
            elif p == "/api/print":
                self._print()
            elif p == "/api/move":
                self._move()
            elif p == "/api/goto":
                self._goto()
            elif p == "/api/extrude":
                self._extrude()
            elif p == "/api/fan":
                self._fan()
            elif p == "/api/console":
                self._console()
            elif p == "/api/settings":
                self._save_settings()
            elif p == "/api/reconnect":
                printer.reset_link()
                try:
                    st = printer.status_cached(max_age=0)
                except Exception:
                    printer.rediscover(force=True)
                    printer.reset_link()
                    try:
                        st = printer.status_cached(max_age=0)
                    except Exception as e:
                        self._json(502, {"error": "Принтер не отвечает: %s. Если так и остаётся — выключи и включи принтер (питание), модуль Wi-Fi мог зависнуть." % e})
                        return
                self._json(200, {"ok": True, "ip": printer.ip, "state": st.get("state")})
            elif p == "/api/discover":
                if not self._guard_printer():   # сканирование стучится в TCP принтера, а во время заливки его трогать нельзя
                    return
                found, scanned = discover(printer.tcp_port, printer.ip)
                self._json(200, {"found": found, "scanned": scanned})
            elif p == "/api/quit":
                if self._local():
                    self._json(200, {"ok": True})
                    threading.Thread(target=lambda: (time.sleep(0.3), APP["stop"]()), daemon=True).start()
                else:
                    self._json(403, {"error": "только с этого компьютера"})
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
                self._send(200, LOGIN_HTML.replace("<!--err-->", "<p class=e>Неверный PIN</p>"), "text/html")

        def _orca_upload(self):
            if not self.headers.get("X-Api-Key"):
                self._json(403, {"error": "В Orca укажи любой непустой API-ключ."})
                return
            body = self._body()
            msg = BytesParser(policy=HTTP_POLICY).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            fname, data, start = None, None, False
            for part in msg.iter_parts():
                pname = part.get_param("name", header="content-disposition")
                if pname == "file":
                    fname, data = part.get_filename(), part.get_payload(decode=True)
                elif pname == "print":
                    start = part.get_content().strip().lower() == "true"
            if not fname or data is None:
                self._json(400, {"error": "no file in request"})
                return
            self._start_job(fname, data, start)
            self._json(201, {"files": {"local": {"name": fname, "origin": "local"}}, "done": True})

        def _panel_upload(self):
            q = parse_qs(urlparse(self.path).query)
            fname = q.get("name", ["model.gcode"])[0]
            self._start_job(fname, self._body(), q.get("print", ["0"])[0] == "1")
            self._json(200, {"ok": True})

        def _start_job(self, fname, data, start):
            tmp = os.path.join(tempfile.gettempdir(), "mks_bridge_%d_%s" % (int(time.time() * 1000), sanitize_name(fname, 60)))
            with open(tmp, "wb") as f:
                f.write(data)
            job = Job(sanitize_name(fname, printer.max_name, printer.ellipsis))

            def work():
                try:
                    run_job(printer, tmp, fname, start, job)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            threading.Thread(target=work, daemon=True).start()

        def _delete(self):
            if not self._guard_printer():
                return
            names = self._jbody().get("names") or []
            try:
                if is_printing(printer.status()):
                    self._json(409, {"error": "Принтер печатает, удалять файлы нельзя."})
                    return
                lst = printer.files()
            except Exception as e:
                self._json(502, {"error": str(e)})
                return
            res = []
            for n in names:
                try:
                    printer.delete(n, _checked=True, listed=lst)
                    res.append({"name": n, "ok": True})
                except Exception as e:
                    res.append({"name": n, "ok": False, "error": str(e)})
            self._json(200, {"results": res})

        def _print(self):
            if not self._guard_printer():
                return
            name = self._jbody().get("name") or ""
            try:
                if is_printing(printer.status()):
                    self._json(409, {"error": "Принтер уже печатает."})
                    return
                if name.lower() not in [n.lower() for n in printer.files()]:
                    self._json(404, {"error": "Файла нет на принтере."})
                    return
                printer.start_print(name)
                STATE["last_name"] = name
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(502, {"error": str(e)})

        def _console(self):
            b = self._jbody()
            cmd = (b.get("cmd") or "").strip()
            if not cmd or "\n" in cmd or "\r" in cmd or len(cmd) > 100:
                self._json(400, {"error": "Одна команда в одну строку."})
                return
            token = cmd.split()[0].upper()
            if token == "M112" and printer.homing:
                try:
                    printer.emergency()
                    log("консоль: M112 во время парковки")
                    self._json(200, {"reply": "M112 отправлена"})
                except OSError as e:
                    self._json(502, {"error": str(e)})
                return
            if not self._guard_printer():
                return
            try:
                if token not in SAFE_WHILE_PRINTING and not b.get("force") and is_printing(printer.status_cached()):
                    self._json(409, {"error": "Идёт печать: %s заблокирована. Отметь «Разрешить опасные команды», "
                                              "если уверен." % token})
                    return
                log("консоль: " + cmd)
                if token == "G28":
                    printer.home(cmd)
                    self._json(200, {"reply": "", "homing": True})
                    return
                reply = printer.cmd([cmd], wait=1.5).strip()
                if token == "M107":
                    printer.fan = 0
                elif token == "M106":
                    m = re.search(r"S\s*(\d+)", cmd, re.I)
                    printer.fan = min(100, round(int(m.group(1)) / 2.55)) if m else 100
                self._json(200, {"reply": reply})
            except Exception as e:
                self._json(502, {"error": str(e)})

        def _idle_guard(self, what):
            if not self._guard_printer():
                return False
            try:
                if is_printing(printer.status_cached()):
                    self._json(409, {"error": "Во время печати нельзя %s." % what})
                    return False
            except Exception as e:
                self._json(502, {"error": str(e)})
                return False
            return True

        def _goto(self):
            if not self._idle_guard("двигать оси"):
                return
            b = self._jbody()
            lim = {"x": CFG["bed_x"], "y": CFG["bed_y"], "z": CFG["bed_z"]}
            vals = {}
            for k in ("x", "y", "z"):
                if b.get(k) is None:
                    continue
                try:
                    vals[k] = float(b[k])
                except (TypeError, ValueError):
                    self._json(400, {"error": "Координата %s должна быть числом." % k.upper()})
                    return
                if not 0 <= vals[k] <= lim[k]:   # NaN и inf тоже не проходят
                    self._json(400, {"error": "%s вне допустимого диапазона 0–%g мм." % (k.upper(), lim[k])})
                    return
            if not vals:
                self._json(400, {"error": "Нужна хотя бы одна координата: x, y или z."})
                return
            try:
                cmds = ["G90"]
                xy = " ".join("%s%.1f" % (k.upper(), vals[k]) for k in ("x", "y") if k in vals)
                if xy:
                    cmds.append("G1 %s F3000" % xy)
                if "z" in vals:
                    cmds.append("G1 Z%.2f F600" % vals["z"])
                cmds.append("M114")
                log("переезд " + " ".join("%s%.1f" % (k.upper(), v) for k, v in vals.items()))
                reply = printer.cmd(cmds, wait=1.5)
                if re.search(r"home\s+[xyz/ ]+first", reply, re.I):
                    log("принтер отказался ехать: %s" % reply.strip().replace("\n", " | "))
                    self._json(409, {"error": "Принтер отказался ехать: сначала нужно сделать Home (G28) по этим осям.", "reply": reply.strip()})
                    return
                self._json(200, {"reply": reply.strip()})
            except Exception as e:
                self._json(502, {"error": str(e)})

        def _extrude(self):
            if not self._idle_guard("подавать пруток"):
                return
            try:
                dist = float(self._jbody().get("dist"))
            except (TypeError, ValueError):
                dist = 0.0
            if dist == 0 or abs(dist) > 200:
                self._json(400, {"error": "Подача от 0 до 200 мм."})
                return
            try:
                noz = printer.status_cached().get("nozzle")
                if not noz or noz[0] < 170:
                    self._json(409, {"error": "Сопло холодное (%s): прошивка не даст двигать пруток. "
                                              "Нагрей сопло до 170°+ (например, M104 S200)."
                                              % ("%.0f°" % noz[0] if noz else "нет данных")})
                    return
                log("подача E%+g" % dist)
                # M83/M82: относительная подача на время команды; слайсер выставит свой режим в начале печати
                self._json(200, {"reply": printer.cmd(["M83", "G1 E%g F300" % dist, "M82", "M114"], wait=2.0).strip()})
            except Exception as e:
                self._json(502, {"error": str(e)})

        def _fan(self):
            if not self._guard_printer():
                return
            try:
                pct = max(0, min(100, int(float(self._jbody().get("percent")))))
            except (TypeError, ValueError):
                self._json(400, {"error": "Нужен процент 0–100."})
                return
            try:
                reply = printer.cmd(["M107" if pct == 0 else "M106 S%d" % ((pct * 255 + 50) // 100)], wait=1.0).strip()
                printer.fan = pct
                self._json(200, {"reply": reply, "percent": pct})
            except Exception as e:
                self._json(502, {"error": str(e)})

        def _move(self):
            if not self._guard_printer():
                return
            b = self._jbody()
            axis = str(b.get("axis", "")).upper()
            try:
                dist = float(b.get("dist"))
            except (TypeError, ValueError):
                dist = 0.0
            if axis not in ("X", "Y", "Z") or dist == 0 or abs(dist) > 100:
                self._json(400, {"error": "Ось X/Y/Z и шаг до 100 мм."})
                return
            try:
                if is_printing(printer.status_cached()):
                    self._json(409, {"error": "Во время печати двигать оси нельзя."})
                    return
                feed = 600 if axis == "Z" else 3000
                log("движение %s%+g" % (axis, dist))
                reply = printer.cmd(["G91", "G1 %s%g F%d" % (axis, dist, feed), "G90", "M114"], wait=1.5)
                if re.search(r"home\s+[xyz/ ]+first", reply, re.I):
                    log("принтер отказался ехать: %s" % reply.strip().replace("\n", " | "))
                    self._json(409, {"error": "Принтер отказался ехать: сначала нужно сделать Home (G28) по этим осям.", "reply": reply.strip()})
                    return
                self._json(200, {"reply": reply.strip()})
            except Exception as e:
                self._json(502, {"error": str(e)})

        def _save_settings(self):
            b = self._jbody()
            # сначала проверяем всё, потом применяем: ошибка в одном поле не должна сохранить половину настроек
            ip = max_name = None
            if "printer_ip" in b:
                ip = str(b["printer_ip"]).strip()
                if ip and not re.fullmatch(r"[\w.\-]+", ip):
                    self._json(400, {"error": "Некорректный адрес принтера."})
                    return
            if "max_name" in b:
                try:
                    max_name = max(12, min(60, int(float(b["max_name"]))))
                except (TypeError, ValueError, OverflowError):
                    self._json(400, {"error": "Длина имени — число от 12 до 60."})
                    return
            bed = {}
            for k in ("bed_x", "bed_y", "bed_z"):
                if k in b:
                    try:
                        v = float(b[k])
                    except (TypeError, ValueError):
                        v = 0.0
                    if not 50 <= v <= 1000:
                        self._json(400, {"error": "Размер стола 50–1000 мм."})
                        return
                    bed[k] = v
            if ip is not None:
                printer.ip = CFG["printer_ip"] = ip
            if max_name is not None:
                printer.max_name = CFG["max_name"] = max_name
            if "overwrite" in b:
                printer.overwrite = CFG["overwrite"] = bool(b["overwrite"])
            if "background_poll" in b:
                CFG["background_poll"] = bool(b["background_poll"])
            CFG.update(bed)
            if "z_bed" in b:
                CFG["z_bed"] = bool(b["z_bed"])
            if self._local():  # доступ с телефона меняем только с самого компьютера
                if "remote" in b:
                    CFG["remote"] = bool(b["remote"])
                    if CFG["remote"] and not CFG["pin"]:
                        CFG["pin"] = "%06d" % secrets.randbelow(10 ** 6)
                if b.get("new_pin"):
                    CFG["pin"] = "%06d" % secrets.randbelow(10 ** 6)
                if "autostart" in b:
                    try:
                        autostart_set(bool(b["autostart"]))
                    except OSError as e:
                        self._json(500, {"error": "автозапуск: %s" % e})
                        return
            cfg_save()
            self._json(200, self._settings())

    return H


APP = {"stop": lambda: os._exit(0)}


def sampler(printer):
    """Фоновый опрос: график непрерывен, даже когда панель закрыта. Быстро при открытой панели, редко без неё."""
    fails, last_scan = 0, 0.0
    while True:
        delay = 3
        try:
            if CFG.get("background_poll", True) and printer.ip and not printer.transferring:
                active = time.time() - ACTIVITY["t"] < 45
                try:
                    printer.status_cached(max_age=0, quiet=True, allow_stale=True)
                    fails, delay = 0, (4 if active else 10)
                except Exception:
                    fails += 1
                    printer.mark_gap()
                    delay = 15 if fails < 4 else 30
                    if fails >= 2 and time.time() - last_scan > 120:  # принтер молчит: вдруг сменил IP
                        last_scan = time.time()
                        printer.rediscover(force=True)
        except Exception:
            delay = 5
        seen = ACTIVITY["t"]
        for _ in range(int(delay)):
            time.sleep(1)
            if delay > 3 and ACTIVITY["t"] != seen:  # открыли панель: не ждём конца паузы
                break


def run_tray(url):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return False
    img = Image.new("RGB", (64, 64), "#0e8f7a")
    ImageDraw.Draw(img).rectangle((16, 16, 48, 48), outline="white", width=6)
    icon = pystray.Icon("mks_bridge", img, "MKS WiFi Bridge", pystray.Menu(
        pystray.MenuItem("Открыть панель", lambda icon, item: webbrowser.open(url), default=True),
        pystray.MenuItem("Выход", lambda icon, item: icon.stop())))
    NOTIFY[0] = lambda title, text: icon.notify(text, title)
    APP["stop"] = icon.stop
    icon.run()
    return True


def main():
    ap = argparse.ArgumentParser(description="MKS WiFi Bridge v2")
    ap.add_argument("--ip", help="IP принтера (сохранится в настройках)")
    ap.add_argument("--port", type=int, help="порт панели (по умолчанию 5000)")
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--tcp-port", type=int)
    ap.add_argument("--http-port", type=int)
    a = ap.parse_args()

    cfg_load()
    layers_load()
    if a.ip:
        CFG["printer_ip"] = a.ip
    if a.port:
        CFG["port"] = a.port
    if a.tcp_port:
        CFG["tcp_port"] = a.tcp_port
    if a.http_port:
        CFG["http_port"] = a.http_port
    first_run = not CFG["printer_ip"]
    cfg_save()

    printer = Printer(CFG["printer_ip"], CFG["http_port"], CFG["tcp_port"], CFG["max_name"])
    printer.overwrite, printer.ellipsis = CFG["overwrite"], CFG["ellipsis"]

    def ip_changed(ip):
        CFG["printer_ip"] = ip
        cfg_save()
    printer.on_ip_change = ip_changed

    url = "http://127.0.0.1:%d" % CFG["port"]
    try:
        srv = QuietServer(("0.0.0.0", CFG["port"]), make_handler(printer))
    except OSError:
        print("Порт %d занят: возможно, мост уже запущен. Открываю панель." % CFG["port"])
        webbrowser.open(url)
        return
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=sampler, args=(printer,), daemon=True).start()
    print("MKS WiFi Bridge %s: %s" % (VERSION, url))

    if first_run:
        def first_scan():
            found, _ = discover(printer.tcp_port, printer.ip)
            if len(found) == 1:
                printer.ip = CFG["printer_ip"] = found[0]
                cfg_save()
                log("первый запуск: принтер найден %s" % found[0])
        threading.Thread(target=first_scan, daemon=True).start()
    if first_run and not a.no_browser:
        webbrowser.open(url)

    if a.no_tray or not run_tray(url):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


# --------------------------------------------------------------------- UI ---

LOGIN_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>MKS Bridge</title><style>body{font:16px system-ui;display:grid;place-items:center;height:90vh;background:#14171a;color:#eee}
form{background:#1d2125;padding:24px;border-radius:12px;text-align:center}input,button{font:inherit;padding:10px;margin:6px;border-radius:8px;border:1px solid #444}
input{width:9em;text-align:center;letter-spacing:.2em;background:#111;color:#eee}button{background:#0e8f7a;color:#fff;border:0}.e{color:#ef5350}</style>
<form method=post action=/login><h3>MKS WiFi Bridge</h3><div>PIN из настроек панели на ПК</div>
<input name=pin inputmode=numeric autocomplete=off autofocus><br><button>Войти</button><!--err--></form>"""

PANEL_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MKS WiFi Bridge</title>
<style>
:root{color-scheme:light dark;--bg:#f4f5f7;--card:#fff;--fg:#1c1f23;--mut:#6b7280;--acc:#0e8f7a;--bad:#c62828;--bd:#e2e5ea;--in:#fff}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14171a;--card:#1d2125;--fg:#e8eaed;--mut:#9aa3ad;--bd:#2b3036;--in:#14171a}}
:root[data-theme=dark]{color-scheme:dark;--bg:#14171a;--card:#1d2125;--fg:#e8eaed;--mut:#9aa3ad;--bd:#2b3036;--in:#14171a}
:root[data-theme=light]{color-scheme:light}
*{box-sizing:border-box}input[type=checkbox]{accent-color:var(--acc)}
body{margin:0 auto;max-width:1800px;padding:10px 12px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,"Segoe UI",sans-serif}
header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 10px}header h1{font-size:18px;margin:0}header .sp{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(420px,100%),1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px 14px;min-width:0;display:flex;flex-direction:column}
.card h2{flex-wrap:wrap;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 8px;display:flex;align-items:center;gap:8px}.card h2 .sp{flex:1}
@media(min-width:1450px) and (min-height:640px){
 html,body{height:100%}body{display:flex;flex-direction:column;overflow:hidden}
 .grid{flex:1;min-height:0;grid-template-columns:minmax(0,40fr) minmax(0,25fr) minmax(0,35fr);grid-template-rows:minmax(0,232fr) minmax(0,323fr);grid-template-areas:"top ext c3" "fro low c3"}
 .grid>#c3{display:flex;flex-direction:column;gap:12px;min-height:0;grid-area:c3}
 #cTop{grid-area:top}#cFro{grid-area:fro}#cEx{grid-area:ext}.grid .lowb{grid-area:low;display:flex;flex-direction:column;gap:12px;min-height:0}
 .card{min-height:0;overflow:auto}#c3>.card{flex:1 1 0}#cTm{flex-grow:8}#cCs{flex-grow:10}#cCtl{flex:0 0 auto}#cFl{flex:1 1 0}
 .vwrap{flex:1;min-height:0;overflow:hidden;display:flex;align-items:center;justify-content:center}
 .vrow{flex-wrap:nowrap}.vrow .sv{width:var(--vw,100%);max-width:none}
 .xw{flex:1;min-height:0;container-type:size;display:flex;align-items:center;justify-content:center}
 .xl .sv{width:min(calc(100cqw - 172px),calc((100cqh - 40px)*.634))}
 .xin .xl{flex:none;width:auto}
 #files,#reflist{flex:1;min-height:40px;max-height:none}#log{flex:1;height:auto;min-height:40px}.cv{flex:1;height:auto;min-height:100px}}
.card h2 .dim{text-transform:none;letter-spacing:0;font-weight:400;opacity:.75}.card h2 .dim::before{content:"·";margin-right:6px}
.badge{font-size:12px;padding:3px 10px;border-radius:99px;background:var(--bd)}.badge.on{background:var(--acc);color:#fff}.badge.off{background:var(--bad);color:#fff}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 14px}.kv span:nth-child(odd){color:var(--mut)}
.big{font-size:28px;font-weight:600}.bar{height:10px;background:var(--bd);border-radius:6px;overflow:hidden;margin:6px 0}.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .4s}
button{font:inherit;font-weight:600;font-size:13.5px;padding:7px 14px;border-radius:9px;border:1px solid var(--bd);background:var(--bg);color:var(--fg);cursor:pointer;transition:border-color .15s,background .15s,transform .08s,filter .15s}
button:hover{border-color:color-mix(in srgb,var(--acc) 65%,var(--bd))}button:active{transform:scale(.97)}
button.p{background:var(--acc);color:#fff;border:1px solid var(--acc)}button.p:hover{filter:brightness(1.1)}button.d{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,var(--bd))}button.sm{padding:6px 12px;font-size:13px}button.s{padding:4px 10px;font-size:12px;border-radius:8px}button.s.on{background:var(--acc);color:#fff;border-color:var(--acc)}
input[type=text],input[type=number]{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--bd);background:var(--in);color:var(--fg);min-width:0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:5px 0}.mut{color:var(--mut);font-size:13px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}.chip{border-left:4px solid var(--c);padding:2px 10px;background:var(--bg);border-radius:6px}.chip b{font-size:18px;margin-left:6px}
.cv{position:relative;height:200px}canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:pan-y}
.drop{border:2px dashed var(--bd);border-radius:10px;padding:9px;text-align:center;color:var(--mut);cursor:pointer;font-size:14px}.drop.h{border-color:var(--acc);color:var(--fg)}
#jobs{max-height:130px;overflow:auto}.job{padding:6px 0;border-top:1px solid var(--bd)}.job b{word-break:break-all}.job.error .n{color:var(--bad)}.job.done .n{color:var(--acc)}
#files{max-height:300px;overflow:auto}#files label{display:flex;gap:8px;padding:4px 0;border-top:1px solid var(--bd);word-break:break-all}
#log{height:200px;overflow:auto;background:var(--in);border:1px solid var(--bd);border-radius:8px;padding:8px;font:12px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap}
#reflist{overflow:auto;max-height:340px}.rg{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:10px 0 4px}
.rr{display:flex;gap:10px;align-items:baseline;padding:4px 0;border-top:1px solid var(--bd)}.rr code{flex:none;min-width:84px;text-align:left;font:600 12px ui-monospace,Consolas,monospace;padding:3px 8px;border-radius:6px;background:var(--bg);border:1px solid var(--bd);color:var(--fg);cursor:pointer}
.rr code:hover{border-color:var(--acc)}.rr.dg code{color:var(--bad)}.rr span{font-size:13px}
.ic{width:44px;height:44px;padding:0;display:inline-grid;place-items:center;border:0;color:#fff;border-radius:10px}
.amber{background:#f59e0b}.green{background:#16a34a}.red{background:#dc2626}.blue{background:#2563eb}.gray{background:#64748b}.teal{background:var(--acc)}
.stats{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:4px}.stats .big{min-width:64px}.stats .sb{flex:1;min-width:140px}
.kv2{display:flex;gap:14px;flex-wrap:wrap;color:var(--mut)}.kv2 b{color:var(--fg);font-weight:600}
#tBed,#fBed,#fBlock,#fNoz,#tNoz,#eBlock,#eNoz{transition:fill 1.4s linear}
.col,.lowb{display:contents}.vwrap,.xw{position:relative}
.veil{position:absolute;inset:0;z-index:3;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;text-align:center;padding:12px;border-radius:10px;background:color-mix(in srgb,var(--card) 68%,transparent);backdrop-filter:blur(2px);-webkit-backdrop-filter:blur(2px)}
.veil[hidden]{display:none}.veil b{font-size:14px;padding:6px 14px;border-radius:99px;background:var(--bg);border:1px solid var(--bd)}.veil span{font-size:12px;color:var(--mut);max-width:260px}
.bt{display:inline-flex;align-items:center;gap:7px;padding:7px 14px;border-radius:9px;font-weight:600;font-size:13.5px;line-height:1.1}.bt svg{width:18px;height:18px}.bt span{display:inline-grid;line-height:0}
.bt.p{background:var(--acc);color:#fff;border:1px solid var(--acc)}.bt.d{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,var(--bd))}.bt:hover{filter:brightness(1.12)}
.seg,.steps{background:var(--bg);border:1px solid var(--bd);border-radius:9px;padding:2px}.seg{display:inline-flex;gap:2px}
.seg button,.steps button{border:0!important;background:transparent!important;color:var(--mut);padding:4px 10px;font-size:12px;border-radius:7px;font-weight:600}.seg button:hover,.steps button:hover{color:var(--fg)}
.seg button.on,.steps button.on{background:var(--acc)!important;color:#fff!important}.steps button{padding:4px 0}
.bt.blk{width:100%;justify-content:center}
.tiles{display:flex;flex-direction:column;gap:8px;margin-bottom:8px}
.tile{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:9px 12px 9px 16px;border:1px solid var(--bd);border-radius:12px;background:var(--bg);position:relative;overflow:hidden}
.tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:5px;background:var(--c)}
.tv{display:flex;flex-direction:column;gap:2px;line-height:1.05}.tl{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);font-weight:600}
.tv b{font-size:25px;font-weight:700;font-variant-numeric:tabular-nums}.tv b i{font-style:normal;font-size:15px;font-weight:600;color:var(--mut)}
.tc{display:flex;align-items:center;gap:6px}
.tin{display:flex;align-items:center;background:var(--card);border:1px solid var(--bd);border-radius:9px;padding-right:10px}.tin:focus-within{border-color:var(--acc)}
.tin input{width:62px;border:0!important;background:transparent!important;padding:7px 6px;text-align:right;font-weight:600;outline:0}.tin input::-webkit-inner-spin-button,.tin input::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}.tin i{font-style:normal;color:var(--mut);font-size:13px}
.hint{font-size:11px;text-transform:none;letter-spacing:0;font-weight:400;color:var(--mut)}.btnrow{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:6px}
.vrow{display:flex;gap:10px;justify-content:center;align-items:flex-start;flex-wrap:wrap}
.vcol{min-width:0;display:flex;flex-direction:column;gap:3px}
.cap{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sv{width:100%;max-width:640px;display:block;user-select:none;-webkit-user-select:none;touch-action:manipulation}.sv text{pointer-events:none}
.side{width:136px;flex:none;display:flex;flex-direction:column;gap:9px;padding-top:16px}.xs{width:150px;padding-top:0}
.fld{display:flex;flex-direction:column;gap:4px}.fld>.mut{font-size:12px}.sm{font-size:12px}
.rd{font:600 13px/1.3 ui-monospace,Consolas,monospace;white-space:nowrap}
.pad{display:grid;grid-template-columns:repeat(3,38px);grid-auto-rows:38px;gap:4px}
.kb{width:38px;height:38px;padding:0;display:inline-grid;place-items:center;border-radius:10px;background:var(--bg);border:1px solid var(--bd);color:var(--fg);transition:border-color .15s,color .15s,transform .08s}
.kb:hover{border-color:var(--acc);color:var(--acc)}.kb:active{transform:scale(.94)}.kb.home{color:var(--acc);border-color:var(--acc)}
.kb.t{width:30px;height:30px;border-radius:8px}.kb.w{width:100%;height:34px;display:flex;gap:6px;align-items:center;justify-content:center;font-size:13px;padding:0 6px}
.kb.w span{display:inline-grid;line-height:0}.kb.w svg,.kb.t svg{width:20px;height:20px}
.zrow{display:flex;gap:4px}
.inl{display:flex;gap:4px}.inl input{width:0;flex:1;padding:5px 6px}.inl button{padding:5px 9px}
.lnk{border:0;background:none;color:var(--mut);padding:0;font-size:12px;text-align:left;text-decoration:underline;text-decoration-style:dotted}.lnk:hover{color:var(--fg)}
.side .steps{display:grid;grid-template-columns:repeat(4,1fr);gap:3px;width:128px}.side .steps button{padding:3px 0;font-size:12px}.xs .steps{width:100%}.steps button{border-radius:7px}.steps button.on{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:600}
.tset{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.tset input{width:76px;padding:5px 7px}.tset button{padding:5px 10px}.tset .chip{margin-right:2px;min-width:176px}
.tbs{display:flex;gap:16px}.tb{border:0;background:none;padding:2px 0;font:inherit;text-transform:inherit;letter-spacing:inherit;color:var(--mut);border-bottom:2px solid transparent;border-radius:0}.tb.on{color:var(--fg);border-bottom-color:var(--acc)}
.pane{flex:1;min-height:0;display:flex;flex-direction:column}.pane[hidden]{display:none}
.xl{display:flex;flex-direction:column;gap:6px;min-width:0;width:260px;max-width:100%}
.xin{display:flex;gap:12px;align-items:flex-start;justify-content:center;flex-wrap:wrap}
.fanrow{display:flex;gap:8px;align-items:center}.fanrow b{min-width:38px;text-align:right;font-size:13px}
.rng{-webkit-appearance:none;appearance:none;flex:1;min-width:0;height:22px;background:transparent;margin:0;cursor:pointer;--v:0%}
.rng::-webkit-slider-runnable-track{height:6px;border-radius:3px;background:linear-gradient(to right,var(--acc) var(--v),var(--bd) var(--v))}
.rng::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--card);border:2px solid var(--acc);margin-top:-5px;box-shadow:0 1px 3px #0005}
.rng::-moz-range-track{height:6px;border-radius:3px;background:var(--bd)}.rng::-moz-range-progress{height:6px;border-radius:3px;background:var(--acc)}
.rng::-moz-range-thumb{width:12px;height:12px;border-radius:50%;background:var(--card);border:2px solid var(--acc)}
.tbs{display:flex;gap:16px}.tb{border:0;background:none;padding:2px 0;font:inherit;text-transform:inherit;letter-spacing:inherit;color:var(--mut);border-bottom:2px solid transparent;border-radius:0}.tb.on{color:var(--fg);border-bottom-color:var(--acc)}
.pane{flex:1;min-height:0;display:flex;flex-direction:column}.pane[hidden]{display:none}
@keyframes dash{to{stroke-dashoffset:-16}}.ring{animation:dash 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}.blades{transform-box:fill-box;transform-origin:center;animation:spin 1s linear infinite;animation-play-state:paused}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;background:#222;color:#fff;padding:10px 16px;border-radius:10px;max-width:90vw;display:none;z-index:9}
.ov{position:fixed;inset:0;background:#0009;display:none;place-items:center;z-index:8}.ov.on{display:grid}.sbox{width:min(580px,94vw);max-height:90vh;overflow:auto}
.tiles.t2{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:8px}
.t2 .tile{flex-wrap:nowrap;gap:8px;padding:7px 8px 7px 14px}.t2 .tv b{font-size:23px;white-space:nowrap}
.t2 .tc{gap:4px}.t2 .tin input{width:42px;padding:6px 4px}.t2 .kb.t{width:32px;height:32px}.t2 .kb.t svg{width:18px;height:18px}
.kb.cool{color:#4fc3f7}.kb.cool:hover{color:#4fc3f7;border-color:#4fc3f7}
.kb.go{background:var(--acc);border-color:var(--acc);color:#fff}.kb.go:hover{color:#fff;filter:brightness(1.1)}
button.s.ib{display:inline-flex;align-items:center;gap:5px;text-transform:none;letter-spacing:0}.s.ib span{display:inline-grid;line-height:0;color:#4fc3f7}.s.ib svg{width:14px;height:14px}
.sbox{width:min(600px,94vw);padding:16px 18px 14px;gap:6px;box-shadow:0 12px 40px #0006}#ov.on .sbox{animation:pop .14s ease-out}
.shd{display:flex;align-items:center;gap:10px;margin-bottom:2px}.shd h3{margin:0;font-size:17px}.shd .sp{flex:1}.shd .kb{font-size:14px}
.sbox .rg{margin:12px 0 2px}
.srow{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:10px 12px;border:1px solid var(--bd);border-radius:12px;background:var(--bg)}
.sl{display:flex;flex-direction:column;gap:2px;min-width:0}.sl b{font-size:14px;font-weight:600}.sl span{font-size:12.5px;color:var(--mut);line-height:1.35}
.sc{display:flex;gap:6px;align-items:center;flex:none}
.sin{width:140px;font:600 14px ui-monospace,Consolas,monospace!important;background:var(--card)!important}
.tin.sxyz{padding:0 8px 0 9px}.tin.sxyz input{width:46px;padding-left:4px}.tin.sxyz i:first-child{font-weight:600}
.sw{position:relative;flex:none;width:42px;height:24px;cursor:pointer}.sw input{position:absolute;opacity:0;width:100%;height:100%;margin:0;cursor:pointer}
.sw i{position:absolute;inset:0;border-radius:99px;background:var(--bd);transition:background .15s;pointer-events:none}
.sw i::after{content:"";position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 1px 3px #0004;transition:transform .15s}
.sw input:checked+i{background:var(--acc)}.sw input:checked+i::after{transform:translateX(18px)}.sw input:focus-visible+i{outline:2px solid var(--acc);outline-offset:2px}
.sremote{padding:10px 12px;border:1px dashed var(--acc);border-radius:12px;display:flex;flex-direction:column;gap:6px;font-size:13px}.sremote[hidden]{display:none}
.sremote code{font:600 13px ui-monospace,Consolas,monospace;padding:2px 8px;border-radius:6px;background:var(--bg);border:1px solid var(--bd);user-select:all;white-space:nowrap;display:inline-block;margin:2px 0}
.sremote .sp{flex:1}.pin{font:700 22px ui-monospace,Consolas,monospace;letter-spacing:.18em;color:var(--fg)}
.sft{display:flex;justify-content:flex-end;margin-top:12px}
@media(max-width:520px){.srow{flex-wrap:wrap}.sc{flex-wrap:wrap}}
#dlg{z-index:10;backdrop-filter:blur(2px);-webkit-backdrop-filter:blur(2px)}#dlg.on .dbox{animation:pop .14s ease-out}
@keyframes pop{from{transform:scale(.96);opacity:0}}
.dbox{width:min(420px,92vw);padding:18px 20px 16px;gap:8px;box-shadow:0 12px 40px #0006}.dbox h3{margin:0;font-size:17px;line-height:1.3}
.dbox p{margin:0;color:var(--mut);font-size:14px;line-height:1.45;word-break:break-word}.dbox p:empty{display:none}
.dch{display:flex;flex-direction:column;gap:6px;margin-top:4px}.dch:empty{display:none}.dch button{text-align:left;font:600 14px ui-monospace,Consolas,monospace}
.dbtn{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}button.dz{background:var(--bad);border-color:var(--bad);color:#fff}button.dz:hover{filter:brightness(1.1)}
</style></head><body>
<header><h1>MKS WiFi Bridge</h1><span id="state" class="badge">…</span><span class="mut">принтер: <span id="ip">—</span></span><span class="sp"></span>
<button class="p sm" id="homeAll" title="Домой по всем осям (G28)" onclick="cmd('G28')" style="display:inline-flex;align-items:center;gap:6px"><span data-i="home" style="display:inline-grid;line-height:0"></span>Домой все оси</button>
<button id="thm" onclick="cycleTheme()" title="Тема">◐</button><button id="gear" onclick="openSet()" title="Настройки"></button></header>
<div class="grid">
<div class="col" id="c1">
<div class="card" id="cTop"><h2>Вид сверху <span class="dim" id="dimTop"></span><span class="sp"></span><span class="hint">клик по головке, затем по точке на столе</span></h2>
 <div class="vwrap"><div class="veil" hidden><b></b><span></span><button class="bt rc" onclick="reconnect()">Переподключиться</button></div><div class="vrow"><div class="vcol"><svg id="svgTop" class="sv" viewBox="0 0 300 232" preserveAspectRatio="xMidYMid meet"></svg></div>
  <div class="side"><div class="fld"><span class="mut">Позиция</span><div class="rd" id="tCoord">—</div></div>
    <div class="pad"><span></span><button class="kb" data-i="chev" data-r="0" title="Y+ (головка назад)" onclick="mv('Y',1)"></button><span></span>
     <button class="kb" data-i="chev" data-r="270" title="X−" onclick="mv('X',-1)"></button><button class="kb home" data-i="home" title="Домой по X и Y (G28 X Y)" onclick="cmd('G28 X Y')"></button><button class="kb" data-i="chev" data-r="90" title="X+" onclick="mv('X',1)"></button>
     <span></span><button class="kb" data-i="chev" data-r="180" title="Y− (головка вперёд)" onclick="mv('Y',-1)"></button><span></span></div>
    <div class="fld"><span class="mut">Шаг, мм</span><div class="steps" id="steps"></div></div>
    <button class="bt blk" title="Отключить моторы (M84): оси можно двигать руками" onclick="cmd('M84')"><span data-i="motor" style="color:var(--mut)"></span>Моторы</button></div></div></div></div>
<div class="card" id="cFro"><h2>Вид спереди <span class="dim" id="dimFront"></span><span class="sp"></span><span class="hint">клик по головке — X, по столу — Z</span></h2>
 <div class="vwrap"><div class="veil" hidden><b></b><span></span><button class="bt rc" onclick="reconnect()">Переподключиться</button></div><div class="vrow"><div class="vcol"><svg id="svgFront" class="sv" viewBox="0 0 300 353" preserveAspectRatio="xMidYMid meet"></svg></div>
  <div class="side"><div class="fld"><span class="mut">Высота</span><div class="rd" id="fZ">Z —</div></div>
    <div class="fld"><span class="mut" id="zcap">Стол</span><div class="zrow"><button class="kb" id="zUp" data-i="chev" data-r="0" onclick="zMove(true)"></button><button class="kb" id="zDn" data-i="chev" data-r="180" onclick="zMove(false)"></button></div><span class="mut sm">шаг <b id="zstep">10</b> мм</span></div>
    <button class="bt blk" title="Домой по Z (G28 Z)" onclick="cmd('G28 Z')"><span data-i="home" style="color:var(--acc)"></span>Домой Z</button></div></div></div></div>
</div>
<div class="col" id="c2">
<div class="card" id="cEx"><h2>Экструдер</h2>
 <div class="xw"><div class="veil" hidden><b></b><span></span><button class="bt rc" onclick="reconnect()">Переподключиться</button></div><div class="xin">
  <div class="xl"><svg id="svgExt" class="sv" viewBox="0 0 180 284" preserveAspectRatio="xMidYMid meet"></svg>
   <div class="fanrow" title="Принтер не сообщает скорость обдува: показывается последнее значение, заданное отсюда"><span class="mut">Обдув</span><input type="range" class="rng" id="fanr" min="0" max="100" step="5" value="0" oninput="fanLabel()" onchange="setFan()"><b id="fanv">—</b></div></div>
  <div class="side xs"><div class="fld"><span class="mut">Положение E</span><div class="rd" id="eE">E —</div></div>
   <div class="fld"><span class="mut">Подача, мм</span><div class="steps" id="esteps"></div>
    <button class="bt blk" title="Втянуть пруток (E−)" onclick="ex(-1)"><span data-i="chev" data-r="0" style="color:#f59e0b"></span>Втянуть</button>
    <button class="bt blk" title="Протолкнуть пруток (E+)" onclick="ex(1)"><span data-i="chev" data-r="180" style="color:#16a34a"></span>Протолкнуть</button></div></div></div></div></div>
<div class="lowb">
<div class="card" id="cCtl"><h2>Печать</h2>
 <div class="stats"><div class="big" id="progtxt">—</div><div class="sb"><div class="bar"><i id="prog"></i></div><div class="mut" id="file"></div></div></div>
 <div class="kv2"><span>Слой <b id="layer">—</b></span><span>Z <b id="zv">—</b></span><span>Прошло <b id="elapsed">—</b></span><span>Осталось <b id="eta">—</b></span></div>
 <div class="mut" id="err" style="color:var(--bad)"></div>
 <div class="btnrow"><button class="bt blk" title="Пауза (M25)" onclick="cmd('M25')"><span data-i="pause" style="color:#f59e0b"></span>Пауза</button><button class="bt blk" title="Продолжить (M24)" onclick="cmd('M24')"><span data-i="play" style="color:#16a34a"></span>Пуск</button>
  <button class="bt blk" title="Стоп (M26)" onclick="cmd('M26')"><span data-i="stop" style="color:#dc2626"></span>Стоп</button></div></div>
<div class="card" id="cFl"><h2>Файлы</h2>
 <div class="drop" id="drop">Перетащи .gcode сюда или нажми, чтобы выбрать<input id="pick" type="file" accept=".gcode,.gco,.g" multiple hidden></div>
 <label class="row mut"><input type="checkbox" id="startAfter"> сразу запускать печать после загрузки</label><div id="jobs"></div>
 <div class="row"><button class="bt" onclick="loadFiles()" title="Обновить список файлов на принтере"><span data-i="refresh"></span>Обновить</button><button class="bt d" onclick="delSel()" title="Удалить отмеченные файлы с принтера"><span data-i="trash"></span>Удалить</button><button class="bt p" onclick="printSel()" title="Запустить печать выбранного файла"><span data-i="play"></span>Печатать</button></div>
 <div id="files" class="mut">Нажми «Обновить», чтобы увидеть файлы на принтере</div></div>
</div>
</div>
<div class="col" id="c3">
<div class="card" id="cTm"><h2>Температуры <span class="sp"></span><button class="s ib" onclick="coolAll()" title="Выключить нагрев сопла и стола (M104 S0, M140 S0)"><span data-i="snow"></span>Остудить всё</button><span id="ranges" class="seg"></span></h2>
 <div class="tiles t2">
  <div class="tile" style="--c:#ef6c00"><div class="tv"><span class="tl">Сопло</span><b id="nozzle">—</b></div>
   <div class="tc"><div class="tin"><input type="number" id="tn" placeholder="цель" min="0" max="300" onkeydown="if(event.key==='Enter')setT('M104','tn')"><i>°C</i></div><button class="kb t go" data-i="check" title="Задать температуру сопла" onclick="setT('M104','tn')"></button><button class="kb t cool" data-i="snow" title="Остудить сопло (M104 S0)" onclick="heatOff('M104')"></button></div></div>
  <div class="tile" style="--c:#1e88e5"><div class="tv"><span class="tl">Стол</span><b id="bed">—</b></div>
   <div class="tc"><div class="tin"><input type="number" id="tb" placeholder="цель" min="0" max="120" onkeydown="if(event.key==='Enter')setT('M140','tb')"><i>°C</i></div><button class="kb t go" data-i="check" title="Задать температуру стола" onclick="setT('M140','tb')"></button><button class="kb t cool" data-i="snow" title="Остудить стол (M140 S0)" onclick="heatOff('M140')"></button></div></div></div>
 <div class="cv"><canvas id="chart"></canvas></div>
 <div class="mut sm">Сплошная — факт, пунктир — цель, разрыв — нет данных. Наведи на график, чтобы увидеть значения.</div></div>
<div class="card" id="cCs"><h2><span class="tbs"><button class="tb on" data-t="log" onclick="tab('log')">G-code консоль</button><button class="tb" data-t="ref" onclick="tab('ref')">Справочник команд</button></span><span class="sp"></span><button class="s" id="clr" onclick="$('#log').textContent=''">Очистить</button></h2>
 <div class="pane" id="pLog"><div id="log"></div></div>
 <div class="pane" id="pRef" hidden><input type="text" id="refq" placeholder="поиск: температура, M115, home…" style="width:100%" oninput="renderRef()">
  <div class="mut" style="margin:4px 0">Нажатие вставляет команду в строку ввода ниже: допиши параметры и нажми Enter. Красные при отправке просят подтверждение.</div><div id="reflist"></div></div>
 <div class="row"><input type="text" id="cin" placeholder="команда, ↑↓ — история" style="flex:1" onkeydown="conKey(event)"><button class="p" onclick="sendCon()">Отправить</button></div>
 <label class="mut"><input type="checkbox" id="force"> Разрешить опасные команды во время печати</label></div>
</div>
</div>
<div id="ov" class="ov" onclick="if(event.target===this)closeSet()"><div class="card sbox" role="dialog" aria-modal="true" aria-labelledby="setT">
 <div class="shd"><h3 id="setT">Настройки</h3><span class="mut" id="ver"></span><span class="sp"></span><button class="kb t" onclick="closeSet()" title="Закрыть (Esc)" aria-label="Закрыть">✕</button></div>
 <div class="rg">Подключение</div>
 <div class="srow"><div class="sl"><b>Адрес принтера</b><span>IP модуля MKS-WIFI в домашней сети</span></div>
  <div class="sc"><input type="text" id="sip" class="sin" spellcheck="false" onchange="saveIp()"><button class="sm" onclick="findPrinter()">Найти</button></div></div>
 <div class="srow"><div class="sl"><b>Опрос в фоне</b><span>График температур не прерывается, даже когда панель закрыта</span></div>
  <label class="sw"><input type="checkbox" id="sbg" onchange="saveOpt()"><i></i></label></div>
 <div class="rg">Файлы</div>
 <div class="srow"><div class="sl"><b>Заменять файл с тем же именем</b><span>Иначе к имени добавится _1, _2…</span></div>
  <label class="sw"><input type="checkbox" id="sow" onchange="saveOpt()"><i></i></label></div>
 <div class="srow"><div class="sl"><b>Длина имени на принтере</b><span>Длинные имена укорачиваются с середины, 12–60 символов</span></div>
  <div class="tin"><input type="number" id="smax" min="12" max="60" onchange="saveOpt()"><i>симв.</i></div></div>
 <div class="rg">Принтер</div>
 <div class="srow"><div class="sl"><b>Размер области печати</b><span>Границы для перемещений и модели в панели</span></div>
  <div class="sc"><div class="tin sxyz"><i>X</i><input type="number" id="gbx" min="50" max="1000" onchange="saveOpt()"></div><div class="tin sxyz"><i>Y</i><input type="number" id="gby" min="50" max="1000" onchange="saveOpt()"></div><div class="tin sxyz"><i>Z</i><input type="number" id="gbz" min="50" max="1000" onchange="saveOpt()"><i>мм</i></div></div></div>
 <div class="srow"><div class="sl"><b>По Z движется стол</b><span>Как у Ghost: +Z опускает стол. Выключи, если по Z ездит головка</span></div>
  <label class="sw"><input type="checkbox" id="gzb" onchange="saveOpt()"><i></i></label></div>
 <div class="srow"><div class="sl"><b>Скорости из прошивки</b><span id="lims">Не считаны: анимация в панели едет с примерной скоростью</span></div>
  <button class="sm" onclick="loadLimits()" title="Прочитать M203/M204 командой M503">Считать</button></div>
 <div class="rg">Доступ и запуск</div>
 <div class="srow"><div class="sl"><b>Доступ с телефона</b><span>Панель откроется с других устройств в сети по PIN-коду</span></div>
  <label class="sw"><input type="checkbox" id="srem" onchange="saveRemote()"><i></i></label></div>
 <div class="sremote" id="remote" hidden></div>
 <div class="srow" id="autorow"><div class="sl"><b>Запускать вместе с Windows</b><span>Мост стартует в фоне при входе в систему</span></div>
  <label class="sw"><input type="checkbox" id="sauto" onchange="saveAuto()"><i></i></label></div>
 <div class="sft"><button class="bt d" onclick="quitApp()"><span data-i="power"></span>Выход из программы</button></div></div></div>
<div id="toast" class="toast"></div>
<div id="dlg" class="ov"><div class="card dbox" role="alertdialog" aria-modal="true" aria-labelledby="dlgT" aria-describedby="dlgX">
 <h3 id="dlgT"></h3><p id="dlgX"></p><div id="dlgC" class="dch"></div><div class="dbtn"><button id="dlgN">Отмена</button><button id="dlgY" class="p">OK</button></div></div></div>
<script>
const $=s=>document.querySelector(s);
const HDR={'X-Requested-With':'mks','Content-Type':'application/json'};
const P={check:'M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z',
 snow:'M22 11h-4.17l3.24-3.24-1.41-1.42L15 11h-2V9l4.66-4.66-1.42-1.41L13 6.17V2h-2v4.17L7.76 2.93 6.34 4.34 11 9v2H9L4.34 6.34 2.93 7.76 6.17 11H2v2h4.17l-3.24 3.24 1.41 1.42L9 13h2v2l-4.66 4.66 1.42 1.41L11 17.83V22h2v-4.17l3.24 3.24 1.42-1.41L13 15v-2h2l4.66 4.66 1.41-1.42L17.83 13H22z',
 pause:'M6 5h4v14H6zM14 5h4v14h-4z',play:'M8 5v14l11-7z',stop:'M6 6h12v12H6z',home:'M12 3 2 12h3v8h5v-6h4v6h5v-8h3z',arrow:'M12 4l8 9h-5v7H9v-7H4z',
 motor:'M3 9h2v6H3zM6 6h10v12H6zM16 10h5v4h-5zM8 8v8h2V8z',power:'M11 3h2v10h-2zM17.7 6.3l-1.4 1.4A6 6 0 1 1 7.7 7.7L6.3 6.3a8 8 0 1 0 11.4 0z',
 refresh:'M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4z',trash:'M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6zM19 4h-3.5l-1-1h-5l-1 1H5v2h14z',
 tune:'M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z'};
const PS={chev:'M6 15l6-6 6 6'};
function ico(n,r){const st=PS[n];return '<svg viewBox="0 0 24 24" width="22" height="22" '+(st?'fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"':'fill="currentColor"')+' style="transform:rotate('+(r||0)+'deg)"><path d="'+(st||P[n])+'"/></svg>'}
document.querySelectorAll('[data-i]').forEach(b=>b.innerHTML=ico(b.dataset.i,b.dataset.r));$('#gear').innerHTML=ico('tune');
function lsGet(k){try{return localStorage.getItem(k)}catch(_){return null}}function lsSet(k,v){try{localStorage.setItem(k,v)}catch(_){}}
async function api(p,body){
  try{const r=await fetch(p,body===undefined?{cache:'no-store'}:{method:'POST',headers:HDR,body:JSON.stringify(body)});
    if(r.status===401){location.reload();return{}}return await r.json()}catch(e){return{error:String(e)}}}
// Окно подтверждения вместо системного confirm(). Возвращает true/false, а с choices — выбранный вариант или null.
// У «красных» действий фокус на «Отмена»: случайный Enter ничего не сломает.
function ask(title,text,ok,danger,choices){return new Promise(res=>{
  const o=$('#dlg'),y=$('#dlgY'),n=$('#dlgN'),ch=$('#dlgC'),no=choices?null:false,prev=document.activeElement;
  if(o.classList.contains('on'))$('#dlgN').click();   // одно окно за раз: предыдущее считаем отменённым
  $('#dlgT').textContent=title;$('#dlgX').textContent=text||'';ch.textContent='';
  const key=e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();done(no)}};
  function done(v){o.classList.remove('on');document.removeEventListener('keydown',key,true);o.onclick=n.onclick=y.onclick=null;
    if(prev&&prev.focus&&document.contains(prev))prev.focus();res(v)}
  if(choices){y.style.display='none';choices.forEach(c=>{const b=document.createElement('button');b.textContent=c;b.onclick=()=>done(c);ch.appendChild(b)})}
  else{y.style.display='';y.textContent=ok||'OK';y.className=danger?'dz':'p';y.onclick=()=>done(true)}
  n.onclick=()=>done(no);o.onclick=e=>{if(e.target===o)done(no)};
  document.addEventListener('keydown',key,true);o.classList.add('on');(choices?ch.firstChild:danger?n:y).focus()})}
async function quitApp(){if(await ask('Выключить мост?','Orca перестанет отправлять файлы на принтер, панель перестанет обновляться.','Выключить',1))api('/api/quit',{})}
function toast(t){const e=$('#toast');e.textContent=t;e.style.display='block';clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.display='none',5500);
  try{navigator.vibrate&&navigator.vibrate(150)}catch(_){}}
// ---- тема
const TN={auto:'как в системе',dark:'тёмная',light:'светлая'};
function applyTheme(){const t=lsGet('mks_theme')||'auto';t==='auto'?document.documentElement.removeAttribute('data-theme'):document.documentElement.dataset.theme=t;
  $('#thm').title='Тема: '+TN[t]+' (нажми, чтобы сменить)';drawChart()}
function cycleTheme(){const n={auto:'dark',dark:'light',light:'auto'}[lsGet('mks_theme')||'auto'];lsSet('mks_theme',n);applyTheme();toast('Тема: '+TN[n])}
// ---- статус
function fmt(s){if(s==null)return'—';s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return(h?h+' ч ':'')+m+' мин'}
function ttH(a){return a?'<span>'+a[0].toFixed(0)+'</span><i> / '+a[1].toFixed(0)+'°</i>':'—'}
function tt(a){return a?a[0].toFixed(0)+' / '+a[1].toFixed(0)+'°':'—'}
const seen={};let HIST=[],NOW=0,RANGE=+(lsGet('mks_range')||900);
async function tick(){
  if(document.hidden)return;
  const s=await api('/api/status?win='+RANGE);
  $('#ip').textContent=s.ip||'не задан';
  const b=$('#state');b.textContent=s.online?(s.homing?'ПАРКОВКА':(s.state||'—')+(s.stale?' …':'')):'нет связи';b.style.cursor=s.online?'':'pointer';b.onclick=s.online?null:reconnect;b.title=s.online?'':'Нажми, чтобы переподключиться';b.className='badge '+(s.online?(/PRINT/.test(s.state||'')?'on':''):'off');
  $('#nozzle').innerHTML=ttH(s.nozzle);$('#bed').innerHTML=ttH(s.bed);
  $('#prog').style.width=(s.progress||0)+'%';$('#progtxt').textContent=s.progress!=null?s.progress+'%':'—';
  $('#layer').textContent=s.layers?(s.layer_exact?'':'≈ ')+s.layer+' / '+s.layers:'—';
  $('#zv').textContent=s.pos&&s.pos.z!=null?s.pos.z.toFixed(2)+' мм':'—';
  $('#elapsed').textContent=fmt(s.elapsed);$('#eta').textContent=s.eta!=null?'≈ '+fmt(s.eta):'—';
  $('#file').textContent=s.file||'';$('#err').textContent=s.error||'';
  HIST=s.history||[];NOW=s.now||Math.floor(Date.now()/1000);drawChart();jobs(s.jobs||[]);updateTwin(s);
  
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick()});
// ---- график: окно времени фиксировано, пропуски данных — разрыв линии
const GAP=40,RANGES=[[300,'5 мин'],[900,'15 мин'],[3600,'1 час']];
$('#ranges').innerHTML=RANGES.map(r=>'<button class="s" data-r="'+r[0]+'" onclick="setRange('+r[0]+')">'+r[1]+'</button>').join(' ');
function setRange(r){RANGE=r;lsSet('mks_range',r);document.querySelectorAll('#ranges button').forEach(b=>b.classList.toggle('on',+b.dataset.r===r));tick()}
document.querySelectorAll('#ranges button').forEach(b=>b.classList.toggle('on',+b.dataset.r===RANGE));
let hoverX=null;
function drawChart(){
  const c=$('#chart'),dpr=window.devicePixelRatio||1,r=c.getBoundingClientRect(),W=Math.max(260,r.width),H=Math.max(110,r.height);
  c.width=W*dpr;c.height=H*dpr;const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,W,H);
  const cs=getComputedStyle(document.documentElement),bd=cs.getPropertyValue('--bd').trim(),mu=cs.getPropertyValue('--mut').trim(),fg=cs.getPropertyValue('--fg').trim(),cd=cs.getPropertyValue('--card').trim();
  const L=34,R=108,T=8,B=20,pw=W-L-R,ph=H-T-B,h=HIST,span=RANGE,now=NOW;
  const vals=[];h.forEach(p=>[1,2,3,4].forEach(k=>{if(p[k])vals.push(p[k])}));
  const mxv=Math.max(50,...vals),step=[10,20,25,50,100].find(s=>mxv/s<=5)||100,ymax=Math.ceil((mxv+1)/step)*step;
  const X=t=>L+pw-(now-t)/span*pw,Y=v=>T+ph-v/ymax*ph,SER=[[1,3,'#ef6c00'],[2,4,'#1e88e5']];
  x.font='11px system-ui,sans-serif';x.lineWidth=1;x.textAlign='right';
  for(let v=0;v<=ymax;v+=step){const y=Y(v);x.strokeStyle=bd;x.beginPath();x.moveTo(L,y);x.lineTo(L+pw,y);x.stroke();x.fillStyle=mu;x.fillText(v,L-5,y+4)}
  const ti=[10,30,60,120,300,600,900,1800].find(s=>span/s<=6)||1800;x.textAlign='center';
  for(let a=0;a<=span;a+=ti){const px=L+pw-a/span*pw;x.strokeStyle=bd;x.beginPath();x.moveTo(px,T);x.lineTo(px,T+ph);x.stroke();
    x.fillStyle=mu;x.fillText(a===0?'сейчас':'−'+(a>=3600?a/3600+' ч':a>=60?a/60+' мин':a+' с'),Math.min(px,W-R+10),H-5)}
  function line(k,col,tg){let prev=null;
    h.forEach(p=>{const v=p[k];if(v==null||(tg&&!v)){prev=null;return}
      if(prev&&p[0]-prev[0]>GAP)prev=null;
      if(prev){const gap=p[0]-prev[0]>GAP;x.beginPath();x.strokeStyle=col;x.lineWidth=tg?1:(gap?1.5:2);x.globalAlpha=tg?.55:(gap?.6:1);x.setLineDash(tg||gap?[4,4]:[]);
        x.moveTo(X(prev[0]),Y(prev[k]));x.lineTo(X(p[0]),Y(v));x.stroke()}
      prev=p});x.setLineDash([]);x.globalAlpha=1}
  x.save();x.beginPath();x.rect(L,T,pw,ph+1);x.clip();SER.forEach(([k,kt,col])=>{line(kt,col,true);line(k,col,false)});x.restore();
  const last=k=>{for(let i=h.length-1;i>=0;i--)if(h[i][k]!=null)return h[i];return null},tags=[];
  SER.forEach(([k,kt,col])=>{const p=last(k);if(!p||now-p[0]>GAP*2)return;
    tags.push({col,y:Y(p[k]),t:Math.round(p[k])+'°',tg:false});if(p[kt])tags.push({col,y:Y(p[kt]),t:Math.round(p[kt])+'°',tg:true})});
  [false,true].forEach(tg=>{const col=tags.filter(t=>t.tg===tg).sort((a,b)=>a.y-b.y);for(let i=1;i<col.length;i++)if(col[i].y-col[i-1].y<19)col[i].y=col[i-1].y+19;
    col.forEach(t=>{const xx=L+pw+4+(tg?54:0),w=x.measureText(t.t).width+14;x.beginPath();x.moveTo(xx,t.y);x.lineTo(xx+6,t.y-9);x.lineTo(xx+6+w-6,t.y-9);x.lineTo(xx+w,t.y-9+0);x.lineTo(xx+w,t.y+9);x.lineTo(xx+6,t.y+9);x.closePath();
      if(tg){x.setLineDash([3,3]);x.strokeStyle=t.col;x.lineWidth=1.2;x.stroke();x.setLineDash([]);x.fillStyle=t.col}else{x.fillStyle=t.col;x.fill();x.fillStyle='#fff'}
      x.textAlign='left';x.font='bold 11px system-ui,sans-serif';x.fillText(t.t,xx+11,t.y+4);x.font='11px system-ui,sans-serif'})});
  if(hoverX!=null&&h.length>1&&hoverX>=L&&hoverX<=L+pw){
    const t=now-(L+pw-hoverX)/pw*span;let p=h[0];h.forEach(q=>{if(Math.abs(q[0]-t)<Math.abs(p[0]-t))p=q});const px=X(p[0]);
    x.strokeStyle=fg;x.globalAlpha=.4;x.lineWidth=1;x.beginPath();x.moveTo(px,T);x.lineTo(px,T+ph);x.stroke();x.globalAlpha=1;
    const f=(a,b)=>a==null?'нет данных':a.toFixed(0)+'°'+(b?' → '+b.toFixed(0)+'°':'');
    const txt='−'+Math.round(now-p[0])+' с   сопло '+f(p[1],p[3])+'   стол '+f(p[2],p[4]),w=x.measureText(txt).width+14,bx=Math.min(Math.max(px-w/2,L),L+pw-w);
    x.fillStyle=cd;x.strokeStyle=bd;x.fillRect(bx,T+2,w,20);x.strokeRect(bx,T+2,w,20);x.fillStyle=fg;x.textAlign='left';x.fillText(txt,bx+7,T+16);
    SER.forEach(([k,kt,col])=>{if(p[k]!=null){x.fillStyle=col;x.beginPath();x.arc(px,Y(p[k]),4,0,7);x.fill()}});
  }
}
(function(){const c=$('#chart');const set=e=>{const q=e.touches?e.touches[0]:e;hoverX=q.clientX-c.getBoundingClientRect().left;drawChart()};
  c.addEventListener('mousemove',set);c.addEventListener('touchstart',set,{passive:true});c.addEventListener('touchmove',set,{passive:true});
  c.addEventListener('mouseleave',()=>{hoverX=null;drawChart()});window.addEventListener('resize',drawChart)})();
// ---- задачи загрузки
function jobs(list){
  const box=$('#jobs');box.textContent='';
  list.slice().reverse().forEach(j=>{
    if(seen[j.id]&&seen[j.id]!==j.state&&j.state!=='working'){toast((j.state==='done'?'✔ ':'✖ ')+j.note);if(j.state==='done'&&$('#files').children.length)loadFiles()}
    seen[j.id]=j.state;
    const d=document.createElement('div');d.className='job '+j.state;const n=document.createElement('b');n.textContent=j.name;d.appendChild(n);
    const p=j.total?Math.round(j.sent*100/j.total):0;
    if(j.state==='working'){const bar=document.createElement('div');bar.className='bar';const i=document.createElement('i');i.style.width=p+'%';bar.appendChild(i);d.appendChild(bar)}
    const m=document.createElement('div');m.className='mut n';m.textContent=(j.state==='working'&&j.total?p+'% · ':'')+j.note;d.appendChild(m);box.appendChild(d)});
}
const drop=$('#drop'),pick=$('#pick');drop.onclick=()=>pick.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('h')};drop.ondragleave=()=>drop.classList.remove('h');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('h');sendFiles(e.dataTransfer.files)};pick.onchange=()=>{sendFiles(pick.files);pick.value=''};
function sendFiles(fl){[...fl].forEach(f=>{
  const x=new XMLHttpRequest();x.open('POST','/api/upload?name='+encodeURIComponent(f.name)+'&print='+($('#startAfter').checked?1:0));
  x.setRequestHeader('X-Requested-With','mks');x.upload.onprogress=e=>{drop.firstChild.textContent='Передача на мост: '+f.name+' '+Math.round(e.loaded*100/e.total)+'%'};
  const done=err=>{drop.firstChild.textContent='Перетащи .gcode сюда или нажми, чтобы выбрать';if(err)toast('✖ '+f.name+': '+err);tick()};
  x.onload=()=>{let e=null;if(x.status!==200){try{e=JSON.parse(x.responseText).error}catch(_){}e=e||('мост ответил HTTP '+x.status)}done(e)};
  x.onerror=()=>done('связь с мостом оборвалась');x.send(f)})}
// ---- файлы
async function loadFiles(){
  const box=$('#files');box.textContent='Загрузка…';const r=await api('/api/list');if(r.error){box.textContent=r.error;return}
  box.textContent='';if(!r.files.length){box.textContent='Файлов нет';return}
  r.files.forEach(n=>{const l=document.createElement('label'),c=document.createElement('input');c.type='checkbox';c.value=n;l.appendChild(c);l.appendChild(document.createTextNode(n));box.appendChild(l)})}
const sel=()=>[...document.querySelectorAll('#files input:checked')].map(c=>c.value);
async function delSel(){const n=sel();if(!n.length){toast('Отметь файлы для удаления');return}
  if(!await ask(n.length===1?'Удалить файл с принтера?':'Удалить '+n.length+' файл(ов) с принтера?',n.join(', '),'Удалить',1))return;
  toast('Удаляю…');const r=await api('/api/delete',{names:n});toast(r.error||('Удалено: '+r.results.filter(x=>x.ok).length+' из '+n.length));loadFiles()}
async function printSel(){const n=sel();if(n.length!==1){toast('Выбери один файл');return}
  if(!await ask('Запустить печать?',n[0],'Печатать'))return;const r=await api('/api/print',{name:n[0]});toast(r.error||'Печать запущена')}
// ---- консоль
// [заголовок, пояснение, кнопка, красная]
const DANGER={G28:['Парковка','Головка и стол поедут к концевикам. Проверь, что на столе и под соплом ничего нет.','Парковать'],
 M26:['Остановить печать?','Печать прервётся, продолжить её с этого места будет нельзя.','Остановить',1],
 M112:['Аварийный стоп','Принтер немедленно остановится и потребует перезагрузки по питанию.','Аварийный стоп',1],
 M500:['Записать в EEPROM?','Текущие настройки платы сохранятся в её память и переживут перезагрузку.','Записать'],
 M502:['Сброс к заводским','Шаги, ускорения и PID вернутся к значениям прошивки. В память не запишется, пока не выполнишь M500.','Сбросить',1],
 M303:['Калибровка PID','Сопло будет греться и остывать несколько минут. Не оставляй принтер без присмотра.','Запустить']};
const CMDH=[];let hi=0;
function log(t,c){const l=$('#log'),d=document.createElement('div');d.textContent=t;if(c)d.style.color=c;l.appendChild(d);l.scrollTop=l.scrollHeight}
function homeAnim(c){const ax=c.trim().toUpperCase().split(/\s+/).slice(1).filter(a=>/^[XYZ]$/.test(a)),all=!ax.length,h=a=>all||ax.includes(a);
  holdMove(h('X')?0:null,h('Y')?0:null,h('Z')?0:null,feedXY(50),Math.min(LIM.z||5,5),3500)}
async function cmd(c){const tok=c.trim().split(/\s+/)[0].toUpperCase(),dg=DANGER[tok];if(dg&&!await ask(dg[0],dg[1],dg[2],dg[3]))return;
  if(tok==='G28'&&!/PRINT|PAUSE/.test(LASTS.state||'')){homeAnim(c);HOMING=Date.now()}
  tab('log');log('> '+c);const r=await api('/api/console',{cmd:c,force:$('#force').checked});
  if(r.error){if(tok==='G28')HOMING=0;log(r.error,'#ef5350')}
  else{log(r.reply||(r.homing?'(принтер паркуется: пока не закончит, остальные команды недоступны)':'(команда отправлена, ответа нет)'));if(r.homing)tick()}}
function sendCon(){const v=$('#cin').value.trim();if(!v)return;CMDH.push(v);hi=CMDH.length;cmd(v);$('#cin').value=''}
function conKey(e){if(e.key==='Enter')sendCon();else if(e.key==='ArrowUp'&&CMDH.length){hi=Math.max(0,hi-1);$('#cin').value=CMDH[hi];e.preventDefault()}
  else if(e.key==='ArrowDown'&&CMDH.length){hi=Math.min(CMDH.length,hi+1);$('#cin').value=CMDH[hi]||'';e.preventDefault()}}
// ---- справочник команд: [код, описание, режим s=отправить сразу / t=подставить в поле]
const REF=[
['Информация',[['M105','Температуры сопла и стола: текущая / заданная','s'],['M114','Текущие координаты X Y Z E','s'],['M115','Версия прошивки и список возможностей','s'],
 ['M503','Все настройки платы: шаги, ускорения, PID, смещения','s'],['M119','Состояние концевиков (сработал или нет)','s'],['M20','Список файлов на карте памяти','s'],
 ['M27','Прогресс печати с карты, в процентах (у MKS)','s'],['M992','MKS: сколько времени идёт печать (чч:мм:сс)','s'],['M994','MKS: имя печатаемого файла','s'],['M997','MKS: состояние (IDLE, PRINTING, PAUSE)','s']]],
['Движение',[['G28','Домой по всем осям (с подтверждением)','s'],['G28 X Y','Домой только по X и Y (не трогая Z)','s'],['G90','Абсолютные координаты (обычный режим)','s'],
 ['G91','Относительные координаты. После перемещений верни G90!','s'],['G1 X','Переместиться: G1 X50 Y50 F3000 (F — скорость, мм/мин)','t'],['G92','Задать текущую позицию без движения: G92 X0 Y0 Z0','t'],
 ['M84','Выключить моторы: оси можно двигать руками','s'],['M400','Дождаться завершения всех движений','s']]],
['Температура и обдув',[['M104 S','Сопло: задать температуру и не ждать. M104 S200; S0 — выключить','t'],['M109 S','Сопло: задать температуру и дождаться нагрева','t'],
 ['M140 S','Стол: задать температуру и не ждать. M140 S60; S0 — выключить','t'],['M190 S','Стол: задать температуру и дождаться нагрева','t'],
 ['M106 S','Вентилятор обдува модели, 0–255. M106 S255 — на полную','t'],['M107','Выключить вентилятор обдува','s']]],
['Печать',[['M23','Выбрать файл на карте: M23 имя.gcode','t'],['M24','Начать выбранный файл / продолжить после паузы','s'],['M25','Пауза печати','s'],
 ['M26','Остановка печати (у MKS так, по Cura-плагину; в чистом Marlin M26 задаёт позицию в файле)','s'],['M220 S','Скорость печати в процентах: M220 S120 — на 20% быстрее','t'],
 ['M221 S','Поток пластика в процентах: M221 S95','t'],['M290 Z','Подстройка высоты Z «на лету», мм: M290 Z-0.05 — ближе к столу. Не сохраняется','t'],
 ['M117','Показать текст на экране принтера: M117 Привет','t'],['M300 S','Звуковой сигнал: M300 S440 P200 (частота Гц, длительность мс)','t']]],
['Калибровка и сервис',[['M900 K','Linear Advance: коэффициент K. M900 K0.04; K0 — выключить','t'],['M303','Автокалибровка PID: M303 E0 S210 C8 (греет сопло, нужен присмотр)','t'],
 ['M500','Сохранить текущие настройки в память платы (EEPROM)','s'],['M501','Загрузить настройки из памяти платы','s'],['M502','Сбросить настройки к заводским значениям (не сохраняя)','s'],
 ['M112','АВАРИЙНЫЙ стоп. Нужна перезагрузка принтера','s']]]];
function renderRef(){
  const q=($('#refq').value||'').toLowerCase(),box=$('#reflist');box.textContent='';
  REF.forEach(([g,items])=>{const it=items.filter(i=>!q||i[0].toLowerCase().includes(q)||i[1].toLowerCase().includes(q));if(!it.length)return;
    const h=document.createElement('div');h.className='rg';h.textContent=g;box.appendChild(h);
    it.forEach(([c,d,m])=>{const r=document.createElement('div');r.className='rr'+(DANGER[c.split(' ')[0]]?' dg':'');
      const b=document.createElement('code');b.textContent=c;b.title='Вставить в строку ввода';
      b.onclick=()=>{const i=$('#cin');i.value=m==='s'||/[SKZX]$/.test(c)?c:c+' ';i.focus();i.setSelectionRange&&i.setSelectionRange(i.value.length,i.value.length)};
      const s=document.createElement('span');s.textContent=d;r.appendChild(b);r.appendChild(s);box.appendChild(r)})});
}
// ---- движение и управление
let STEP=10;
$('#steps').innerHTML=[0.1,1,10,50].map(s=>'<button data-s="'+s+'" onclick="setStep('+s+')">'+s+'</button>').join('');
function setStep(s){STEP=s;$('#zstep').textContent=s;document.querySelectorAll('#steps button').forEach(b=>b.classList.toggle('on',+b.dataset.s===s))}setStep(10);
async function mv(a,sign){const d=sign*STEP;
  if(d<0&&a==='Z'&&STEP>=10&&!await ask('Сблизить сопло и стол на '+STEP+' мм?','Проверь, что под соплом ничего нет.','Сблизить'))return;
  log('> '+a+(d>0?'+':'')+d+' мм');
  let plan;{const c=TGTP||CUR||{x:0,y:0,z:0},lim={X:GEOM.bx,Y:GEOM.by,Z:GEOM.bz}[a],nv=Math.max(0,Math.min(lim,c[a.toLowerCase()]+d)),t={x:c.x,y:c.y,z:c.z};t[a.toLowerCase()]=nv;
   plan=holdPlan(t.x,t.y,t.z,feedXY(50),Math.min(10,LIM.z||10))}
  const r=await guardedMove(plan,()=>api('/api/move',{axis:a,dist:d}));if(r.error)log(r.error+(r.reply?' · '+r.reply.split('\n')[0]:''),'#ef5350');else log(r.reply||'(команда отправлена)');tick()}
function zMove(up){mv('Z',(up?1:-1)*(GEOM.z_bed?-1:1))}
function zTitles(){const b=GEOM.z_bed;$('#zUp').title=b?'Стол вверх, ближе к соплу (Z−)':'Сопло вверх (Z+)';$('#zDn').title=b?'Стол вниз, дальше от сопла (Z+)':'Сопло вниз (Z−)';$('#zcap').textContent=b?'Стол':'Сопло'}
let ESTEP=5;
$('#esteps').innerHTML=[1,5,10,50].map(s=>'<button data-s="'+s+'" onclick="setEStep('+s+')">'+s+'</button>').join('');
function setEStep(s){ESTEP=s;document.querySelectorAll('#esteps button').forEach(b=>b.classList.toggle('on',+b.dataset.s===s))}setEStep(5);
async function ex(sign){const d=sign*ESTEP;log('> пруток '+(d>0?'+':'')+d+' мм');const r=await api('/api/extrude',{dist:d});
  if(r.error){log(r.error,'#ef5350');return}log(r.reply||'(отправлено)');const m=/E:\s*(-?[\d.]+)/.exec(r.reply||'');setE(m?parseFloat(m[1]):(tgtE||0)+d);tick()}
function tab(n){$('#pLog').hidden=n!=='log';$('#pRef').hidden=n!=='ref';$('#clr').style.visibility=n==='log'?'visible':'hidden';document.querySelectorAll('.tb').forEach(b=>b.classList.toggle('on',b.dataset.t===n))}
function fanLabel(){const r=$('#fanr'),v=r.value;$('#fanv').textContent=v+'%';if(r.style&&r.style.setProperty)r.style.setProperty('--v',v+'%')}
async function setFan(){const v=+$('#fanr').value;log('> обдув '+v+'%');const r=await api('/api/fan',{percent:v});r.error?log(r.error,'#ef5350'):log(r.reply||'(отправлено)');tick()}
// ---- цифровая копия принтера
let GEOM={bx:255,by:210,bz:210,z_bed:true},TW=null,FR=null,visE=0,tgtE=null,eRaf=0,lastT=0,LASTS={},ARM=null,DOWN=null;
const TOP={x0:22,y0:15,aw:256,ah:196},MINZ=0.5,SNAP=7;   // MINZ — ниже по клику не опускаем: промах мышью не должен вогнать сопло в стол
// SNAP — полоса вокруг рабочей зоны (в единицах рисунка): клик в ней даёт крайнее значение, иначе в 0/максимум не попасть
// плавный градиент: серый -> жёлтый -> оранжевый -> красный (непрерывный, без порогов)
const STOPS=[[0,[100,116,139]],[.28,[245,176,20]],[.62,[240,100,20]],[1,[215,38,38]]];
function tcol(t,max){const r=t==null?0:Math.max(0,Math.min(1,(t-25)/(max-25)));let i=1;while(i<STOPS.length-1&&r>STOPS[i][0])i++;
  const a=STOPS[i-1],b=STOPS[i],k=(r-a[0])/(b[0]-a[0]);return'rgb('+a[1].map((v,j)=>Math.round(v+(b[1][j]-v)*k)).join(',')+')'}
const LBL='style="fill:#fff;paint-order:stroke;stroke:#000a;stroke-width:3;font-weight:700"';
const HIT='fill="transparent"',RING='fill="none" class="ring" stroke-width="2" stroke-dasharray="5 3" style="stroke:var(--acc);display:none"';
function buildTwin(){
  const g=GEOM,sc=Math.min(TOP.aw/g.bx,TOP.ah/g.by),bw=g.bx*sc,bh=g.by*sc,ox=TOP.x0+(TOP.aw-bw)/2,oy=TOP.y0+(TOP.ah-bh)/2;TW={s:sc,ox,oy,bw,bh};
  {const T=g.bz*sc,fl=93+4+T,bot=fl+24;FR={l:ox,bw:bw,tip:93,T:T,floor:fl,bot:bot,vbH:Math.round(bot+36)}}
  $('#svgFront').setAttribute('viewBox','0 0 300 '+FR.vbH);   // вид спереди использует ту же ось X, что и вид сверху
  let grid='';for(let v=50;v<g.bx;v+=50)grid+='<line x1="'+(ox+v*sc)+'" y1="'+oy+'" x2="'+(ox+v*sc)+'" y2="'+(oy+bh)+'" stroke="#fff" stroke-opacity=".25"/>';
  for(let v=50;v<g.by;v+=50)grid+='<line x1="'+ox+'" y1="'+(oy+bh-v*sc)+'" x2="'+(ox+bw)+'" y2="'+(oy+bh-v*sc)+'" stroke="#fff" stroke-opacity=".25"/>';
  const rod='style="fill:var(--mut)"';
  $('#svgTop').innerHTML=
   '<rect x="'+(ox-16)+'" y="'+(oy-12)+'" width="'+(bw+32)+'" height="'+(bh+24)+'" rx="8" fill="none" style="stroke:var(--bd)" stroke-width="3"/>'+
   '<line x1="'+(ox-9)+'" y1="'+(oy-6)+'" x2="'+(ox-9)+'" y2="'+(oy+bh+6)+'" style="stroke:var(--mut)" stroke-width="3" opacity=".7"/><line x1="'+(ox+bw+9)+'" y1="'+(oy-6)+'" x2="'+(ox+bw+9)+'" y2="'+(oy+bh+6)+'" style="stroke:var(--mut)" stroke-width="3" opacity=".7"/>'+
   '<rect id="tBed" x="'+ox+'" y="'+oy+'" width="'+bw+'" height="'+bh+'" rx="4" fill="#64748b" fill-opacity=".9" style="stroke:var(--mut)"/>'+grid+
   '<circle cx="'+ox+'" cy="'+(oy+bh)+'" r="2.5" style="fill:var(--fg)"/>'+
   '<g id="tGant"><rect x="'+(ox-14)+'" y="-7" width="'+(bw+28)+'" height="3" rx="1.5" '+rod+' opacity=".85"/><rect x="'+(ox-14)+'" y="4" width="'+(bw+28)+'" height="3" rx="1.5" '+rod+' opacity=".85"/></g>'+
   '<text id="tBedT" x="'+(ox+bw/2)+'" y="'+(oy+bh-8)+'" text-anchor="middle" font-size="9" '+LBL+'>—</text>'+
   '<rect id="tZone" x="'+(ox-SNAP)+'" y="'+(oy-SNAP)+'" width="'+(bw+2*SNAP)+'" height="'+(bh+2*SNAP)+'" rx="6" style="fill:var(--acc);fill-opacity:.09;stroke:var(--acc);stroke-opacity:.5;display:none;pointer-events:none" stroke-width="1"/>'+
   '<g id="tHead" style="cursor:pointer"><rect x="-19" y="-14" width="38" height="28" rx="4" style="fill:var(--card);stroke:var(--fg)" stroke-width="2"/><rect x="-10" y="-10" width="20" height="20" rx="3" style="fill:var(--bg);stroke:var(--mut)"/>'+
   '<circle id="tNoz" r="3.5" fill="#64748b" style="stroke:var(--fg)"/><rect x="-23" y="-18" width="46" height="36" '+HIT+'/><rect id="tRing" x="-25" y="-19" width="50" height="38" rx="8" '+RING+'/></g>'+
   '<text x="'+(ox+bw/2)+'" y="'+(oy+bh+19)+'" text-anchor="middle" font-size="6.5" letter-spacing=".08em" style="fill:var(--mut)" opacity=".8">перед</text>'+
   '<g id="tHov" style="display:none;pointer-events:none"><line id="tHovH" x1="'+ox+'" x2="'+(ox+bw)+'" style="stroke:var(--acc)" stroke-width="1.3" stroke-dasharray="4 3"/><line id="tHovV" y1="'+oy+'" y2="'+(oy+bh)+'" style="stroke:var(--acc)" stroke-width="1.3" stroke-dasharray="4 3"/><circle id="tHovC" r="3.5" style="fill:var(--acc)"/><rect id="tHovR" rx="3" height="15" style="fill:var(--card);fill-opacity:.94;stroke:var(--acc)" stroke-width="1"/><text id="tHovT" font-size="10" style="fill:var(--fg);font-weight:700"></text></g>';
  const rx1=ox+22,rx2=ox+bw-22;
  let fins='';for(let i=0;i<4;i++)fins+='<rect x="-9" y="'+(59+i*4)+'" width="18" height="2" rx="1" '+rod+'/>';
  $('#svgFront').innerHTML=
   '<rect x="'+(ox-26)+'" y="'+FR.bot+'" width="'+(bw+52)+'" height="26" rx="6" style="fill:var(--bd);stroke:var(--mut)" stroke-width="1.5"/><rect x="'+(ox-26)+'" y="'+FR.bot+'" width="'+(bw+52)+'" height="7" rx="3" '+rod+' opacity=".45"/>'+
   '<rect x="'+(ox-14)+'" y="'+(FR.bot+26)+'" width="26" height="6" rx="2" '+rod+'/><rect x="'+(ox+bw-12)+'" y="'+(FR.bot+26)+'" width="26" height="6" rx="2" '+rod+'/>'+
   '<rect x="'+(ox-18)+'" y="4" width="'+(bw+36)+'" height="10" rx="3" '+rod+' opacity=".85"/>'+
   '<rect x="'+(ox-18)+'" y="14" width="10" height="'+(FR.bot-14)+'" rx="3" style="fill:var(--bd)"/><rect x="'+(ox+bw+8)+'" y="14" width="10" height="'+(FR.bot-14)+'" rx="3" style="fill:var(--bd)"/>'+
   '<line x1="'+rx1+'" y1="14" x2="'+rx1+'" y2="'+FR.bot+'" style="stroke:var(--mut)" stroke-width="3" opacity=".55"/><line x1="'+rx2+'" y1="14" x2="'+rx2+'" y2="'+FR.bot+'" style="stroke:var(--mut)" stroke-width="3" opacity=".55"/>'+
   '<line x1="'+(ox+bw/2)+'" y1="40" x2="'+(ox+bw/2)+'" y2="'+FR.bot+'" style="stroke:var(--mut)" stroke-width="2" stroke-dasharray="3 3" opacity=".5"/>'+
   '<rect id="fZone" rx="4" style="fill:var(--acc);fill-opacity:.09;stroke:var(--acc);stroke-opacity:.5;display:none;pointer-events:none" stroke-width="1"/>'+
   '<g id="fBedG" style="cursor:pointer"><rect id="fBed" x="'+ox+'" y="0" width="'+bw+'" height="10" rx="2" fill="#64748b" style="stroke:var(--mut)"/>'+
   '<rect x="'+(rx1-9)+'" y="10" width="18" height="7" rx="2" '+rod+'/><rect x="'+(rx2-9)+'" y="10" width="18" height="7" rx="2" '+rod+'/>'+
   '<text id="fBedT" x="'+(ox+bw/2)+'" y="8" text-anchor="middle" font-size="7.5" '+LBL+'>—</text>'+
   '<rect x="'+ox+'" y="-4" width="'+bw+'" height="24" '+HIT+'/><rect id="fRingB" x="'+(ox-3)+'" y="-4" width="'+(bw+6)+'" height="24" rx="4" '+RING+'/></g>'+
   '<g id="fGantry"><line x1="'+(ox-8)+'" y1="44" x2="'+(ox+bw+8)+'" y2="44" style="stroke:var(--mut)" stroke-width="4" stroke-linecap="round"/>'+
   '<g id="fHead" style="cursor:pointer"><rect x="-19" y="14" width="38" height="22" rx="3" style="fill:var(--card);stroke:var(--fg)" stroke-width="1.5"/><circle cx="0" cy="25" r="6" fill="none" style="stroke:var(--mut)"/>'+
   '<rect x="-18" y="36" width="36" height="22" rx="3" style="fill:var(--card);stroke:var(--fg)" stroke-width="1.5"/><circle cx="-11" cy="42" r="2" '+rod+'/><circle cx="11" cy="42" r="2" '+rod+'/>'+
   '<rect x="-4" y="58" width="8" height="16" style="fill:var(--bd);stroke:var(--mut)"/>'+fins+'<rect x="-2" y="74" width="4" height="5" '+rod+'/>'+
   '<rect id="fBlock" x="-7" y="79" width="14" height="8" rx="1" fill="#64748b" style="stroke:var(--fg)"/><rect x="-4" y="87" width="8" height="2.6" fill="#b45309" style="stroke:var(--fg)" stroke-width=".6"/><polygon id="fNoz" points="-2.2,89.6 2.2,89.6 .8,93 -.8,93" fill="#64748b" style="stroke:var(--fg)" stroke-width=".6"/>'+
   '<text id="fNozT" x="22" y="86" font-size="7.5" '+LBL+'>—</text><rect x="-22" y="12" width="44" height="84" '+HIT+'/><rect id="fRingH" x="-24" y="10" width="48" height="88" rx="6" '+RING+'/></g></g>'+
   '<g id="fHov" style="display:none;pointer-events:none"><line id="fHovL" style="stroke:var(--acc)" stroke-width="1.5" stroke-dasharray="4 3"/><circle id="fHovC" r="3.5" style="fill:var(--acc)"/><rect id="fHovR" rx="3" height="15" style="fill:var(--card);fill-opacity:.94;stroke:var(--acc)" stroke-width="1"/><text id="fHovT" font-size="10" style="fill:var(--fg);font-weight:700"></text></g>';
  let efins='';for(let i=0;i<7;i++)efins+='<rect x="62" y="'+(146+i*8)+'" width="56" height="4" rx="2" '+rod+' opacity=".85"/>';
  $('#svgExt').innerHTML=
   '<rect x="12" y="10" width="156" height="124" rx="12" style="fill:var(--card);stroke:var(--mut)" stroke-width="2"/>'+
   '<g id="gDrive"><circle cx="62" cy="72" r="25" fill="none" style="stroke:var(--fg)" stroke-width="8" stroke-dasharray="7 4.7"/><circle cx="62" cy="72" r="19" style="fill:var(--bg);stroke:var(--mut)"/><line x1="62" y1="72" x2="62" y2="56" style="stroke:var(--mut)" stroke-width="3"/><circle cx="62" cy="72" r="4" '+rod+'/></g>'+
   '<g id="gIdler"><circle cx="118" cy="72" r="21" fill="none" style="stroke:var(--mut)" stroke-width="7" stroke-dasharray="6 4.5"/><circle cx="118" cy="72" r="16" style="fill:var(--bg);stroke:var(--bd)"/><line x1="118" y1="72" x2="118" y2="59" style="stroke:var(--mut)" stroke-width="3"/><circle cx="118" cy="72" r="3.5" '+rod+'/></g>'+
   '<rect x="83" y="134" width="14" height="10" rx="2" fill="#3b82f6" style="stroke:var(--fg)"/>'+
   '<rect x="82" y="144" width="16" height="58" style="fill:var(--bd);stroke:var(--mut)"/>'+efins+
   '<rect x="86" y="202" width="8" height="16" style="fill:var(--bd);stroke:var(--mut)"/><rect x="87.5" y="206" width="5" height="8" '+rod+'/>'+
   '<rect id="eBlock" x="64" y="218" width="52" height="26" rx="3" fill="#64748b" style="stroke:var(--fg)" stroke-width="1.5"/>'+
   '<rect x="116" y="224" width="16" height="7" rx="3" fill="#7f1d1d" style="stroke:var(--fg)"/><path d="M132 227 H146 Q152 227 152 234" fill="none" stroke="#7f1d1d" stroke-width="2"/>'+
   '<circle cx="70" cy="238" r="2.5" style="fill:var(--bg);stroke:var(--fg)"/>'+
   '<text id="eNozT" x="90" y="235" text-anchor="middle" font-size="11" '+LBL+'>—</text>'+
   '<rect x="78" y="244" width="24" height="8" rx="1.5" fill="#b45309" style="stroke:var(--fg)" stroke-width="1"/><line x1="84" y1="244" x2="84" y2="252" style="stroke:var(--fg)" stroke-opacity=".35"/><line x1="96" y1="244" x2="96" y2="252" style="stroke:var(--fg)" stroke-opacity=".35"/><polygon id="eNoz" points="84,252 96,252 92.4,259.5 87.6,259.5" fill="#64748b" style="stroke:var(--fg)" stroke-width="1"/><rect x="89.3" y="257" width="1.4" height="2.5" fill="#000" fill-opacity=".75"/>'+
   '<line id="eFil" x1="90" y1="0" x2="90" y2="218" stroke="#22c55e" stroke-width="5" stroke-dasharray="9 6" stroke-dashoffset="0"/>'+
   '<line id="eMelt" x1="90" y1="260.5" x2="90" y2="268" stroke="#f59e0b" stroke-width="3" stroke-linecap="round" style="opacity:0"/>'+
   '<g transform="translate(34,262)"><circle r="17" fill="none" style="stroke:var(--mut)" stroke-width="2"/><g id="eBlades" class="blades"><path d="M0 0 L-3 -13 A13 13 0 0 1 8 -10Z M0 0 L13 -3 A13 13 0 0 1 10 8Z M0 0 L3 13 A13 13 0 0 1 -8 10Z M0 0 L-13 3 A13 13 0 0 1 -10 -8Z" '+rod+'/></g></g>';
  $('#dimTop').textContent='X '+g.bx+' × Y '+g.by+' мм';$('#dimFront').textContent='X '+g.bx+' × Z '+g.bz+' мм';
  document.querySelectorAll('.ring').forEach(fixRing);
  zTitles();renderE();syncArm();fitViews();renderPos()}
// ---- плавное движение каретки и стола: на экране едут с реальной скоростью (≈100 мм/с по XY, 25 мм/с по Z), но любой переезд укладывается в ~1.5 с
let ANIM=true,CUR=null,TGTP=null,MOTION=null,posRaf=0,posT=0,HOLD=0,HOMING=0,LASTGOOD=Date.now(),EVER=false,LIM={x:null,y:null,z:null},LIMTRIED=false;
const T0=Date.now();
function renderPos(){if(!TW||!FR||!CUR)return;const g=GEOM,c=CUR,px=TW.ox+c.x*TW.s,py=TW.oy+(g.by-c.y)*TW.s,fx=FR.l+(c.x/g.bx)*FR.bw;
  $('#tHead').setAttribute('transform','translate('+px+','+py+')');$('#tGant').setAttribute('transform','translate(0,'+py+')');
  $('#fHead').setAttribute('transform','translate('+fx+',0)');$('#fGantry').setAttribute('transform','translate(0,'+frontDy(c.z)+')');
  $('#fBedG').setAttribute('transform','translate(0,'+bedYAt(c.z)+')')}
function setPosTarget(x,y,z,o){TGTP={x,y,z};MOTION=o||null;if(!CUR||!ANIM){CUR={x,y,z};renderPos();return}
  if(!posRaf){posT=performance.now();posRaf=requestAnimationFrame(animPos)}}
function animPos(t){const dt=Math.min(.1,(t-posT)/1000);posT=t;const c=CUR,g=TGTP;
  const dx=g.x-c.x,dy=g.y-c.y,dd=Math.hypot(dx,dy);
  const o=MOTION;
  if(dd<.02){c.x=g.x;c.y=g.y}else{const st=Math.min(dd,(o?o.vxy:Math.max(100,dd/1.5))*dt);c.x+=dx*st/dd;c.y+=dy*st/dd}
  const dz=g.z-c.z;if(Math.abs(dz)<.02)c.z=g.z;else c.z+=Math.sign(dz)*Math.min(Math.abs(dz),(o?o.vz:Math.max(25,Math.abs(dz)/1.5))*dt);
  renderPos();
  posRaf=(c.x!==g.x||c.y!==g.y||c.z!==g.z)?requestAnimationFrame(animPos):0}
function setVeil(s,fresh){
  let msg=null,sub='';
  if(!EVER&&Date.now()-T0<5000){msg='Загрузка…'}
  else if(s.online===false||(s.error&&!fresh)){msg='Нет связи с принтером';sub=(s.error?String(s.error).replace(/\.?\s*$/,'. '):'')+'Показано последнее известное положение.'}
  else if(!fresh&&homing()){msg='Принтер едет домой';sub='Во время парковки (G28) прошивка не отвечает на запросы — это нормально. Положение обновится, когда он закончит.'}
  else if(!fresh&&Date.now()-LASTGOOD>25000){msg='Принтер долго не отвечает';sub='Возможно, он занят движением. Показано последнее известное положение.'}
  const wait=msg==='Загрузка…'||msg==='Принтер едет домой';
  document.querySelectorAll('.veil').forEach(v=>{v.hidden=!msg;if(msg){v.children[0].textContent=msg;v.children[1].textContent=sub;v.children[2].style.display=wait?'none':''}})}
// парковку отслеживает мост (s.homing); HOMING закрывает несколько секунд до первого ответа моста после нажатия
function homing(){return !!LASTS.homing||(HOMING&&Date.now()-HOMING<4000)}
async function reconnect(){toast('Переподключаюсь…');const r=await api('/api/reconnect',{});toast(r.error||('Связь есть: '+(r.state||'ok')));tick()}
function feedXY(f){return Math.min(f,LIM.x||f,LIM.y||f)}
function holdPlan(tx,ty,tz,vxy,vz,extra){const c=TGTP||CUR||{x:0,y:0,z:0};
  tx=tx==null?c.x:tx;ty=ty==null?c.y:ty;tz=tz==null?c.z:tz;
  return{tx,ty,tz,vxy,vz,extra,prev:{x:c.x,y:c.y,z:c.z},est:Math.max(Math.hypot(tx-c.x,ty-c.y)/vxy,Math.abs(tz-c.z)/vz)}}
function holdRun(p){HOLD=Date.now()+p.est*1000+(p.extra||1200);setPosTarget(p.tx,p.ty,p.tz,{vxy:p.vxy,vz:p.vz})}
function holdMove(tx,ty,tz,vxy,vz,extra){holdRun(holdPlan(tx,ty,tz,vxy,vz,extra))}
// Каретка начинает ехать на экране не сразу: если принтер тут же откажет («Home first»), она не дёргается и не бежит назад.
async function guardedMove(p,call){let started=false;const go=()=>{if(!started){started=true;holdRun(p)}};const tm=setTimeout(go,350);
  const r=await call();clearTimeout(tm);
  if(r.error){HOLD=0;if(started)setPosTarget(p.prev.x,p.prev.y,p.prev.z);toast(r.error)}else go();return r}
function showLimits(v){if(!v)return;LIM={x:v.x,y:v.y,z:v.z};$('#lims').textContent='Макс. скорости (M203), мм/с: X '+v.x+' · Y '+v.y+' · Z '+v.z+(v.e?' · E '+v.e:'')}
async function loadLimits(){toast('Читаю настройки платы (M503)…');const r=await api('/api/limits?refresh=1');if(r.error){toast(r.error);return}
  const v=r.limits&&r.limits.vmax;if(v){showLimits(v);toast('Скорости считаны')}else toast(r.note||'Прошивка не прислала M203')}
function fitViews(){
  try{const mq=window.matchMedia&&window.matchMedia('(min-width:1450px) and (min-height:640px)'),root=document.documentElement.style,gr=document.querySelector('.grid');
    if(!mq||!mq.matches){root.removeProperty&&root.removeProperty('--vw');if(gr&&gr.style)gr.style.gridTemplateRows='';return}
    if(gr&&gr.style&&FR)gr.style.gridTemplateRows='minmax(0,232fr) minmax(0,'+FR.vbH+'fr)';
    let w=1e9;[['#cTop',232],['#cFro',FR?FR.vbH:323]].forEach(([id,vh])=>{const e=document.querySelector(id+' .vwrap');if(!e)return;const r=e.getBoundingClientRect();w=Math.min(w,r.width-148,(r.height-4)*300/vh)});
    if(w>40&&w<1e9)root.setProperty('--vw',Math.floor(w)+'px')}catch(_){}}
window.addEventListener("resize",fitViews);setTimeout(fitViews,400);
function frontDy(z){const g=GEOM;return g.z_bed?0:(FR.floor-4-FR.tip)-(Math.min(z,g.bz)/g.bz)*FR.T}
function bedYAt(z){const g=GEOM;return g.z_bed?FR.tip+4+(Math.min(z,g.bz)/g.bz)*FR.T:FR.floor}
function updateTwin(s){
  if(!TW||!FR)return;LASTS=s||{};const g=GEOM,p=s.pos,nt=s.nozzle,bt=s.bed,cl=(v,m)=>Math.max(0,Math.min(m,v));
  const fresh=!!p&&!s.stale&&!s.pos_stale&&s.online!==false;
  if(fresh&&HOMING&&!s.homing&&Date.now()-HOMING>4000){HOMING=0;HOLD=0}   // парковка кончилась: сразу на настоящие координаты
  if(fresh){LASTGOOD=Date.now();EVER=true;if(Date.now()>=HOLD)setPosTarget(cl(p.x,g.bx),cl(p.y,g.by),cl(p.z,g.bz))}
  setVeil(s,fresh);
  const x=TGTP?TGTP.x:0,pp=p||(TGTP?{x:TGTP.x,y:TGTP.y,z:TGTP.z}:null),bc=tcol(bt&&bt[0],110),nc=tcol(nt&&nt[0],260);
  $('#tBed').style.fill=bc;$('#tNoz').style.fill=nc;$('#tBedT').textContent=bt?'стол '+tt(bt):'стол —';
  $('#tCoord').textContent=pp?'X '+pp.x.toFixed(1)+'  Y '+pp.y.toFixed(1):'нет данных';
  const fx=FR.l+(x/g.bx)*FR.bw;
  $('#fBed').style.fill=bc;$('#fBlock').style.fill=nc;$('#fNoz').style.fill=nc;
  $('#fBedT').textContent=bt?tt(bt):'';$('#fNozT').textContent=nt?tt(nt):'';
  const right=fx>FR.l+FR.bw*.7,ft=$('#fNozT');ft.setAttribute('x',right?-22:22);ft.setAttribute('text-anchor',right?'end':'start');
  $('#fZ').textContent=pp?'Z '+pp.z.toFixed(2)+' мм':'Z —';
  $('#eBlock').style.fill=nc;$('#eNoz').style.fill=nc;$('#eNozT').textContent=nt?tt(nt):'—';
  $('#eMelt').style.opacity=nt?Math.max(0,Math.min(1,(nt[0]-150)/80)):0;
  const f=s.fan;$('#fanv').textContent=f==null?'—':f+'%';
  const bl=$('#eBlades');bl.style.animationPlayState=f>0?'running':'paused';bl.style.animationDuration=(f>0?(1.6-1.45*f/100):1)+'s';
  if(f!=null&&document.activeElement!==$('#fanr')){$('#fanr').value=f;fanLabel()}
  if(p&&p.e!=null)setE(p.e)}
function setE(v){if(tgtE==null||Math.abs(v-visE)>60)visE=v;tgtE=v;if(!eRaf){lastT=performance.now();eRaf=requestAnimationFrame(animE)}}
function animE(t){const dt=Math.min(.1,(t-lastT)/1000);lastT=t;const d=tgtE-visE;visE+=Math.sign(d)*Math.min(Math.abs(d),25*dt);renderE();
  eRaf=Math.abs(tgtE-visE)>.005?requestAnimationFrame(animE):0}
function renderE(){const a=visE*20;$('#gDrive').setAttribute('transform','rotate('+a+' 62 72)');$('#gIdler').setAttribute('transform','rotate('+(-a*25/21)+' 118 72)');
  $('#eFil').setAttribute('stroke-dashoffset',-(a*Math.PI/180*24));$('#eE').textContent='E '+visE.toFixed(1)+' мм'}
// ---- управление кликом: клик по головке или столу включает режим, следующий клик по месту назначения запускает движение
function syncArm(){if(!TW)return;$('#tRing').style.display=ARM==='xy'?'':'none';$('#fRingH').style.display=ARM==='x'?'':'none';$('#fRingB').style.display=ARM==='z'?'':'none';
  $('#svgTop').style.cursor=ARM==='xy'?'crosshair':'';$('#svgFront').style.cursor=ARM==='x'||ARM==='z'?'crosshair':'';$('#tHov').style.display='none';$('#fHov').style.display='none';
  $('#tZone').style.display=ARM==='xy'?'':'none';
  const fz=frontZone(),fe=$('#fZone');fe.style.display=fz?'':'none';
  if(fz){['x','y','width','height'].forEach((k,i)=>fe.setAttribute(k,fz[i]));}}
// где можно кликнуть на виде спереди: по X — полоса хода головки, по Z — ход стола (или сопла, если по Z ездит головка)
function frontZone(){if(ARM==='x'){const y=(CUR&&!GEOM.z_bed?frontDy(CUR.z):0)+10;return[FR.l-SNAP,y,FR.bw+2*SNAP,88]}
  if(ARM==='z')return[FR.l-6,(GEOM.z_bed?FR.tip+4:FR.floor-4-FR.T)-SNAP,FR.bw+12,FR.T+2*SNAP];return null}
// пунктир обводок: длина узора 5+3 должна укладываться в периметр целое число раз, иначе в углу, где узор начинается, — обрубок
function fixRing(e){const w=+e.getAttribute('width'),h=+e.getAttribute('height'),r=+e.getAttribute('rx')||0,P=2*(w+h)-(8-2*Math.PI)*r;
  e.setAttribute('pathLength',Math.max(8,Math.round(P/8)*8))}
function setArm(k){if(/PRINT|PAUSE/.test(LASTS.state||'')){toast('Во время печати двигать оси нельзя');return}
  ARM=ARM===k?null:k;syncArm();if(ARM)toast({xy:'Теперь нажми на стол на виде сверху: головка поедет туда',x:'Теперь нажми на нужное место по горизонтали: головка поедет по X',z:'Теперь нажми на нужную высоту: стол поедет по Z'}[ARM]+'. Esc — отмена.')}
function svgQ(svg,e){const pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;return pt.matrixTransform(svg.getScreenCTM().inverse())}
// цель клика: значение зажимается в рабочую зону, а прицел (q) «прилипает» к краю — видно, куда реально поедет
function topTarget(e){const q=svgQ($('#svgTop'),e),g=GEOM,cl=(v,m)=>Math.max(0,Math.min(m,v)),
  ok=q.x>=TW.ox-SNAP&&q.x<=TW.ox+TW.bw+SNAP&&q.y>=TW.oy-SNAP&&q.y<=TW.oy+TW.bh+SNAP,
  x=cl((q.x-TW.ox)/TW.s,g.bx),y=cl(g.by-(q.y-TW.oy)/TW.s,g.by);
  return{x,y,ok,q:{x:TW.ox+x*TW.s,y:TW.oy+(g.by-y)*TW.s}}}
function frontTarget(e){const q=svgQ($('#svgFront'),e),g=GEOM;
  const zn=frontZone(),ok=!!zn&&q.x>=zn[0]&&q.x<=zn[0]+zn[2]&&q.y>=zn[1]&&q.y<=zn[1]+zn[3];   // кликать можно ровно там, где подсвечено
  if(ARM==='x'){const val=Math.max(0,Math.min(g.bx,(q.x-FR.l)/FR.bw*g.bx));return{ok,val,q:{x:FR.l+val/g.bx*FR.bw,y:q.y}}}
  const k=g.z_bed?(q.y-(FR.tip+4))/FR.T:(FR.floor-4-q.y)/FR.T,val=Math.max(MINZ,Math.min(g.bz,k*g.bz)),kv=val/g.bz*FR.T;
  return{ok,val,q:{x:q.x,y:g.z_bed?FR.tip+4+kv:FR.floor-4-kv}}}
async function goTo(p){ARM=null;syncArm();log('> переезд '+Object.keys(p).map(k=>k.toUpperCase()+p[k].toFixed(1)).join(' '));
  const plan=holdPlan(p.x,p.y,p.z,feedXY(50),Math.min(10,LIM.z||10));
  const r=await guardedMove(plan,()=>api('/api/goto',p));if(r.error)log(r.error+(r.reply?' · '+r.reply.split('\n')[0]:''),'#ef5350');else log(r.reply||'(отправлено)');tick()}
function hovLabel(rid,tid,txt,x,y,minx,maxx,miny,maxy){const t=$(tid),r=$(rid);t.textContent=txt;let w=0;try{w=t.getBBox().width}catch(_){}if(!w)w=txt.length*6;w+=10;const h=15;
  let lx=x+10;if(lx+w>maxx)lx=x-10-w;if(lx<minx)lx=minx;let ly=y-10-h;if(ly<miny)ly=y+10;if(ly+h>maxy)ly=maxy-h;
  r.setAttribute('x',lx);r.setAttribute('y',ly);r.setAttribute('width',w);t.setAttribute('x',lx+5);t.setAttribute('y',ly+11)}
function inEl(t,id){return !!(t&&t.closest&&t.closest(id))}
(function(){const T=$('#svgTop'),F=$('#svgFront'),down=e=>{e.preventDefault();DOWN={x:e.clientX,y:e.clientY,t:e.target}},
  moved=e=>!DOWN||Math.hypot(e.clientX-DOWN.x,e.clientY-DOWN.y)>8;
  T.addEventListener('pointerdown',down);F.addEventListener('pointerdown',down);
  T.addEventListener('pointerup',e=>{const bad=moved(e),t=DOWN&&DOWN.t;DOWN=null;if(bad||!TW)return;
    if(ARM!=='xy'){if(inEl(t,'#tHead'))setArm('xy');else if(ARM){ARM=null;syncArm()}return}
    const r=topTarget(e);if(!r.ok){ARM=null;syncArm();return}goTo({x:r.x,y:r.y})});
  F.addEventListener('pointerup',e=>{const bad=moved(e),t=DOWN&&DOWN.t;DOWN=null;if(bad||!TW)return;
    if(ARM!=='x'&&ARM!=='z'){if(inEl(t,'#fHead'))setArm('x');else if(inEl(t,'#fBedG'))setArm('z');else if(ARM){ARM=null;syncArm()}return}
    const r=frontTarget(e);if(!r.ok){ARM=null;syncArm();return}goTo(ARM==='x'?{x:r.val}:{z:r.val})});
  T.addEventListener('pointermove',e=>{if(ARM!=='xy'||!TW)return;const r=topTarget(e),hv=$('#tHov');T.style.cursor=r.ok?'crosshair':'not-allowed';if(!r.ok){hv.style.display='none';return}
    hv.style.display='';const H=$('#tHovH'),V=$('#tHovV'),C=$('#tHovC');H.setAttribute('y1',r.q.y);H.setAttribute('y2',r.q.y);V.setAttribute('x1',r.q.x);V.setAttribute('x2',r.q.x);C.setAttribute('cx',r.q.x);C.setAttribute('cy',r.q.y);
    hovLabel('#tHovR','#tHovT','X '+r.x.toFixed(0)+'   Y '+r.y.toFixed(0),r.q.x,r.q.y,2,298,2,230)});
  F.addEventListener('pointermove',e=>{if((ARM!=='x'&&ARM!=='z')||!TW)return;const r=frontTarget(e),hv=$('#fHov');F.style.cursor=r.ok?'crosshair':'not-allowed';if(!r.ok){hv.style.display='none';return}
    const L=$('#fHovL'),C=$('#fHovC');hv.style.display='';C.setAttribute('cx',r.q.x);C.setAttribute('cy',r.q.y);
    if(ARM==='x'){const zn=frontZone();L.setAttribute('x1',r.q.x);L.setAttribute('x2',r.q.x);L.setAttribute('y1',zn[1]);L.setAttribute('y2',zn[1]+zn[3]);hovLabel('#fHovR','#fHovT','X '+r.val.toFixed(0),r.q.x,r.q.y,2,298,2,FR.vbH-2)}
    else{L.setAttribute('x1',FR.l);L.setAttribute('x2',FR.l+FR.bw);L.setAttribute('y1',r.q.y);L.setAttribute('y2',r.q.y);hovLabel('#fHovR','#fHovT','Z '+r.val.toFixed(1)+(r.val<=MINZ?' · мин':''),r.q.x,r.q.y,2,298,2,FR.vbH-2)}});
  T.addEventListener('pointerleave',()=>{if(TW)$('#tHov').style.display='none'});F.addEventListener('pointerleave',()=>{if(TW)$('#fHov').style.display='none'});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&ARM){ARM=null;syncArm()}});
  document.addEventListener('pointerdown',e=>{if(ARM&&!inEl(e.target,'#svgTop')&&!inEl(e.target,'#svgFront')){ARM=null;syncArm()}},true)})();
function setT(code,id){const v=$('#'+id).value;if(v==='')return toast('Введи температуру');cmd(code+' S'+(+v))}
const printing=()=>/PRINT|PAUSE/.test(LASTS.state||'');
const SPOIL=['Выключить нагрев во время печати?','Пластик перестанет плавиться, печать будет испорчена.','Выключить',1];
async function heatOff(code){if(printing()&&!await ask(...SPOIL))return;cmd(code+' S0')}
async function coolAll(){if(printing()&&!await ask(...SPOIL))return;
  tab('log');log('> остудить всё: M104 S0, M140 S0');
  for(const c of['M104 S0','M140 S0']){const r=await api('/api/console',{cmd:c});if(r.error){log(r.error,'#ef5350');toast(r.error);return}}
  log('нагрев сопла и стола выключен');toast('Нагрев выключен');tick()}
// ---- настройки
function openSet(){$('#ov').classList.add('on');loadSettings()}function closeSet(){$('#ov').classList.remove('on')}
async function loadSettings(){
  const s=await api('/api/settings');if(s.error){toast(s.error);return}
  $('#sip').value=s.printer_ip||'';$('#sow').checked=!!s.overwrite;$('#sbg').checked=!!s.background_poll;$('#smax').value=s.max_name;
  $('#srem').checked=!!s.remote;$('#sauto').checked=!!s.autostart;$('#autorow').style.display=s.can_autostart?'':'none';$('#ver').textContent='v'+s.version;
  if(s.geom){GEOM=s.geom;$('#gbx').value=GEOM.bx;$('#gby').value=GEOM.by;$('#gbz').value=GEOM.bz;$('#gzb').checked=!!GEOM.z_bed;buildTwin();updateTwin(LASTS)}
  showLimits(s.limits&&s.limits.vmax);
  const rm=$('#remote');rm.hidden=!s.remote;rm.textContent='';
  if(s.remote){const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
    const a=el('div',null,'Открой на телефоне (он должен быть в той же Wi-Fi-сети): ');s.urls.forEach((u,i)=>{if(i)a.append(' или ');a.append(el('code',null,u))});rm.append(a);
    if(s.pin){const p=el('div','row');p.style.margin='0';p.append(el('span','mut','PIN'),el('span','pin',s.pin),el('span','sp'));
      const b=el('button','sm','Новый PIN');b.onclick=newPin;p.append(b);rm.append(p)}}}
async function newPin(){if(!await ask('Сменить PIN?','Телефоны, на которых уже входили, попросят ввести новый.','Сменить'))return;
  const r=await api('/api/settings',{new_pin:true});toast(r.error||'PIN изменён');loadSettings()}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('#ov').classList.contains('on')&&!$('#dlg').classList.contains('on'))closeSet()});
async function saveIp(){const r=await api('/api/settings',{printer_ip:$('#sip').value});toast(r.error||'Сохранено');tick();loadSettings()}
async function saveOpt(){const r=await api('/api/settings',{overwrite:$('#sow').checked,background_poll:$('#sbg').checked,max_name:+$('#smax').value,bed_x:+$('#gbx').value,bed_y:+$('#gby').value,bed_z:+$('#gbz').value,z_bed:$('#gzb').checked});toast(r.error||'Сохранено');loadSettings()}
async function saveRemote(){const r=await api('/api/settings',{remote:$('#srem').checked});toast(r.error||'Сохранено');loadSettings()}
async function saveAuto(){const r=await api('/api/settings',{autostart:$('#sauto').checked});toast(r.error||'Сохранено')}
async function findPrinter(){toast('Ищу принтер в сети…');const r=await api('/api/discover',{});
  if(r.error){toast('Ошибка поиска: '+r.error);return}const sc=(r.scanned||[]).map(b=>b+'.1–254').join(', ');
  if(!r.found||!r.found.length){toast('Принтер не найден. Проверено: '+(sc||'ничего')+'. Включён ли принтер и в той же ли он сети? Можно ввести IP вручную.');return}
  let ip=r.found[0];if(r.found.length>1){ip=await ask('Найдено несколько принтеров','Выбери, к какому подключаться:',null,0,r.found);if(!ip)return}
  $('#sip').value=ip;saveIp()}
renderRef();buildTwin();applyTheme();loadSettings();tick();setInterval(tick,3000);
</script></body></html>
"""

if __name__ == "__main__":
    main()
