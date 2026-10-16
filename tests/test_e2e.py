"""Test koncow do konca: relay + host + kontroler, bez GUI i bez ruszania kursora.

Sprawdza to, co faktycznie musi dzialac w klonie AnyDesk:
  wydanie ID -> 'dzwonek' -> zgoda -> tunel przez relay -> handshake E2E ->
  klatki ekranu -> wejscie -> schowek -> plik -> rozlaczanie.

Uruchom:  python3 tests/test_e2e.py   (albo pytest)
"""

from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="desklink-e2e-")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "cache")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "config")

from rd.agent import Agent  # noqa: E402
from rd.capture import SyntheticSource  # noqa: E402
from rd.settings import ConfigStore  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Recorder:
    """Zamiast pyautogui - zapisuje co dostal, zeby nie ruszac prawdziwego kursora."""

    def __init__(self) -> None:
        self.events = []
        self.applied = 0
        self.rejected = 0
        self.last_error = ""
        self.enabled = True

    def set_area(self, left, top, width, height):
        self.events.append(("area", left, top, width, height))

    def move(self, fx, fy):
        self.applied += 1
        self.events.append(("move", round(fx, 3), round(fy, 3)))

    def button(self, name, action, fx=None, fy=None, count=1):
        self.applied += 1
        self.events.append(("button", name, action, fx, fy))

    def scroll(self, dx, dy, fx=None, fy=None):
        self.applied += 1
        self.events.append(("scroll", dx, dy))

    def key(self, canon, action, char=None):
        self.applied += 1
        self.events.append(("key", canon, action, char))

    def hotkey(self, combo):
        self.applied += 1
        self.events.append(("hotkey", combo))

    def type_text(self, text, interval=0.008):
        self.applied += 1
        self.events.append(("text", text))

    def release_all(self):
        self.events.append(("release",))


class RelayFixture:
    def __init__(self, port: int):
        self.port = port
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "desklink_relay.py"),
             "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                s.close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    out = self.proc.stdout.read() if self.proc.stdout else ""
                    raise RuntimeError(f"relay padl:\n{out}")
                time.sleep(0.2)
        raise RuntimeError("relay nie wstal")

    def logs(self) -> str:
        try:
            return "".join(_drain(self.proc.stdout))
        except Exception:  # noqa: BLE001
            return ""

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(4)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _drain(stream) -> list:
    out = []
    if stream is None:
        return out
    while True:
        line = stream.readline()
        if not line:
            break
        out.append(line)
    return out


def wait_event(agent: Agent, kind: str, timeout: float = 20.0):
    """Czeka na zdarzenie danego typu, po drodze zwracajac wszystko inne do 'seen'."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            got, data = agent.ui.get(timeout=0.25)
        except queue.Empty:
            continue
        seen.append((got, data))
        if got == kind:
            return data, seen
        if got == "connect_failed" and kind not in ("connect_failed",):
            continue
    raise AssertionError(f"zdarzenie '{kind}' nie przyszlo w {timeout}s; widzialem: "
                         f"{[s[0] for s in seen]}")


def make_agent(tag: str, relay_port: int, **cfg_kw) -> Agent:
    store = ConfigStore(Path(TMP) / f"cfg-{tag}.json")
    defaults = dict(relay_host="127.0.0.1", relay_port=relay_port, listen_enabled=False,
                    lan_discovery=False, fps=15, quality=55, adaptive=False)
    defaults.update(cfg_kw)
    store.update(**defaults)
    store.flush(force=True)
    agent = Agent(store)
    agent.source_override = SyntheticSource(800, 500, fps=15)
    agent.injector_override = Recorder()
    agent.start_relay("127.0.0.1", relay_port)
    return agent


class E2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.relay = RelayFixture(cls.port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.relay.stop()

    def setUp(self) -> None:
        self.agents: list[Agent] = []

    def tearDown(self) -> None:
        for agent in self.agents:
            agent.stop()
        time.sleep(0.3)

    # ------------------------------------------------------------------ happy path
    def test_full_session_through_relay(self) -> None:
        host = make_agent("host", self.port, confirm_incoming=True, allow_input=True)
        self.agents.append(host)
        welcome, _ = wait_event(host, "address", 25)
        host_id = welcome["id"]
        self.assertGreaterEqual(host_id, 100_000_000)

        ctrl = make_agent("ctrl", self.port)
        self.agents.append(ctrl)
        wait_event(ctrl, "address", 25)

        result = ctrl.connect(str(host_id), password="", view_only=False)
        self.assertTrue(result["ok"], result)

        ring_data, _ = wait_event(host, "incoming", 20)
        self.assertEqual(ring_data["peer"]["desk_id"], ctrl.cfg.identity.desk_id)
        host.answer_incoming(True, allow_input=True)

        host_conn, _ = wait_event(host, "connected", 25)
        ctrl_conn, _ = wait_event(ctrl, "connected", 25)
        self.assertEqual(host_conn["role"], "host")
        self.assertEqual(ctrl_conn["role"], "controller")

        # klatki ekranu hosta trafia do kontrolera
        session = ctrl.session
        self.assertIsNotNone(session)
        deadline = time.monotonic() + 12
        frames = 0
        while time.monotonic() < deadline:
            frames = session.stats["frames_in"]
            if frames >= 4:
                break
            time.sleep(0.2)
        self.assertGreaterEqual(frames, 4, f"za malo klatek: {frames}")
        img, gen = session.pull_image()
        self.assertIsNotNone(img)
        self.assertEqual(img.size, (800, 500))
        self.assertEqual(gen, session.gen)

        # wejscie leci do hosta (do Recordera, nie do pyautogui)
        self.assertTrue(ctrl.session.granted_input, "host nie przyznal kontroli")
        ctrl.session.send_mouse(0.25, 0.75)
        ctrl.session.send_mouse(0.5, 0.5, btn="left", act="down")
        ctrl.session.send_mouse(0.5, 0.5, btn="left", act="up")
        ctrl.session.send_key("enter", "press")
        ctrl.session.send_text("halo z linuxa")
        ctrl.session.send_scroll(0, -3, 0.4, 0.4)
        deadline = time.monotonic() + 6
        rec: Recorder = host.session.injector
        wanted = {"move", "button", "key", "text", "scroll"}
        while time.monotonic() < deadline:
            if wanted <= {e[0] for e in rec.events}:
                break
            time.sleep(0.2)
        kinds = [e[0] for e in rec.events]
        for expected in ("move", "button", "key", "text", "scroll"):
            self.assertIn(expected, kinds, f"host nie dostal '{expected}', byly: {kinds}")
        # wspolrzedne w ulamkach -> host przelicza na swoj obszar
        area = next(e for e in rec.events if e[0] == "area")
        self.assertEqual(area[3], 800)
        self.assertEqual(area[4], 500)

        # latency / PING-PONG
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if ctrl.session.stats.get("rtt_ms", 0) > 0:
                break
            time.sleep(0.3)
        self.assertGreater(ctrl.session.stats.get("rtt_ms", 0), 0)

        # schowek w dwie strony
        ctrl.session.copy_clipboard("skopiowane przez desklink")
        got, _ = wait_event(host, "clipboard", 8)
        self.assertEqual(got, "skopiowane przez desklink")

        # plik: kontroler -> host
        src = Path(TMP) / "do-hosta.txt"
        src.write_bytes(b"x" * 300_000)
        host.cfg.allow_files = True
        ctrl.cfg.allow_files = True
        self.assertTrue(ctrl.session.send_file(str(src)))
        done, _ = wait_event(host, "file_done", 25)
        out = Path(done["path"])
        self.assertTrue(out.exists(), done)
        self.assertEqual(out.stat().st_size, 300_000)
        self.assertEqual(out.read_bytes(), src.read_bytes())

        # rozlaczenie
        ctrl.session.stop("test-koniec")
        closed, _ = wait_event(host, "closed", 15)
        self.assertIn("test-koniec", closed["reason"], closed)

    # ------------------------------------------------------------------ haslo
    def test_password_required_and_wrong(self) -> None:
        host = make_agent("host-pw", self.port, confirm_incoming=False)
        host.cfg.set_password("poprawne-haslo")
        host.store.flush(force=True)
        self.agents.append(host)
        wait_event(host, "address", 25)
        host_id = host.cfg.identity.desk_id

        # 1) zle haslo -> odmowa
        ctrl = make_agent("ctrl-bad", self.port)
        self.agents.append(ctrl)
        wait_event(ctrl, "address", 25)
        ctrl.connect(str(host_id), password="zle-haslo")
        # ring trafia do hosta, ale host z haslem i confirm_incoming=False przyjmuje sam
        try:
            ring, _ = wait_event(host, "incoming", 4)
            host.answer_incoming(True, allow_input=True)
        except AssertionError:
            pass
        failed, _ = wait_event(ctrl, "auth_failed", 25)
        self.assertEqual(failed["reason"], "password")

        # 2) dobre haslo -> sesja
        ctrl2 = make_agent("ctrl-good", self.port)
        self.agents.append(ctrl2)
        wait_event(ctrl2, "address", 25)
        ctrl2.connect(str(host_id), password="poprawne-haslo")
        try:
            ring, _ = wait_event(host, "incoming", 4)
            host.answer_incoming(True, allow_input=True)
        except AssertionError:
            pass
        conn, _ = wait_event(ctrl2, "connected", 30)
        self.assertEqual(conn["role"], "controller")
        session = ctrl2.session
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and session.stats["frames_in"] < 2:
            time.sleep(0.2)
        self.assertGreaterEqual(session.stats["frames_in"], 2)

    # ------------------------------------------------------------------ view-only
    def test_view_only_blocks_input(self) -> None:
        host = make_agent("host-vo", self.port, confirm_incoming=True, allow_input=True)
        self.agents.append(host)
        wait_event(host, "address", 25)
        host_id = host.cfg.identity.desk_id

        ctrl = make_agent("ctrl-vo", self.port)
        self.agents.append(ctrl)
        wait_event(ctrl, "address", 25)
        ctrl.connect(str(host_id), view_only=True)
        ring, _ = wait_event(host, "incoming", 20)
        self.assertFalse(ring["want_input"], "kontroler prosil o sam podglad")
        host.answer_incoming(True, allow_input=False)
        wait_event(ctrl, "connected", 25)
        self.assertFalse(ctrl.session.granted_input)
        # liczymy tylko prawdziwe wejście - 'area' to ustawienie obszaru ekranu,
        # ktore host robi u siebie i nie ma znaczenia dla zgody na kontrole
        real = ("move", "button", "key", "text", "scroll", "hotkey")
        before = sum(1 for e in host.session.injector.events if e[0] in real)
        ctrl.session.send_mouse(0.3, 0.3)
        ctrl.session.send_key("a", "press", char="a")
        time.sleep(1.2)
        after = [e for e in host.session.injector.events if e[0] in real]
        self.assertEqual(len(after), before, f"view-only, a host wykonal wejscie: {after[before:]}")

        # a teraz host sam przyznaje sterowanie - wejscie ma przejsc
        host.session.set_input(True)
        got, _ = wait_event(ctrl, "grant", 10)
        self.assertTrue(got["input"], got)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            ctrl.session.send_mouse(0.7, 0.7)
            time.sleep(0.25)
            if any(e[0] == "move" for e in host.session.injector.events):
                break
        self.assertTrue(any(e[0] == "move" for e in host.session.injector.events),
                        "host przyznal sterowanie, a move nie dotarlo")

    # ------------------------------------------------------------------ bezposrednio po IP
    def test_direct_ip_session(self) -> None:
        """Ta sama siec / inne wifi: host nasluchuje, kontroler wali po adresie IP:port."""
        port = free_port()
        host = make_agent("host-direct", self.port, confirm_incoming=False,
                          listen_enabled=True, listen_port=port)
        host.cfg.set_password("klucz-lan")
        host.store.flush(force=True)
        self.assertTrue(host.start_direct(), "host nie wystartowal nasluchiwania")
        wait_event(host, "address", 25)

        # 1) zly klucz po IP - host ma sie rozlaczyc, a nie czekac w nieskonczonosc
        bad = make_agent("ctrl-direct-bad", self.port)
        self.agents.append(bad)
        wait_event(bad, "address", 25)
        bad.connect(f"127.0.0.1:{port}", password="nie-ten-klucz")
        fail, _ = wait_event(bad, "auth_failed", 25)
        self.assertEqual(fail["reason"], "password")
        wait_event(host, "closed", 20)

        # 2) dobry klucz - pelna sesja bez relaya
        ctrl = make_agent("ctrl-direct", self.port)
        self.agents.append(ctrl)
        wait_event(ctrl, "address", 25)
        res = ctrl.connect(f"127.0.0.1:{port}", password="klucz-lan")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "direct")
        conn, _ = wait_event(ctrl, "connected", 25)
        self.assertEqual(conn["role"], "controller")
        self.assertTrue(ctrl.session.granted_input, "host nie przyznal sterowania")

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and ctrl.session.stats["frames_in"] < 2:
            time.sleep(0.2)
        self.assertGreaterEqual(ctrl.session.stats["frames_in"], 2, "brak klatek na polaczeniu IP")

        self.assertTrue(host.session.peer_controls_me, "host nie ustawil peer_controls_me")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ctrl.session.send_mouse(0.2, 0.4)
            time.sleep(0.2)
            if any(e[0] == "move" for e in host.session.injector.events):
                break
        self.assertTrue(any(e[0] == "move" for e in host.session.injector.events),
                        "wejscie nie przeszlo polaczeniem bezposrednim")

    # ------------------------------------------------------------------ denial
    def test_declined_call(self) -> None:
        host = make_agent("host-decline", self.port, confirm_incoming=True)
        self.agents.append(host)
        wait_event(host, "address", 25)
        ctrl = make_agent("ctrl-decline", self.port)
        self.agents.append(ctrl)
        wait_event(ctrl, "address", 25)
        ctrl.connect(str(host.cfg.identity.desk_id))
        wait_event(host, "incoming", 20)
        host.answer_incoming(False)
        fail, _ = wait_event(ctrl, "connect_failed", 20)
        self.assertEqual(fail["reason"], "declined")

    # ------------------------------------------------------------------ offline
    def test_unknown_id_offline(self) -> None:
        ctrl = make_agent("ctrl-offline", self.port)
        self.agents.append(ctrl)
        wait_event(ctrl, "address", 25)
        ctrl.connect("111222333")
        fail, _ = wait_event(ctrl, "connect_failed", 20)
        self.assertEqual(fail["reason"], "offline")

    # ------------------------------------------------------------------ reclaim ID
    def test_id_reclaim_after_reconnect(self) -> None:
        first = make_agent("host-reclaim", self.port)
        self.agents.append(first)
        welcome, _ = wait_event(first, "address", 25)
        desk_id = welcome["id"]
        secret = first.cfg.identity.secret
        first.stop()
        time.sleep(0.8)
        second = Agent(ConfigStore(Path(TMP) / "cfg-host-reclaim.json"))
        self.agents.append(second)
        second.source_override = SyntheticSource(640, 400, fps=8)
        second.injector_override = Recorder()
        second.cfg.identity.secret = secret
        second.start_relay("127.0.0.1", self.port)
        welcome2, _ = wait_event(second, "address", 25)
        self.assertEqual(welcome2["id"], desk_id, "ID ucieklo po restarcie")


if __name__ == "__main__":
    unittest.main(verbosity=2)
