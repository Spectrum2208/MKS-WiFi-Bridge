"""Картинка модели для стоковых экранов MKS: PNG-эскиз из G-code -> ;simage:/;;gimage: в начале файла.

Формат — как у плагина MKS WiFi для Cura: пиксели RGB565, по 4 hex-символа (младший байт первым),
каждая строка картинки заканчивается на «\\rM10086 ;», после картинки ещё «\\r». Экран ищет её в самом начале файла.
"""
import base64
import os
import re
import shutil
import struct
import zlib

# размеры (маленькая — в списке файлов, большая — перед печатью); Ghost 4S/5 — по пресету плагина для Cura
PRESETS = {"ghost": (50, 200), "mks": (100, 200)}
_HEAD = 4 * 1024 * 1024   # эскизы слайсер пишет в шапку файла
_THUMB = re.compile(rb"; thumbnail begin (\d+)x(\d+) \d+\r?\n(.*?); thumbnail end", re.S)


def _png_rgb(data):
    """PNG -> (ширина, высота, список строк [(r,g,b), ...]); прозрачное — на чёрном фоне. None, если формат не тот."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, idat, plte, trns = 8, [], b"", b""
    w = h = depth = ctype = inter = None
    while pos + 8 <= len(data):
        ln, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, inter = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if not w or depth != 8 or inter != 0 or chans is None or not (0 < w <= 2048 and 0 < h <= 2048):
        return None
    raw = zlib.decompress(b"".join(idat))
    stride = w * chans
    rows, prev = [], bytearray(stride)
    for y in range(h):
        i = y * (stride + 1)
        f, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        for x in range(stride):   # фильтры PNG: 0 нет, 1 Sub, 2 Up, 3 Average, 4 Paeth
            a = line[x - chans] if x >= chans else 0
            b = prev[x]
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                c = prev[x - chans] if x >= chans else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        px = []
        for x in range(w):
            v = line[x * chans:(x + 1) * chans]
            if ctype == 3:
                k = v[0]
                r, g, bl = plte[3 * k:3 * k + 3] or b"\0\0\0"
                al = trns[k] if k < len(trns) else 255
            elif ctype in (0, 4):
                r = g = bl = v[0]
                al = v[1] if ctype == 4 else 255
            else:
                r, g, bl = v[0], v[1], v[2]
                al = v[3] if ctype == 6 else 255
            px.append((r * al // 255, g * al // 255, bl * al // 255))
        rows.append(px)
    return w, h, rows


def _fit(w, h, rows, n):
    """Вписать в квадрат n×n с сохранением пропорций (усреднение по площади), поля — чёрные."""
    k = min(n / w, n / h)
    tw, th = max(1, round(w * k)), max(1, round(h * k))
    ox, oy = (n - tw) // 2, (n - th) // 2
    out = [[(0, 0, 0)] * n for _ in range(n)]
    for ty in range(th):
        y0 = ty * h // th
        y1 = max(y0 + 1, (ty + 1) * h // th)
        for tx in range(tw):
            x0 = tx * w // tw
            x1 = max(x0 + 1, (tx + 1) * w // tw)
            r = g = b = 0
            for y in range(y0, y1):
                for p in rows[y][x0:x1]:
                    r, g, b = r + p[0], g + p[1], b + p[2]
            cnt = (y1 - y0) * (x1 - x0)
            out[oy + ty][ox + tx] = (r // cnt, g // cnt, b // cnt)
    return out


def _encode(tag, img):
    parts = [tag]
    for row in img:
        for r, g, b in row:
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            parts.append("%02x%02x" % (v & 255, v >> 8))
        parts.append("\rM10086 ;")
    parts.append("\r")
    return "".join(parts)


def find_png(head):
    """Самый крупный PNG-эскиз из шапки G-code (Orca: «; thumbnail begin 200x200 ...»)."""
    best = None
    for m in _THUMB.finditer(head):
        w, h = int(m.group(1)), int(m.group(2))
        if best is None or w * h > best[0]:
            best = (w * h, m.group(3))
    if best is None:
        return None
    b64 = re.sub(rb"[;\s]", b"", best[1])
    try:
        return base64.b64decode(b64)
    except ValueError:
        return None


def add_preview(path, preset):
    """Ставит картинку MKS в начало файла (на месте). Возвращает строку для журнала; ошибки печати не мешают."""
    sizes = PRESETS.get(preset)
    if not sizes:
        return None
    with open(path, "rb") as f:
        head = f.read(_HEAD)
    if head.startswith((b";simage:", b";;gimage:")):
        return "картинка MKS уже есть в файле"
    png = find_png(head)
    if png is None:
        return "нет PNG-эскиза в G-code: в Orca включи эскиз, например 200x200/PNG"
    img = _png_rgb(png)
    if img is None:
        return "эскиз в G-code не удалось прочитать (нужен PNG 8 бит)"
    w, h, rows = img
    tags = (";simage:", ";;gimage:")
    data = "".join(_encode(t, _fit(w, h, rows, n)) for t, n in zip(tags, sizes)) + "\r"
    tmp = path + ".mks"
    with open(tmp, "wb") as out, open(path, "rb") as src:
        out.write(data.encode("ascii"))
        shutil.copyfileobj(src, out, 1 << 20)
    os.replace(tmp, path)
    return "картинка MKS %d и %d px из эскиза %dx%d (+%d КБ)" % (sizes[0], sizes[1], w, h, len(data) // 1024)
