"""DeskLink GUI - wlasne widgety rysowane na Canvasie.

Dlaczego canvas, a nie ttk? Pod Wine (i na 'surowym' Tk z Windowsa) ttk kiepsko
radzi sobie z pelnym, ciemnym motywem: obramowania i cursor zostaja jasne.
Canvas + jawne kolory daja ten sam efekt na trzech platformach i nie potrzebuja
zadnych obrazkow z dysku (wazne dla .exe z PyInstallera).
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, List, Optional

from .theme import Style


def _parent_bg(master) -> str:
    try:
        return str(master.cget("bg"))
    except (tk.TclError, AttributeError):
        return "#12161C"


def _round_rect(w: float, h: float, ratio: float = 0.34) -> List[float]:
    """Punkty zaokraglonego prostokata dla create_polygon(smooth=True)."""
    r = min(w, h) * ratio
    return [r, 0, w - r, 0, w, r, w, h - r, w - r, h, r, h, 0, h - r, 0, r]


# --------------------------------------------------------------------------- karta
class Card(tk.Frame):
    """Prostokatna 'karta' z 1px obramowaniem - podstawowy blok UI."""

    def __init__(self, master, st: Style, padding: int = 16, accent: bool = False, **kw):
        super().__init__(master, bg=st.c["line"], **kw)
        self.st = st
        fill = st.c["card_hi"] if accent else st.c["card"]
        self.border_inner = tk.Frame(self, bg=fill)
        self.border_inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.inner = tk.Frame(self.border_inner, bg=fill)
        self.inner.pack(fill="both", expand=True, padx=padding, pady=padding)

    def set_accent(self, on: bool) -> None:
        col = self.st.c["card_hi"] if on else self.st.c["card"]
        self.border_inner.configure(bg=col)
        self.inner.configure(bg=col)


class SectionTitle(tk.Frame):
    """Naglowek sekcji: duze litery + cienka linia w prawo."""

    def __init__(self, master, st: Style, text: str, right: Optional[tk.Widget] = None, **kw):
        bg = kw.pop("bg", _parent_bg(master))
        super().__init__(master, bg=bg, **kw)
        self.st = st
        self.label = tk.Label(self, text=text.upper(), bg=bg, fg=st.c["muted"],
                              font=st.f.eyebrow)
        self.label.pack(side="left")
        if right is not None:
            right.pack(side="right")
        self.rule = tk.Frame(self, bg=st.c["line_soft"], height=1)
        self.rule.pack(fill="x", side="left", expand=True, padx=(12, 0), pady=(0, 4))

    def set_bg(self, bg: str) -> None:
        self.configure(bg=bg)
        self.label.configure(bg=bg)


# --------------------------------------------------------------------------- przyciski
class PillButton(tk.Canvas):
    """Zaokraglony przycisk. variant: primary | ghost | danger | quiet."""

    HEIGHTS = {"sm": 30, "md": 38, "lg": 46}

    def __init__(self, master, st: Style, text: str = "", command: Optional[Callable] = None,
                 variant: str = "primary", width: int = 140, size: str = "md",
                 enabled: bool = True, font=None, bg: Optional[str] = None, **kw):
        super().__init__(master, width=width, height=self.HEIGHTS.get(size, 38),
                         bg=bg if bg is not None else _parent_bg(master),
                         highlightthickness=0, bd=0, **kw)
        self.st = st
        self.command = command
        self.variant = variant
        self.text = text
        self.enabled = enabled
        self._hover = False
        self._pressed = False
        self._cw = int(self.cget("width"))
        self._base_w = self._cw
        self._ch = int(self.cget("height"))
        self.font = font or (st.f.small_bold if size == "sm" else st.f.body_bold)
        self._rect = self.create_polygon(*_round_rect(self._cw, self._ch), smooth=True,
                                        splinesteps=16)
        self._label = self.create_text(self._cw // 2, self._ch // 2, text=text, font=self.font)
        self.tag_lower(self._rect)
        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", lambda e: self._state(hover=True))
        self.bind("<Leave>", lambda e: self._state(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._state(pressed=True))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint()

    def _colors(self):
        c = self.st.c
        if not self.enabled:
            return c["card"], c["faint"], c["line_soft"]
        if self.variant == "primary":
            if self._pressed:
                return c["accent_dark"], "#FFFFFF", c["accent_dark"]
            if self._hover:
                return self.st.mix(c["accent"], 1.14), "#FFFFFF", self.st.mix(c["accent"], 1.14)
            return c["accent"], "#FFFFFF", c["accent"]
        if self.variant == "danger":
            if self._pressed:
                return "#A82326", "#FFFFFF", "#A82326"
            if self._hover:
                return self.st.mix(c["danger"], 1.12), "#FFFFFF", self.st.mix(c["danger"], 1.12)
            return c["danger"], "#FFFFFF", c["danger"]
        if self.variant == "quiet":
            fill = c["card_hi"] if (self._hover or self._pressed) else c["card"]
            return fill, c["text"], c["line"]
        fill = c["accent_soft"] if (self._hover or self._pressed) else c["card"]
        edge = c["accent"] if self._hover else c["line"]
        return fill, c["text"], edge

    def _paint(self) -> None:
        fill, fg, outline = self._colors()
        self.itemconfigure(self._rect, fill=fill, outline=outline, width=1)
        self.itemconfigure(self._label, fill=fg, text=self.text)

    def _state(self, hover=None, pressed=None) -> None:
        if hover is not None:
            self._hover = hover
        if pressed is not None:
            self._pressed = pressed
        self._paint()

    def _on_release(self, event) -> None:
        self._state(pressed=False)
        if not self.enabled or not self.command:
            return
        if 0 <= event.x <= self._cw and 0 <= event.y <= self._ch:
            self.command()

    def _on_resize(self, event) -> None:
        if int(event.width) == self._cw and int(event.height) == self._ch:
            return
        self._cw, self._ch = int(event.width), int(event.height)
        self.coords(self._rect, *_round_rect(self._cw, self._ch))
        self.coords(self._label, self._cw // 2, self._ch // 2)

    # ------------------------------------------------------------------ API
    def set_text(self, text: str) -> None:
        """Zmiana napisu; przycisk urosnie, zeby tekst sie nie utnal."""
        self.text = text
        try:
            need = int(self.font.measure(text)) + 30
        except Exception:  # noqa: BLE001 - brak fonta w testach headless
            need = 0
        if need > self._cw:
            self.configure(width=max(self._base_w, need))
        self._paint()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint()

    def invoke(self) -> None:
        if self.enabled and self.command:
            self.command()


class IconButton(PillButton):
    """PillButton do toolbara sesji: krotki napis, wariant 'quiet'."""

    def __init__(self, master, st: Style, text: str, command: Callable, **kw):
        kw.setdefault("variant", "quiet")
        kw.setdefault("width", 88)
        kw.setdefault("size", "sm")
        super().__init__(master, st, text=text, command=command, **kw)


# --------------------------------------------------------------------------- przelacznik
class Toggle(tk.Canvas):
    """Przelacznik ON/OFF (bez etykiety - napis doloz obok)."""

    W, H = 44, 22

    def __init__(self, master, st: Style, command: Optional[Callable] = None,
                 initial: bool = False, bg: Optional[str] = None, **kw):
        super().__init__(master, width=self.W, height=self.H,
                         bg=bg if bg is not None else _parent_bg(master),
                         highlightthickness=0, bd=0, **kw)
        self.st = st
        self.command = command
        self._on = bool(initial)
        self._track = self.create_polygon(*_round_rect(self.W, self.H, 0.5), smooth=True,
                                         splinesteps=16)
        self._dot = self.create_oval(0, 0, 0, 0)
        self.bind("<ButtonRelease-1>", self._toggle)
        self.configure(cursor="hand2")
        self._paint()

    def _paint(self) -> None:
        c = self.st.c
        col = c["accent"] if self._on else c["line"]
        self.itemconfigure(self._track, fill=col, outline=col, width=0)
        d = self.H - 6
        x = 3 + (0 if not self._on else self.W - self.H)
        self.coords(self._dot, x, 3, x + d, 3 + d)
        self.itemconfigure(self._dot, fill="#FFFFFF", outline="#FFFFFF", width=0)

    def _toggle(self, _e=None) -> None:
        self.set(not self._on)
        if self.command:
            self.command()

    def set(self, on: bool) -> None:
        self._on = bool(on)
        self._paint()

    @property
    def on(self) -> bool:
        return self._on


# --------------------------------------------------------------------------- kapki i znak
class StatusChip(tk.Canvas):
    """Kapka statusu: 'online', 'relay offline', 'sesja 12 fps'."""

    TONES = {"ok", "warn", "danger", "muted", "accent", "faint", "text"}

    def __init__(self, master, st: Style, text: str = "", tone: str = "muted",
                 padx: int = 9, bg: Optional[str] = None, **kw):
        super().__init__(master, height=24, bg=bg if bg is not None else _parent_bg(master),
                         highlightthickness=0, bd=0, **kw)
        self.st, self.padx, self.bg = st, padx, (bg if bg is not None else _parent_bg(master))
        self._rect = self.create_polygon(*_round_rect(60, 24), smooth=True, splinesteps=12)
        self._dot = self.create_oval(0, 0, 0, 0)
        self._txt = self.create_text(0, 12, text="", anchor="w", font=st.f.small_bold)
        self.tag_lower(self._rect)
        self.set(text, tone)

    def set(self, text: Optional[str] = None, tone: Optional[str] = None) -> None:
        self.text = text if text is not None else getattr(self, "text", "")
        self.tone = tone if tone in self.TONES else "muted" if tone is None else self.tone
        self._paint()

    def _paint(self) -> None:
        c = self.st.c
        tone = getattr(self, "tone", "muted")
        base = c.get(tone, c["muted"])
        fg = c["text"] if tone in ("danger", "accent") else base
        soft = self.st.mix(base, 0.24 if self.st.theme == "dark" else 0.90)
        h = 24
        self.itemconfigure(self._txt, text=getattr(self, "text", ""), fill=fg)
        bbox = self.bbox(self._txt) or [0, 0, 40, 12]
        w = int(bbox[2] - bbox[0]) + self.padx * 2 + 14
        self.configure(width=w, height=h, bg=self.bg)
        self.coords(self._rect, *_round_rect(w, h, 0.34))
        self.itemconfigure(self._rect, fill=soft, outline=c["line"], width=1)
        cy = h // 2
        self.coords(self._dot, self.padx - 1, cy - 3, self.padx + 5, cy + 3)
        self.itemconfigure(self._dot, fill=base, outline=base, width=0)
        self.coords(self._txt, self.padx + 12, cy)
        self.tag_raise(self._txt)


class LogoMark(tk.Canvas):
    """Znak aplikacji: zaokraglony 'ekran' ze strzalka przesylu - recznie rysowany."""

    def __init__(self, master, st: Style, size: int = 30, bg: Optional[str] = None):
        super().__init__(master, width=size, height=size,
                         bg=bg if bg is not None else _parent_bg(master),
                         highlightthickness=0, bd=0)
        self.st, self.size = st, size
        self._paint()

    def _paint(self) -> None:
        c, s = self.st.c, self.size
        self.delete("all")
        self.create_polygon(*_round_rect(s, s, 0.26), smooth=True, splinesteps=16,
                            fill=c["accent"], outline=c["accent"])
        lw = max(2, s // 11)
        inset = s * 0.28
        self.create_line(inset, s / 2, s - inset, s / 2, fill="#FFFFFF", width=lw,
                         capstyle="round")
        head = s * 0.15
        self.create_line(s - inset - head, s / 2 - head, s - inset, s / 2,
                         s - inset - head, s / 2 + head, fill="#FFFFFF", width=lw,
                         joinstyle="round", capstyle="round")


# --------------------------------------------------------------------------- pola
class Entry(tk.Entry):
    """Entry z placeholderem, ciemne, z akcentowana ramka w focusie."""

    def __init__(self, master, st: Style, placeholder: str = "", **kw):
        kw.setdefault("bg", st.c["entry"])
        kw.setdefault("fg", st.c["text"])
        kw.setdefault("insertbackground", st.c["accent"])
        kw.setdefault("selectbackground", st.c["sel"])
        kw.setdefault("selectforeground", st.c["text"])
        kw.setdefault("relief", "flat")
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightcolor", st.c["accent"])
        kw.setdefault("highlightbackground", st.c["line"])
        kw.setdefault("font", st.f.body)
        kw.setdefault("bd", 0)
        kw.setdefault("insertwidth", 2)
        super().__init__(master, **kw)
        self.st = st
        self.placeholder = placeholder
        self._showing = False
        self._mask = str(kw.get("show", "") or "")
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        if placeholder:
            self._focus_out()

    # ---------------------------------------------------- maska a placeholder
    def configure(self, cnf=None, **kw):
        """'show' trzymamy u siebie: placeholder musi byt czytelny, a nie
        zamaskowany kropkami (inaczej puste pole hasla wyglada na wypelnione)."""
        if kw.get("show") is not None:
            self._mask = str(kw["show"])
            if getattr(self, "_showing", False):
                kw = dict(kw)
                kw["show"] = ""
        if isinstance(cnf, dict) and cnf.get("show") is not None:
            self._mask = str(cnf["show"])
            if getattr(self, "_showing", False):
                cnf = dict(cnf)
                cnf["show"] = ""
        return super().configure(cnf, **kw)

    def __setitem__(self, key, value):
        if key == "show":
            self._mask = str(value or "")
            if self._showing:
                value = ""
        super().__setitem__(key, value)

    def _focus_in(self, _e=None) -> None:
        if self._showing:
            self.delete(0, "end")
            self._showing = False
            super().configure(fg=self.st.c["text"], show=self._mask)

    def _focus_out(self, _e=None) -> None:
        if self.get() == "" or self._showing:
            self._showing = bool(self.placeholder)
            self.delete(0, "end")
            if self.placeholder:
                self.insert(0, self.placeholder)
            super().configure(fg=self.st.c["faint"], show="" if self._showing else self._mask)

    def value(self) -> str:
        return "" if self._showing else self.get().strip()

    def set_value(self, text: str) -> None:
        self._focus_in()
        self.delete(0, "end")
        self.insert(0, text)

    def focus_show(self) -> None:
        self.focus_set()
        self._focus_in()


class CheckRow(tk.Frame):
    """Lastyka + opis (wlasny rendering, bo tk.Checkbutton pod Wine ma jasne tlo)."""

    def __init__(self, master, st: Style, text: str, initial: bool = False,
                 command: Optional[Callable] = None, bg: Optional[str] = None):
        bg = bg if bg is not None else _parent_bg(master)
        super().__init__(master, bg=bg)
        self.st, self.command = st, command
        self._on = bool(initial)
        self.box = tk.Canvas(self, width=18, height=18, bg=bg, highlightthickness=0, bd=0)
        self.box.pack(side="left", pady=2)
        self.label = tk.Label(self, text=text, bg=bg, fg=st.c["text"], font=st.f.body)
        self.label.pack(side="left", padx=(8, 0))
        for w in (self.box, self.label, self):
            w.bind("<ButtonRelease-1>", self._click)
            w.configure(cursor="hand2")
        self._paint()

    def _click(self, _e=None) -> None:
        self.set(not self._on)
        if self.command:
            self.command()

    def set(self, on: bool) -> None:
        self._on = bool(on)
        self._paint()

    @property
    def on(self) -> bool:
        return self._on

    def _paint(self) -> None:
        c = self.st.c
        self.box.delete("all")
        if self._on:
            self.box.create_rectangle(1, 1, 17, 17, fill=c["accent"], outline=c["accent"])
            self.box.create_line(4, 9, 8, 13, 15, 5, fill="#FFFFFF", width=2,
                                 capstyle="round", joinstyle="round")
        else:
            self.box.create_rectangle(1, 1, 17, 17, outline=c["line"], width=1)


class BorderedRow(tk.Frame):
    """Wiersz na liscie (np. ostatnie komputery) z hoverem i klikiem."""

    def __init__(self, master, st: Style, on_click: Optional[Callable] = None,
                 height: int = 60, accent: bool = False):
        super().__init__(master, bg=st.c["line_soft"])
        self.st, self.on_click = st, on_click
        fill = st.c["card_hi"] if accent else st.c["card"]
        self.body = tk.Frame(self, bg=fill, height=height)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self._fill = fill
        for w in (self.body,):
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)
            if on_click:
                w.bind("<ButtonRelease-1>", lambda e: self._click())
                w.configure(cursor="hand2")

    @property
    def inner(self) -> tk.Frame:
        return self.body

    def _enter(self, _e=None) -> None:
        self.body.configure(bg=self.st.c["card_hi"])

    def _leave(self, _e=None) -> None:
        self.body.configure(bg=self._fill)

    def _click(self) -> None:
        if self.on_click:
            self.on_click()


class ScrollColumn(tk.Frame):
    """Pionowa, przewijalna kolumna widgetow (canvas + window)."""

    def __init__(self, master, st: Style, bg: Optional[str] = None, **kw):
        bg = bg if bg is not None else _parent_bg(master)
        super().__init__(master, bg=bg, **kw)
        self.st, self.bg = st, bg
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                takefocus=0, yscrollincrement=16)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.bar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                bg=st.c["card_hi"], troughcolor=bg, relief="flat", bd=0,
                                highlightthickness=0, activebackground=st.c["accent"])
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda e: self._bind_wheel(False))
        self.inner.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.inner.bind("<Leave>", lambda e: self._bind_wheel(False))
        self.canvas.bind("<Button-4>", self._up)
        self.canvas.bind("<Button-5>", self._down)
        self.inner.bind("<Button-4>", self._up)
        self.inner.bind("<Button-5>", self._down)

    def _on_inner(self, _e=None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass

    def _bind_wheel(self, on: bool) -> None:
        seq = "%W"  # bind_all usuwamy dokladnie tym samym identyfikatorem
        try:
            if on:
                self.canvas.bind_all("<MouseWheel>", self._wheel)
            else:
                self.canvas.unbind_all("<MouseWheel>")
        except tk.TclError:  # pragma: no cover
            pass
        del seq

    def _wheel(self, event) -> None:
        if event.num:  # na Windowsie delta = +/-120
            step = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(step, "units")

    def _up(self, _e=None) -> None:
        self.canvas.yview_scroll(-2, "units")

    def _down(self, _e=None) -> None:
        self.canvas.yview_scroll(2, "units")


class Slider(tk.Frame):
    """Skala z etykieta i wartoscia (Slider zamiast ttk.Scale - widac akcent)."""

    def __init__(self, master, st: Style, label: str, from_: float, to: float, value: float,
                 on_change: Optional[Callable] = None, fmt: str = "{:.0f}",
                 bg: Optional[str] = None, width: int = 160, height: int = 30):
        bg = bg if bg is not None else _parent_bg(master)
        super().__init__(master, bg=bg)
        self.st, self.on_change, self.fmt = st, on_change, fmt
        self.lo, self.hi = float(from_), float(to)
        self.value = float(value)
        self._cw, self._ch = width, height
        self.canvas = tk.Canvas(self, width=width, height=height, bg=bg,
                                highlightthickness=0, bd=0)
        self.canvas.pack(side="left")
        self.out = tk.Label(self, text=self.fmt.format(self.value), bg=bg, fg=st.c["text"],
                            font=st.f.small_bold, width=5, anchor="e")
        self.out.pack(side="right", padx=(6, 0))
        self.cap = tk.Label(self, text=label, bg=bg, fg=st.c["muted"], font=st.f.small)
        self.cap.pack(side="left", padx=(0, 8))
        self.canvas.pack_forget()
        self.canvas.pack(side="left")
        self.canvas.bind("<ButtonRelease-1>", self._pick)
        self.canvas.bind("<B1-Motion>", self._pick)
        self.canvas.bind("<Configure>", lambda e: self._paint())
        self._paint()

    def _x(self, v: float) -> float:
        span = self.hi - self.lo or 1.0
        return 6 + (self._cw - 12) * ((v - self.lo) / span)

    def _paint(self) -> None:
        c = self.st.c
        w, h = self._cw, self._ch
        self.canvas.delete("all")
        y = h // 2
        self.canvas.create_rectangle(6, y - 2, w - 6, y + 2, fill=c["line"], outline=c["line"])
        x = self._x(self.value)
        self.canvas.create_rectangle(6, y - 2, x, y + 2, fill=c["accent"], outline=c["accent"])
        self.canvas.create_oval(x - 6, y - 6, x + 6, y + 6, fill="#FFFFFF", outline=c["accent"],
                                width=2)

    def _pick(self, event) -> None:
        span = self.hi - self.lo
        ratio = max(0.0, min(1.0, (event.x - 6) / max(1.0, self._cw - 12)))
        self.set(self.lo + span * ratio, notify=True)

    def set(self, value: float, notify: bool = False) -> None:
        v = max(self.lo, min(self.hi, float(value)))
        if v == self.value and not notify:
            return
        self.value = v
        self.out.configure(text=self.fmt.format(v))
        self._paint()
        if notify and self.on_change:
            self.on_change(v)
