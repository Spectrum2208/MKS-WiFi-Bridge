"""Автозапуск моста вместе с Windows (ключ Run в реестре пользователя)."""
import os
import sys

# скрипт запуска в корне проекта (для автозапуска без .exe)
LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_bridge.py")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_get():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "MKSBridge")
            return True
    except OSError:
        return False


def autostart_set(on):
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            exe = sys.executable
            if getattr(sys, "frozen", False):
                cmd = '"%s" --no-browser' % exe
            else:
                pw = exe.replace("python.exe", "pythonw.exe")
                cmd = '"%s" "%s" --no-browser' % (pw if os.path.exists(pw) else exe, LAUNCHER)
            winreg.SetValueEx(k, "MKSBridge", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "MKSBridge")
            except OSError:
                pass
