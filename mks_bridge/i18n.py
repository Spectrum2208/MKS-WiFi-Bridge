"""Языки интерфейса. Тексты лежат в mks_bridge/lang/<код>.json в виде «ключ -> текст» (как языковые файлы Minecraft):
новый язык — это новый файл в той папке, код менять не нужно. Название языка для настроек — ключ lang.name.
Журнал моста остаётся на русском: он для разбора ошибок, а не для пользователя."""
import json
import os
import re
import sys

from .config import CFG, log

LANG_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "mks_bridge", "lang")
FALLBACK = "en"   # чего нет в выбранном языке, берётся отсюда (перевод может быть неполным)
_files = {}       # код -> (время изменения файла, словарь): файлы перечитываются после правки, без перезапуска моста
_system = []


def _load(code):
    path = os.path.join(LANG_DIR, code + ".json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _files.get(code, (None,))[0] != mtime:
        try:
            with open(path, encoding="utf-8") as f:
                _files[code] = (mtime, json.load(f))
        except (OSError, ValueError) as e:
            log("языковой файл %s не читается: %s" % (path, e))
            _files[code] = (mtime, {})
    return _files[code][1]


def languages():
    """-> {код: название}: все языковые файлы из папки lang."""
    try:
        codes = sorted(n[:-5] for n in os.listdir(LANG_DIR) if n.endswith(".json"))
    except OSError:
        codes = []
    return {c: _load(c).get("lang.name", c) for c in codes if re.fullmatch(r"[A-Za-z_\-]+", c)}


def system_lang():
    """Язык Windows (или LANG в других системах), если для него есть файл; иначе FALLBACK."""
    if not _system:
        code = ""
        try:
            if sys.platform == "win32":
                import ctypes
                import locale
                code = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
            else:
                code = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
        except Exception:
            pass
        _system.append(code[:2].lower())
    return _system[0] if os.path.exists(os.path.join(LANG_DIR, _system[0] + ".json")) else FALLBACK


def current():
    """Код языка интерфейса: выбранный в настройках, а пока не выбран — язык системы."""
    code = CFG.get("lang") or ""
    if code and re.fullmatch(r"[A-Za-z_\-]+", code) and os.path.exists(os.path.join(LANG_DIR, code + ".json")):
        return code
    return system_lang()


def strings():
    """Все тексты текущего языка (с подстановкой недостающих из FALLBACK) — их же получает панель."""
    code = current()
    out = dict(_load(FALLBACK))
    out.update(_load(code))
    out["lang.code"] = code
    return out


def tr(key, **kw):
    """Текст по ключу на текущем языке; {имя} в тексте заменяется значением аргумента."""
    s = _load(current()).get(key)
    if s is None:
        s = _load(FALLBACK).get(key, key)
    if kw:
        s = re.sub(r"\{(\w+)\}", lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0), s)
    return s
