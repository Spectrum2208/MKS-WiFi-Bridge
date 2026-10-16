"""Действия панели: состояние принтера, файлы, движение, консоль, настройки."""
import bisect
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse

from . import VERSION
from .autostart import autostart_get, autostart_set
from .config import CFG, CFG_DIR, LOG_PATH, cfg_save, log
from .discovery import discover, local_ip
from .jobs import JOBS, Job, run_job
from .printer import is_printing, sanitize_name
from .store import LAYERS, OUTL_DIR, job_key, layer_info, outline_load, remember_started

SAFE_WHILE_PRINTING = {"M105", "M114", "M115", "M27", "M25", "M24", "M26", "M220", "M221", "M104",
                       "M140", "M106", "M107", "M117", "M503", "M991", "M992", "M994", "M997"}
ACTIVITY = {"t": 0.0}   # когда последний раз открывали панель
APP = {"stop": lambda: os._exit(0)}


class PanelApi:
    # --- состояние ---
    def _get_status(self):
        self._json(200, self._status())

    def _status(self):
        ACTIVITY["t"] = time.time()
        q = parse_qs(urlparse(self.path).query)
        try:
            win = max(60, min(7200, int(q.get("win", ["900"])[0])))
        except ValueError:
            win = 900
        now = int(time.time())
        bg = CFG.get("background_poll", True)
        out = {"ip": self.printer.ip, "online": False, "now": now, "jobs": [j.as_dict() for j in list(JOBS)]}
        if not self.printer.ip:
            out["error"] = "IP принтера не задан: открой настройки и нажми «Найти принтер»."
            out["history"] = []
            return out
        try:
            st = self.printer.status_cached(max_age=12 if bg else 2.5, quiet=bg, allow_stale=True)
            out.update(st)
            out["online"] = True
            if is_printing(st):
                li = layer_info(st.get("file"), st.get("progress"), (st.get("pos") or {}).get("z"), st.get("sd"))
                if li:
                    out.update(li)
                if st.get("left") is not None:   # оставшееся время от слайсера (M73) точнее нашей оценки
                    out["eta"] = st["left"]
                t0 = CFG.get("print_started") or 0
                if not st.get("elapsed") and 0 < time.time() - t0 < 7 * 86400:   # экран MKS тут тоже отвечает нулём
                    out["elapsed"] = int(time.time() - t0)
                k = job_key(st.get("file"))
                if k and os.path.exists(os.path.join(OUTL_DIR, k + ".json")):
                    out["outline"] = k
        except Exception as e:
            out["error"] = str(e)
        out["ip"] = self.printer.ip  # адрес мог поменяться при автопоиске прямо во время запроса
        out["fan"] = self.printer.fan
        sd, idx = out.get("sd"), LAYERS.get(job_key(out.get("file")))
        if is_printing(out) and sd and idx and idx.get("fan") is not None and abs(sd[1] - idx["size"]) <= 64:
            i = bisect.bisect_right([o for o, _ in idx["fan"]], sd[0]) - 1   # последняя команда обдува до позиции печати
            out["fan"] = idx["fan"][i][1] if i >= 0 else 0
            out["fan_src"] = "gcode"
        if out.get("stale") and out.get("good_t"):
            out["stale_age"] = round(time.time() - out["good_t"], 1)
        hist = [p for p in list(self.printer.history) if p[0] >= now - win]
        step = max(1, -(-len(hist) // 400))
        out["history"] = hist[::step]
        if hist and out["history"][-1] is not hist[-1]:
            out["history"].append(hist[-1])
        return out

    def _get_settings(self):
        self._json(200, self._settings())

    def _settings(self):
        port = CFG["port"]
        urls = ["http://%s:%d" % (socket.gethostname(), port), "http://%s:%d" % (local_ip(), port)]
        return {"printer_ip": self.printer.ip, "max_name": CFG["max_name"], "overwrite": CFG["overwrite"],
                "background_poll": CFG.get("background_poll", True),
                "geom": {"bx": CFG["bed_x"], "by": CFG["bed_y"], "bz": CFG["bed_z"], "z_bed": CFG["z_bed"]}, "remote": CFG["remote"], "pin": CFG["pin"] if self._local() else "",
                "urls": urls, "autostart": autostart_get(), "version": VERSION, "limits": self.printer.limits or {},
                "can_autostart": sys.platform == "win32", "local": self._local()}

    def _log_download(self):
        """Журнал целиком (с предыдущей частью) — чтобы приложить к сообщению об ошибке."""
        parts = []
        for path in (LOG_PATH + ".1", LOG_PATH):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    parts.append(f.read())
            except OSError:
                pass
        self._send(200, "".join(parts) or "журнал пуст\n", "text/plain",
                   {"Content-Disposition": 'attachment; filename="mks_bridge_log.txt"'})

    def _open_logdir(self):
        if not self._local() or sys.platform != "win32":
            self._json(403, {"error": "только с этого компьютера"})
            return
        os.startfile(CFG_DIR)
        self._json(200, {"ok": True})

    def _list(self):
        if self._guard_printer():
            try:
                lst = self.printer.files()
                busy = is_printing(self.printer._cache[1] or {})
                if lst:
                    if lst != CFG.get("files_seen"):
                        CFG["files_seen"] = lst   # в настройках: перезапуск моста посреди печати не теряет список
                        cfg_save()
                elif busy:
                    self._json(200, {"files": CFG.get("files_seen") or [], "cached": True})
                    return
                self._json(200, {"files": lst})
            except Exception as e:
                self._json(502, {"error": str(e)})

    def _outline(self):
        q = parse_qs(urlparse(self.path).query)
        d = outline_load(q.get("file", [""])[0])
        if d is None:
            self._json(404, {"error": "контура нет"})
        elif "layer" in q:
            try:
                n = int(q["layer"][0])
            except ValueError:
                n = 0
            lay = d["layers"][n - 1] if 1 <= n <= len(d["layers"]) else []
            self._json(200, {"layer": n, "polys": lay})
        else:
            self._json(200, {"layers": len(d["layers"]), "zs": d["zs"], "ext": d["ext"], "ghost": d["ghost"]})

    def _limits(self):
        if self._guard_printer():
            try:
                refresh = parse_qs(urlparse(self.path).query).get("refresh", ["0"])[0] == "1"
                if self.printer.limits is None or refresh:
                    if self.printer.transferring or is_printing(self.printer.status_cached()):
                        self._json(200, {"limits": self.printer.limits or {}, "note": "печать идёт, настройки платы не запрашивались"})
                        return
                self._json(200, {"limits": self.printer.get_limits(refresh)})
            except Exception as e:
                self._json(502, {"error": str(e)})

    # --- файлы и печать ---
    def _panel_upload(self):
        q = parse_qs(urlparse(self.path).query)
        fname = q.get("name", ["model.gcode"])[0]
        self._start_job(fname, self._body(), q.get("print", ["0"])[0] == "1")
        self._json(200, {"ok": True})

    def _start_job(self, fname, data, start):
        tmp = os.path.join(tempfile.gettempdir(), "mks_bridge_%d_%s" % (int(time.time() * 1000), sanitize_name(fname, 60)))
        with open(tmp, "wb") as f:
            f.write(data)
        job = Job(sanitize_name(fname, self.printer.max_name, self.printer.ellipsis))

        def work():
            try:
                run_job(self.printer, tmp, fname, start, job)
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
            if is_printing(self.printer.status()):
                self._json(409, {"error": "Принтер печатает, удалять файлы нельзя."})
                return
            lst = self.printer.files()
        except Exception as e:
            self._json(502, {"error": str(e)})
            return
        res = []
        for n in names:
            try:
                self.printer.delete(n, _checked=True, listed=lst)
                res.append({"name": n, "ok": True})
            except Exception as e:
                res.append({"name": n, "ok": False, "error": str(e)})
        self._json(200, {"results": res})

    def _print(self):
        if not self._guard_printer():
            return
        name = self._jbody().get("name") or ""
        try:
            if is_printing(self.printer.status()):
                self._json(409, {"error": "Принтер уже печатает."})
                return
            if name.lower() not in [n.lower() for n in self.printer.files()]:
                self._json(404, {"error": "Файла нет на принтере."})
                return
            self.printer.start_print(name)
            remember_started(name)
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(502, {"error": str(e)})

    # --- команды ---
    def _console(self):
        b = self._jbody()
        cmd = (b.get("cmd") or "").strip()
        if not cmd or "\n" in cmd or "\r" in cmd or len(cmd) > 100:
            self._json(400, {"error": "Одна команда в одну строку."})
            return
        token = cmd.split()[0].upper()
        if token == "M112" and self.printer.homing:
            try:
                self.printer.emergency()
                log("консоль: M112 во время парковки")
                self._json(200, {"reply": "M112 отправлена"})
            except OSError as e:
                self._json(502, {"error": str(e)})
            return
        if not self._guard_printer():
            return
        try:
            if token not in SAFE_WHILE_PRINTING and not b.get("force") and is_printing(self.printer.status_cached()):
                self._json(409, {"error": "Идёт печать: %s заблокирована. Отметь «Разрешить опасные команды», "
                                          "если уверен." % token})
                return
            log("консоль: " + cmd)
            if token == "G28":
                self.printer.home(cmd)
                self._json(200, {"reply": "", "homing": True})
                return
            reply = self.printer.cmd([cmd], wait=1.5).strip()
            if token == "M107":
                self.printer.fan = 0
            elif token == "M106":
                m = re.search(r"S\s*(\d+)", cmd, re.I)
                self.printer.fan = min(100, round(int(m.group(1)) / 2.55)) if m else 100
            self._json(200, {"reply": reply})
        except Exception as e:
            self._json(502, {"error": str(e)})

    def _idle_guard(self, what):
        if not self._guard_printer():
            return False
        try:
            if is_printing(self.printer.status_cached()):
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
            reply = self.printer.cmd(cmds, wait=1.5)
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
            noz = self.printer.status_cached().get("nozzle")
            if not noz or noz[0] < 170:
                self._json(409, {"error": "Сопло холодное (%s): прошивка не даст двигать пруток. "
                                          "Нагрей сопло до 170°+ (например, M104 S200)."
                                          % ("%.0f°" % noz[0] if noz else "нет данных")})
                return
            log("подача E%+g" % dist)
            # M83/M82: относительная подача на время команды; слайсер выставит свой режим в начале печати
            self._json(200, {"reply": self.printer.cmd(["M83", "G1 E%g F300" % dist, "M82", "M114"], wait=2.0).strip()})
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
            reply = self.printer.cmd(["M107" if pct == 0 else "M106 S%d" % ((pct * 255 + 50) // 100)], wait=1.0).strip()
            self.printer.fan = pct
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
            if is_printing(self.printer.status_cached()):
                self._json(409, {"error": "Во время печати двигать оси нельзя."})
                return
            feed = 600 if axis == "Z" else 3000
            log("движение %s%+g" % (axis, dist))
            reply = self.printer.cmd(["G91", "G1 %s%g F%d" % (axis, dist, feed), "G90", "M114"], wait=1.5)
            if re.search(r"home\s+[xyz/ ]+first", reply, re.I):
                log("принтер отказался ехать: %s" % reply.strip().replace("\n", " | "))
                self._json(409, {"error": "Принтер отказался ехать: сначала нужно сделать Home (G28) по этим осям.", "reply": reply.strip()})
                return
            self._json(200, {"reply": reply.strip()})
        except Exception as e:
            self._json(502, {"error": str(e)})

    # --- настройки и связь ---
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
            self.printer.ip = CFG["printer_ip"] = ip
        if max_name is not None:
            self.printer.max_name = CFG["max_name"] = max_name
        if "overwrite" in b:
            self.printer.overwrite = CFG["overwrite"] = bool(b["overwrite"])
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

    def _reconnect(self):
        self.printer.reset_link()
        try:
            st = self.printer.status_cached(max_age=0)
        except Exception:
            self.printer.rediscover(force=True)
            self.printer.reset_link()
            try:
                st = self.printer.status_cached(max_age=0)
            except Exception as e:
                self._json(502, {"error": "Принтер не отвечает: %s. Если так и остаётся — выключи и включи принтер (питание), модуль Wi-Fi мог зависнуть." % e})
                return
        self._json(200, {"ok": True, "ip": self.printer.ip, "state": st.get("state")})

    def _discover(self):
        if not self._guard_printer():   # сканирование стучится в TCP принтера, а во время заливки его трогать нельзя
            return
        found, scanned = discover(self.printer.tcp_port, self.printer.ip)
        self._json(200, {"found": found, "scanned": scanned})

    def _quit(self):
        if self._local():
            self._json(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), APP["stop"]()), daemon=True).start()
        else:
            self._json(403, {"error": "только с этого компьютера"})
