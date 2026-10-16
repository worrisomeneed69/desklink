"""DeskLink - platformowo neutralne nazwy klawiszy.

Problem: kontroler moze byc na Windows 11, host pod Wine albo na Linuxie (i odwrotnie).
pynput zwraca nazwy zalezne od systemu, pyautogui przyjmuje nazwy zalezne od systemu.
Wiec lecimy przez warstwe posrednia: CANON. Kazda strona tlumaczy na swoj glos.
"""

from __future__ import annotations

from typing import Optional, Tuple

# pynput (Key.name / KeyCode)  ->  CANON
_PYNPUT = {
    "enter": "enter", "return": "enter",
    "backspace": "backspace",
    "delete": "delete", "delete_lock": "delete",
    "tab": "tab",
    "esc": "esc", "escape": "esc",
    "space": "space",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end",
    "page_up": "pageup", "page_down": "pagedown",
    "pg_up": "pageup", "pg_down": "pagedown",
    "insert": "insert",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl", "control": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "altgr",
    "cmd": "super", "cmd_r": "super", "win": "super", "win_r": "super",
    "super": "super", "super_l": "super", "super_r": "super", "meta": "super",
    "caps_lock": "capslock",
    "num_lock": "numlock",
    "scroll_lock": "scrolllock",
    "print_screen": "printscreen",
    "pause": "pause",
    "menu": "menu",
    "backtick": "grave",
}
for _i in range(1, 25):
    _PYNPUT[f"f{_i}"] = f"f{_i}"
for _i in range(0, 10):
    _PYNPUT[f"num{_i}"] = f"num{_i}"

# CANON -> nazwa dla pyautogui na danym systemie
_WIN = {
    "enter": "enter", "backspace": "backspace", "delete": "delete", "tab": "tab",
    "esc": "escape", "space": "space",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "insert": "insert", "capslock": "capslock", "numlock": "numlock",
    "scrolllock": "scrolllock", "printscreen": "printscreen", "pause": "pause",
    "shift": "shift", "ctrl": "ctrl", "alt": "alt", "altgr": "ralt", "super": "win",
    "menu": "apps", "grave": "`",
}
_LINUX = {
    "enter": "enter", "backspace": "backspace", "delete": "delete", "tab": "tab",
    "esc": "esc", "space": "space",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "insert": "insert", "capslock": "caps_lock", "numlock": "num_lock",
    "scrolllock": "scroll_lock", "printscreen": "print", "pause": "pause",
    "shift": "shift", "ctrl": "ctrl", "alt": "alt", "altgr": "altgr", "super": "super",
    "menu": "menu", "grave": "grave",
}

MODIFIERS = {"shift", "ctrl", "alt", "super", "altgr"}

# Tk <Button-N> / pynput Button -> CANON przycisku myszy
BUTTONS = {1: "left", 2: "middle", 3: "right", 4: "wheelup", 5: "wheeldown", 8: "back", 9: "forward"}
REVERSE_BUTTONS = {v: k for k, v in BUTTONS.items()}


# Tk keysym -> CANON. Kontroler klika w okno Tk, wiec potrzebujemy tlumaczenia
# z nazewnictwa X11/Windows (Keysym) na nasza warstwe posrednia.
_TK = {
    "Return": "enter", "KP_Enter": "enter", "BackSpace": "backspace",
    "Delete": "delete", "KP_Delete": "delete", "Tab": "tab", "ISO_Left_Tab": "tab",
    "Escape": "esc", "space": "space",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Home": "home", "End": "end", "Prior": "pageup", "Next": "pagedown",
    "Page_Up": "pageup", "Page_Down": "pagedown", "Insert": "insert",
    "Caps_Lock": "capslock", "Num_Lock": "numlock", "Scroll_Lock": "scrolllock",
    "Print": "printscreen", "Sys_Req": "printscreen", "Pause": "pause",
    "Menu": "menu", "ContextMenu": "menu",
    "Shift_L": "shift", "Shift_R": "shift",
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "altgr", "Meta_L": "super", "Meta_R": "super",
    "Super_L": "super", "Super_R": "super", "Win_L": "super", "Win_R": "super",
    "grave": "grave",
}

# Tk nazywa znaki przestankowe slowami - to te same znaki, tylko bez 'len==1'
_TK_CHAR = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "apostrophe": "'", "quoteright": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",",
    "minus": "-", "period": ".", "slash": "/", "colon": ":", "semicolon": ";",
    "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "quoteleft": "`", "bar": "|", "braceleft": "{",
    "asciitilde": "~", "braceright": "}",
    "KP_Add": "+", "KP_Subtract": "-", "KP_Multiply": "*", "KP_Divide": "/",
    "KP_Decimal": ".", "KP_Separator": ",", "mu": "\u03bc",
}
for _i in range(1, 25):
    _TK[f"F{_i}"] = f"f{_i}"
for _i in range(10):
    _TK[f"KP_{_i}"] = f"num{_i}"

# Pod Wine i na Windows Tkm potrafi oddac keysym w innej pisowni (np. 'ctrl_l'
# zamiast 'Control_L'). Szukamy wiec dodatkowo bez wzgledu na wielkosc liter.
_TK_LOW = {k.lower(): v for k, v in _TK.items()}
_TK_CHAR_LOW = {k.lower(): v for k, v in _TK_CHAR.items()}

_MOD_BASE = {
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "meta": "super", "super": "super", "win": "super", "command": "super",
    "altgr": "altgr",
}


def _modifier(low: str) -> Optional[str]:
    """'control_l' / 'ctrl-l' / 'shift_r' -> CANON modyfikatora (bez _l/_r)."""
    key = low.replace("-", "_")
    if key == "alt_r":                     # pod X11 prawy Alt to AltGr
        return "altgr"
    return _MOD_BASE.get(key.split("_")[0])


def key_from_tk(keysym: str) -> Tuple[Optional[str], Optional[str]]:
    """(canon, char) z Tk event.keysym - jedno z dwoch jest nie-None.

    Znaki drukowalne (a, A, 7, :) ida jako 'char', bo pyautogui i tak wciska je
    przez znaki; klawisze sterujace ida jako 'canon', zeby host przetlumaczyl je
    na swoj system (Windows <-> Linux/Wine).
    """
    if not keysym:
        return None, None
    if keysym in _TK:
        return _TK[keysym], None
    if keysym in _TK_CHAR:
        return None, _TK_CHAR[keysym]
    low = keysym.lower()
    if low in _TK_LOW:
        return _TK_LOW[low], None
    if low in _TK_CHAR_LOW:
        return None, _TK_CHAR_LOW[low]
    mod = _modifier(low)
    if mod:
        return mod, None
    if len(keysym) == 1:
        return None, keysym
    if keysym[:1] == "U" and keysym[1:].isalnum():     # Tk: 'U0142' dla 'l z kreska'
        try:
            return None, chr(int(keysym[1:], 16))
        except ValueError:
            return None, None
    if low.startswith("f") and low[1:].isdigit():
        return low, None
    return None, None


def normalize_pynput_key(key) -> Tuple[Optional[str], Optional[str]]:
    """(canon, char) z tego co dostarcza pynput. Jedno z dwoch jest nie-None."""
    name = getattr(key, "name", None)
    if name:
        low = str(name).lower()
        canon = _PYNPUT.get(low) or _PYNPUT.get(low.replace("-", "_"))
        if canon:
            return canon, None
        # nieznazone, ale ksztalt f1..f24 / num0..9
        if low.startswith("f") and low[1:].isdigit():
            return low, None
        return None, None
    char = getattr(key, "char", None)
    if char:
        return None, char
    return None, None


def host_key_name(canon: str, windows: bool) -> Optional[str]:
    table = _WIN if windows else _LINUX
    if canon in table:
        return table[canon]
    if canon.startswith("f") and canon[1:].isdigit():
        return canon
    if canon.startswith("num") and canon[3:].isdigit():
        return canon
    return None


def button_from_tk(number: int) -> str:
    return BUTTONS.get(number, "left")


def tk_number(button: str) -> int:
    return REVERSE_BUTTONS.get(button, 1)
