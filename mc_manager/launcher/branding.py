"""Launcher icon and Windows taskbar identity."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import tkinter as tk

APP_ID = "Monkeycraft.Launcher"
ASSETS = Path(__file__).with_name("assets")


def set_process_identity() -> None:
    """Call before creating Tk so Windows groups the launcher independently."""
    if os.name == "nt":
        try:
            set_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            set_id.argtypes = [ctypes.c_wchar_p]
            set_id.restype = ctypes.c_long
            set_id(APP_ID)
        except (AttributeError, OSError):
            pass  # Branding must not stop the launcher from opening.


def set_window_icon(root: tk.Tk) -> None:
    """Use native multi-resolution ICO on Windows and PNG elsewhere."""
    try:
        if os.name == "nt":
            root.iconbitmap(default=str(ASSETS / "launcher.ico"))
        else:
            icon = tk.PhotoImage(master=root, file=str(ASSETS / "launcher-icon.png"))
            root.iconphoto(True, icon)
            root._launcher_icon = icon
    except tk.TclError:
        pass
