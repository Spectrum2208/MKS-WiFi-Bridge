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
