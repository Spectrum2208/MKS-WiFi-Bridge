"""Обновление моста: проверка нового релиза на GitHub и установка по кнопке из панели.

Сам мост ничего не ставит без спроса: раз в сутки узнаёт номер последнего релиза и показывает его в панели.
Установка — только по кнопке и только с этого компьютера. Способ зависит от того, как мост поставлен:
установщик (рядом лежит unins000.exe) — скачиваем новый установщик и запускаем его в тихом режиме;
отдельный exe — подменяем файл; из исходников — только ссылка (обновляться через git)."""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import unquote

from . import VERSION
from .config import CFG, CFG_DIR, log, notify, write_json
from .i18n import tr

# Адреса зашиты в код: мост скачивает и запускает программу, брать её откуда-то ещё нельзя
REPO = "Spectrum2208/MKS-WiFi-Bridge"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
PAGE = "https://github.com/%s/releases/latest" % REPO
ASSET_PREFIX = "https://github.com/%s/releases/download/" % REPO
ASSETS = {"setup": "MKSBridge-Setup.exe", "exe": "MKSBridge.exe"}
SUMS = "SHA256SUMS.txt"   # файл в релизе с контрольными суммами (его пишет packaging/build_exe.py)
CACHE_PATH = os.path.join(CFG_DIR, "update.json")
TMP_DIR = os.path.join(CFG_DIR, "update")
CHECK_EVERY = 24 * 3600
MAX_SIZE = 200 * 1024 * 1024

INFO = {}   # что известно о последнем релизе: t, latest, notes, url, assets, error, notified
JOB = {"state": "idle", "done": 0, "total": 0, "error": ""}   # idle / downloading / installing / error
_lock = threading.Lock()


def vkey(v):
    """Номер версии для сравнения: 2.1.0 > 2.1.0-beta.2 > 2.1.0-beta.1 > 2.0.9. Непонятная строка — None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.\-]+))?", (v or "").strip())
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums += (0,) * (3 - len(nums))
    if m.group(2) is None:
        return nums, (1,)   # готовая версия старше любой своей «беты»
    return nums, (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in m.group(2).split("."))


def newer(latest, current=VERSION):
    a, b = vkey(latest), vkey(current)
    return bool(a and b and a > b)


def mode():
    """Как поставлен мост: 'setup' (установщиком), 'exe' (отдельный файл), 'source' (из исходников)."""
    if not getattr(sys, "frozen", False):
        return "source"
    return "setup" if os.path.exists(os.path.join(os.path.dirname(sys.executable), "unins000.exe")) else "exe"


def cache_load():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            INFO.update(json.load(f))
    except (OSError, ValueError):
        pass


def _open(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "MKS-WiFi-Bridge/" + VERSION,
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _from_api():
    """Основной способ: GitHub API — номер версии, описание и контрольные суммы файлов одним ответом."""
    with _open(API, 15) as r:
        rel = json.loads(r.read(2_000_000))
    assets = {a["name"]: {"url": a.get("browser_download_url") or "", "size": int(a.get("size") or 0),
                          "sha256": (a.get("digest") or "").partition("sha256:")[2].lower()}
              for a in rel.get("assets") or [] if a.get("name") in ASSETS.values()}
    return {"latest": str(rel.get("tag_name") or "").lstrip("vV"), "notes": str(rel.get("body") or "")[:6000],
            "url": str(rel.get("html_url") or PAGE), "assets": assets}


def _from_site():
    """Запасной способ. API пускает без входа 60 запросов в час с одного адреса, а за общим адресом (VPN, провайдер)
    их тратят чужие люди. Страница релиза такого ограничения не имеет: номер версии — из адреса, на который
    она переадресует, суммы — из файла SHA256SUMS.txt в релизе. Описания версии тут нет."""
    with _open(PAGE, 15) as r:
        final = r.geturl()
    m = re.fullmatch(r"https://github\.com/%s/releases/tag/([^/?#]+)" % re.escape(REPO), final)
    if not m:
        raise ValueError("нет релизов или непонятный адрес: %s" % final)
    tag, assets = m.group(1), {}
    try:
        with _open(ASSET_PREFIX + tag + "/" + SUMS, 15) as r:
            for line in r.read(20000).decode("utf-8", "replace").splitlines():
                p = line.split(None, 1)   # формат sha256sum: «сумма  имя» или «сумма *имя»
                name = p[1].strip().lstrip("*") if len(p) == 2 else ""
                if name in ASSETS.values():
                    assets[name] = {"url": ASSET_PREFIX + tag + "/" + name, "size": 0, "sha256": p[0].lower()}
    except OSError:
        pass   # в релизе нет файла сумм: версию покажем, а ставить придётся вручную
    return {"latest": unquote(tag).lstrip("vV"), "notes": "", "url": final, "assets": assets}


def check():
    """Спросить у GitHub последний релиз. Возвращает True, если ответ получен (новая версия или нет — в INFO)."""
    with _lock:
        try:
            try:
                rel = _from_api()
            except Exception as e:
                log("проверка обновлений через API не удалась (%s): пробую страницу релиза" % e)
                rel = _from_site()
            if not vkey(rel["latest"]):
                raise ValueError("непонятный номер версии: %r" % rel["latest"])
            INFO.update(rel, t=time.time(), error="")
            log("проверка обновлений: последний релиз %s, у нас %s" % (rel["latest"], VERSION))
            ok = True
        except Exception as e:
            INFO.update({"t": time.time(), "error": str(e)[:200]})   # время пишем и при ошибке: не долбить GitHub
            log("проверка обновлений не удалась: %s" % e)
            ok = False
        _cache_save()
        return ok


def _cache_save():
    try:
        write_json(CACHE_PATH, dict(INFO), indent=1)
    except OSError:
        pass


def watch():
    """Фоновая проверка раз в сутки (выключается в настройках). Нашли новую версию — уведомление из трея, один раз."""
    time.sleep(20)   # не на старте: сначала мост должен подняться и найти принтер
    while True:
        try:
            if CFG.get("update_check", True) and time.time() - INFO.get("t", 0) > CHECK_EVERY:
                check()
            latest = INFO.get("latest")
            if latest and newer(latest) and INFO.get("notified") != latest and CFG.get("update_check", True):
                INFO["notified"] = latest
                _cache_save()
                notify(tr("update.notify.title"), tr("update.notify.text", v=latest))
        except Exception as e:
            log("проверка обновлений: %s" % e)
        time.sleep(1800)


def _asset():
    a = (INFO.get("assets") or {}).get(ASSETS.get(mode(), ""))
    # без контрольной суммы и не с нашего адреса не ставим: остаётся ссылка «скачать вручную»
    if a and a["url"].startswith(ASSET_PREFIX) and re.fullmatch(r"[0-9a-f]{64}", a["sha256"]) and 0 <= a["size"] <= MAX_SIZE:
        return a
    return None


def status(local):
    latest = INFO.get("latest") or ""
    avail = newer(latest)
    return {"current": VERSION, "latest": latest, "available": avail, "notes": INFO.get("notes", "") if avail else "",
            "url": INFO.get("url") or PAGE, "checked": INFO.get("t", 0), "check_error": INFO.get("error", ""),
            "mode": mode(), "auto": bool(CFG.get("update_check", True)),
            "can_install": bool(local and avail and _asset()), "state": JOB["state"],
            "done": JOB["done"], "total": JOB["total"], "error": JOB["error"]}


def clean_env():
    """Окружение для запуска установщика и новой версии. Собранный exe (PyInstaller) передаёт потомкам свои
    служебные переменные; с ними новый exe принял бы себя за часть старого и искал бы его удалённые файлы."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI_", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _spawn(args):
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP   # переживёт выход моста
    subprocess.Popen(args, env=clean_env(), close_fds=True, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _download(a, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    h, got = hashlib.sha256(), 0
    JOB.update(state="downloading", done=0, total=a["size"], error="")
    with _open(a["url"], 30) as r:
        if not r.geturl().startswith("https://"):   # GitHub переадресует на своё хранилище — но только по HTTPS
            raise ValueError(tr("update.err.insecure"))
        if not a["size"]:   # размер заранее неизвестен (суммы из файла): для полосы хода берём из ответа
            JOB["total"] = int(r.headers.get("Content-Length") or 0)
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > (a["size"] or MAX_SIZE):
                    raise ValueError(tr("update.err.checksum"))
                h.update(chunk)
                f.write(chunk)
                JOB["done"] = got
    if (a["size"] and got != a["size"]) or h.hexdigest() != a["sha256"]:
        raise ValueError(tr("update.err.checksum"))


def install(stop, busy):
    """Скачать и поставить последнюю версию. stop() завершает мост; busy() — идёт ли заливка файла на принтер.
    Запускает работу в фоне и сразу возвращает None либо текст ошибки."""
    with _lock:
        if JOB["state"] in ("downloading", "installing"):
            return tr("update.err.running")
        how, a = mode(), _asset()
        if how == "source" or not a or not newer(INFO.get("latest")):
            return tr("update.err.nothing")
        JOB.update(state="downloading", done=0, total=a["size"], error="")
    threading.Thread(target=_run, args=(how, a, INFO["latest"], stop, busy), daemon=True).start()
    return None


def _run(how, a, latest, stop, busy):
    exe = sys.executable
    dest = exe + ".new" if how == "exe" else os.path.join(TMP_DIR, ASSETS["setup"])
    try:
        log("обновление до %s: скачиваю %s" % (latest, a["url"]))
        _download(a, dest)
        if busy():   # пока качали, началась заливка файла на принтер: перезапуск её оборвёт
            raise ValueError(tr("err.uploading"))
        JOB["state"] = "installing"
        if how == "setup":
            # /SILENT — окно с ходом установки и ошибками видно, вопросов нет. Новый установщик сам запускает мост.
            # /WAITPID — установщик ждёт, пока этот мост закроется сам (см. installer.iss): иначе он застаёт мост
            # закрывающимся и полминуты ждёт его через диспетчер перезапуска Windows. У exe два процесса:
            # загрузчик и сам мост; файл держит загрузчик, он выходит последним — ждать надо его.
            _spawn([dest, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/WAITPID=%d" % os.getppid(),
                    "/LOG=" + os.path.join(CFG_DIR, "update_setup.log")])
            log("обновление до %s: запущен установщик, мост закрывается" % latest)
        else:
            # Работающий exe нельзя перезаписать, но можно переименовать: старый — в .old, новый — на его место
            old = exe + ".old"
            if os.path.exists(old):
                os.remove(old)
            os.replace(exe, old)
            try:
                os.replace(dest, exe)
            except OSError:
                os.replace(old, exe)
                raise
            _spawn([exe, "--no-browser", "--updated"])
            log("обновление до %s: файл заменён, запущена новая версия, мост закрывается" % latest)
        time.sleep(1.0)   # панель успевает узнать, что установка началась
        stop()
    except Exception as e:
        log("обновление не удалось: %s" % e)
        JOB.update(state="error", error=str(e)[:300])
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass


def cleanup():
    """После обновления: убрать старый exe и скачанный установщик. Старый процесс может ещё закрываться — пробуем не раз."""
    def work():
        targets = [os.path.join(TMP_DIR, ASSETS["setup"])]
        if getattr(sys, "frozen", False):
            targets += [sys.executable + ".old", sys.executable + ".new"]
        for _ in range(20):
            left = [p for p in targets if os.path.exists(p)]
            for p in left:
                try:
                    os.remove(p)
                except OSError:
                    pass
            if not [p for p in left if os.path.exists(p)]:
                return
            time.sleep(3)
    threading.Thread(target=work, daemon=True).start()
