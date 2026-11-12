"""ToolHub beim Windows-Login starten (HKCU\\...\\Run, nur für den aktuellen Nutzer)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "ToolHub"
OLD_VALUES = ("ToolManager",)  # frühere Namen, werden beim Ändern mit aufgeräumt


def command() -> str:
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}"'
    pyw = exe.with_name("pythonw.exe")
    root = Path(__file__).resolve().parent.parent
    return f'"{pyw if pyw.exists() else exe}" "{root / "ToolHub.pyw"}"'


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE)
            return True
    except OSError:
        return False


def set_enabled(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        for old in OLD_VALUES:
            try:
                winreg.DeleteValue(k, old)
            except FileNotFoundError:
                pass
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass
