"""DeskLink GUI - okno glowne w stylu AnyDesk.

Jedna petla Tk (30 Hz) odbiera zdarzenia z kolejki Agent/Session i aktualizuje
UI. Watki sieciowe NIGDY nie dotykaja widgetow - tylko wkladaja zdarzenia do
kolejki, czego pilnuje `Agent.emit`.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Callable, List, Optional

from .. import net
from ..agent import Agent
from ..capture import SyntheticSource  # noqa: F401  (przydatne w trybie demo)
from ..osutil import IS_WINDOWS, describe, enable_dpi_awareness, running_under_wine
from ..settings import ConfigStore, format_id, looks_like_address, parse_id
from . import theme as theme_mod
from .session_view import RemoteWindow, SharingWindow
from .widgets import (BorderedRow, Card, CheckRow, Entry, LogoMark, PillButton,
                      ScrollColumn, SectionTitle, StatusChip, Toggle)

DIAL_TIMEOUT = 45.0


def human_ts(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------- dialog pomocniczy
class Dialog(tk.Toplevel):
    """Modalna ramka z pytaniem - uzywana przy zgodzie, hasle i odcisku klucza."""

    def __init__(self, app: "MainApp", title: str, width: int = 460, height: int = 0,
                 parent: Optional[tk.Misc] = None):
        st = app.st
        host = parent or app.root
        super().__init__(host)
        self.app = app
        self.st = st
        self.result = None
        self._counted = False
        self.title(title)
        self.configure(bg=st.c["bg"])
        self.resizable(False, False)
        # Uwaga: '560x0' NIE znaczy w Tk 'dopasuj wysokosc'. Okno dostaje wtedy ~38 px
        # i cala tresc jest ucinana (dialog zgody byl jedna kreska). Gdy height == 0,
        # w ogole nie podajemy rozmiaru, a szerokosc kotwicymy niewidocznym paskiem.
        if height:
            self.geometry(f"{width}x{height}")
        tk.Frame(self, bg=st.c["bg"], width=width, height=1).pack(fill="x")
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.transient(host)
        self.protocol("WM_DELETE_WINDOW", self._reject)
        self.bind("<Destroy>", self._release_modal)
        self.app._modal_depth = getattr(self.app, "_modal_depth", 0) + 1
        self.head = tk.Label(self, text=title, bg=st.c["bg"], fg=st.c["text"],
                             font=st.f.title, anchor="w")
        self.head.pack(fill="x", padx=22, pady=(20, 4))
        self.body = tk.Frame(self, bg=st.c["bg"])
        self.body.pack(fill="both", expand=True, padx=22)
        self.foot = tk.Frame(self, bg=st.c["bg"])
        self.foot.pack(fill="x", padx=22, pady=(14, 18))
        tk.Frame(self, bg=st.c["line"], height=1).pack(fill="x", padx=22, pady=(16, 0))

    def text(self, line: str, color: str = "muted", font=None, wrap: int = 420) -> tk.Label:
        lbl = tk.Label(self.body, text=line, bg=self.st.c["bg"], fg=self.st.c[color],
                       font=font or self.st.f.body, justify="left", wraplength=wrap, anchor="w")
        lbl.pack(fill="x", pady=(0, 6))
        return lbl

    def buttons(self, items: List[tuple]) -> None:
        for label, cb, variant in reversed(items):
            b = PillButton(self.foot, self.st, label, cb, variant=variant, width=140, size="md",
                           bg=self.st.c["bg"])
            b.pack(side="right", padx=(8, 0))

    def _reject(self) -> None:
        self.result = None
        self.destroy()

    def _release_modal(self, _e=None) -> None:
        if not self._counted:
            self._counted = True
            self.app._modal_depth = max(0, getattr(self.app, "_modal_depth", 1) - 1)

    def wait(self):
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_force()
        self.wait_window()
        return self.result


# --------------------------------------------------------------------------- okno glowne
class MainApp:
    def __init__(self, store: Optional[ConfigStore] = None, agent: Optional[Agent] = None,
                 demo: bool = False):
        self.demo = demo
        dpi = enable_dpi_awareness()  # Windows: bez tego mysz trafia obok
        self.store = store or ConfigStore()
        self.cfg = self.store.config
        self.agent = agent or Agent(self.store)

        self.root = tk.Tk(className="DeskLink")
        self.title_tag = os.environ.get("DESKLINK_TITLE", "").strip()
        self.root.title("DeskLink - zdalny pulpit" + (f"  [{self.title_tag}]" if self.title_tag else ""))
        self.st = theme_mod.apply(self.root, self.cfg.theme)
        w, h, x, y = (self.cfg.window + [1080, 700, 90, 70])[:4]
        self.root.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
        self.root.minsize(900, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.windows: List[tk.Misc] = []
        self.active_window = None
        self.log_lines: List[str] = []
        self._ui_errors: List[str] = []
        self._dial_target = ""
        self._dial_started = 0.0
        self._last_password_prompt = 0.0
        self._modal_depth = 0   # liczba otwartych modalnych dialogow
        self._closing = False
        # Tk zjada wyjatki z callbackow (drukuje je na stderr, ktorego pod .exe
        # nikt nie widzi). Przejmujemy to: polowicznie zbudowane okno bez wpisu
        # w logu wyglada po prostu na 'brzydkie'.
        self.root.report_callback_exception = self._on_ui_error
        self.startup_note = f"DPI: {dpi}" + (" | Wine " + (describe().get("wine") or ""))
        if running_under_wine():
            self.startup_note += " (warstwa Wine)"

        self._build()
        self._start_agent()
        self.root.after(33, self._pump)
        self.root.after(200, self._refresh_status)

    # ------------------------------------------------------------------ bledy UI
    def _on_ui_error(self, exc_type, exc, tb) -> None:
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc, tb))
        tail = str(self.store.path) if getattr(self.store, "path", None) else ""
        try:
            from ..settings import cache_dir

            log = cache_dir() / "ui-error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {tail}\n{text}")
        except OSError:
            pass
        line = f"{exc_type.__name__}: {exc}"
        if line not in self._ui_errors:          # ten sam blad 30x/s to spam
            self._ui_errors.append(line)
        try:
            self.log(f"blad UI: {line}")
        except Exception:  # noqa: BLE001 - log moze padac przy zamkniatym oknie
            pass

    @property
    def ui_errors(self) -> List[str]:
        return list(self._ui_errors)

    # ------------------------------------------------------------------ start agenta
    def _start_agent(self) -> None:
        if self.demo:
            # tryb pokazy/demo: nadajemy generowana animacje, nie prawdziwy ekran
            self.agent.source_override = SyntheticSource(width=1280, height=720, fps=12)
            self.log("tryb demo: nadajemy syntetyczny obraz (1280x720)")
        self.agent.start_direct()
        if self.cfg.relay_host:
            self.agent.start_relay()
        if self.cfg.lan_discovery:
            self.agent.start_lan()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        st = self.st
        c = st.c

        # ---------------- gorny pasek
        bar = tk.Frame(self.root, bg=c["panel"], height=58)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        left = tk.Frame(bar, bg=c["panel"])
        left.pack(side="left", padx=16)
        LogoMark(left, st, size=30, bg=c["panel"]).pack(side="left")
        tk.Label(left, text="DeskLink", bg=c["panel"], fg=c["text"],
                 font=st.f.title).pack(side="left", padx=(9, 0))
        tk.Label(left, text="zdalny pulpit", bg=c["panel"], fg=c["faint"],
                 font=st.f.small).pack(side="left", padx=(8, 0), pady=(3, 0))

        right = tk.Frame(bar, bg=c["panel"])
        right.pack(side="right", padx=16)
        self.chip_relay = StatusChip(right, st, "relay...", "muted", bg=c["panel"])
        self.chip_relay.pack(side="left", padx=(0, 10))
        tk.Label(right, text="Dostępny dla innych", bg=c["panel"], fg=c["muted"],
                 font=st.f.small).pack(side="left", padx=(0, 8))
        self.tgl_available = Toggle(right, st, initial=self.agent.available,
                                    command=self._toggle_available, bg=c["panel"])
        self.tgl_available.pack(side="left", padx=(0, 14))
        PillButton(right, st, "Ustawienia", self.open_settings, variant="ghost",
                   width=112, size="sm", bg=c["panel"]).pack(side="left", padx=(0, 8))
        PillButton(right, st, "O programie", self.open_about, variant="quiet",
                   width=100, size="sm", bg=c["panel"]).pack(side="left")

        # ---------------- stopka slogiem
        self.logbox = tk.Text(self.root, height=4, bg=c["entry"], fg=c["muted"], bd=0,
                              highlightthickness=1, highlightbackground=c["line"],
                              font=st.f.mono_small, insertwidth=0, state="disabled",
                              wrap="word", padx=10, pady=6)
        self.logbox.pack(fill="x", side="bottom", padx=14, pady=(0, 12))

        # ---------------- glowna siatka
        grid = tk.Frame(self.root, bg=c["bg"])
        grid.pack(fill="both", expand=True, padx=14, pady=14)
        grid.columnconfigure(0, weight=10, uniform="col")
        grid.columnconfigure(1, weight=13, uniform="col")
        grid.rowconfigure(0, weight=1)

        self._build_left(grid)
        self._build_right(grid)

    def _build_left(self, parent) -> None:
        st, c = self.st, self.st.c
        col = tk.Frame(parent, bg=c["bg"])
        col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        card = Card(col, st, padding=18)
        card.pack(fill="x")
        inner = card.inner
        SectionTitle(inner, st, "Twój adres").pack(fill="x", pady=(0, 10))

        self.id_value = tk.Label(inner, text="--- --- ---", bg=c["card"], fg=c["text"],
                                font=st.f.id_big, anchor="w")
        self.id_value.pack(fill="x")
        self.id_value.bind("<Double-Button-1>", lambda e: self.copy_id())

        row = tk.Frame(inner, bg=c["card"])
        row.pack(fill="x", pady=(6, 2))
        PillButton(row, st, "Kopiuj ID", self.copy_id, variant="ghost", width=118, size="sm",
                   bg=c["card"]).pack(side="left")
        self.chip_state = StatusChip(row, st, "start...", "muted", bg=c["card"])
        self.chip_state.pack(side="left", padx=(10, 0))

        self.machine_line = tk.Label(inner, text="", bg=c["card"], fg=c["muted"],
                                     font=st.f.body, anchor="w", justify="left",
                                     wraplength=360)
        self.machine_line.pack(fill="x", pady=(10, 2))
        self.fingerprint_line = tk.Label(inner, text="", bg=c["card"], fg=c["faint"],
                                        font=st.f.mono_small, anchor="w", justify="left",
                                        wraplength=360)
        self.fingerprint_line.pack(fill="x")

        # alias
        alias_row = tk.Frame(inner, bg=c["card"])
        alias_row.pack(fill="x", pady=(12, 0))
        tk.Label(alias_row, text="Alias", bg=c["card"], fg=c["muted"],
                 font=st.f.small).pack(side="left", padx=(0, 8))
        self.entry_alias = Entry(alias_row, st, placeholder="np. studio", width=16)
        self.entry_alias.pack(side="left", ipady=3)
        PillButton(alias_row, st, "Ustaw", self.set_alias, variant="quiet", width=90,
                   size="sm", bg=c["card"]).pack(side="left", padx=8)

        # nasluchiwanie bezposrednie + siec lokalna
        card2 = Card(col, st, padding=16)
        card2.pack(fill="x", pady=(12, 0))
        SectionTitle(card2.inner, st, "Polaczenie bezposrednie (ta sama siec)").pack(
            fill="x", pady=(0, 8))
        row2 = tk.Frame(card2.inner, bg=c["card"])
        row2.pack(fill="x")
        tk.Label(row2, text="Nasłuchuj na porcie", bg=c["card"], fg=c["muted"],
                 font=st.f.body).pack(side="left")
        self.entry_port = Entry(row2, st, width=7)
        self.entry_port.set_value(str(self.cfg.listen_port))
        self.entry_port.pack(side="left", padx=(8, 8), ipady=3)
        self.tgl_listen = Toggle(row2, st, initial=self.cfg.listen_enabled,
                                 command=self._toggle_listen, bg=c["card"])
        self.tgl_listen.pack(side="right")
        self.direct_line = tk.Label(card2.inner, text="", bg=c["card"], fg=c["faint"],
                                    font=st.f.mono_small, justify="left", anchor="w",
                                    wraplength=360)
        self.direct_line.pack(fill="x", pady=(8, 0))

        # ostatnie polaczenia
        card3 = Card(col, st, padding=16)
        card3.pack(fill="both", expand=True, pady=(12, 0))
        SectionTitle(card3.inner, st, "Ostatnie komputery").pack(fill="x", pady=(0, 6))
        self.recent_scroll = ScrollColumn(card3.inner, st, bg=c["card"])
        self.recent_scroll.pack(fill="both", expand=True)
        self._render_recent()

    def _build_right(self, parent) -> None:
        st, c = self.st, self.st.c
        col = tk.Frame(parent, bg=c["bg"])
        col.grid(row=0, column=1, sticky="nsew")

        card = Card(col, st, padding=18)
        card.pack(fill="x")
        inner = card.inner
        SectionTitle(inner, st, "Połącz z komputerem").pack(fill="x", pady=(0, 10))
        self.entry_target = Entry(inner, st, placeholder="123 456 789  albo  192.168.1.20:7000")
        self.entry_target.pack(fill="x", ipady=8)
        self.entry_target.bind("<Return>", lambda e: self.connect_now())

        opts = tk.Frame(inner, bg=c["card"])
        opts.pack(fill="x", pady=(10, 0))
        self.chk_view_only = CheckRow(opts, st, "Tylko podgląd (bez sterowania)",
                                      initial=self.cfg.view_only_default)
        self.chk_view_only.pack(anchor="w")

        pw_row = tk.Frame(inner, bg=c["card"])
        pw_row.pack(fill="x", pady=(8, 0))
        tk.Label(pw_row, text="Hasło", bg=c["card"], fg=c["muted"],
                 font=st.f.small).pack(side="left", padx=(0, 8))
        self.entry_password = Entry(pw_row, st, placeholder="jeśli wymaga", show="", width=22)
        self.entry_password.configure(show="•")
        self.entry_password.pack(side="left", ipady=3)
        self.chk_show_pw = CheckRow(pw_row, st, "pokaż", initial=False,
                                    command=self._toggle_pw_visibility)
        self.chk_show_pw.pack(side="left", padx=(8, 0))

        actions = tk.Frame(inner, bg=c["card"])
        actions.pack(fill="x", pady=(14, 0))
        self.btn_connect = PillButton(actions, st, "Połącz", self.connect_now,
                                      variant="primary", width=150, size="lg")
        self.btn_connect.pack(side="left")
        self.btn_cancel = PillButton(actions, st, "Anuluj", self.cancel_dial,
                                     variant="ghost", width=110, size="md", enabled=False)
        self.btn_cancel.pack(side="left", padx=(10, 0))
        self.dial_line = tk.Label(inner, text="", bg=c["card"], fg=c["faint"],
                                  font=st.f.small, anchor="w", justify="left")
        self.dial_line.pack(fill="x", pady=(10, 0))

        # wykryte w sieci lokalnej
        card4 = Card(col, st, padding=16)
        card4.pack(fill="both", expand=True, pady=(12, 0))
        head = tk.Frame(card4.inner, bg=c["card"])
        head.pack(fill="x")
        SectionTitle(head, st, "Znalezione w sieci lokalnej").pack(side="left", fill="x")
        PillButton(head, st, "Skanuj", self.scan_lan, variant="ghost", width=96, size="sm",
                   bg=c["card"]).pack(side="right")
        self.lan_scroll = ScrollColumn(card4.inner, st, bg=c["card"])
        self.lan_scroll.pack(fill="both", expand=True, pady=(8, 0))
        tk.Label(self.lan_scroll.inner, text="'Skanuj' odswieza liste hostow nasluchujacych "
                 "w tej samej podsieci (UDP 51255).",
                 bg=c["card"], fg=c["faint"], font=st.f.small, wraplength=420,
                 justify="left", anchor="w").pack(fill="x", pady=4)

    # ------------------------------------------------------------------ akcje UI
    def copy_id(self) -> None:
        text = format_id(self.cfg.identity.desk_id)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            pass
        self.chip_state.set("skopiowano ID", "ok")
        self.root.after(1800, self._refresh_status)

    def set_alias(self) -> None:
        alias = self.entry_alias.value()
        if not alias:
            return
        self.agent.set_alias(alias)

    def _toggle_pw_visibility(self) -> None:
        self.entry_password.configure(show="" if self.chk_show_pw.on else "•")

    def _toggle_available(self) -> None:
        self.agent.set_available(self.tgl_available.on)
        self._refresh_status()

    def _toggle_listen(self) -> None:
        self.cfg.listen_enabled = self.tgl_listen.on
        try:
            self.cfg.listen_port = int(self.entry_port.value() or self.cfg.listen_port)
        except ValueError:
            pass
        self.store.flush(force=True)
        if self.tgl_listen.on:
            self.agent.start_direct()
        elif self.agent.direct is not None:
            self.agent.direct.stop()
            self.agent.direct = None
        self._refresh_status()

    def connect_now(self) -> None:
        target = self.entry_target.value()
        if not target:
            self.dial_line.configure(text="wpisz ID lub adres IP:port", foreground=self.st.c["warn"])
            return
        self._dial_target = target
        self._dial_started = time.monotonic()
        res = self.agent.connect(target, password=self.entry_password.value(),
                                 view_only=self.chk_view_only.on)
        if not res.get("ok"):
            reason = res.get("reason", "blad")
            self.dial_line.configure(text=str(reason), foreground=self.st.c["danger"])
            return
        self.dial_line.configure(
            text=("dzwonie na %s" % format_id(int(res.get("id") or 0)))
            if res.get("mode") == "relay" else f"lacze sie z {target}",
            foreground=self.st.c["muted"])
        self.btn_connect.set_enabled(False)
        self.btn_cancel.set_enabled(True)

    def cancel_dial(self) -> None:
        self.agent.cancel_dial()
        self._dial_target = ""
        self._dial_started = 0.0
        self.dial_line.configure(text="")
        self.btn_connect.set_enabled(True)
        self.btn_cancel.set_enabled(False)

    def scan_lan(self) -> None:
        for child in list(self.lan_scroll.inner.winfo_children()):
            if getattr(child, "_lan_row", False):
                child.destroy()
        hosts = self.agent.scan_lan()
        if not hosts:
            row = tk.Label(self.lan_scroll.inner,
                           text="Nikogo nie słyszałem - upewnij się, że DeskLink działa "
                                "po drugiej stronie i ma włączone nasłuchiwanie.",
                           bg=self.st.c["card"], fg=self.st.c["faint"], font=self.st.f.small,
                           wraplength=420, justify="left", anchor="w")
            row._lan_row = True  # type: ignore[attr-defined]
            row.pack(fill="x", pady=4)
            return
        for host in hosts:
            self._lan_row(host)

    def _lan_row(self, host) -> None:
        st = self.st
        row = BorderedRow(self.lan_scroll.inner, st,
                         on_click=lambda h=host: self._connect_direct(h), height=54)
        row._lan_row = True  # type: ignore[attr-defined]
        row.pack(fill="x", pady=3)
        body = row.inner
        tk.Label(body, text=host.name, bg=st.c["card"], fg=st.c["text"],
                 font=st.f.body_bold).pack(side="left", padx=12, pady=10)
        tk.Label(body, text=host.addr, bg=st.c["card"], fg=st.c["muted"],
                 font=st.f.mono_small).pack(side="left")
        if host.desk_id:
            tk.Label(body, text=format_id(host.desk_id), bg=st.c["card"], fg=st.c["faint"],
                     font=st.f.mono_small).pack(side="left", padx=(8, 0))
        tk.Label(body, text=host.platform or "", bg=st.c["card"], fg=st.c["faint"],
                 font=st.f.small).pack(side="right", padx=12)

    def _connect_direct(self, host) -> None:
        # LanHost.addr ma już 'ip:port'
        self.entry_target.set_value(host.addr)
        self.connect_now()

    def _render_recent(self) -> None:
        st = self.st
        for child in list(self.recent_scroll.inner.winfo_children()):
            child.destroy()
        if not self.cfg.recent:
            tk.Label(self.recent_scroll.inner,
                     text="Tu pojawią się komputery, z którymi ostatnio sie łączysz.",
                     bg=st.c["card"], fg=st.c["faint"], font=st.f.small, wraplength=340,
                     justify="left", anchor="w").pack(fill="x", pady=6)
            return
        for rec in self.cfg.recent:
            self._recent_row(rec)

    def _recent_row(self, rec: dict) -> None:
        st = self.st
        desk_id = int(rec.get("desk_id") or 0)
        row = BorderedRow(self.recent_scroll.inner, st,
                         on_click=lambda i=desk_id: self._recall(i), height=56)
        row.pack(fill="x", pady=3)
        body = row.inner
        left = tk.Frame(body, bg=st.c["card"])
        left.pack(side="left", fill="both", expand=True, padx=12, pady=8)
        tk.Label(left, text=rec.get("name") or "nieznana maszyna", bg=st.c["card"],
                 fg=st.c["text"], font=st.f.body_bold, anchor="w").pack(fill="x")
        tk.Label(left, text=f"{format_id(desk_id)}   {human_ts(float(rec.get('ts') or 0))}",
                 bg=st.c["card"], fg=st.c["muted"], font=st.f.mono_small, anchor="w").pack(fill="x")

        def forget() -> None:
            self.cfg.recent = [r for r in self.cfg.recent if int(r.get("desk_id") or 0) != desk_id]
            self.store.flush(force=True)
            self._render_recent()

        PillButton(body, st, "Usuń", forget, variant="quiet", width=74, size="sm",
                   bg=st.c["card"]).pack(side="right", padx=(0, 8))

    def _recall(self, desk_id: int) -> None:
        self.entry_target.set_value(format_id(desk_id))
        self.entry_target.focus_show()

    # ------------------------------------------------------------------ status
    def _refresh_status(self) -> None:
        st = self.st
        desk_id = self.cfg.identity.desk_id
        self.id_value.configure(text=format_id(desk_id) if desk_id else "--- --- ---")
        d = describe()
        wine = f"  •  Wine {d.get('wine')}" if d.get("wine") else ""
        self.machine_line.configure(
            text=f"{d.get('hostname')}  •  {d.get('os')} {d.get('release')}{wine}  •  {d.get('res')}")
        self.fingerprint_line.configure(
            text=f"klucz: {self.cfg.identity.fp or 'generowanie...'}")
        if self.cfg.identity.alias:
            self.entry_alias.set_value(self.cfg.identity.alias)

        connected = bool(self.agent.link and self.agent.link.connected)
        if connected:
            self.chip_relay.set("relay online", "ok")
            self.chip_state.set(f"oczekuje na połączenia", "accent" if self.agent.available else "muted")
        else:
            host = self.cfg.relay_host or "brak relay"
            self.chip_relay.set(f"relay: {host}", "warn" if self.cfg.relay_host else "muted")
            self.chip_state.set("offline", "muted")
        if not self.agent.available:
            self.chip_state.set("niedostępny (blokada)", "warn")

        listen = f"{self.cfg.listen_port}"
        addrs = ", ".join(net.local_addresses())
        state = "nasłuchuje" if (self.agent.direct and self.agent.direct.active) else "wyłączone"
        self.direct_line.configure(
            text=f"port {listen} — {state}" + (f"\nadresy: {addrs}" if addrs else ""))

    # ------------------------------------------------------------------ petla zdarzen
    def _pump(self) -> None:
        if self._closing:
            return
        # Pod modalnym dialogiem kreci sie wlasna petla Tk - nie przesylamy wtedy
        # zdarzen (zeby nie otworzyc kolejnego dialoga w srodku biezacego), ale
        # klatki zdalnego ekranu ciagna dalej.
        drained = 0
        if self._modal_depth == 0:
            try:
                while drained < 40:
                    kind, data = self.agent.ui.get_nowait()
                    drained += 1
                    self._on_event(kind, data)
            except queue.Empty:
                pass
        if self.active_window is not None and self.active_window.winfo_exists():
            try:
                self.active_window.tick()
            except tk.TclError:
                self.active_window = None
        self.store.tick()
        if not self.agent.available and self.tgl_available.on:
            self.tgl_available.set(False)
        if self._dial_started and time.monotonic() - self._dial_started > DIAL_TIMEOUT:
            self.cancel_dial()
            self.dial_line.configure(text="brak odpowiedzi - odwołane", foreground=self.st.c["warn"])
        self.root.after(33, self._pump)

    def _on_event(self, kind: str, data) -> None:  # noqa: C901 - jedna mapa zdarzen
        win = self.active_window
        if win is not None and getattr(win, "winfo_exists", None):
            try:
                if win.winfo_exists() and win.on_event(kind, data):
                    return
            except tk.TclError:
                self.active_window = None

        if kind == "address":
            self._refresh_status()
            self.log(f"relay przydzielil ID {format_id(int(data.get('id') or 0))}")
        elif kind == "alias":
            self._refresh_status()
        elif kind == "listen":
            self._refresh_status()
        elif kind == "relay":
            self._refresh_status()
            if data.get("error"):
                self.log(f"relay: {data['error']}")
        elif kind == "dialing":
            self.dial_line.configure(text=f"dzwonie {format_id(int(data.get('peer') or 0))}...")
        elif kind == "session_started":
            self._open_session_window(data or {})
        elif kind == "connected":
            self.btn_connect.set_enabled(True)
            self.btn_cancel.set_enabled(False)
            self._dial_started = 0.0
            self.dial_line.configure(text="")
        elif kind == "incoming":
            self.ask_incoming(data or {})
        elif kind == "incoming_cancel":
            self._dismiss("incoming")
        elif kind == "permission":
            self.ask_permission(data or {})
        elif kind == "input_request":
            self.ask_control(data or {})
        elif kind == "auth_failed":
            self._handle_auth_failed(data or {})
        elif kind == "fp_changed":
            self.ask_fingerprint(data or {})
        elif kind == "connect_failed":
            self.btn_connect.set_enabled(True)
            self.btn_cancel.set_enabled(False)
            self._dial_started = 0.0
            self.dial_line.configure(text=f"nie udalo sie: {data.get('reason')}",
                                     foreground=self.st.c["danger"])
        elif kind == "clipboard":
            self._apply_remote_clipboard(str(data or ""))
        elif kind == "file_done":
            self.log(f"plik odebrany: {data.get('path')}")
        elif kind == "session_closed":
            self._after_session_closed(data or {})
        elif kind == "closed":
            self._after_session_closed(data or {})
        elif kind == "log":
            self.log(str(data))
        elif kind in ("peer_info", "caps", "tunnel", "stats", "screen_info", "sharing",
                      "file_started", "file_progress", "grant", "clipboard_get"):
            pass  # obslugiwanwe w oknie sesji

    # ------------------------------------------------------------------ slogi
    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {line}")
        del self.log_lines[:-60:]
        try:
            self.logbox.configure(state="normal")
            self.logbox.delete("1.0", "end")
            self.logbox.insert("1.0", "\n".join(self.log_lines[-6:]))
            self.logbox.configure(state="disabled")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ okna sesji
    def _open_session_window(self, info: dict) -> None:
        role = info.get("role")
        session = self.agent.session
        if session is None:
            return
        if self.active_window is not None:
            try:
                if self.active_window.winfo_exists():
                    self.active_window.destroy()
            except tk.TclError:
                pass
        if role == "controller":
            win = RemoteWindow(self, self.st, session)
        else:
            win = SharingWindow(self, self.st, session)
        self.active_window = win
        self.windows.append(win)

    def session_window_closed(self, win, reason: str) -> None:
        self.log(f"sesja zamknieta: {reason}")
        try:
            win.destroy()
        except tk.TclError:
            pass
        if self.active_window is win:
            self.active_window = None
        if win in self.windows:
            self.windows.remove(win)
        self.btn_connect.set_enabled(True)
        self.btn_cancel.set_enabled(False)
        self._dial_started = 0.0
        self._refresh_status()

    def _after_session_closed(self, data: dict) -> None:
        self.log_lines.append("")
        reason = str(data.get("reason") or "polaczenie zamkniete")
        if self.active_window is not None:
            win = self.active_window
            self.active_window = None
            self.session_window_closed(win, reason)
        else:
            self._refresh_status()
            self.btn_connect.set_enabled(True)
            self.btn_cancel.set_enabled(False)

    # ------------------------------------------------------------------ dialogi
    def ask_incoming(self, ring: dict) -> None:
        peer = ring.get("peer") or {}
        dlg = Dialog(self, "Połączenie przychodzące", width=470)
        dlg.text(f"{peer.get('name') or 'Nieznana maszyna'} chce sie połączyć.", "text",
                 font=self.st.f.machine)
        dlg.text(f"ID {format_id(int(peer.get('desk_id') or 0))}   odcisk {peer.get('fp') or '?'}")
        if not ring.get("want_input"):
            dlg.text("Prosi o sam podgląd ekranu.", "ok")
        var = tk.BooleanVar(value=bool(ring.get("want_input", True)) and bool(self.cfg.allow_input))
        row = tk.Frame(dlg.body, bg=self.st.c["bg"])
        row.pack(fill="x", pady=(10, 0))
        sw = Toggle(row, self.st, initial=var.get(), command=lambda: var.set(sw.on),
                    bg=self.st.c["bg"])
        sw.pack(side="right")
        tk.Label(row, text="Zezwól na sterowanie myszką i klawiaturą", bg=self.st.c["bg"],
                 fg=self.st.c["text"], font=self.st.f.body).pack(side="left")
        out = {}

        def accept() -> None:
            out["allow"] = True
            out["input"] = var.get()
            dlg.destroy()

        def reject() -> None:
            out["allow"] = False
            dlg.destroy()

        dlg.buttons([("Odrzuć", reject, "danger"), ("Przyjmij", accept, "primary")])
        dlg.lift()
        dlg.wait()
        if "allow" not in out:
            self.agent.answer_incoming(False)
            return
        self.agent.answer_incoming(out["allow"], out.get("input", False))
        if out["allow"]:
            self.log(f"przyjalem polaczenie od {format_id(int(peer.get('desk_id') or 0))}")

    def ask_permission(self, info: dict) -> None:
        peer = info.get("peer") or {}
        dlg = Dialog(self, "Zdalny pulpit - zgoda", width=470)
        dlg.text(f"{peer.get('name') or 'Maszyna'} ma już połączenie i chce widzieć Twój ekran.",
                 "text", font=self.st.f.machine)
        if info.get("password_ok"):
            dlg.text("Hasło podane poprawnie.", "ok")
        var = tk.BooleanVar(value=bool(info.get("want_input", True)) and bool(self.cfg.allow_input))
        row = tk.Frame(dlg.body, bg=self.st.c["bg"])
        row.pack(fill="x", pady=(8, 0))
        sw = Toggle(row, self.st, initial=var.get(), command=lambda: var.set(sw.on),
                    bg=self.st.c["bg"])
        sw.pack(side="right")
        tk.Label(row, text="Zezwól na sterowanie", bg=self.st.c["bg"], fg=self.st.c["text"],
                 font=self.st.f.body).pack(side="left")

        def answer(allow: bool) -> None:
            session = self.agent.session
            if session is not None:
                session.answer_permission(allow, var.get())
            dlg.destroy()

        dlg.buttons([("Odrzuć", lambda: answer(False), "danger"),
                     ("Zezwól", lambda: answer(True), "primary")])
        dlg.lift()
        dlg.wait()

    def ask_control(self, info: dict, session=None) -> None:
        peer = (info or {}).get("peer") or {}
        session = session or self.agent.session
        if session is None:
            return
        dlg = Dialog(self, "Prośba o sterowanie", width=430)
        dlg.text(f"{peer.get('name') or 'Pilot'} prosi o przejecie myszki i klawiatury.", "text",
                 font=self.st.f.machine)
        if peer.get("desk_id"):
            dlg.text(f"ID {format_id(int(peer.get('desk_id') or 0))}", "muted",
                     font=self.st.f.mono_id)

        def answer(allow: bool) -> None:
            session.answer_input_request(allow)
            try:
                dlg.destroy()
            except tk.TclError:
                pass

        dlg.buttons([("Odmów", lambda: answer(False), "danger"),
                     ("Przekaż", lambda: answer(True), "primary")])
        dlg.lift()
        dlg.wait()

    def ask_fingerprint(self, info: dict) -> None:
        peer = info.get("peer") or {}
        dlg = Dialog(self, "Ostrzeżenie o kluczu", width=480)
        dlg.text("Odcisk klucza tej maszyny różni sie od zapisanego.", "danger",
                 font=self.st.f.machine)
        dlg.text("To moze oznaczac zmiane systemu/instalacji na tamtym komputerze albo atak "
                 "'w srodku'. Sprawdz ID i odcisk z druga strona glosem lub innym kanalem.")
        dlg.text(f"ID {format_id(int(peer.get('desk_id') or 0))}   odcisk {peer.get('fp') or '?'}",
                 "muted", font=self.st.f.mono_id)

        def trust() -> None:
            self.cfg.trust(int(peer.get("desk_id") or 0), peer.get("fp") or "")
            self.store.flush(force=True)
            dlg.destroy()
            if self._dial_target:
                self.connect_now()

        dlg.buttons([("Anuluj", dlg._reject, "ghost"), ("Zaufaj", trust, "primary")])
        dlg.lift()
        dlg.wait()

    def _handle_auth_failed(self, data: dict) -> None:
        reason = str(data.get("reason") or "")
        self.btn_connect.set_enabled(True)
        self.btn_cancel.set_enabled(False)
        if reason == "password" and self._dial_target:
            if time.monotonic() - self._last_password_prompt < 1.5:
                return
            self._last_password_prompt = time.monotonic()
            dlg = Dialog(self, "Hasło dostepu", width=420)
            dlg.text("Druga strona wymaga hasła. Podaj je i sprobuje ponownie.", "text")
            entry = Entry(dlg.body, self.st, placeholder="haslo", show="•")
            entry.pack(fill="x", ipady=6, pady=(6, 0))
            focus = getattr(self.active_window, "_modal_guard", None)
            if focus:
                focus(True)

            def submit() -> None:
                value = entry.value()
                dlg.destroy()
                if focus:
                    focus(False)
                if value:
                    self.entry_password.set_value(value)
                    self.connect_now()

            entry.bind("<Return>", lambda e: submit())
            dlg.buttons([("Anuluj", dlg._reject, "ghost"), ("Dalej", submit, "primary")])
            entry.focus_force()
            dlg.wait()
        else:
            self.dial_line.configure(text=f"odmowa: {reason or 'nieznany powod'}",
                                     foreground=self.st.c["danger"])

    def _dismiss(self, kind: str) -> None:
        self.log(f"zdarzenie {kind} - nie ma okna do obslugi")

    # ------------------------------------------------------------------ settings / about
    def open_settings(self) -> None:
        SettingsDialog(self)

    def open_about(self) -> None:
        AboutDialog(self)

    def _apply_remote_clipboard(self, text: str) -> None:
        if not text or not self.cfg.clipboard_sync:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text[:100000])
        except tk.TclError:
            pass
        self.log("schowek zdalny zapisany do lokalnego")

    # ------------------------------------------------------------------ zamykanie
    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.cfg.window = [self.root.winfo_width(), self.root.winfo_height(),
                           self.root.winfo_x(), self.root.winfo_y()]
        self.store.flush(force=True)
        for win in list(self.windows):
            try:
                win.destroy()
            except tk.TclError:
                pass
        threading.Thread(target=self.agent.stop, daemon=True).start()
        self.root.after(120, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- ustawienia
class SettingsDialog:
    TABS = ("Przychodzace", "Haslo", "Jakosc obrazu", "Relay", "Zaawansowane")

    def __init__(self, app: MainApp):
        self.app = app
        self.st = app.st
        self.cfg = app.cfg
        self.dlg = Dialog(app, "Ustawienia DeskLink", width=560, parent=app.root)
        self.note = ttk.Notebook(self.dlg.body)
        self.note.pack(fill="both", expand=True)
        for name in self.TABS:
            frame = tk.Frame(self.note, bg=self.st.c["bg"])
            self.note.add(frame, text=name)
        for builder, tab in zip((self._tab_incoming, self._tab_password, self._tab_quality,
                                 self._tab_relay, self._tab_advanced), self.note.tabs()):
            builder(self.note.nametowidget(str(tab)))
        self.dlg.buttons([("Zamknij", self._close, "ghost"), ("Zapisz", self._save, "primary")])
        self.dlg.lift()

    def _label(self, parent, text: str, key: str = "muted", font=None) -> tk.Label:
        return tk.Label(parent, text=text, bg=self.st.c["bg"], fg=self.st.c[key],
                        font=font or self.st.f.body, anchor="w", justify="left",
                        wraplength=520)

    def _entry(self, parent, value: str, width: int = 26, show: str = "") -> Entry:
        e = Entry(parent, self.st, width=width)
        if show:
            e.configure(show=show)
        e.set_value(str(value))
        e.pack(fill="x", ipady=5, pady=(2, 8))
        return e

    def _switch_row(self, parent, text: str, initial: bool) -> Toggle:
        row = tk.Frame(parent, bg=self.st.c["bg"])
        row.pack(fill="x", pady=6)
        sw = Toggle(row, self.st, initial=initial, bg=self.st.c["bg"])
        sw.pack(side="right")
        self._label(row, text, "text").pack(side="left", fill="x", expand=True)
        return sw

    # ------------------------------------------------------------------ karty
    def _tab_incoming(self, parent) -> None:
        pad = tk.Frame(parent, bg=self.st.c["bg"])
        pad.pack(fill="both", expand=True, padx=18, pady=16)
        self._label(pad, "Te ustawienia decyduja, co dzieje sie, gdy ktos dzwoni na Twoje ID.",
                    "faint", self.st.f.small).pack(anchor="w", pady=(0, 8))
        self.sw_confirm = self._switch_row(pad, "Pytaj o zgode przy kazdym polaczeniu",
                                          self.cfg.confirm_incoming)
        self.sw_input = self._switch_row(pad, "Zezwal na sterowanie myszka i klawiatura",
                                        self.cfg.allow_input)
        self.sw_files = self._switch_row(pad, "Zezwal na przesyl plikow", self.cfg.allow_files)
        self.sw_clip = self._switch_row(pad, "Synchronizuj schowek", self.cfg.clipboard_sync)
        self.sw_view = self._switch_row(pad, "Domyślnie tylko podgląd", self.cfg.view_only_default)
        self.sw_lock = self._switch_row(pad, "Blokuj lokalna mysz podczas udostepniania (Linux/X11)",
                                       self.cfg.auto_lock_local)

    def _tab_password(self, parent) -> None:
        pad = tk.Frame(parent, bg=self.st.c["bg"])
        pad.pack(fill="both", expand=True, padx=18, pady=16)
        self._label(pad, "Haslo do polaczen bez nadzoru (unattended). Nie jest zapisywane "
                    "w konfiguracji - trzymamy tylko weryfikator PBKDF2-SHA256.",
                    "faint", self.st.f.small).pack(anchor="w", pady=(0, 10))
        self._label(pad, "Nowe haslo (zostaw puste, zeby usunac)", "text").pack(anchor="w")
        self.entry_pw = self._entry(pad, "", 30, show="•")
        self._label(pad, "Powtorz haslo", "text").pack(anchor="w")
        self.entry_pw2 = self._entry(pad, "", 30, show="•")
        self.pw_state = self._label(pad, "stan: " + ("haslo ustawione" if self.cfg.has_password
                                              else "bez hasla"), "muted", self.st.f.small)
        self.pw_state.pack(anchor="w", pady=(6, 0))

    def _tab_quality(self, parent) -> None:
        pad = tk.Frame(parent, bg=self.st.c["bg"])
        pad.pack(fill="both", expand=True, padx=18, pady=16)
        self._label(pad, "Parametry nadawania Twojego ekranu (gdy ktos sie laczy).",
                    "faint", self.st.f.small).pack(anchor="w", pady=(0, 10))
        self.var_fps = tk.IntVar(value=int(self.cfg.fps))
        self.var_q = tk.IntVar(value=int(self.cfg.quality))
        self.var_scale = tk.DoubleVar(value=float(self.cfg.scale))
        self._slider_row(pad, "Klatki na sekunde", 3, 60, self.var_fps, "{:.0f}")
        self._slider_row(pad, "Jakose JPEG", 20, 95, self.var_q, "{:.0f}")
        self._slider_row(pad, "Skala obrazu", 0.3, 1.0, self.var_scale, "{:.2f}")
        self._label(pad, "Format", "text").pack(anchor="w", pady=(10, 2))
        self.var_fmt = tk.StringVar(value=self.cfg.encoding)
        row = tk.Frame(pad, bg=self.st.c["bg"])
        row.pack(anchor="w")
        for fmt in ("jpeg", "png"):
            tk.Radiobutton(row, text=fmt.upper(), value=fmt, variable=self.var_fmt, bg=self.st.c["bg"],
                           fg=self.st.c["text"], selectcolor=self.st.c["entry"],
                           activebackground=self.st.c["bg"], activeforeground=self.st.c["text"],
                           font=self.st.f.body, highlightthickness=0, bd=0).pack(side="left")
        self.sw_adapt = self._switch_row(pad, "Adaptacyjna jakosc (chroni przed zatkaniem laca)",
                                        bool(self.cfg.adaptive))

    def _slider_row(self, parent, text: str, lo: int, hi: int, var, fmt: str) -> None:
        row = tk.Frame(parent, bg=self.st.c["bg"])
        row.pack(fill="x", pady=6)
        tk.Label(row, text=text, bg=self.st.c["bg"], fg=self.st.c["muted"],
                 font=self.st.f.small, width=18, anchor="w").pack(side="left")
        scale = tk.Scale(row, from_=lo, to=hi, orient="horizontal", variable=var,
                         bg=self.st.c["bg"], fg=self.st.c["text"], troughcolor=self.st.c["entry"],
                         highlightthickness=0, bd=0, showvalue=True, resolution=1,
                         activebackground=self.st.c["accent"], sliderrelief="flat",
                         font=self.st.f.small, length=230)
        scale.pack(side="left", padx=8)

    def _tab_relay(self, parent) -> None:
        pad = tk.Frame(parent, bg=self.st.c["bg"])
        pad.pack(fill="both", expand=True, padx=18, pady=16)
        self._label(pad, "Serwer rendezvous nadaje ID i laczy dwie strony przez NAT. "
                    "Postaw własny lub uzyj publicznego.", "faint", self.st.f.small).pack(
                        anchor="w", pady=(0, 10))
        self._label(pad, "Host relay", "text").pack(anchor="w")
        self.entry_relay = self._entry(pad, self.cfg.relay_host, 34)
        row = tk.Frame(pad, bg=self.st.c["bg"])
        row.pack(fill="x")
        self._label(row, "Port", "text").pack(side="left")
        self.entry_relay_port = self._entry(row, self.cfg.relay_port, 8)
        self.entry_relay_port.pack_configure(side="left", padx=(8, 0), fill="none")
        self.sw_tls = self._switch_row(pad, "TLS do relaya (własny certyfikat)", self.cfg.relay_tls)
        self.sw_tls_bad = self._switch_row(
            pad, "Akceptuj self-signed certyfikat relaya (tylko wlasna infrastruktura)",
            bool(getattr(self.cfg, "relay_tls_insecure", False)))
        self._label(pad, "Klucz wstepu relaya (gdy relay wystartowal z --require-auth)",
                    "text").pack(anchor="w", pady=(10, 2))
        self.entry_key = self._entry(pad, getattr(self.cfg, "relay_key", "") or "", 30,
                                     show="•")
        self.sw_lan = self._switch_row(pad, "Odkrywanie w sieci lokalnej (UDP)",
                                      self.cfg.lan_discovery)
        self.sw_relay = self._switch_row(pad, "Lacz sie z relayem przy starcie",
                                        bool(self.cfg.relay_host))

    def _tab_advanced(self, parent) -> None:
        pad = tk.Frame(parent, bg=self.st.c["bg"])
        pad.pack(fill="both", expand=True, padx=18, pady=16)
        self._label(pad, "Lokalne sciezki i identyfikatory.", "faint", self.st.f.small).pack(
            anchor="w", pady=(0, 10))
        from ..settings import config_dir

        self._label(pad, f"Config: {config_dir()}", "muted", self.st.f.mono_small).pack(anchor="w")
        self._label(pad, f"ID: {format_id(self.cfg.identity.desk_id)}", "text",
                    self.st.f.mono_id).pack(anchor="w", pady=(6, 0))
        self._label(pad, f"Odcisk: {self.cfg.identity.fp}", "muted",
                    self.st.f.mono_small).pack(anchor="w")
        self.sw_cursor = self._switch_row(pad, "Pokazuj kursor po drugiej stronie",
                                         self.cfg.show_cursor_remote)
        self._label(pad, "Motyw", "text").pack(anchor="w", pady=(8, 2))
        self.var_theme = tk.StringVar(value=self.cfg.theme)
        row = tk.Frame(pad, bg=self.st.c["bg"])
        row.pack(anchor="w")
        for name in ("dark", "light"):
            tk.Radiobutton(row, text=name, value=name, variable=self.var_theme,
                           bg=self.st.c["bg"], fg=self.st.c["text"],
                           selectcolor=self.st.c["entry"], activebackground=self.st.c["bg"],
                           activeforeground=self.st.c["text"], font=self.st.f.body,
                           highlightthickness=0, bd=0).pack(side="left")

    # ------------------------------------------------------------------ zapis
    def _save(self) -> None:
        cfg = self.cfg
        store = self.app.store
        cfg.confirm_incoming = self.sw_confirm.on
        cfg.allow_input = self.sw_input.on
        cfg.allow_files = self.sw_files.on
        cfg.clipboard_sync = self.sw_clip.on
        cfg.view_only_default = self.sw_view.on
        cfg.auto_lock_local = self.sw_lock.on
        cfg.fps = int(self.var_fps.get())
        cfg.quality = int(self.var_q.get())
        cfg.scale = float(self.var_scale.get())
        cfg.encoding = self.var_fmt.get()
        cfg.adaptive = self.sw_adapt.on
        cfg.relay_host = self.entry_relay.value()
        try:
            cfg.relay_port = int(self.entry_relay_port.value() or cfg.relay_port)
        except ValueError:
            pass
        cfg.relay_tls = self.sw_tls.on
        cfg.relay_tls_insecure = self.sw_tls_bad.on
        key = self.entry_key.value()
        if key:
            cfg.relay_key = key
        cfg.lan_discovery = self.sw_lan.on
        cfg.show_cursor_remote = self.sw_cursor.on
        cfg.theme = self.var_theme.get()
        pw = self.entry_pw.value()
        if pw:
            if pw != self.entry_pw2.value():
                self.pw_state.configure(text="stan: hasla nie identyczne - nie zapisano",
                                       fg=self.st.c["danger"])
                return
            cfg.set_password(pw)
        elif not pw and self.entry_pw2.value() == "" and cfg.has_password and \
                self._asked_clear():
            cfg.set_password("")
        store.flush(force=True)
        key = self.entry_key.value()
        if not self.sw_relay.on and self.app.agent.link is not None:
            self.app.agent.link.close()
            self.app.agent.link = None
        elif self.sw_relay.on and cfg.relay_host:
            # start_relay musi sie wykonac takze gdy watek wisi na STARYM adresie:
            # inaczej poprawienie zlego hosta w tym oknie nie dawałoby zadnego efektu
            # az do restartu aplikacji (link byl nie-None, wiec galaz sie pomijala).
            if self.app.agent.needs_relay_restart(cfg.relay_host, cfg.relay_port,
                                                  cfg.relay_tls, key):
                self.app.agent.start_relay(cfg.relay_host, cfg.relay_port, cfg.relay_tls, key)
        self.app._refresh_status()
        self.pw_state.configure(text="zapisano", fg=self.st.c["ok"])
        self.app.log("ustawienia zapisane")

    def _asked_clear(self) -> bool:
        return False

    def _close(self) -> None:
        try:
            self.dlg.destroy()
        except tk.TclError:
            pass


class AboutDialog:
    def __init__(self, app: MainApp):
        from .. import __version__

        self.app = app
        st = app.st
        dlg = Dialog(app, "DeskLink", width=480)
        head = tk.Frame(dlg.body, bg=st.c["bg"])
        head.pack(fill="x", pady=(0, 8))
        LogoMark(head, st, size=34, bg=st.c["bg"]).pack(side="left")
        tk.Label(head, text=f"DeskLink {__version__}", bg=st.c["bg"], fg=st.c["text"],
                 font=st.f.title).pack(side="left", padx=10)
        d = describe()
        dlg.text(
            "Klon AnyDesk pisany od zera: ID jak w AnyDesk, relay przez NAT, "
            "szyfrowanie E2E (X25519 + AES-GCM), haslo na polaczenia bez nadzoru, "
            "schowek, pliki, zgoda na sterowanie.\n\n"
            f"Ta maszyna: {d.get('os')} {d.get('release')} • {d.get('hostname')} • "
            f"Python {d.get('python')} • ekran {d.get('res')}\n"
            f"Warstwa: {'Wine ' + str(d.get('wine')) if d.get('wine') else 'natywnie'}  |  "
            f"{app.startup_note}",
            "muted", font=st.f.small, wrap=440)
        dlg.text("Jak to dziala: obie strony lacza sie z relayem, relay podglada ID i "
                 "przepuszcza rure. Obraz i wejscie ida przez nurek, relay nie widzi "
                 "tresci (E2E).", "faint", font=st.f.small, wrap=440)
        dlg.buttons([("Zamknij", dlg._reject, "ghost")])
        dlg.lift()


def run_gui(store: Optional[ConfigStore] = None, agent=None, demo: bool = False) -> None:
    app = MainApp(store=store, agent=agent, demo=demo)
    app.run()
