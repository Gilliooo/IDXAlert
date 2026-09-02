#!/usr/bin/env python3
"""
Run-at-login toggle, via the per-user Run key in the registry.

Registers under its OWN key name (IDXAlert3) so it can coexist with an older
install - two watchers auto-starting into the same feed would double the
request rate and duplicate every alert.

HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run needs no admin rights and
uses only the standard library - no shortcut files, no pywin32.
"""
import os
import sys

APP_NAME = "IDXAlert3"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def supported():
    return os.name == "nt"


def target_command(script=None):
    """The command Windows should run at login.

    Frozen  -> the .exe itself.
    Source  -> pythonw.exe (no console window) plus idx3tray.py.
    """
    if getattr(sys, "frozen", False):
        return '"%s"' % os.path.abspath(sys.executable)

    here = os.path.dirname(os.path.abspath(__file__))
    script = script or os.path.join(here, "idx3tray.py")
    exe = sys.executable or "python"
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return '"%s" "%s"' % (exe, os.path.abspath(script))


def is_enabled():
    if not supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def current_value():
    if not supported():
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, APP_NAME)[0]
    except OSError:
        return ""


def set_enabled(on, script=None):
    """Returns (ok, message)."""
    if not supported():
        return False, "only available on Windows"
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if on:
                cmd = target_command(script)
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                return True, cmd
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
            return True, "removed"
    except OSError as exc:
        return False, str(exc)


if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if arg in ("on", "enable"):
        print(set_enabled(True))
    elif arg in ("off", "disable"):
        print(set_enabled(False))
    else:
        print("enabled:", is_enabled())
        print("command:", current_value() or "(not set)")
