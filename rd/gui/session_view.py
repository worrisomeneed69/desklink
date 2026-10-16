"""DeskLink GUI - okna sesji.

Dwa widoki, bo role sa rozne jak w AnyDesk:
  * RemoteWindow    - patrzamy i sterujemy cudzym ekranem (kontroler),
  * SharingWindow  - udostepniamy wlasny ekran (gosc/host) i pilnujemy zgody.

Okna nie czytaja gniazda - dostaja klatki z Session (pull_image) i zdarzenia
przekazywane przez glowna petle aplikacji, wiec Tkinter jest jednowatkowy.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import filedialog
from typing import Callable, List, Optional

from .render import FrameView
from .widgets import IconButton, PillButton, Slider, StatusChip, Toggle
from .theme import Style

WYL_ON = "Sterowanie: WŁĄCZONE"
WYL_OFF = "Sterowanie: WYŁĄCZONE"


_PRESS_TYPES = {tk.EventType.KeyPress, "KeyPress"}


def _is_press(event) -> bool:
    """Czy to naciśniecie, czy puszczenie klawisza.

    Tk zwraca 'event.type' jako tkinter.EventType (Enum), a nie tekst - dawne
    'event.type == tk.KeyPress' nie istnieje i wywolywalo AttributeError w
    srodku handlera, przez co klawiatura w ogole nie leciala w dal.
    """
    kind = getattr(event, "type", None)
    if kind is None:
        return True
    return kind in _PRESS_TYPES or getattr(kind, "name", None) == "KeyPress"


def _fmt_hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _fmt_kb(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1048576:.1f} MB"


class BaseSessionWindow(tk.Toplevel):
    """Wspolna ramka: pasek gorny, stopka ze statystykami, pump zdarzen sesji."""

    def __init__(self, app, st: Style, session, title: str, size=(980, 640)):
        super().__init__(app.root)
        self.app = app
        self.st = st
        self.session = session
        self.closed_by_user = False
        self.configure(bg=st.c["bg"])
        # DESKLINK_TITLE ladowany jest do tytulow okien sesji po to, zeby testy QA
        # (i czlowiek z dwiema instancjami na ekranie) wiedzialy, ktore okno do
        # ktorego procesu nalezy.
        tag = str(getattr(app, "title_tag", "") or "").strip()
        self.title(f"{title}  [{tag}]" if tag else title)
        w, h = size
        self.geometry(f"{w}x{h}+{int(w / 8)}+{int(h / 12)}")
        self.minsize(560, 380)
        self.attributes("-topmost", False)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.top = tk.Frame(self, bg=st.c["panel"], height=52)
        self.top.pack(fill="x", side="top")
        self.top.pack_propagate(False)
        self.bottom = tk.Frame(self, bg=st.c["panel"], height=30)
        self.bottom.pack(fill="x", side="bottom")
        self.bottom.pack_propagate(False)
        self.mid = tk.Frame(self, bg=st.c["canvas"])
        self.mid.pack(fill="both", expand=True)

        peer = session.peer
        self._peer_sig = None
        self.peer_name = tk.Label(self.top, text="",
                                  bg=st.c["panel"], fg=st.c["text"], font=st.f.machine)
        self.peer_name.pack(side="left", padx=(16, 6), pady=10)
        self.peer_id = tk.Label(self.top,
                                text="",
                                bg=st.c["panel"], fg=st.c["muted"], font=st.f.mono_id)
        self.peer_id.pack(side="left", pady=10)
        self.platform = tk.Label(self.top, text="", bg=st.c["panel"],
                                 fg=st.c["faint"], font=st.f.small)
        self.platform.pack(side="left", padx=10, pady=10)
        self.set_peer(peer)

        self.chip = StatusChip(self.bottom, st, "polaczenie...", "muted")
        self.chip.pack(side="left", padx=(12, 0), pady=3)
        self.stats_label = tk.Label(self.bottom, text="", bg=st.c["panel"], fg=st.c["muted"],
                                    font=st.f.mono_small)
        self.stats_label.pack(side="left", padx=14, pady=3)
        self.toast_label = tk.Label(self.bottom, text="", bg=st.c["panel"], fg=st.c["faint"],
                                    font=st.f.small)
        self.toast_label.pack(side="right", padx=14, pady=3)
        self._toast_until = 0.0

        self._t0 = time.time()
        self._last_frame_gen = -1
        self._last_canvas = (0, 0)

    # ------------------------------------------------------------------ UI
    def toast(self, text: str, hold: float = 3.0) -> None:
        self.toast_label.configure(text=text)
        self._toast_until = time.monotonic() + hold

    def set_peer(self, peer) -> None:
        """Pasek gorny: nazwa/ID/platforma drugiej strony. Dostajemy je dopiero
        w rekopiesci, a okno powstaja minute wczesniej - stad odswiezanie."""
        try:
            sig = (str(getattr(peer, "name", "") or ""), int(getattr(peer, "desk_id", 0) or 0),
                   str(getattr(peer, "platform", "") or ""))
        except (TypeError, ValueError):
            return
        if sig == self._peer_sig:
            return
        self._peer_sig = sig
        name, desk_id, platform = sig
        self.peer_name.configure(text=name or "zdalny pulpit")
        self.peer_id.configure(text=f"ID {desk_id}" if desk_id else "ID ?")
        self.platform.configure(text=platform)

    def tick(self) -> None:
        """Wywolywane z petli aplikacji ~30 Hz."""
        self.set_peer(self.session.peer)
        if self._toast_until and time.monotonic() > self._toast_until:
            self.toast_label.configure(text="")
            self._toast_until = 0.0

    def update_stats(self, stats: dict) -> None:
        rtt = float(stats.get("rtt_ms") or 0)
        # Pokazujemy to, co dla tej strony ma sens: kontroler odbiera (in),
        # host nadaje (out). Wczesniej controler zawsze widzial 0 kb/s.
        incoming = getattr(self.session, "role", "") == "controller"
        fps = float(stats.get("fps_in" if incoming else "fps_out") or 0.0)
        if not fps:
            fps = float(stats.get("local_fps") or 0.0)
        kbps = float(stats.get("kbps_in" if incoming else "kbps_out") or 0.0)
        if not kbps:
            kbps = float(stats.get("kbps_out" if incoming else "kbps_in") or 0.0)
        q = int(stats.get("quality") or 0)
        self.stats_label.configure(
            text=(f"{_fmt_hms(time.time() - self._t0)}   fps {fps:.1f}   "
                  f"rtt {rtt:.0f} ms   {kbps:.0f} kb/s"
                  + (f"   jakosc {q}" if q else "")))

    def on_close(self) -> None:
        self.close_session("user")

    def close_session(self, reason: str) -> None:
        if not self.closed_by_user:
            self.closed_by_user = True
            try:
                self.session.stop(reason)
            except Exception:  # noqa: BLE001
                pass
        # Zamkniecie zgłaszamy przez okno główne, a nie tylko self.destroy():
        # 'active_window' i przyciski wracają do stanu „bez sesji" natychmiast.
        # Wcześniej app czekał na zdarzenie session_closed z watku sesji — przy
        # wolniejszym hoście (Wine) spozniało sie o sekundy i okno wisialo jako
        # „aktywne", choc juz zniszczone (a 'Polacz' zostal wylaczony).
        forget = getattr(self.app, "session_window_closed", None)
        if callable(forget):
            forget(self, reason)
        else:
            self.destroy()

    # ------------------------------------------------------------------ zdarzenia
    def on_event(self, kind: str, data) -> bool:
        """True - zdarzenie zuzyte przez to okno."""
        if kind == "stats":
            self.update_stats(data or {})
            return True
        if kind == "grant":
            self.on_grant(data or {})
            return True
        if kind == "clipboard":
            self.on_remote_clipboard(str(data or ""))
            return True
        if kind == "clipboard_get":
            self.offer_local_clipboard()
            return True
        if kind == "file_started":
            self.toast(f"plik: {data.get('name')} ({_fmt_kb(int(data.get('size') or 0))})")
            return True
        if kind == "file_progress":
            return True
        if kind == "file_done":
            self.toast(f"zapisano: {data.get('path')}")
            return True
        if kind == "closed":
            reason = str((data or {}).get("reason") or "polaczenie zamkniete")
            self.app.session_window_closed(self, reason)
            return True
        if kind == "log":
            self.app.log(str(data))
            return True
        return False

    def on_grant(self, obj: dict) -> None:  # pragma: no cover - nadpisywane
        pass

    def on_remote_clipboard(self, text: str) -> None:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- kontroler
class RemoteWindow(BaseSessionWindow):
    """Podglad + sterowanie zdalnym ekranem."""

    def __init__(self, app, st: Style, session):
        super().__init__(app, st, session, "DeskLink - polaczenie zdalne", size=(1040, 680))
        self.view = FrameView(self)
        self._down_btn: Optional[str] = None
        self._held: set = set()
        self._mods: set = set()
        self._last_move = 0.0
        self._pointer = None
        self._fit = True
        self._full = False
        self._modal = 0

        # toolbar
        self.tools = tk.Frame(self.top, bg=st.c["panel"])
        self.tools.pack(side="right", padx=(0, 10))
        self.btn_control = self._tool("Sterowanie", self.toggle_control)
        self.btn_clip = self._tool("Schowek", self.push_clipboard)
        self.btn_file = self._tool("Plik...", self.send_file)
        self.btn_monitor = self._tool("Monitor", self.cycle_monitor)
        self.btn_quality = self._tool("Jakosc", self.quality_menu)
        self.btn_full = self._tool("Pelny ekran", self.toggle_fullscreen)
        self.btn_disconnect = PillButton(self.top, st, "Rozlacz", self.close_session_request,
                                         variant="danger", width=104, size="sm",
                                         bg=st.c["panel"])
        self.btn_disconnect.pack(side="right", padx=(10, 0))

        # canvas zdalnego ekranu
        self.canvas = tk.Canvas(self.mid, bg=st.c["canvas"], highlightthickness=0, bd=0,
                               takefocus=1)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw(force=True))
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
        self._img_item = self.canvas.create_image(0, 0, anchor="nw")
        self._marker = self.canvas.create_oval(0, 0, 0, 0, state="hidden", outline="", width=0)
        self._overlay = self.canvas.create_text(0, 0, anchor="center", fill=st.c["muted"],
                                                font=st.f.title, text="")
        self._last_size = (0, 0)

        # mysz
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", lambda e: self._on_button(e, "left", "down"))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._on_button(e, "left", "up"))
        self.canvas.bind("<ButtonPress-2>", lambda e: self._on_button(e, "middle", "down"))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self._on_button(e, "middle", "up"))
        self.canvas.bind("<ButtonPress-3>", lambda e: self._on_button(e, "right", "down"))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._on_button(e, "right", "up"))
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -1))
        self.canvas.bind("<MouseWheel>", self._on_wheel_win)
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())

        # klawiatura: okno przechwytuje znaki, o ile nie mamy otwartego dialogu
        for seq in ("<KeyPress>", "<KeyRelease>"):
            self.bind(seq, self._on_key, add="+")
        self.bind("<FocusIn>", lambda e: self.canvas.focus_set())

        self._monitors: List[dict] = []
        self._mon_index = 0
        self._hint_state: Optional[bool] = None
        self._render_hint()

    def _tool(self, text: str, command: Callable) -> IconButton:
        b = IconButton(self.tools, self.st, text, command, width=92)
        b.pack(side="left", padx=3)
        return b

    # ------------------------------------------------------------------ rysowanie
    def tick(self) -> None:
        super().tick()
        self._sync_hint()
        self._redraw()

    def _redraw(self, force: bool = False) -> None:
        img, gen = self.session.pull_image()
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 20 or ch < 20:
            return
        resized = (cw, ch) != self._last_size
        if resized:
            self._last_size = (cw, ch)
        if img is None:
            if force or resized:
                self.canvas.coords(self._overlay, cw // 2, ch // 2)
                self.canvas.itemconfigure(self._overlay,
                                          text="Oczekiwanie na obraz z drugiej strony...")
            return
        self.canvas.itemconfigure(self._overlay, text="")
        if not force and gen == self._last_frame_gen and not resized:
            return
        self._last_frame_gen = gen
        photo = self.view.update(img, cw, ch, keep_aspect=self._fit)
        if photo is None:
            return
        iw, ih = self.view.drawn_size
        x = (cw - iw) // 2
        y = (ch - ih) // 2
        self.canvas.coords(self._img_item, x, y)
        self.canvas.itemconfigure(self._img_item, image=photo)
        self.canvas.tag_lower(self._img_item, self._marker)
        self._draw_marker()

    def _draw_marker(self) -> None:
        """Krazek w miejscu, ktore widzi zdalny pulpit - pomocne przy lagach."""
        if self._pointer is None or not self.session.granted_input:
            self.canvas.itemconfigure(self._marker, state="hidden")
            return
        fx, fy = self._pointer
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        iw, ih = self.view.drawn_size
        x = (cw - iw) / 2 + fx * iw
        y = (ch - ih) / 2 + fy * ih
        r = 9
        self.canvas.coords(self._marker, x - r, y - r, x + r, y + r)
        self.canvas.itemconfigure(self._marker, state="normal",
                                  outline=self.st.c["accent"], width=2)

    def _render_hint(self) -> None:
        self.btn_control.set_text(WYL_ON if self.session.granted_input else WYL_OFF)
        self.btn_control.set_enabled(True)
        self._hint_state = bool(self.session.granted_input)

    def _sync_hint(self) -> None:
        """Ekran okna powstaje przy 'session_started', a zgoda na sterowanie wchodzi
        minute pozniej. Bez tego przycisk pokazywal 'WYLACZONE' przy aktywnym pilocie."""
        if self._hint_state != bool(self.session.granted_input):
            self._render_hint()
            self._draw_marker()

    # ------------------------------------------------------------------ mapa pozycji
    def _frac(self, x: int, y: int):
        iw, ih = self.view.drawn_size
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if not iw or not ih:
            return None
        ox, oy = (cw - iw) / 2, (ch - ih) / 2
        if not self._fit:
            ox = oy = 0
            iw, ih = cw, ch
        fx = (x - ox) / iw
        fy = (y - oy) / ih
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            return None
        return max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy))

    # ------------------------------------------------------------------ mysz
    def _on_motion(self, event) -> None:
        frac = self._frac(event.x, event.y)
        if frac is None:
            self._pointer = None
            self._draw_marker()
            return
        self._pointer = frac
        now = time.monotonic()
        if now - self._last_move < 0.012:  # ~80 Hz wiecej niz wystarczy
            return
        self._last_move = now
        self.session.send_mouse(*frac)
        self._draw_marker()

    def _on_button(self, event, name: str, action: str) -> None:
        frac = self._frac(event.x, event.y) or self._pointer or (0.5, 0.5)
        self.session.send_mouse(frac[0], frac[1], btn=name,
                                act="press" if action == "down" else "release")
        if action == "down":
            self._down_btn = name
        else:
            self._down_btn = None

    def _on_double(self, event) -> None:
        frac = self._frac(event.x, event.y) or self._pointer or (0.5, 0.5)
        self.session.send_mouse(frac[0], frac[1], btn="left", act="press", n=2)
        self.session.send_mouse(frac[0], frac[1], btn="left", act="release")

    def _on_wheel(self, event, direction: int) -> None:
        frac = self._frac(event.x, event.y) or self._pointer or (0.5, 0.5)
        self.session.send_scroll(0.0, -float(direction), frac[0], frac[1])

    def _on_wheel_win(self, event) -> None:
        steps = int(event.delta / 120) or (-1 if event.delta < 0 else 1)
        self._on_wheel(event, steps)

    # ------------------------------------------------------------------ klawiatura
    def _on_key(self, event) -> None:
        if self._modal:  # otwarty dialog (haslo, plik) - nie wysylamy nic w dal
            return
        if event.widget is not self.canvas and isinstance(event.widget, tk.Entry):
            return
        press = _is_press(event)
        from ..keys import key_from_tk  # lokalny import: keys.py nie wymaga Tk
        canon, char = key_from_tk(event.keysym)
        if canon is None and not char and event.char and event.char.isprintable():
            char = event.char[:1]
        if not self.session.granted_input:
            return
        if canon in ("shift", "ctrl", "alt", "super", "altgr"):
            if press:
                self._mods.add(canon)
                self.session.send_key(canon, "down")
            else:
                self._mods.discard(canon)
                self.session.send_key(canon, "up")
            return
        combo_mods = {m for m in self._mods if m != "shift"}
        if press and combo_mods and self._mods:
            parts = sorted(self._mods) + [canon or (char or "").lower()]
            self.session.send_key(None, combo="+".join([p for p in parts if p]))
            return
        key_id = canon or char
        if key_id is None:
            return
        if press:
            if key_id in self._held:  # auto-repeat z X/Windowsa
                return
            self._held.add(key_id)
            if char:
                self.session.send_key(canon, "press", char=char)
            else:
                self.session.send_key(canon, "down")
        else:
            self._held.discard(key_id)
            if not char:
                self.session.send_key(canon, "up")

    # ------------------------------------------------------------------ akcje toolbara
    def toggle_control(self) -> None:
        if self.session.granted_input:
            self.session.set_input(False)
        else:
            self.session.set_input(True)

    def on_grant(self, obj: dict) -> None:
        self._render_hint()
        self._draw_marker()
        if not obj.get("input"):
            self._held.clear()
            self._mods.clear()
        self.toast("sterowanie przejęte" if obj.get("input") else "sterowanie zwrócone hostowi")

    def push_clipboard(self) -> None:
        try:
            text = self.app.root.clipboard_get()
        except tk.TclError:
            self.toast("lokalny schowek pusty")
            return
        self.session.copy_clipboard(text)
        self.toast(f"schowek wyslany ({len(text)} zn.)")

    def offer_local_clipboard(self) -> None:
        if not self.app.cfg.clipboard_sync:
            return
        try:
            text = self.app.root.clipboard_get()
        except tk.TclError:
            return
        if text:
            self.session.copy_clipboard(text)

    def on_remote_clipboard(self, text: str) -> None:
        if not text:
            return
        self.toast(f"schowek ze zdalnego pulpitu: {text[:38]}{'...' if len(text) > 38 else ''}")

    def send_file(self) -> None:
        self._modal_guard(True)
        try:
            path = filedialog.askopenfilename(parent=self, title="Wyslij plik")
        finally:
            self._modal_guard(False)
        if not path:
            return
        if self.session.send_file(path):
            self.toast(f"wysylam {path.rsplit('/', 1)[-1]}")

    def on_event(self, kind: str, data) -> bool:
        if kind == "screen_info":
            self.set_monitors((data or {}).get("monitors") or [])
            self.toast("dostalem informacje o ekranie")
            return True
        return super().on_event(kind, data)

    def cycle_monitor(self) -> None:
        if not self._monitors:
            self.toast("druga strona ma jeden monitor")
            return
        self._mon_index = (self._mon_index + 1) % len(self._monitors)
        mon = self._monitors[self._mon_index]
        self.session.select_monitor(int(mon.get("i") or self._mon_index))
        self.toast(f"monitor {mon.get('i')}: {mon.get('w')}x{mon.get('h')}")

    def set_monitors(self, monitors: List[dict]) -> None:
        self._monitors = list(monitors or [])
        self._mon_index = 0

    def quality_menu(self) -> None:
        menu = tk.Menu(self, tearoff=0, bg=self.st.c["card"], fg=self.st.c["text"],
                       activebackground=self.st.c["accent"], activeforeground="#FFFFFF",
                       relief="flat", bd=0, font=self.st.f.body)
        presets = [("Szybko (niska)", 18, 40), ("Zrownowazona", 24, 65),
                   ("Wysoka", 30, 85), ("Bardzo wysoka", 40, 95)]
        for label, fps, q in presets:
            menu.add_command(label=label, command=lambda f=fps, qq=q: self._apply_quality(f, qq))
        menu.add_separator()
        menu.add_command(label="Dopasuj / wypelnij", command=self.toggle_fit)
        menu.tk_popup(self.btn_quality.winfo_rootx(), self.btn_quality.winfo_rooty() + 30)

    def _apply_quality(self, fps: int, quality: int) -> None:
        self.session.set_stream_params(fps=fps, quality=quality)
        self.toast(f"jakosc: {fps} fps / {quality}")

    def toggle_fit(self) -> None:
        self._fit = not self._fit
        self._redraw(force=True)

    def toggle_fullscreen(self) -> None:
        self._full = not self._full
        try:
            self.attributes("-fullscreen", self._full)
        except tk.TclError:
            pass
        if not self._full:
            self.geometry("1040x680")
        self.canvas.focus_set()

    def close_session_request(self) -> None:
        self.close_session("user")

    def _modal_guard(self, on: bool) -> None:
        self._modal += 1 if on else -1
        if self._modal < 0:
            self._modal = 0


# --------------------------------------------------------------------------- host
class SharingWindow(BaseSessionWindow):
    """Mini-panel udostepniania: kto patrzy, co widzi, i szybkie hamulec."""

    def __init__(self, app, st: Style, session):
        super().__init__(app, st, session, "DeskLink - udostepniasz ekran", size=(520, 460))
        self.mid.configure(bg=st.c["bg"])
        self.resizable(False, False)

        head = tk.Frame(self.mid, bg=st.c["bg"])
        head.pack(fill="x", padx=18, pady=(16, 6))
        tk.Label(head, text="TWOJ EKRAN JEST UDOSTEPNIANY", bg=st.c["bg"],
                 fg=st.c["warn"], font=st.f.small_bold).pack(anchor="w")
        self.who = tk.Label(head, text="", bg=st.c["bg"], fg=st.c["text"], font=st.f.title)
        self.who.pack(anchor="w", pady=(4, 0))
        self.fp = tk.Label(head, text="", bg=st.c["bg"], fg=st.c["faint"], font=st.f.mono_small)
        self.fp.pack(anchor="w")
        self._peer_info(session.peer)

        box = tk.Frame(self.mid, bg=st.c["card"], highlightbackground=st.c["line"],
                       highlightthickness=1)
        box.pack(fill="x", padx=18, pady=10)

        self.sw_share = self._switch(box, "Udostepniaj obraz ekranu", session.share_screen,
                                     self._on_share)
        self.sw_input = self._switch(box, "Zezwal na sterowanie mysza i klawiatura",
                                     session.peer_controls_me, self._on_input_switch)
        self.sw_lock = self._switch(box, "Zablokuj te maszynie wejście lokalne",
                                    bool(self.app.cfg.auto_lock_local), self._on_lock)
        if not self.app.cfg.allow_input:
            self.sw_input.set(False)

        self.slider_fps = Slider(box, st, "fps", 3, 60, float(self.app.cfg.fps),
                                 lambda v: self._params(fps=int(v)), fmt="{:.0f}")
        self.slider_q = Slider(box, st, "jakosc", 20, 95, float(self.app.cfg.quality),
                               lambda v: self._params(quality=int(v)), fmt="{:.0f}")
        self.slider_fps.pack(fill="x", pady=(10, 0))
        self.slider_q.pack(fill="x", pady=(2, 6))

        row = tk.Frame(box, bg=st.c["card"])
        row.pack(fill="x", pady=(6, 2))
        self.btn_file = PillButton(row, st, "Odbierz plik...", self._open_downloads,
                                   variant="ghost", width=150, size="sm", bg=st.c["card"])
        self.btn_file.pack(side="left")
        self.btn_monitor = PillButton(row, st, "Monitor", self.cycle_monitor,
                                      variant="ghost", width=110, size="sm", bg=st.c["card"])
        self.btn_monitor.pack(side="left", padx=8)
        self.btn_stop = PillButton(row, st, "ROZLACZ", self.close_session_request,
                                   variant="danger", width=120, size="sm", bg=st.c["card"])
        self.btn_stop.pack(side="right")

        self.note = tk.Label(self.mid, text="", bg=st.c["bg"], fg=st.c["muted"],
                             font=st.f.small, justify="left", wraplength=460)
        self.note.pack(fill="x", padx=18, pady=(4, 12), anchor="w")

    # ------------------------------------------------------------------ UI
    def _peer_info(self, peer) -> None:
        self.who.configure(text=peer.name or "pilot")
        self.fp.configure(text=f"ID {peer.desk_id or '?'}   odcisk {peer.fp or 'nieznany'}")

    def _switch(self, parent, label: str, initial: bool, command: Callable) -> Toggle:
        row = tk.Frame(parent, bg=st_bg(parent))
        row.pack(fill="x", pady=6)
        sw = Toggle(row, self.st, initial=initial, command=command, bg=st_bg(parent))
        sw.pack(side="right")
        txt = tk.Label(row, text=label, bg=st_bg(parent), fg=self.st.c["text"], font=self.st.f.body)
        txt.pack(side="left")
        return sw

    def _on_share(self) -> None:
        self.session.set_sharing(self.sw_share.on)

    def _on_input_switch(self) -> None:
        want = self.sw_input.on
        self.session.set_input(want)
        if want and not self.session.peer_controls_me:
            self.toast("sterowanie zablokowane w Ustawieniach (zakladka Dostep)")

    def _sync_switches(self) -> None:
        """Przelaczniki pokazuja stan sesji, nie zyczenia. Okno potrafi powstac
        zanim host zdazy przyznac dostep (przyjecie bez pytania), wiec uzupelniamy
        to w kazdym tyku - inaczej panel klamie 'brak sterowania' przy aktywnym pilocie."""
        s = self.session
        if self.sw_share.on != bool(s.share_screen):
            self.sw_share.set(bool(s.share_screen))
        if self.sw_input.on != bool(s.peer_controls_me):
            self.sw_input.set(bool(s.peer_controls_me))

    def _on_lock(self) -> None:
        self.app.cfg.auto_lock_local = self.sw_lock.on
        self.app.store.flush(force=True)
        try:
            if self.sw_lock.on:
                self.session.blocker.grab()
            else:
                self.session.blocker.ungrab()
        except Exception as exc:  # noqa: BLE001
            self.app.log(f"blokada lokalna: {exc}")

    def _params(self, fps: Optional[int] = None, quality: Optional[int] = None) -> None:
        self.session.set_stream_params(fps=fps, quality=quality)
        if fps is not None:
            self.app.cfg.fps = int(fps)
        if quality is not None:
            self.app.cfg.quality = int(quality)
        self.app.store.tick()

    def cycle_monitor(self) -> None:
        mons = self.session.local_monitors()
        if len(mons) < 2:
            self.toast("masz jeden monitor")
            return
        cur = int(getattr(self.session.source, "monitor_index", 0) or 0)
        idx = [int(m["i"]) for m in mons].index(cur) + 1 if cur in [int(m["i"]) for m in mons] else 0
        idx %= len(mons)
        self.session.set_monitor(int(mons[idx]["i"]))
        self.toast(f"monitor {mons[idx]['i']}: {mons[idx]['w']}x{mons[idx]['h']}")

    def _open_downloads(self) -> None:
        from ..settings import cache_dir

        path = cache_dir() / "downloads"
        self.toast(f"pliki trafiaja do {path}")
        try:
            import subprocess
            import sys

            if sys.platform.startswith("win") or path.exists():
                web = str(path)
                subprocess.Popen(["xdg-open", web] if not sys.platform.startswith("win")
                                 else ["explorer", web], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ zdarzenia
    def on_grant(self, obj: dict) -> None:
        self.sw_input.set(bool(obj.get("input")))

    def on_event(self, kind: str, data) -> bool:
        if kind == "input_request":
            peer = (data or {}).get("peer") or {}
            self.app.ask_control(data or {}, self.session)
            return True
        if kind == "sharing":
            self.sw_share.set(bool((data or {}).get("on")))
            return True
        return super().on_event(kind, data)

    def tick(self) -> None:
        super().tick()
        self._sync_switches()
        st = self.session.stats
        self.note.configure(
            text=("Nadawanie: %.1f fps   %.0f kb/s   zgubione klatki: %d   "
                  "pilot steruje: %s"
                  % (float(st.get("fps_out", 0.0)), float(st.get("kbps_out", 0.0)),
                     int(st.get("dropped", 0)),
                     "tak" if self.session.peer_controls_me else "nie")))

    def close_session_request(self) -> None:
        self.close_session("user")


def st_bg(widget) -> str:
    try:
        return str(widget.cget("bg"))
    except (tk.TclError, AttributeError):
        return "#1E252F"
