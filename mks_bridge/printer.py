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
from .i18n import tr

_CYR = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n",
     "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y",
     "", "e", "yu", "ya"]))


_PUNCT = {"—": "-", "–": "-", "«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "’": "'", "…": "...", "°": "deg", "×": "x", "№": "N"}


def translit(text):
    """Кириллица и типографские знаки -> латиница/ASCII; прочее не-ASCII -> «?»."""
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
            continue
        low = ch.lower()
        if low in _CYR:
            t = _CYR[low]
            out.append(t.capitalize() if ch != low else t)
        else:
            out.append(_PUNCT.get(ch, "?"))
    return "".join(out)


def sanitize_name(name, max_len, ellipsis="~"):
    """Имя для экрана принтера: латиница, <= max_len; у длинных вырезается середина."""
    base = os.path.basename(name)
    stem, ext = os.path.splitext(base)
    if ext.lower() not in (".gcode", ".gco", ".g"):
        stem, ext = base, ".gcode"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", translit(stem)).strip("_.") or "model"
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
        # Стоковая прошивка Ghost 5 на M114 отвечает одним «ok»: координат нет вообще. Узнаём это сами и говорим панели,
        # чтобы она не ждала их вечно. Marlin не отвечает на M114 и пока греется (M109/M190 держат очередь) — поэтому
        # считаем только время, когда принтер точно должен ответить: простой или печать, у которой двигается прогресс.
        self.pos_seen = False    # принтер хоть раз прислал координаты
        self.no_pos = False      # принтер координат не сообщает (стоковая прошивка)
        self._nopos = None       # (с какого момента нет координат, прогресс в тот момент)
        # Положение по собственным командам — только для таких прошивок. После парковки оно известно точно, дальше мост
        # прибавляет свои перемещения. Теряется при печати, выключении моторов и любой команде движения из консоли.
        # Движений с экрана принтера мост не видит: это цена отсутствия M114.
        self.est = None          # {"x":, "y":, "z":} — None по оси, которая не парковалась
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
                    # один опрос без ответа во время печати — обычное дело; ищем принтер в сети со второго сбоя подряд
                    if not rediscover or not self.fails or not self.rediscover():
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
            notify("MKS WiFi Bridge", tr("notify.moved", ip=self.ip))
            return True
        return False

    def home(self, command):
        """Парковка. Marlin не отвечает на запросы, пока паркуется, но команды из того же соединения ставит в очередь:
        шлём G28 и сразу M114 и держим соединение — ответ на M114 придёт ровно в момент окончания парковки.
        Пока ждём, прошивка каждые 2 с присылает «busy: processing»; тишина 15 с — значит связь пропала."""
        if self.homing:
            raise RuntimeError(tr("err.already_homing"))
        self.homing = True
        started, err = threading.Event(), []

        def run():
            t0, buf, done, broke = time.time(), b"", False, False
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
                            # прошивка без координат: M114 отвечает одним «ok», а «ok» на G28 Marlin шлёт после парковки
                            if self.no_pos and buf.count(b"ok") >= 2:
                                done = True
                                break
                    except OSError as e:
                        broke = True
                        log("парковка: связь оборвалась: %r" % (e,))
                    finally:
                        self._home_sock = None
                        s.close()
                log("парковка %s: %s за %.1f с" % (command, "готово" if done else "конца не дождались", time.time() - t0))
                if not broke:   # прошивке без M114 конец парковки не узнать точнее: после неё оси в нуле
                    axes = [a for a in "XYZ" if a in command.upper().split()[1:]] or list("XYZ")
                    self.est_set(**{a.lower(): 0.0 for a in axes})
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
        m = last(r"M997\s+([A-Z ]+)")
        if m:
            st["state"] = m.strip()
        m = last(r"M27\s+(\d+)")
        if m:
            st["progress"] = int(m)
        m = last(r"T:\s*(-?[\d.]+)\s*/\s*([\d.]+)\s+B:\s*(-?[\d.]+)\s*/\s*([\d.]+)")   # минус: оборванный датчик
        if m:
            st["nozzle"] = (float(m[0]), float(m[1]))
            st["bed"] = (float(m[2]), float(m[3]))
        # модуль Wi-Fi на M27 отвечает из своей памяти и прогресса не знает; Marlin сообщает точно
        m = last(r"SD printing byte (\d+)/(\d+)")
        if m and int(m[1]):
            st["sd"] = (int(m[0]), int(m[1]))
            st["sd_live"] = True   # эту строку Marlin выдаёт только во время печати; на паузе — «Not SD printing»
        # Ответы идут по порядку: «Not SD printing» после последней позиции в файле — Marlin файл больше не печатает
        # (печать кончилась или стоит на паузе), и ждать, пока «печать» сама сойдёт через 40 с, незачем (см. _merge).
        if text.rfind("Not SD printing") > text.rfind("SD printing byte"):
            st["sd_stopped"] = True
            st.pop("sd_live", None)
        if st["progress"] == 0 and (is_printing(cached) or is_printing(st)):
            st["progress"] = None  # «M27 0» от модуля во время печати ничего не значит: ждём данных от Marlin
        m = last(r"M73 Progress: (\d+)%(?:; Time left: (\d+)m)?")
        if m and m[1]:
            st["left"] = int(m[1]) * 60
        m = last(r"M992\s+(\d+):(\d+):(\d+)")
        if m:
            st["elapsed"] = int(m[0]) * 3600 + int(m[1]) * 60 + int(m[2])
        m = last(r"(?i)M994\s+/?([^\r\n;]+\.(?:gcode|gco|gc|g))")
        if m:
            # стоковая прошивка MKS пишет имя с диском и путём («1:/FBG5_key.gcode»), а мост помнит файлы по имени
            st["file"] = m.strip().replace("\\", "/").rsplit("/", 1)[-1].strip()
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
            if st.get("sd_live"):   # первый опрос после запуска моста посреди печати: модулю на слово не верим и тут
                st["sd_moving_t"], st["state"] = now, "PRINTING"
            return st
        if not got:
            old = dict(prev)
            old["stale"] = True
            return old
        st["good_t"] = now
        for k in ("pos", "nozzle", "bed"):
            if k == "pos" and self.no_pos:
                continue   # положение у такой прошивки — только расчётное, его подставляет status_cached
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
            # когда M73 сменил значение: «Осталось» между редкими строками M73 отсчитываем сами
            st["left_t"] = prev.get("left_t") if st.get("left") == prev.get("left") and prev.get("left_t") else now
            if st.get("sd") and not st.get("progress"):
                st["progress"] = st["sd"][0] * 100 // st["sd"][1]
        # Экран MKS не знает о печати, запущенной не с него (M23/M24 по Wi-Fi), и сообщает «PAUSE».
        # Правду говорит позиция в файле: растёт — значит печатает.
        moved = st.get("sd_live") or (st.get("sd") and prev.get("sd") and st["sd"][0] > prev["sd"][0])
        # позиция в файле может не приходить минуту; голова, которая ездит посреди печати, — тоже печать
        if (not moved and prev.get("state") == "PRINTING" and not st.get("pos_stale") and st.get("pos")
                and prev.get("pos") and st["pos"] != prev["pos"]):
            moved = True
        if st.get("sd_stopped"):
            st["sd_moving_t"] = 0
        elif st.get("sd_live") and st["sd"][0] >= st["sd"][1]:
            # Файл дочитан до конца: печать кончается. «Not SD printing» модуль MKS-WIFI сюда не пропускает (проверено
            # на живом принтере 2026-11-11), поэтому вместо 40 с держим «печать» ещё 10 — на последние движения.
            st["sd_moving_t"] = now - 30
        else:
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
                st["state"] = tr("state.uploading")
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
                raise RuntimeError(self.last_err or tr("err.no_link"))
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
            self._check_pos(raw, st)
            st["no_pos"] = self.no_pos
            if self.no_pos:
                if is_printing(st):
                    self.est = None   # печать (в том числе запущенная с экрана): где голова потом — неизвестно
                st.pop("pos_stale", None)
                st["pos"] = self._est_pos()
                st["pos_est"] = st["pos"] is not None
            if self._gen != gen:   # пока мы спрашивали, ушла команда движения/нагрева: ответ уже не отражает принтер
                st = dict(st, stale=True, pos_stale=True)
                self._cache = (0.0, st)
            else:
                self._cache = (time.time(), st)
            return st
        finally:
            self._status_lock.release()

    def est_set(self, **axes):
        lim = {"x": CFG["bed_x"], "y": CFG["bed_y"], "z": CFG["bed_z"]}
        e = self.est or {"x": None, "y": None, "z": None}
        for k, v in axes.items():
            e[k] = round(max(0.0, min(lim[k], float(v))), 3)
        self.est = e
        self._est_to_cache()

    def est_add(self, axis, dist):
        if self.est and self.est.get(axis) is not None:
            self.est_set(**{axis: self.est[axis] + dist})

    def est_lost(self):
        self.est = None
        self._est_to_cache()

    def _est_pos(self):
        e = self.est
        return dict(e, e=None) if e and None not in e.values() else None

    def _est_to_cache(self):
        """Пока принтер занят движением, панели отдаётся прошлое состояние из кэша: кладём туда новое положение сразу."""
        c = self._cache[1]
        if c is not None and self.no_pos:
            pos = self._est_pos()
            self._cache = (0.0, dict(c, pos=pos, pos_est=pos is not None))
        else:
            self.invalidate()

    def _check_pos(self, raw, st):
        """Сообщает ли принтер координаты. Ответ без них в простое 30 с или во время печати 90 с (при этом прогресс
        двигается, то есть принтер не стоит на нагреве) — значит, прошивка их не даёт. Первые же координаты это снимают."""
        if raw.get("pos"):
            if self.no_pos:
                log("принтер прислал координаты: панель снова показывает положение головы")
            self.pos_seen, self.no_pos, self._nopos = True, False, None
            return
        if self.pos_seen or self.no_pos or not raw.get("nozzle") or self.homing:
            return
        now, prog = time.time(), st.get("progress")
        if self._nopos is None:
            self._nopos = (now, prog)
            return
        t0, p0 = self._nopos
        printing = is_printing(st)
        if printing and (prog is None or prog == p0):
            return   # прогресс стоит (нагрев перед печатью): Marlin в это время и не должен отвечать
        if now - t0 >= (90 if printing else 30):
            self.no_pos = True
            log("принтер не сообщает координаты (M114 без X:/Y:), панель показывает схему без положения головы")

    def printing_now(self):
        """Печатает ли принтер прямо сейчас — для блокировок удаления, запуска и заливки.
        Сырой status() тут не годится: модуль на M997 может ответить IDLE при идущей печати. Спрашиваем заново через
        status_cached, где состояние поправлено по ответам самого Marlin («SD printing byte»)."""
        self.fail_until = 0.0   # действие пользователя: пауза после сбоя связи попытке не мешает
        return is_printing(self.status_cached(max_age=0))

    def delete(self, name, _checked=False, listed=None):
        if not _checked and self.printing_now():
            raise RuntimeError(tr("err.delete_printing"))
        listed = listed if listed is not None else self.files()
        actual = next((n for n in listed if n.lower() == name.lower()), None)
        if actual is None:
            raise RuntimeError(tr("err.no_such_file", name=name))
        for form in ("M30 %s", "M30 /%s", "M30 0:/%s"):
            self.cmd([form % actual], wait=1.0)
            time.sleep(0.8)
            if actual.lower() not in [n.lower() for n in self.files()]:
                log("удалён %s командой %r" % (actual, form % "<имя>"))
                return form % "<имя>"
        raise RuntimeError(tr("err.delete_failed"))

    def start_print(self, name):
        reply = self.cmd(["M23 " + name], wait=1.0)
        if re.search(r"open failed", reply, re.I):   # Marlin: «open failed, File: имя.»
            log("M23 %s: %r" % (name, reply[:200]))
            raise RuntimeError(tr("err.open_failed", name=name))
        CFG["print_started"] = time.time()
        cfg_save()
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
                        raise RuntimeError(tr("job.broken", sent=sent, size=size, pct=sent * 100 // max(1, size), e=repr(e)))
                    sent += len(chunk)
                    progress(sent, size)
            try:
                resp = conn.getresponse()
                body = resp.read()
                log("ответ принтера на заливку: HTTP %d %r" % (resp.status, body[:200]))
                if resp.status >= 400:
                    raise RuntimeError(tr("job.http", code=resp.status))
            except (http.client.HTTPException, ConnectionError, socket.timeout) as e:
                log("принтер не прислал нормальный ответ после заливки: %r" % (e,))
        finally:
            conn.close()
