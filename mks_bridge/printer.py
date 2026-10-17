"""Протокол MKS-WIFI: команды по TCP (порт 8080), заливка файла по HTTP, разбор ответов Marlin и модуля Wi-Fi.
Модуль держит одного TCP-клиента, поэтому все обращения идут через один замок, а во время заливки — никаких."""
import http.client
import os
import re
import socket
import threading
import time
from collections import deque
from urllib.parse import quote

from .config import CFG, cfg_save, log, notify
from .discovery import discover

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
        self._autoreport = False # M155 уже включён для текущей печати
        self._home_sock = None
        self.fan = None          # последняя заданная скорость обдува, % (принтер её не сообщает)
        self.history = deque(maxlen=3000)
        self._tcp_lock = threading.Lock()
        self._urgent = 0         # сколько команд пользователя ждут соединения: опрос уступает им место
        self._urgent_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._cache = (0.0, None)
        self._gen = 0            # растёт после каждой команды, меняющей состояние: опрос, начатый раньше, уже устарел
        self._last_scan = 0.0
        self.last_raw = ""
        self.files_complete = True  # последний список файлов пришёл целиком
        self.last_status_raw = ""   # сырой ответ на последний опрос состояния (для журнала)
        self.described = False      # что принтер сообщает о себе, уже записано в журнал
        self.limits = CFG.get("limits")   # максимальные скорости из прошивки (M203), мм/с; читаются только по кнопке и запоминаются
        self.fails = 0           # подряд неудачных опросов
        self.fail_until = 0.0    # до этого момента принтер не дёргаем (пауза после сбоя)
        self.busy_until = 0.0    # принтер занят движением: не засыпаем его запросами
        self.last_err = ""

    # --- TCP ---
    def _connect(self, poll=False):
        """Модуль принтера держит одного клиента: при «отказе»/сбросе соединения пробуем ещё раз, а не считаем принтер пропавшим."""
        last = None
        for i in range(3):
            try:
                return socket.create_connection((self.ip, self.tcp_port), timeout=4)
            except (ConnectionRefusedError, ConnectionResetError) as e:
                last = e
                if poll and self._urgent:
                    break   # опрос не занимает очередь повторами, пока ждёт команда
                time.sleep(0.6)
        raise last

    def _cmd_once(self, commands, wait, until, poll=False):
        with self._connect(poll) as s:
            s.sendall("".join(c + "\r\n" for c in commands).encode())
            s.settimeout(0.15 if poll else 0.4)   # опрос чаще проверяет, не ждёт ли команда
            buf, deadline = b"", time.time() + wait
            while time.time() < deadline:
                if poll and self._urgent:
                    break   # пользователь ждёт команду: опрос бросает ожидание, недостающее доберёт в следующий раз
                try:
                    d = s.recv(4096)
                except socket.timeout:
                    if buf and not until:
                        break   # конец ответа неизвестен: пауза — значит, принтер договорил
                    continue
                if not d:
                    break
                buf += d
                if isinstance(until, tuple):
                    if all(u.encode() in buf for u in until):
                        break
                elif until and until.lower().encode() in buf.lower():
                    break
        return buf.decode(errors="replace")

    def invalidate(self):
        self._cache = (0.0, self._cache[1])

    def _bump_urgent(self, d):
        with self._urgent_lock:
            self._urgent += d

    def cmd(self, commands, wait=1.5, until=None, rediscover=True, poll=False):
        """poll=True — фоновый опрос: уступает соединение командам пользователя."""
        changes = not all(c.split()[0].upper() in ("M105", "M997", "M27", "M992", "M994", "M114", "M20") for c in commands if c.split())
        if changes:
            self.invalidate()  # после команд движения/нагрева следующий статус должен быть свежим
        toks = [c.split()[0].upper() for c in commands if c.split()]
        if "G28" in toks:
            self.busy_until = time.time() + 25     # хоуминг: в это время принтер лучше не опрашивать
        elif any(t in ("G0", "G1") for t in toks):
            self.busy_until = time.time() + 4
        t0, waiting = time.time(), not poll
        if waiting:
            self._bump_urgent(1)
        try:
            with self._tcp_lock:
                if waiting:
                    self._bump_urgent(-1)
                    waiting = False
                    if time.time() - t0 > 0.5:
                        log("команда %s ждала соединения %.1f с" % (toks[0] if toks else "?", time.time() - t0))
                try:
                    r = self._cmd_once(commands, wait, until, poll)
                except OSError:
                    if not rediscover or not self.rediscover():
                        raise
                    r = self._cmd_once(commands, wait, until, poll)
                self._after_cmd(changes)
        finally:
            if waiting:
                self._bump_urgent(-1)
        self._recovered()
        return r

    def _after_cmd(self, changes):
        if changes:
            # Опрос, который держал соединение до нас, прочитал состояние ДО команды и сейчас положит его в кэш
            # как свежее. Новое поколение не даст ему этого: см. status_cached.
            self._gen += 1
            self.invalidate()

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

    def describe(self):
        """Один раз за запуск: прошивка (M115) и пример ответа на опрос — по ним видно, чем чужой принтер
        отличается от проверенного (другая прошивка или модуль)."""
        self.described = True
        log("ответ принтера на опрос состояния: %r" % self.last_status_raw[:500])
        try:
            log("прошивка (M115): %r" % self.cmd(["M115"], wait=2.5, until="FIRMWARE_NAME", poll=True)[:400])
        except OSError as e:
            log("M115 не получен: %s" % e)

    def files(self):
        """Список G-code на карте. self.files_complete — пришёл ли список целиком (до «End file list»):
        по Wi-Fi большой список идёт с паузами, обрезанный нельзя считать правдой о карте."""
        for attempt in (1, 2):
            text = self.cmd(["M20"], wait=10, until="End file list")
            if "end file list" in text.lower():
                break
            log("список файлов пришёл не целиком (попытка %d), %d байт" % (attempt, len(text)))
        self.last_raw = text
        self.files_complete = "end file list" in text.lower()
        names = []
        for line in text.splitlines():
            line = line.strip().lstrip("/")
            # Marlin_FB4S через Wi-Fi пишет только имя; обычный Marlin после имени пишет размер в байтах («MODEL.GCO 123456»)
            m = re.match(r"(.+?\.(?:gcode|gco|gc|g))(?:\s+\d+)?$", line, re.I)
            if m:
                names.append(m.group(1))
        return names

    def status(self, quiet=False):
        cached = self._cache[1] or {}
        cmds = ["M105", "M997", "M114"]
        if is_printing(cached) or not cached:
            cmds = ["M105", "M997", "M27", "M992", "M994", "M114"]   # подробности нужны только во время печати
        # Во время печати Marlin отвечает с опозданием и пачками (вперемешку с «busy: processing»), часть ответов
        # приходит уже в следующее соединение. Поэтому ждём, пока придут температура и координаты, а из пачки
        # берём последние (самые свежие) значения.
        printing = is_printing(cached)
        text = self.cmd(cmds, wait=3.0 if printing else 1.5, until=("T:", "Count"), rediscover=not quiet, poll=quiet)
        self.last_status_raw = text
        last = lambda rx: (re.findall(rx, text) or [None])[-1]
        st = {"state": "unknown", "progress": None, "nozzle": None, "bed": None,
              "elapsed": None, "file": None, "pos": None, "sd": None, "left": None}
        m = re.search(r"M997\s+([A-Z ]+)", text)
        if m:
            st["state"] = m.group(1).strip()
        m = re.search(r"M27\s+(\d+)", text)
        if m:
            st["progress"] = int(m.group(1))
        m = last(r"T:\s*([\d.]+)\s*/\s*([\d.]+)\s+B:\s*([\d.]+)\s*/\s*([\d.]+)")
        if m:
            st["nozzle"] = (float(m[0]), float(m[1]))
            st["bed"] = (float(m[2]), float(m[3]))
        # модуль Wi-Fi на M27 отвечает из своей памяти и прогресса не знает; Marlin сообщает точно
        m = last(r"SD printing byte (\d+)/(\d+)")
        if m and int(m[1]):
            st["sd"] = (int(m[0]), int(m[1]))
            st["sd_live"] = True   # эту строку Marlin выдаёт только во время печати; на паузе — «Not SD printing»
        if st["progress"] == 0 and (is_printing(cached) or is_printing(st)):
            st["progress"] = None  # «M27 0» от модуля во время печати ничего не значит: ждём данных от Marlin
        m = last(r"M73 Progress: (\d+)%(?:; Time left: (\d+)m)?")
        if m and m[1]:
            st["left"] = int(m[1]) * 60
        m = re.search(r"M992\s+(\d+):(\d+):(\d+)", text)
        if m:
            st["elapsed"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        m = re.search(r"M994\s+/?([^\r\n;]+\.(?:gcode|gco|gc|g))", text, re.I)
        if m:
            st["file"] = m.group(1).strip()
        # после координат Marlin пишет «Count X:.. Y:.. Z:..» — это счётчики шагов моторов, их пропускаем
        m = last(r"(?<!Count )X:\s*(-?[\d.]+)\s+Y:\s*(-?[\d.]+)\s+Z:\s*(-?[\d.]+)(?:\s+E:\s*(-?[\d.]+))?")
        if m:
            st["pos"] = {"x": float(m[0]), "y": float(m[1]), "z": float(m[2]), "e": float(m[3]) if m[3] else None}
        if st["sd"] and not st["progress"]:
            st["progress"] = st["sd"][0] * 100 // st["sd"][1]
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
        # «SD printing byte» и M73 от Marlin приходят не в каждом опросе (ответы опаздывают и идут пачками):
        # держим последние известные, иначе прогресс скачет к нулю — модуль Wi-Fi на M27 отвечает «0»
        if is_printing(st):
            for k in ("sd", "left"):
                if st.get(k) is None and prev.get(k) is not None and now - prev.get("good_t", 0) < 90:
                    st[k] = prev[k]
            if st.get("sd") and not st.get("progress"):
                st["progress"] = st["sd"][0] * 100 // st["sd"][1]
        # Экран MKS не знает о печати, запущенной не с него (M23/M24 по Wi-Fi), и сообщает «PAUSE».
        # Правду говорит позиция в файле: растёт — значит печатает.
        moved = st.get("sd_live") or (st.get("sd") and prev.get("sd") and st["sd"][0] > prev["sd"][0])
        st["sd_moving_t"] = now if moved else prev.get("sd_moving_t", 0)
        # На это опираются и блокировки опасных команд: модуль может сказать и «IDLE», а печать идёт.
        if st["state"] != "PRINTING" and now - st["sd_moving_t"] < 40:   # строка от Marlin бывает и раз в 20–30 с
            st["state"] = "PRINTING"
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
        # Панели (allow_stale) не ждут опроса, который уже идёт (во время печати он длится до 3 с): отдаём последнее известное
        if not self._status_lock.acquire(blocking=not (allow_stale and self._cache[1])):
            return self._cache[1]
        try:
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
            if n or b:   # опрос, уступивший место команде, мог не дождаться температур: это не разрыв графика
                self.history.append((int(time.time()), n[0] if n else None, b[0] if b else None,
                                     n[1] if n else None, b[1] if b else None))
            st = self._merge(raw, self._cache[1])
            if self._gen != gen:   # пока мы спрашивали, ушла команда движения/нагрева: ответ уже не отражает принтер
                st = dict(st, stale=True, pos_stale=True)
                self._cache = (0.0, st)
            else:
                self._cache = (time.time(), st)
            return st
        finally:
            self._status_lock.release()

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
        CFG["print_started"] = time.time()
        cfg_save()
        self.cmd(["M23 " + name], wait=1.0)
        time.sleep(0.5)
        self.cmd(["M24"], wait=1.0)
        self.enable_autoreport()

    def enable_autoreport(self):
        """Печать запущена по Wi-Fi (M23/M24): модуль MKS-WIFI отвечает на M105 застывшими значениями из своей памяти
        (Marlin во время печати не отвечает ему на опрос). Просим плату саму присылать температуру раз в 2 с (M155) —
        это ничего не меняет в печати, только частоту отчёта."""
        self._autoreport = True
        try:
            self.cmd(["M155 S2"], wait=0.5)
            log("включён автоотчёт температуры (M155 S2)")
        except OSError as e:
            log("M155 не отправлен: %s" % e)

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
