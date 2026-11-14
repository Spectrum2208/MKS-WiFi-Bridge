"""Сборка MKSBridge.exe (Windows) через PyInstaller.

Нужен Python с пакетами pyinstaller, pystray, pillow (лучше в отдельном виртуальном окружении):
    python -m venv buildenv
    buildenv\\Scripts\\python -m pip install pyinstaller pystray pillow
    buildenv\\Scripts\\python packaging\\build_exe.py
Готовый файл: dist\\MKSBridge.exe
Если установлен Inno Setup 6, рядом собирается и установщик dist\\MKSBridge-Setup.exe (packaging\\installer.iss).
"""
import glob
import hashlib
import os
import re
import subprocess
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
PyInstaller.__main__.run([
    "run_bridge.py",
    "--name", "MKSBridge",
    "--onefile",
    "--windowed",                                   # без окна консоли: управление — значок в трее и веб-панель
    "--icon", os.path.join("packaging", "icon.ico"),
    "--add-data", os.path.join("mks_bridge", "web") + os.pathsep + os.path.join("mks_bridge", "web"),
    "--add-data", os.path.join("mks_bridge", "lang") + os.pathsep + os.path.join("mks_bridge", "lang"),   # языковые файлы
    "--hidden-import", "pystray._win32",
    "--noconfirm",
    "--clean",
] + sys.argv[1:])


def build_setup():
    """Установщик: Inno Setup ставится отдельно (winget install JRSoftware.InnoSetup); без него — только exe."""
    pf = [os.environ.get(k) for k in ("LOCALAPPDATA", "ProgramFiles(x86)", "ProgramFiles")]
    found = [c for base in pf if base for c in glob.glob(os.path.join(base, "*Inno Setup 6", "ISCC.exe"))
             + glob.glob(os.path.join(base, "Programs", "Inno Setup 6", "ISCC.exe"))]
    if not found:
        print("Inno Setup не найден: установщик не собран, есть только dist/MKSBridge.exe")
        return
    with open(os.path.join("mks_bridge", "__init__.py"), encoding="utf-8") as f:
        version = re.search(r'VERSION = "([^"]+)"', f.read()).group(1)
    subprocess.run([found[0], "/Q", "/DAppVersion=" + version, os.path.join("packaging", "installer.iss")], check=True)
    print("Установщик: dist/MKSBridge-Setup.exe (" + version + ")")



def write_sums():
    """Контрольные суммы для обновления из панели (mks_bridge/update.py): файл прикладывается к релизу вместе с exe."""
    lines = []
    for name in ("MKSBridge-Setup.exe", "MKSBridge.exe"):
        path = os.path.join("dist", name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                lines.append("%s  %s\n" % (hashlib.sha256(f.read()).hexdigest(), name))
    with open(os.path.join("dist", "SHA256SUMS.txt"), "w", encoding="ascii", newline="\n") as f:
        f.writelines(lines)
    print("Контрольные суммы: dist/SHA256SUMS.txt")


build_setup()
write_sums()
