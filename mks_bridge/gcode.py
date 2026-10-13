"""Разбор G-code: начала слоёв, оценка времени, команды обдува и контуры внешних стенок."""
import math
import re

def parse_duration(txt):
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)\s*([dhms])", txt)) or None


def index_gcode(path):
    """Начало каждого слоя (смещение в файле и высота Z) + оценка времени печати."""
    with open(path, "rb") as f:
        data = f.read()
    offs, zs, last = [], [], 0.0
    for m in re.finditer(rb";LAYER_CHANGE|;LAYER:\d+", data):
        chunk = data[m.end(): m.end() + 800]
        zm = re.search(rb";Z:([\d.]+)", chunk) or re.search(rb"[\r\n]G[01]\s[^\r\n;]*Z([\d.]+)", chunk)
        if zm:
            last = float(zm.group(1))
        offs.append(m.start())
        zs.append(round(last, 3))
    est = None
    m = re.search(rb"estimated printing time[^=\r\n]*=\s*([^\r\n]+)", data)
    if m:
        est = parse_duration(m.group(1).decode(errors="ignore"))
    else:
        m = re.search(rb";TIME:(\d+)", data)
        if m:
            est = int(m.group(1))
    # обдув: где в файле какая скорость задаётся. Прошивка её не сообщает, а позицию печати в файле мы знаем
    fan = []
    for m in re.finditer(rb"(?m)^(M106|M107)\b([^;\r\n]*)", data):
        args = m.group(2).upper()
        pi = re.search(rb"P\s*(\d+)", args)
        if pi and int(pi.group(1)) != 0:   # другие вентиляторы (камеры, вспомогательный) — не обдув модели
            continue
        sv = re.search(rb"S\s*([\d.]+)", args)
        pct = 0 if m.group(1) == b"M107" else min(100, round(float(sv.group(1)) / 2.55) if sv else 100)
        fan.append([m.start(), pct])
    return {"size": len(data), "offsets": offs, "zs": zs, "est": est, "fan": fan}


# --- контуры модели ---
# По каждому слою — линии внешних стенок в десятых долях мм. Панель рисует по ним текущий слой и силуэт модели.

OUTER_TYPES = (b"outer wall", b"overhang wall", b"external perimeter", b"overhang perimeter", b"wall-outer")
_GWORD = re.compile(rb"([A-Z])\s*(-?\d*\.?\d+)")
_LAYER_MARK = re.compile(rb";LAYER_CHANGE|;LAYER:\d+")   # те же метки, что в index_gcode: номера слоёв должны совпадать


def outline_gcode(path):
    """-> (слои, высоты слоёв). В слое — линии внешних стенок. Если стенок в слое нет (калибровки Orca рисуют
    линии как ;TYPE:Custom), берём все линии слоя. Нет меток слоёв вовсе — весь файл считается одним слоем."""
    walls, other, zs = [], [], []
    cw = co = poly = target = None
    x = y = z = e = 0.0
    abs_xyz = abs_e = True
    typed = outer = marked = False

    def close():
        nonlocal poly
        if poly and len(poly) >= 4:
            target.append(poly)
        poly = None

    def new_layer():
        nonlocal cw, co
        close()
        cw, co = [], []
        walls.append(cw)
        other.append(co)
        zs.append(None)

    def add(px, py, step):
        qx, qy = round(px * 10), round(py * 10)
        if abs(qx - poly[-2]) + abs(qy - poly[-1]) >= step:
            poly.extend((qx, qy))

    with open(path, "rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line[:1] == b";":
                if _LAYER_MARK.match(line):
                    if not marked:   # всё, что набралось до первой метки (стартовый код), к слоям не относится
                        walls.clear(), other.clear(), zs.clear()
                        poly, marked = None, True
                    new_layer()
                elif line.startswith(b";TYPE:"):
                    close()
                    typed = True
                    outer = line[6:].strip().lower().startswith(OUTER_TYPES)
                continue
            code = line.split(b";", 1)[0].upper()
            cmd = code.split(None, 1)[0] if code else b""
            if cmd in (b"G0", b"G1", b"G2", b"G3"):
                w = {k.decode(): float(v) for k, v in _GWORD.findall(code[len(cmd):])}
                nx = (w["X"] if abs_xyz else x + w["X"]) if "X" in w else x
                ny = (w["Y"] if abs_xyz else y + w["Y"]) if "Y" in w else y
                nz = (w["Z"] if abs_xyz else z + w["Z"]) if "Z" in w else z
                ne = (w["E"] if abs_e else e + w["E"]) if "E" in w else e
                if ne > e + 1e-5 and (nx != x or ny != y):
                    if cw is None:
                        new_layer()   # меток слоёв (пока) нет: копим в неявный слой
                    dst = cw if outer or not typed else co
                    if poly is not None and target is not dst:
                        close()
                    if poly is None:
                        poly, target = [round(x * 10), round(y * 10)], dst
                    if zs[-1] is None:
                        zs[-1] = round(nz, 3)
                    if cmd in (b"G2", b"G3") and ("I" in w or "J" in w):   # дуга: режем на хорды
                        cx, cy = x + w.get("I", 0.0), y + w.get("J", 0.0)
                        a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
                        sweep = a1 - a0
                        if cmd == b"G2" and sweep >= 0:
                            sweep -= 2 * math.pi
                        elif cmd == b"G3" and sweep <= 0:
                            sweep += 2 * math.pi
                        r = math.hypot(x - cx, y - cy)
                        n = max(2, min(72, int(abs(sweep) * r / 1.0)))
                        for i in range(1, n):
                            a = a0 + sweep * i / n
                            add(cx + r * math.cos(a), cy + r * math.sin(a), 2)
                    add(nx, ny, 2)
                elif "E" in w or "X" in w or "Y" in w:
                    close()   # перемещение без подачи разрывает линию
                x, y, z, e = nx, ny, nz, ne
            elif cmd == b"G90":
                abs_xyz = abs_e = True   # как в Marlin: G90/G91 переключают и экструдер
            elif cmd == b"G91":
                abs_xyz = abs_e = False
            elif cmd == b"M82":
                abs_e = True
            elif cmd == b"M83":
                abs_e = False
            elif cmd == b"G92":
                w = {k.decode(): float(v) for k, v in _GWORD.findall(code[3:])}
                x, y, z, e = w.get("X", x), w.get("Y", y), w.get("Z", z), w.get("E", e)
    close()
    layers = [cw if cw else [_thin(p, 4) for p in co] for cw, co in zip(walls, other)]
    if not typed:   # слайсер не размечает типы линий: взяли всё подряд, проредим погрубее
        layers = [[_thin(p, 10) for p in lay] for lay in layers]
    return layers, zs


def _thin(p, step):
    out = p[:2]
    for i in range(2, len(p), 2):
        if abs(p[i] - out[-2]) + abs(p[i + 1] - out[-1]) >= step or i == len(p) - 2:
            out.extend(p[i:i + 2])
    return out


def outline_spans(layers):
    """На каждом слое — отрезки по X, занятые моделью (для силуэта спереди): у каждой детали свой,
    иначе детали, стоящие рядом, сливаются в одну полосу."""
    ext = []
    for lay in layers:
        merged = []
        for a0, a1 in sorted((min(p[0::2]), max(p[0::2])) for p in lay):
            if merged and a0 <= merged[-1][1] + 10:   # зазор меньше 1 мм — одна деталь
                merged[-1][1] = max(merged[-1][1], a1)
            else:
                merged.append([a0, a1])
        ext.append(merged)
    return ext
