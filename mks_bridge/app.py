"""Запуск моста: аргументы, сервер, фоновый опрос принтера, значок в трее."""
import argparse
import http.client
import os
import platform
import sys
import threading
import time
import traceback
import webbrowser

from . import VERSION
from .api import ACTIVITY, APP
from .config import CFG, NOTIFY, cfg_load, cfg_save, log
from .discovery import discover
from .printer import Printer, is_printing
from .server import WEB_DIR, make_server
from .store import STATE, layers_load

def sampler(printer):
    """Фоновый опрос: график непрерывен, даже когда панель закрыта. Быстро при открытой панели, редко без неё."""
    fails, last_scan = 0, 0.0
    while True:
        delay = 3
        try:
            if CFG.get("background_poll", True) and printer.ip and not printer.transferring:
                active = time.time() - ACTIVITY["t"] < 45
                try:
                    st = printer.status_cached(max_age=0, quiet=True, allow_stale=True)
                    fails, delay = 0, (4 if active else 10)
                    if not printer.described:
                        printer.describe()
                    if is_printing(st) and not printer._autoreport and not printer.homing:
                        printer.enable_autoreport()   # печать идёт (мост перезапущен посреди неё или её запустили не мы)
                    elif not is_printing(st):
                        printer._autoreport = False
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
        from PIL import Image
        img = Image.open(os.path.join(WEB_DIR, "icon.png"))
    except Exception:
        return False
    icon = pystray.Icon("mks_bridge", img, "MKS WiFi Bridge", pystray.Menu(
        pystray.MenuItem("Открыть панель", lambda icon, item: webbrowser.open(url), default=True),
        pystray.MenuItem("Выход", lambda icon, item: icon.stop())))
    NOTIFY[0] = lambda title, text: icon.notify(text, title)
    APP["stop"] = icon.stop
    icon.run()
    return True


def bridge_running(port):
    """На порту отвечает именно наш мост (а не чужая программа)?"""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/version", headers={"Host": "127.0.0.1"})
        return b"MKS bridge" in conn.getresponse().read()
    except (OSError, http.client.HTTPException):
        return False


def show_error(msg):
    """Окно с ошибкой: у exe без консоли print никто не увидит."""
    print(msg)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "MKS WiFi Bridge", 0x10)
        except Exception:
            pass


def log_crashes():
    """Без консоли (ярлык, exe) неожиданная ошибка пропала бы бесследно — пишем её в журнал."""
    def on_error(kind, tb):
        log("НЕОЖИДАННАЯ ОШИБКА (%s):\n%s" % (kind, "".join(tb)))
    sys.excepthook = lambda t, v, b: on_error("основной поток", traceback.format_exception(t, v, b))
    threading.excepthook = lambda a: on_error("поток %s" % (a.thread.name if a.thread else "?"),
                                              traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback))


def main():
    log_crashes()
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
    STATE["last_name"] = CFG.get("last_print")
    if a.ip:
        CFG["printer_ip"] = a.ip
    if a.port:
        CFG["port"] = a.port
    if a.tcp_port:
        CFG["tcp_port"] = a.tcp_port
    if a.http_port:
        CFG["http_port"] = a.http_port
    first_run = not CFG["printer_ip"]
    log("запуск MKS WiFi Bridge %s | %s | Python %s | %s | панель на порту %d | принтер %s (TCP %d, HTTP %d)"
        % (VERSION, platform.platform(), platform.python_version(),
           "exe" if getattr(sys, "frozen", False) else "из исходников", CFG["port"],
           CFG["printer_ip"] or "не задан", CFG["tcp_port"], CFG["http_port"]))
    cfg_save()

    printer = Printer(CFG["printer_ip"], CFG["http_port"], CFG["tcp_port"], CFG["max_name"])
    printer.overwrite, printer.ellipsis = CFG["overwrite"], CFG["ellipsis"]

    def ip_changed(ip):
        CFG["printer_ip"] = ip
        cfg_save()
    printer.on_ip_change = ip_changed

    url = "http://127.0.0.1:%d" % CFG["port"]
    try:
        srv = make_server(printer, CFG["port"])
    except OSError:
        if bridge_running(CFG["port"]):
            print("Мост уже запущен. Открываю панель.")
            webbrowser.open(url)
        else:
            msg = ("Порт %d занят другой программой, мост не может запуститься.\n\n"
                   "Запустите мост с другим портом, например: MKSBridge.exe --port 5001,\n"
                   "и укажите этот порт в настройках принтера в Orca (http://127.0.0.1:5001)." % CFG["port"])
            log(msg.replace("\n", " "))
            show_error(msg)
        return
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
    if not a.no_browser:   # запустили руками (двойной клик по exe) — сразу показать панель; автозапуск идёт с --no-browser
        webbrowser.open(url)

    if a.no_tray or not run_tray(url):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
