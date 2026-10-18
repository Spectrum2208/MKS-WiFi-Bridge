"""Что мост помнит о залитых файлах: слои, контуры моделей, последний запущенный файл."""
import bisect
import json
import os
import re

from .config import CFG, LAYERS_PATH, OUTL_DIR, cfg_save, log
from .gcode import _thin, outline_spans

LAYERS = {}   # имя файла на принтере (lower) -> {"size", "offsets", "est"}
STATE = {"last_name": None}


def layers_load():
    try:
        with open(LAYERS_PATH, encoding="utf-8") as f:
            LAYERS.update(json.load(f))
    except (OSError, ValueError):
        pass


def layers_save():
    data = {k: LAYERS[k] for k in list(LAYERS)[-30:]}
    try:
        with open(LAYERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def layer_info(file, progress, z, sd=None):
    """Слой точно по высоте Z (из g-code); процент выполнения страхует от подъёмов Z при перемещениях."""
    idx = LAYERS.get(job_key(file))
    if not idx or not idx["offsets"]:
        return None
    if sd and abs(sd[1] - idx["size"]) <= 64:   # Marlin сообщил позицию в файле: слой известен точно
        frac = sd[0] / float(sd[1])
        eta = int(idx["est"] * (1 - frac)) if idx["est"] else None
        return {"layer": max(1, bisect.bisect_right(idx["offsets"], sd[0])), "layers": len(idx["offsets"]),
                "eta": eta, "layer_exact": True}
    if progress is None:   # ни позиции в файле, ни процента (нагрев перед печатью): Z остался от прошлого, не гадаем
        return None
    n, zs = len(idx["offsets"]), idx.get("zs") or []
    lp = None if progress is None else bisect.bisect_right(idx["offsets"], idx["size"] * progress / 100.0)
    layer, exact = lp, False
    if z is not None and len(zs) == n:
        win = None if lp is None else 1 + -(-n // 100)  # допуск: точность процента + опережение буфера
        near = lambda c: win is None or abs(c - lp) <= win
        hit = [i + 1 for i, v in enumerate(zs) if abs(v - z) < 0.011 and near(i + 1)]
        cand = hit or [i + 1 for i, v in enumerate(zs) if z - 2.0 < v <= z + 0.011 and near(i + 1)]
        if cand:
            ref = lp if lp is not None else max(cand)
            layer, exact = min(cand, key=lambda c: abs(c - ref)), len(hit) == 1
    if layer is None:
        return None
    eta = None if not idx["est"] or progress is None else int(idx["est"] * (100 - progress) / 100.0)
    return {"layer": layer, "layers": n, "eta": eta, "layer_exact": exact}


def outline_save(name, layers, zs, detail=None):
    """Контур + сводка для панели: ширина модели по X на каждом слое (силуэт спереди) и «призрак» модели сверху."""
    ext = outline_spans(layers)
    pick = sorted({round(i * (len(layers) - 1) / 15) for i in range(16)}) if layers else []
    ghost = [_thin(p, 8) for i in pick for p in layers[i]]
    data = {"v": 1, "layers": layers, "zs": zs, "ext": ext, "ghost": ghost}
    if detail and len(detail) == len(layers):
        data["detail"] = detail
    try:
        os.makedirs(OUTL_DIR, exist_ok=True)
        with open(os.path.join(OUTL_DIR, name.lower() + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        files = sorted((os.path.join(OUTL_DIR, n) for n in os.listdir(OUTL_DIR)), key=os.path.getmtime)
        for old in files[:-12]:   # храним последние 12 файлов
            os.remove(old)
    except OSError as ex:
        log("контур %s не сохранён: %s" % (name, ex))
    OUTL_CACHE.clear()


OUTL_CACHE = {}


def outline_load(key):
    if not re.fullmatch(r"[\w.\-~]+", key or ""):
        return None
    if key not in OUTL_CACHE:
        try:
            with open(os.path.join(OUTL_DIR, key + ".json"), encoding="utf-8") as f:
                d = json.load(f)
            if any(e and not isinstance(e[0], list) for e in d["ext"]):   # старый формат: одна полоса на слой
                d["ext"] = outline_spans(d["layers"])
            OUTL_CACHE.clear()
            OUTL_CACHE[key] = d
        except (OSError, ValueError):
            return None
    return OUTL_CACHE[key]


def job_key(file):
    """Ключ залитого через мост файла, который сейчас печатается (имя на принтере в нижнем регистре).
    Имя от принтера (M994) главнее: экран мог запустить другой файл. Пока принтер имя не сообщает
    (модуль MKS-WIFI, например, во время нагрева отвечает «M994 ;0»), берём последний файл, запущенный самим мостом."""
    k = (file or STATE["last_name"] or "").lower()
    return k if k in LAYERS else None


def remember_started(name):
    STATE["last_name"] = CFG["last_print"] = name   # в настройках, чтобы перезапуск моста не стирал
    cfg_save()
