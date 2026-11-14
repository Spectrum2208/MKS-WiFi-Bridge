"""Настройки (config.json), журнал и уведомления. Всё лежит в %APPDATA%\\MKSBridge\\."""
import json
import os
import threading
import time

CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "MKSBridge")
os.makedirs(CFG_DIR, exist_ok=True)
CFG_PATH = os.path.join(CFG_DIR, "config.json")
LAYERS_PATH = os.path.join(CFG_DIR, "layers.json")
LOG_PATH = os.path.join(CFG_DIR, "mks_bridge.log")
OUTL_DIR = os.path.join(CFG_DIR, "outlines")   # контуры моделей залитых файлов (для отрисовки печати в панели)

DEFAULTS = {"printer_ip": "", "port": 5000, "max_name": 30, "ellipsis": "~", "overwrite": True,
            "remote": False, "pin": "", "tcp_port": 8080, "http_port": 80, "background_poll": True,
            "bed_x": 255, "bed_y": 210, "bed_z": 210, "z_bed": True, "preview": "off", "token": "", "lang": "",
            "update_check": True}
CFG = dict(DEFAULTS)
_cfg_lock = threading.Lock()
_log_lock = threading.Lock()


def write_json(path, obj, **kw):
    """Запись целиком или никак: сначала во временный файл, потом подмена. Сбой посреди записи не оставит
    обрезанный файл, из-за которого при следующем запуске пропали бы все настройки."""
    data = json.dumps(obj, ensure_ascii=False, **kw)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def cfg_load():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        CFG.update(saved)
        if "lang" not in saved:   # мост уже стоял до появления языков: он был русским, таким и остаётся
            CFG["lang"] = "ru"
    except OSError:
        pass
    except ValueError as e:
        log("config.json не читается (%s): взяты настройки по умолчанию" % e)


def cfg_save():
    with _cfg_lock:
        try:
            write_json(CFG_PATH, dict(CFG), indent=1)   # копия: другой поток может менять настройки прямо сейчас
        except OSError:
            pass


def log(msg):
    with _log_lock:   # пишут из многих потоков: без замка смена файла журнала на Windows теряла строки
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
                os.replace(LOG_PATH, LOG_PATH + ".1")   # предыдущая часть журнала остаётся рядом
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
