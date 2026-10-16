"""DeskLink GUI - paleta, typografia i stylizacja ttk.

Cel: ciemny, "sprzetowy" interfejs w duchu AnyDesk (duze ID, jeden cieply
akcent), ale bez kopiowania jego grafik. Musi wygladac identycznie pod
Windows 11, pod Wine i na Linuksie, dlatego:
  * zadnych zdjec/ikon z pliku - wszystko rysowane na Canvasie,
  * kolory zawsze podawane jawne (Widgety tk akceptuja bg na kazdej platformie),
  * ttk tylko tam, gdzie i tak potrzebujemy (notebook, scrollbar, progressbar).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Optional

# --------------------------------------------------------------------- paleta
DARK = {
    "bg": "#12161C",          # okno
    "panel": "#171D25",       # gorny pasek / stopka
    "card": "#1E252F",        # karty
    "card_hi": "#242E3A",     # karta w focusie / hover
    "line": "#2C3743",        # obramowania
    "line_soft": "#222B35",
    "text": "#E9EEF4",
    "muted": "#8FA0B3",
    "faint": "#66778A",
    "accent": "#FF4B2B",      # 'Polacz' - cieply red-orange
    "accent_dark": "#D33A1E",
    "accent_soft": "#3A211C",
    "ok": "#3FD6A6",
    "warn": "#FFB547",
    "danger": "#F2555A",
    "entry": "#0F141A",
    "sel": "#2B4557",
    "canvas": "#0B0E12",      # tlo pod zdalnym ekranem
}

LIGHT = {
    "bg": "#EEF1F5",
    "panel": "#FFFFFF",
    "card": "#FFFFFF",
    "card_hi": "#F2F5F9",
    "line": "#D4DBE4",
    "line_soft": "#E4E9F0",
    "text": "#16202B",
    "muted": "#5C6B7C",
    "faint": "#8B99A8",
    "accent": "#E8401F",
    "accent_dark": "#C22F12",
    "accent_soft": "#FFE9E4",
    "ok": "#0F9E70",
    "warn": "#B8730A",
    "danger": "#D23141",
    "entry": "#F7F9FC",
    "sel": "#CFE3F3",
    "canvas": "#20262E",
}

FALLBACK_FAMILY = {
    "windows": "Segoe UI",
    "darwin": "Helvetica Neue",
    "other": "DejaVu Sans",
}
# Wine nie ma Segoe UI z paczki corefonts - dlatego lista probowa
FAMILY_PROBES = ("Segoe UI", "Segoe UI Semibold", "Tahoma", "Verdana",
                 "DejaVu Sans", "Noto Sans", "Liberation Sans", "Arial")
MONO_PROBES = ("Consolas", "DejaVu Sans Mono", "Cascadia Mono", "Courier New",
              "Liberation Mono")


def palette(theme: str = "dark") -> dict:
    return DARK if theme != "light" else LIGHT


def _pick_family(root: tk.Misc, probes: tuple) -> str:
    try:
        families = {f.lower() for f in tkfont.families(root)}
    except tk.TclError:  # brak displayu (testy importu)
        return probes[-1]
    for name in probes:
        if name.lower() in families:
            return name
    return "TkDefaultFont"


class Typography:
    """Wlasne obiekty fontow - Tk nie 'pogrubuje' czcionki sam z siebie."""

    def __init__(self, root: tk.Misc, theme: str = "dark"):
        self.root = root
        self.family = _pick_family(root, FAMILY_PROBES)
        self.mono = _pick_family(root, MONO_PROBES)
        scale = 1 if theme == "dark" else 1

        def mk(size: int, weight: str = "normal", family: Optional[str] = None,
               underline: bool = False) -> tkfont.Font:
            return tkfont.Font(root=root, family=family or self.family,
                               size=int(size * scale), weight=weight, underline=underline)

        self.eyebrow = mk(9, "bold", underline=False)     # 'TWOJ ADRES'
        self.body = mk(11)
        self.body_bold = mk(11, "bold")
        self.small = mk(9)
        self.small_bold = mk(9, "bold")
        self.title = mk(15, "bold")
        self.machine = mk(12, "bold")
        self.id_big = mk(32, "bold")
        self.id_huge = mk(40, "bold")
        self.mono_small = mk(9, family=self.mono)
        self.mono_id = mk(11, family=self.mono)
        self.mono_big = mk(13, "bold", family=self.mono)


class Style:
    """Wspolny kontekst dla widgetow: paleta + fonty + nazwa motywu."""

    def __init__(self, root: tk.Misc, theme: str = "dark"):
        self.theme = theme if theme in ("dark", "light") else "dark"
        self.c = palette(self.theme)
        self.f = Typography(root, self.theme)
        self._style = ttk.Style(root)
        self._configure_ttk()

    # -------------------------------------------------------------- pomocnicze
    def mix(self, color: str, factor: float) -> str:
        """Jaśniejszy/ciemniejszy wariant koloru (0.5 = polowa jasnosci)."""
        try:
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
        except (ValueError, IndexError):
            return color
        clamp = lambda v: max(0, min(255, int(v)))  # noqa: E731
        return f"#{clamp(r * factor):02X}{clamp(g * factor):02X}{clamp(b * factor):02X}"

    @property
    def bg(self) -> str:
        return self.c["bg"]

    def text(self, size: int = 11, weight: str = "normal", mono: bool = False) -> tkfont.Font:
        base = self.f.mono if mono else self.f.family
        return tkfont.Font(family=base, size=size, weight=weight)

    # -------------------------------------------------------------- ttk
    def _configure_ttk(self) -> None:
        s = self._style
        c = self.c
        try:
            s.theme_use("clam")
        except tk.TclError:
            return
        s.configure(".", font=self.f.body, background=c["bg"], foreground=c["text"],
                    bordercolor=c["line"], lightcolor=c["card"], darkcolor=c["card"],
                    focuscolor=c["accent"], troughcolor=c["entry"])
        s.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", background=c["card"], foreground=c["muted"],
                    padding=(14, 8), borderwidth=0, font=self.f.body_bold)
        s.map("TNotebook.Tab",
              background=[("selected", c["card_hi"])],
              foreground=[("selected", c["text"])])
        s.configure("TFrame", background=c["bg"])
        s.configure("Card.TFrame", background=c["card"])
        s.configure("Treeview", background=c["card"], fieldbackground=c["card"],
                    foreground=c["text"], rowheight=28, borderwidth=0, font=self.f.body)
        s.configure("Treeview.Heading", background=c["panel"], foreground=c["muted"],
                    borderwidth=0, font=self.f.small_bold, relief="flat")
        s.map("Treeview", background=[("selected", c["sel"])])
        s.configure("TProgressbar", background=c["accent"], troughcolor=c["entry"],
                    bordercolor=c["line"], lightcolor=c["accent"], darkcolor=c["accent"],
                    thickness=6)
        s.configure("Horizontal.TScale", background=c["bg"], troughcolor=c["entry"])
        s.configure("Horizontal.TScale.trough", background=c["entry"], troughcolor=c["entry"])
        s.configure("Horizontal.TScale.slider", background=c["accent"],
                    bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"])
        s.configure("Vertical.TScrollbar", background=c["card_hi"], troughcolor=c["bg"],
                    bordercolor=c["bg"], arrowcolor=c["muted"])
        s.configure("TLabelframe", background=c["bg"], bordercolor=c["line"])
        s.configure("TLabelframe.Label", background=c["bg"], foreground=c["muted"],
                    font=self.f.small_bold)


def apply(root: tk.Misc, theme: str = "dark") -> Style:
    """Ustawia tlo okna + zwraca Style (wolaj raz, przy starcie aplikacji)."""
    st = Style(root, theme)
    root.configure(bg=st.c["bg"])
    # OptionNadzy: kazdy nowy Label/Frame dziedziczy kolory bez przekazywania ich
    for opt, key in (("background", "bg"), ("foreground", "text"),
                     ("selectbackground", "sel"), ("selectforeground", "text"),
                     ("highlightbackground", "line")):
        try:
            root.option_add(f"*{opt}", st.c[key], "widgetDefault")
        except tk.TclError:
            pass
    return st
