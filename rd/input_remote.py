"""DeskLink - wstrzykiwanie wejscia po stronie hosta.

Wspolrzedne przychodza jako ulamek (0..1) ekranu zdalnego, więc dziala nawet gdy
kontroler ma 4K a host laptopa 1366x768 (albo gdy Wine zglupieje na skalowaniu).
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Set, Tuple

from . import keys
from .osutil import IS_LINUX, IS_WINDOWS

try:
    import pyautogui

    pyautogui.FAILSAFE = False  # inaczej ruch w rog ekranu zabija polaczenie
    pyautogui.PAUSE = 0
    HAVE_PYAUTOGUI = True
except Exception:  # noqa: BLE001
    pyautogui = None
    HAVE_PYAUTOGUI = False


class InputError(RuntimeError):
    pass


class InputInjector:
    def __init__(self) -> None:
        self.area: Tuple[int, int, int, int] = (0, 0, 1920, 1080)  # left, top, width, height
        self.enabled = True
        self.applied = 0
        self.rejected = 0
        self.last_error = ""
        self._held_keys: Set[str] = set()
        self._held_buttons: Set[str] = set()
        self._lock = threading.Lock()
        self._last_move = 0.0

    # ------------------------------------------------------------------ konfiguracja
    def set_area(self, left: int, top: int, width: int, height: int) -> None:
        self.area = (int(left), int(top), int(width), int(height))

    @property
    def available(self) -> bool:
        return HAVE_PYAUTOGUI

    def _to_px(self, fx: float, fy: float) -> Tuple[int, int]:
        left, top, w, h = self.area
        x = left + int(round(max(0.0, min(1.0, fx)) * (w - 1)))
        y = top + int(round(max(0.0, min(1.0, fy)) * (h - 1)))
        return x, y

    def _call(self, fname: str, *args, **kwargs):
        if not HAVE_PYAUTOGUI:
            self.rejected += 1
            self.last_error = "brak pyautogui"
            return False
        if not self.enabled:
            self.rejected += 1
            return False
        fn = getattr(pyautogui, fname, None)
        if fn is None:
            self.last_error = f"pyautogui.{fname} nieistnieje"
            self.rejected += 1
            return False
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - X11/Wine potrafi rzucic byle czym
            self.last_error = f"{fname}: {exc}"
            self.rejected += 1
            return False
        self.applied += 1
        return True

    # ------------------------------------------------------------------ mysz
    def move(self, fx: float, fy: float) -> None:
        now = time.monotonic()
        if now - self._last_move < 0.004:  # ~250 Hz enough, chroni kolejke
            return
        self._last_move = now
        x, y = self._to_px(fx, fy)
        self._call("moveTo", x, y, duration=0)

    def button(self, name: str, action: str, fx: Optional[float] = None, fy: Optional[float] = None,
               count: int = 1) -> None:
        if fx is not None and fy is not None:
            x, y = self._to_px(fx, fy)
            self._call("moveTo", x, y, duration=0)
        button = name if name in ("left", "right", "middle") else "left"
        with self._lock:
            if action == "down":
                self._held_buttons.add(button)
                self._call("mouseDown", button=button)
            elif action == "up":
                self._held_buttons.discard(button)
                self._call("mouseUp", button=button)
            elif action in ("click", "single"):
                self._call("click", button=button, clicks=1)
            elif action == "double":
                self._call("click", button=button, clicks=2, interval=0.05)
            elif action == "triple":
                self._call("click", button=button, clicks=3, interval=0.05)

    def scroll(self, dx: float, dy: float, fx: Optional[float] = None, fy: Optional[float] = None) -> None:
        if fx is not None and fy is not None:
            x, y = self._to_px(fx, fy)
            self._call("moveTo", x, y, duration=0)
        clicks = int(round(abs(dy) * 3))
        if clicks:
            self._call("scroll", clicks if dy < 0 else -clicks)
        hclicks = int(round(abs(dx) * 3))
        if hclicks:
            fn = getattr(pyautogui, "hscroll", None) if HAVE_PYAUTOGUI else None
            if fn:
                try:
                    fn(hclicks if dx > 0 else -hclicks)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ klawiatura
    def key(self, canon: Optional[str], action: str, char: Optional[str] = None) -> None:
        windows = IS_WINDOWS
        if char and action in ("press", "single"):
            # pynput zwraca te znaki jako KeyCode.char - podajemy je wprost do pyautogui
            if len(char) == 1 and char not in ("\x1b", "\r", "\n", "\t", "\b"):
                self._call("press", char)
                return
        name = keys.host_key_name(canon or "", windows) if canon else None
        if name is None:
            self.rejected += 1
            return
        if action == "press":
            with self._lock:
                self._held_keys.add(name)
            self._call("press", name)
        elif action == "down":
            with self._lock:
                self._held_keys.add(name)
            self._call("keyDown", name)
        elif action == "up":
            with self._lock:
                self._held_keys.discard(name)
            self._call("keyUp", name)

    def hotkey(self, combo: str) -> None:
        if not combo:
            return
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        mapped = [keys.host_key_name(p, IS_WINDOWS) or p for p in parts]
        self._call("hotkey", *mapped)

    def type_text(self, text: str, interval: float = 0.008) -> None:
        if not text:
            return
        if len(text) > 2 and text.isprintable():
            # najpierw proba przez schowek (szybko i bez bledów layoutu)
            if self._paste_text(text):
                return
        self._call("write", text, interval=interval)

    @staticmethod
    def _paste_text(text: str) -> bool:
        try:
            import pyperclip

            pyperclip.copy(text)
            time.sleep(0.02)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.03)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ bezpieczenstwo
    def release_all(self) -> None:
        """Po przerwaniu sesji trzeba zwolnic trzymane klawisze, inaczej Windows dostaje 'wciety ctrl'."""
        with self._lock:
            held_keys = list(self._held_keys)
            held_buttons = list(self._held_buttons)
            self._held_keys.clear()
            self._held_buttons.clear()
        if not HAVE_PYAUTOGUI:
            return
        for button in held_buttons:
            try:
                pyautogui.mouseUp(button=button)
            except Exception:  # noqa: BLE001
                pass
        for name in reversed(held_keys):
            try:
                pyautogui.keyUp(name)
            except Exception:  # noqa: BLE001
                pass


class LocalInputBlock:
    """Blokada lokalnej myszy/klawiatury podczas zdalnej kontroli (tylko X11).

    Pod Windows/Wine nie robimy tego z bezpieczenstwa - latwo zablokowac sobie
    maszyny bez wyjscia. Tu mamy twardy timeout i watchdog.
    """

    def __init__(self, hard_timeout: float = 120.0):
        self.hard_timeout = hard_timeout
        self.active = False
        self._display = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def supported(self) -> bool:
        return IS_LINUX and bool(__import__("os").environ.get("DISPLAY"))

    def grab(self) -> bool:
        if self.active or not self.supported:
            return False
        try:
            from Xlib import display as xdisplay
            from Xlib.ext import record  # noqa: F401
            from Xlib import X

            self._display = xdisplay.Display()
            root = self._display.screen().root
            root.grab_pointer(False, X.EventMask(0), X.GrabModeAsync, X.GrabModeAsync)
            self._display.grab_keyboard(True, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            self._display.sync()
        except Exception:  # noqa: BLE001
            self._display = None
            return False
        self.active = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._watchdog, name="input-block", daemon=True)
        self._thread.start()
        return True

    def _watchdog(self) -> None:
        end = time.monotonic() + self.hard_timeout
        while not self._stop.is_set() and time.monotonic() < end:
            time.sleep(0.5)
        self.ungrab()

    def ungrab(self) -> None:
        self._stop.set()
        if not self.active:
            return
        try:
            from Xlib import X

            if self._display is not None:
                self._display.ungrab_keyboard(X.CurrentTime)
                self._display.screen().root.ungrab_pointer()
                self._display.sync()
                self._display.close()
        except Exception:  # noqa: BLE001
            pass
        self._display = None
        self.active = False
