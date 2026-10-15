"""Запуск моста: аргументы, сервер, фоновый опрос принтера, значок в трее."""
import argparse
import os
import threading
import time
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
        print("Порт %d занят: возможно, мост уже запущен. Открываю панель." % CFG["port"])
        webbrowser.open(url)
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
    if first_run and not a.no_browser:
        webbrowser.open(url)

    if a.no_tray or not run_tray(url):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
