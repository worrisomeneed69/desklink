"""DeskLink - helpery platformowe (Windows 11 natywnie, Wine, Linux)."""

from __future__ import annotations

import os
import platform
import sys
from typing import Tuple

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def running_under_wine() -> bool:
    """Wykrywa Wine - przydatne, bo kilka rzeczy (DPI, keyb_layout) zachowuje sie inaczej."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
        # NT::wine_get_version istnieje tylko pod Wine
        if hasattr(ntdll, "wine_get_version"):
            ntdll.wine_get_version.restype = ctypes.c_char_p
            return ntdll.wine_get_version() is not None
    except Exception:  # noqa: BLE001
        pass
    return "WINELOADER" in os.environ or "wine" in platform.vendor.lower()


def wine_version() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import ctypes

        ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
        ntdll.wine_get_version.restype = ctypes.c_char_p
        return (ntdll.wine_get_version() or b"").decode()
    except Exception:  # noqa: BLE001
        return ""


def enable_dpi_awareness() -> str:
    """Windows: bez tego mysz idzie w skladowanych pikselach i trafianie obok okna murowane.

    Wine zazwyczaj ignoruje te API (zwraca bledy) - dlatego dostajemy opis stanu.
    """
    if not IS_WINDOWS:
        return "nie-windows"
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        return "per-monitor-v2"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:  # noqa: BLE001
        return "brak"


def screen_physical_size() -> Tuple[int, int]:
    if IS_WINDOWS:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    try:
        import pyautogui

        return int(pyautogui.size()[0]), int(pyautogui.size()[1])
    except Exception:  # noqa: BLE001
        return 0, 0


def describe() -> dict:
    """Widoczny w UI opis tej maszyny."""
    import socket

    return {
        "os": "Windows" if IS_WINDOWS else ("macOS" if IS_MAC else "Linux"),
        "release": platform.release(),
        "hostname": socket.gethostname().split(".")[0],
        "python": platform.python_version(),
        "wine": wine_version() or None,
        "res": "%dx%d" % screen_physical_size(),
    }
