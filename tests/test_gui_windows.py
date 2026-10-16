"""Testy okien GUI na fikcyjnej sesji.

Po co to istnieje: blad w ktorymkolwiek widgecie przerywal konstruktora okna
w polowie, a Tk only drukuje wyjatek na stderr (pod .exe nikt tego nie widzi).
W ten sposob panel hosta stracil suwaki i przycisk ROZLACZ - 'Slider' odwolywal
sie do nieistniejacej zmiennej 'to_'. Testy protokolu tego nie lapia, bo w ogole
nie buduja okien.

Wymaga wyswietlacza (DISPLAY pod X11); bez niego cala klasa sie pomija.
Uruchom:  python3 tests/test_gui_windows.py
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HAS_DISPLAY = bool(os.environ.get("DISPLAY")) or sys.platform.startswith("win")

try:
    import tkinter as tk
    from rd.gui import theme
    from rd.gui.app import Dialog
    from rd.gui.session_view import WYL_OFF, WYL_ON, RemoteWindow, SharingWindow
    from rd.gui.widgets import Slider
except Exception:  # noqa: BLE001 - brak Tk / brak wyswietlacza
    HAS_DISPLAY = False


class FakePeer:
    def __init__(self):
        self.name = ""
        self.desk_id = 0
        self.fp = ""
        self.platform = ""


class FakeSession:
    """Tyle sesji, ile potrzebuje okno: stan + licznik wywolan."""

    def __init__(self, role="host"):
        self.role = role
        self.peer = FakePeer()
        self.share_screen = True
        self.peer_controls_me = True
        self.granted_input = False
        self.stats = {"fps_out": 19.0, "kbps_out": 4200.0, "dropped": 0,
                      "fps_in": 20.0, "rtt_ms": 1.0}
        self.calls = []
        self.local_monitors_result = []

    # host
    def set_sharing(self, on):
        self.calls.append(("sharing", bool(on)))
        self.share_screen = bool(on)

    def set_input(self, on):
        self.calls.append(("input", bool(on)))
        self.peer_controls_me = bool(on)

    def set_stream_params(self, fps=None, quality=None):
        self.calls.append(("params", fps, quality))

    def local_monitors(self):
        return self.local_monitors_result

    def set_monitor(self, i):
        self.calls.append(("monitor", i))

    def stop(self, reason):
        self.calls.append(("stop", reason))

    # kontroler
    def pull_image(self):
        return None, -1

    def send_mouse(self, *a, **kw):
        self.calls.append(("mouse", a))

    def send_key(self, *a, **kw):
        self.calls.append(("key", a))

    def send_scroll(self, *a, **kw):
        self.calls.append(("scroll", a))


class FakeStore:
    def __init__(self, cfg):
        self.config = cfg
        self.path = Path("/tmp/desklink-test-config.json")

    def tick(self, **kw):
        pass

    def flush(self, force=False):
        pass


def make_app(root, cfg):
    return SimpleNamespace(root=root, st=theme.apply(root, "dark"), cfg=cfg,
                           store=FakeStore(cfg), title_tag="TEST", agent=None,
                           active_window=None, log=lambda *a: None,
                           ask_control=lambda *a: None,
                           session_window_closed=lambda *a: None)


def make_cfg():
    return SimpleNamespace(allow_input=True, auto_lock_local=False, fps=20, quality=72,
                           clipboard_sync=True, show_cursor_remote=True,
                           view_only_default=False, scale=1.0)


@unittest.skipUnless(HAS_DISPLAY, "brak wyswietlacza - okna Tk sie nie zbuduja")
class WindowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.cfg = make_cfg()
        self.app = make_app(self.root, self.cfg)

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _spin(self, win, times=3):
        for _ in range(times):
            self.root.update_idletasks()
            self.root.update()
            win.tick()

    # ------------------------------------------------------------------ panel hosta
    def test_sharing_window_is_complete(self):
        sess = FakeSession("host")
        win = SharingWindow(self.app, self.app.st, sess)
        for attr in ("sw_share", "sw_input", "sw_lock", "slider_fps", "slider_q",
                     "btn_file", "btn_monitor", "btn_stop", "note"):
            self.assertTrue(hasattr(win, attr), f"panel hosta nie ma {attr} - konstruktor padl")
        self._spin(win)
        self.assertTrue(win.sw_share.on, "udostepnianie obrazu powinno byc wlaczone")
        self.assertTrue(win.sw_input.on, "pilot ma sterowanie, a panel pokazuje 'wylaczone'")
        self.assertFalse(win.sw_lock.on)

    def test_sharing_switches_follow_session_state(self):
        sess = FakeSession("host")
        win = SharingWindow(self.app, self.app.st, sess)
        self._spin(win)
        # pilot dostal po pecie i zwril sterowanie -> panel ma sie dostosowac
        sess.peer_controls_me = False
        self._spin(win)
        self.assertFalse(win.sw_input.on)
        sess.share_screen = False
        self._spin(win)
        self.assertFalse(win.sw_share.on)

    def test_sharing_switch_click_reaches_session(self):
        sess = FakeSession("host")
        win = SharingWindow(self.app, self.app.st, sess)
        win.sw_input.set(False)
        win._on_input_switch()
        self.assertIn(("input", False), sess.calls)

    def test_top_bar_gets_peer_after_handshake(self):
        sess = FakeSession("host")
        win = SharingWindow(self.app, self.app.st, sess)
        self._spin(win)
        self.assertEqual(win.peer_name.cget("text"), "zdalny pulpit")
        sess.peer.name = "studio-biuro"
        sess.peer.desk_id = 123456789
        sess.peer.platform = "Windows 11"
        self._spin(win)
        self.assertEqual(win.peer_name.cget("text"), "studio-biuro")
        self.assertEqual(win.peer_id.cget("text"), "ID 123456789")
        self.assertEqual(win.platform.cget("text"), "Windows 11")

    # ------------------------------------------------------------------ okno kontrolera
    def test_remote_window_is_complete(self):
        sess = FakeSession("controller")
        win = RemoteWindow(self.app, self.app.st, sess)
        for attr in ("canvas", "btn_control", "btn_clip", "btn_file", "btn_monitor",
                     "btn_quality", "btn_full", "btn_disconnect", "chip", "stats_label"):
            self.assertTrue(hasattr(win, attr), f"okno kontrolera nie ma {attr}")
        self._spin(win)
        self.assertEqual(win.btn_control.text, WYL_OFF)

    def test_remote_window_hint_follows_grant(self):
        sess = FakeSession("controller")
        win = RemoteWindow(self.app, self.app.st, sess)
        self._spin(win)
        sess.granted_input = True
        self._spin(win)
        self.assertEqual(win.btn_control.text, WYL_ON)
        # klikniecie przycisku musi poprosic sesje o zwrot sterowania
        win.toggle_control()
        self.assertTrue(any(c[0] == "input" for c in sess.calls), "brak wywolania set_input")

    # ------------------------------------------------------------------ dialogi
    def test_dialog_without_height_fits_its_content(self):
        """'560x0' w Tk to nie 'auto' - okno bylo ucinane do ~38 px."""
        self.root.deiconify()          # okno musi byc zmapowane, inaczej 1x1
        d = Dialog(self.app, "Probe", width=520, parent=self.root)
        for i in range(6):
            d.text(f"linia {i}")
        d.buttons([("OK", lambda: None, "primary")])
        end = time.time() + 0.5
        while time.time() < end:
            self.root.update()
        self.assertGreaterEqual(d.winfo_width(), 500, "szerokosc dialogu sie nie zgadza")
        self.assertGreater(d.winfo_height(), 200, f"dialog ucinety do {d.winfo_height()} px")
        d.destroy()

    # ------------------------------------------------------------------ zamkniecie sesji
    def test_close_session_goes_through_app(self):
        """'close_session' ma zglosic zamkniecie oknu glownemu, nie znikac po cichu.

        Inaczej app.active_window wskazuje na zniszczone okno, a przycisk 'Polacz'
        zostaje wylaczony do czasu zdarzenia session_closed z watku sesji - przy
        wolniejszym hoście (Wine) to sekundy, w najgorszym razie nigdy.
        """
        called = []

        def fake_closed(w, reason):          # tak robi naprawde MainApp
            called.append((w, reason))
            w.destroy()

        self.app.session_window_closed = fake_closed
        sess = FakeSession("host")
        win = SharingWindow(self.app, self.app.st, sess)
        win.close_session("qa-koniec")
        self.assertEqual([c[1] for c in called], ["qa-koniec"],
                         "app nie zostal powiadomiony o zamknieciu okna")
        self.assertIs(called[0][0], win)
        self.assertIn(("stop", "qa-koniec"), sess.calls, "sesja nie zostala zatrzymana")
        self.assertFalse(win.winfo_exists(), "okno ma zniknac razem z sesja")

    # ------------------------------------------------------------------ widgety
    def test_slider_bounds_and_callback(self):
        seen = []
        s = Slider(self.root, self.app.st, "fps", 3, 60, 20,
                   on_change=lambda v: seen.append(v), fmt="{:.0f}")
        self.assertEqual((s.lo, s.hi), (3.0, 60.0))
        s.set(99)
        self.assertEqual(s.value, 60.0)
        s.set(-5)
        self.assertEqual(s.value, 3.0)
        s.set(30, notify=True)
        self.assertEqual(seen, [30.0])
        s.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
