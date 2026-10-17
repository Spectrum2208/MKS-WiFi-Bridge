"""Поиск принтера в локальной сети: кто отвечает на M105 по TCP-порту модуля."""
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from .config import log

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


def lan_ips(hint_ip=""):
    """Адреса компьютера для телефона в той же сети: сначала из подсети принтера, потом обычные домашние,
    в конце остальные (туннели VPN). Основной маршрут в интернет часто идёт через VPN — его адрес телефону не подходит."""
    near = hint_ip.rsplit(".", 1)[0] + "." if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", hint_ip or "") else None

    def rank(ip):
        if near and ip.startswith(near):
            return 0
        if ip.startswith("192.168."):
            return 1
        if ip.startswith("10.") or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
            return 2
        return 3
    return sorted(local_ips(), key=rank)


def local_ip(hint_ip=""):
    ips = lan_ips(hint_ip)
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
