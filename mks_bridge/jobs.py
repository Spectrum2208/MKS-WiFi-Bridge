"""Задания заливки: файл из Orca или панели -> имя для экрана -> принтер (с повторами) -> запуск печати."""
import itertools
import os
import threading
import time
import traceback
from collections import deque

from .config import CFG, log, notify
from .i18n import tr
from .gcode import ascii_gcode, index_gcode, outline_gcode, pad_for_mks
from .printer import sanitize_name
from .store import layers_put, outline_save, remember_started
from .thumbs import add_preview

JOBS = deque(maxlen=12)
_job_lock = threading.Lock()
_job_ids = itertools.count(int(time.time() * 1000))   # два файла, брошенные разом, не должны получить один id


class Job:
    def __init__(self, name):
        self.id = next(_job_ids)
        self.name, self.state, self.sent, self.total = name, "working", 0, 0
        self.note = tr("job.queued")
        self.end_t = None   # когда закончилась: старые карточки панель прячет
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
            self.state, self.note, self.end_t = "done", m[1], time.time()
            notify(tr("notify.done"), m[1])
        elif k == "error":
            self.state, self.note, self.end_t = "error", m[1], time.time()
            notify(tr("notify.error"), m[1])

    def as_dict(self):
        return {"id": self.id, "name": self.name, "state": self.state,
                "sent": self.sent, "total": self.total, "note": self.note,
                "age": int(time.time() - self.end_t) if self.end_t else None}


def run_job(printer, path, orig_name, start, ui):
    with _job_lock:
        name = sanitize_name(orig_name, printer.max_name, printer.ellipsis)
        orig_base = os.path.basename(orig_name)
        renamed = name.lower() != orig_base.lower()
        ui.post(("start", name))
        try:
            try:
                if printer.printing_now():
                    raise RuntimeError(tr("job.printing"))
            except OSError as e:
                raise RuntimeError(tr("job.no_link", ip=printer.ip, e=e))
            warn = ""
            try:  # до картинки и разбора слоёв: меняет длину строк, а значит и позиции байтов
                n = ascii_gcode(path)
                if n:
                    log("русские буквы в %d строках G-code переведены в латиницу" % n)
            except Exception as ex:
                log("перевод G-code в латиницу не удался: %r" % (ex,))
            try:  # до разбора слоёв: картинка в начале файла сдвигает позиции байтов, по которым считается прогресс
                note = add_preview(path, CFG.get("preview"))
                if note:
                    log(note)
                    if note.startswith(("нет ", "эскиз ")):
                        warn = tr("job.no_preview", why=tr("thumb.none" if note.startswith("нет ") else "thumb.bad"))
            except Exception as ex:
                log("картинка MKS не добавлена: %r" % (ex,))
            try:  # в конец файла: позиции байтов печати не сдвигает
                log("размер подогнан под передачу MKS-WIFI: +%d байт комментария" % pad_for_mks(path))
            except Exception as ex:
                log("подгонка размера не удалась: %r" % (ex,))
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
                ui.post(("note", tr("job.replacing")))
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
            ui.post(("note", tr("job.sending")))
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
                        raise RuntimeError(tr("job.failed3", e=e))
                    ui.post(("note", tr("job.retry", e=e, n=attempt)))
                    time.sleep(2)

            confirmed, last, t_sent = False, [], time.time()
            for _ in range(15):  # принтер дописывает файл на карту уже после передачи
                ui.post(("note", tr("job.saving")))
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
                raise RuntimeError(tr("job.not_listed", sec=int(time.time() - t_sent)))
            log("OK: %s залит и подтверждён" % name)
            layers_put(name, idx)
            if outl is not None:
                outline_save(name, outl, idx["zs"] if len(idx["zs"]) == len(outl) else outl_zs, outl_full)
            remember_started(name)

            tail = (tr("job.orig_name", name=orig_base) if renamed else "") + warn
            if start:
                ui.post(("note", tr("job.starting")))
                printer.start_print(name)
                ui.post(("done", tr("job.done_print", name=name) + tail))
            else:
                ui.post(("done", tr("job.done", name=name) + tail))
        except Exception as e:
            log(traceback.format_exc())
            ui.post(("error", str(e)))
