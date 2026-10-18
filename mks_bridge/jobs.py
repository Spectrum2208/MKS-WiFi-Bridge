"""Задания заливки: файл из Orca или панели -> имя для экрана -> принтер (с повторами) -> запуск печати."""
import itertools
import os
import threading
import time
import traceback
from collections import deque

from .config import CFG, log, notify
from .gcode import index_gcode, outline_gcode
from .printer import is_printing, sanitize_name
from .store import LAYERS, layers_save, outline_save, remember_started
from .thumbs import add_preview

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
            warn = ""
            try:  # до разбора слоёв: картинка в начале файла сдвигает позиции байтов, по которым считается прогресс
                note = add_preview(path, CFG.get("preview"))
                if note:
                    log(note)
                    if note.startswith(("нет ", "эскиз ")):
                        warn = " (без картинки: %s)" % note
            except Exception as ex:
                log("картинка MKS не добавлена: %r" % (ex,))
            idx = index_gcode(path)
            try:
                t0 = time.time()
                outl, outl_zs, outl_full = outline_gcode(path)
                log("контур: %d слоёв за %.1f с" % (len(outl), time.time() - t0))
            except Exception as ex:
                outl = None
                log("контур не построен: %r" % (ex,))

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
            if outl is not None:
                outline_save(name, outl, idx["zs"] if len(idx["zs"]) == len(outl) else outl_zs, outl_full)
            remember_started(name)

            tail = (" (исходное имя: %s)" % orig_base if renamed else "") + warn
            if start:
                ui.post(("note", "Запускаю печать…"))
                printer.start_print(name)
                ui.post(("done", "Загружено, печать запущена: " + name + tail))
            else:
                ui.post(("done", "Загружено: " + name + tail))
        except Exception as e:
            log(traceback.format_exc())
            ui.post(("error", str(e)))
