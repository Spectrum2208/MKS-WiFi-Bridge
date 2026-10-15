"""Сборка MKSBridge.exe (Windows) через PyInstaller.

Нужен Python с пакетами pyinstaller, pystray, pillow (лучше в отдельном виртуальном окружении):
    python -m venv buildenv
    buildenv\\Scripts\\python -m pip install pyinstaller pystray pillow
    buildenv\\Scripts\\python packaging\\build_exe.py
Готовый файл: dist\\MKSBridge.exe
"""
import os
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
    "--hidden-import", "pystray._win32",
    "--noconfirm",
    "--clean",
] + sys.argv[1:])
