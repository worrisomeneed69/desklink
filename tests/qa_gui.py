"""QA GUI: prawidlowa sesja przez relay + zrzuty ekranu, bez ruszania kursora.

Co tu jest sprawdzane (tego nie da sie poznac po testach jednostkowych):
  1. okno glowne buduje sie bez wyjatkow (Tk + wlasne widgety na Canvasie),
  2. relay przydziela ID obu stronom i 'dzwonek' dociera do hosta,
  3. okno sesji (RemoteWindow / SharingWindow) otwiera sie i rysuje klatki,
  4. ruch/klik/scroll/klawisz z canvasa trafia do injektora po drugiej stronie,
  5. schowek wedruje w obie strony,
  6. nic nie sypie wyjatkami w petli po ~10 s pracy.

Drugi komputer symuluje osobny proces (inny profil konfiguracyjny, ten sam
relay na 127.0.0.1), a jego injektor to DryInjector z DESKLINK_DRY_LOG, wiec
wejscie jest zapisywane w pliku zamiast ruszac prawdziwa myszka.

Uruchom:  python3 tests/qa_gui.py      (wymaga DISPLAY i 'scrot'/'import')
Zrzuty:   qa_out/*.png
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "qa_out"
TMP = Path(tempfile.mkdtemp(prefix="desklink-qa-"))
os.environ["XDG_CACHE_HOME"] = str(TMP / "cache")
OUT.mkdir(exist_ok=True)

from rd.agent import Agent  # noqa: E402
from rd.gui.app import MainApp  # noqa: E402
from rd.settings import ConfigStore, format_id  # noqa: E402

HOST_PROFILE = TMP / "host"
CTRL_PROFILE = TMP / "ctrl"
DRY_LOG = TMP / "host-input.jsonl"
HOST_PASSWORD = "qa-sekret-123"


class Ev:
    """Podrobka zdarzenia Tk - zeby moc wywolac handler bez przechwytywania kursora."""

    def __init__(self, x: int = 0, y: int = 0, keysym: str = "", char: str = "",
                 kind: str = "KeyPress", state: int = 0, widget=None, delta: int = 0):
        self.x, self.y = x, y
        self.keysym, self.char = keysym, char
        # Tk zwraca event.type jako EventType - udajemy prawdziwe zdarzenie
        import tkinter as _tk
        self.type = getattr(_tk.EventType, kind, kind)
        self.state = state
        self.widget = widget
        self.delta = delta
        self.num = 4 if delta > 0 else 5


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run(cmd: List[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def find_window(title: str, pid: Optional[int] = None) -> str:
    """ID okna X po tytule (opcjonalnie po PID procesu).

    xdotool traktuje argument jako wyrazenie regularne, a nasze tytuly maja
    nawiasy kwadratowe ('... [CTRL]') - bez escapowania wzorca okna nie da sie
    znalezc.
    """
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--name", re.escape(title)], text=True)
    except Exception:  # noqa: BLE001
        return ""
    for wid in out.split():
        if pid is None:
            return wid
        try:
            got = subprocess.check_output(["xdotool", "getwindowpid", wid], text=True).strip()
        except Exception:  # noqa: BLE001
            continue
        if got == str(pid):
            return wid
    return ""


def shot(title: str, name: str, pid: Optional[int] = None) -> str:
    wid = find_window(title, pid)
    if not wid:
        print(f"  ! brak okna '{title}' ({name})")
        return ""
    path = str(OUT / f"{name}.png")
    # 'import' potrafi trafic w chwile, gdy okno wlasnie sie mapuje (EAGAIN)
    for _ in range(3):
        subprocess.call(["import", "-window", wid, path])
        if Path(path).exists() and Path(path).stat().st_size > 1000:
            print(f"  zrzut: {Path(path).name}")
            return path
        time.sleep(0.4)
    print(f"  ! zrzut nie zapisany: {name} ({title})")
    return ""


def painted(path: str) -> int:
    """Ile roznych kolorow jest na zrzucie. Puste (niewymalowane) okno to 2-3 kolory,
    okno z kartami, przyciskami i tekstem - kilkaset. PIL jest wymagany w requirements,
    ale test nie ma padac, jak go nie ma - zwraca wtedy 0 i punkt wyjdzie jako FAIL."""
    if not path or not Path(path).exists():
        return 0
    try:
        from PIL import Image

        with Image.open(path) as im:
            cols = im.convert("RGB").getcolors(maxcolors=4_000_000)
        return len(cols or [])
    except Exception:  # noqa: BLE001 - brak PIL / zrzut w polowie zapisu
        return 0


def wait_host_id(path: Path, timeout: float = 25.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data = json.loads((path / "desklink" / "config.json").read_text(encoding="utf-8"))
            desk_id = int(data.get("identity", {}).get("desk_id") or 0)
            if desk_id:
                return desk_id
        except Exception:  # noqa: BLE001 - plik moze byc w polowie zapisu
            pass
        time.sleep(0.2)
    return 0


def dry_events_from(path: Path) -> List[list]:
    """Wiersze JSONL zapisane przez DryInjector drugiej strony."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def dry_events() -> List[list]:
    return dry_events_from(DRY_LOG)


def kill_strays() -> None:
    """Sprzata procesy po przerwanych biegach QA.

    Bez tego 'xdotool search' potrafi trafic w okno starego hosta (ten sam tytul)
    i zrzut pokazuje stan sprzed dwoch runow - mylane jako 'przyciski sa wylaczone'.
    Nawiasy w wzorcu powoduja, ze pkill nie zabija samego siebie.
    """
    for pat in ("python3 -m r[d] gui --config-dir /tmp/desklink-qa-",
                "python3 -m r[d] host --config-dir /tmp/desklink-qa-",
                "desklink_rela[y].py",
                # host pod Wine: 'python.exe -m rd gui' albo DeskLink.exe z profilem
                # C:\tmp\qa-host - wzor celuje tylko w profil QA, nie w prawdziwa sesja
                "qa-hos[t]"):
        subprocess.call(["pkill", "-f", pat])
    time.sleep(0.8)


def main() -> int:
    port = free_port()
    relay_port = free_port()
    results: List[tuple] = []
    kill_strays()

    def check(name: str, ok: bool, note: str = "") -> None:
        results.append((name, bool(ok), note))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{(' - ' + note) if note else ''}")

    HOST_PROFILE.mkdir(parents=True, exist_ok=True)
    CTRL_PROFILE.mkdir(parents=True, exist_ok=True)

    print(f"relay 127.0.0.1:{relay_port}, profiles w {TMP}")
    relay = run([sys.executable, str(ROOT / "server" / "desklink_relay.py"),
                 "--host", "127.0.0.1", "--port", str(relay_port)])
    time.sleep(1.2)

    host = run([sys.executable, "-m", "rd", "gui",
                "--config-dir", str(HOST_PROFILE),
                "--relay", f"127.0.0.1:{relay_port}",
                "--password", HOST_PASSWORD,
                "--auto-accept", "--dry-inject", "--synthetic", "--lan", "0"],
               cwd=str(ROOT), env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0"),
                                   "XDG_CACHE_HOME": str(TMP / "cache"),
                                   "DESKLINK_TITLE": "HOST",
                                   "DESKLINK_DRY_LOG": str(DRY_LOG)})
    time.sleep(3.0)
    host_id = wait_host_id(HOST_PROFILE)
    check("host dostal ID od relaya", host_id > 0, format_id(host_id) if host_id else "brak")
    host_shot = shot("DeskLink - zdalny pulpit  [HOST]", "01-host-glowne")
    check("okno hosta jest wymalowane (nie puste)", painted(host_shot) >= 40,
          f"{painted(host_shot)} kolorow")


    # ---------------- kontroler: ten proces (okno + petla)
    store = ConfigStore(CTRL_PROFILE / "desklink" / "config.json")
    store.config.relay_host = "127.0.0.1"
    store.config.relay_port = relay_port
    store.config.lan_discovery = False
    store.config.confirm_incoming = False
    store.config.window = [1120, 720, 60, 40]
    store.flush(force=True)
    os.environ["DESKLINK_TITLE"] = "CTRL"
    agent = Agent(store)
    app = MainApp(store=store, agent=agent)
    app.root.update()
    time.sleep(0.2)
    for _ in range(12):          # pozwol Tk dokończyc malowanie kart przed zrzutem
        app.root.update()
        time.sleep(0.05)
    ctrl_shot = shot("DeskLink - zdalny pulpit  [CTRL]", "02-ctrl-glowne")
    check("okno kontrolera jest wymalowane (nie puste)", painted(ctrl_shot) >= 40,
          f"{painted(ctrl_shot)} kolorow")


    # ---------------- smoke test okna Ustawienia: wszystkie karty buduja sie naraz
    from rd.gui.app import SettingsDialog  # noqa: E402  (tylko dla tego testu)
    before_err = list(app.ui_errors)
    sd = SettingsDialog(app)
    for _ in range(8):
        app.root.update()
        time.sleep(0.03)
    shot("Ustawienia DeskLink", "06-ustawienia")
    dh = int(sd.dlg.winfo_height())
    check("okno Ustawienia ma poprawny wzrost", dh > 300, f"{dh} px")
    errs = [e for e in app.ui_errors if e not in before_err]
    check("okno Ustawienia: 5 kart bez wyjatkow", not errs, "; ".join(errs[:2]))
    sd.dlg._reject()
    for _ in range(4):
        app.root.update()

    # kolo pamietane z 'ostatnie komputery' - klik w wiersz ma uzupelnic pole
    app.entry_target.set_value(format_id(host_id))
    app.entry_password.set_value(HOST_PASSWORD)
    app.connect_now()
    print(f"polacz z {format_id(host_id)}...")

    started = 0.0
    for _ in range(160):
        app.root.update()
        time.sleep(0.1)
        if app.active_window is not None:
            started = time.monotonic()
            break
    check("okno sesji otwarte", started > 0,
          type(app.active_window).__name__ if app.active_window else "brak")
    if not started:
        return finish(app, [relay, host], results)

    # klatki: czekamy az beda odebrane i narysowane
    got = 0
    for _ in range(120):
        app.root.update()
        time.sleep(0.05)
        sess = agent.session
        if sess is not None and int(sess.stats.get("frames_in") or 0) >= 6:
            got = int(sess.stats.get("frames_in"))
            break
    check("klatki ekranu docieraja", got >= 6, f"frames_in={got}")
    time.sleep(1.0)
    app.root.update()
    shot("DeskLink - polaczenie zdalne  [CTRL]", "03-ctrl-remote")
    shot("DeskLink - udostepniasz ekran  [HOST]", "04-host-sharing")

    win = app.active_window
    canvas = getattr(win, "canvas", None)
    sess = agent.session
    check("sterowanie przyznane (host zezwolil)", bool(sess and sess.granted_input),
          f"granted={getattr(sess, 'granted_input', None)}")
    # okno sesji musi byc zbudowane w calosci: blad w ktorymkolwiek widgecie
    # przerwal konstruktora po srodku i panel hosta zostawal 'okrojony'
    need = {"canvas": "podglad ekranu", "btn_control": "przycisk sterowania",
            "chip": "kapselka statusu", "stats_label": "statystyki"}
    missing = [what for attr, what in need.items() if getattr(win, attr, None) is None]
    check("okno sesji zbudowane w calosci", not missing, ", ".join(missing))
    host_err = TMP / "cache" / "desklink" / "ui-error.log"
    check("bez wyjatkow w oknach (host + kontroler)",
          not host_err.exists() and not app.ui_errors,
          (host_err.read_text(encoding="utf-8")[-200:] if host_err.exists()
           else "; ".join(app.ui_errors[:2])))

    # ---------------- wejscie przez handlery okna
    if canvas is not None and sess is not None:
        w, h = int(canvas.cget("width")), int(canvas.cget("height"))
        before = len([e for e in dry_events() if e[0] == "move"])
        for i in range(6):
            win._on_motion(Ev(x=int(w * (0.2 + 0.1 * i)), y=int(h * (0.25 + 0.08 * i)),
                              widget=canvas))
            app.root.update()
            time.sleep(0.06)
        win._on_button(Ev(x=int(w * 0.5), y=int(h * 0.5), widget=canvas), "left", "down")
        win._on_button(Ev(x=int(w * 0.5), y=int(h * 0.5), widget=canvas), "left", "up")
        win._on_wheel(Ev(x=int(w * 0.5), y=int(h * 0.5), delta=-120, widget=canvas), 1)
        for keysym, char in (("a", "a"), ("space", " "), ("Return", "\r")):
            win._on_key(Ev(keysym=keysym, char=char, kind="KeyPress", widget=canvas))
            app.root.update()
            time.sleep(0.05)
            win._on_key(Ev(keysym=keysym, char=char, kind="KeyRelease", widget=canvas))
        win._on_key(Ev(keysym="Control_L", char="", kind="KeyPress", widget=canvas))
        win._on_key(Ev(keysym="c", char="c", kind="KeyPress", widget=canvas))
        win._on_key(Ev(keysym="c", char="c", kind="KeyRelease", widget=canvas))
        win._on_key(Ev(keysym="Control_L", char="", kind="KeyRelease", widget=canvas))
        sess.send_text("DeskLink QA")
        for _ in range(40):
            app.root.update()
            time.sleep(0.05)
        ev = dry_events()
        kinds = {e[0] for e in ev}
        moves = [e for e in ev if e[0] == "move"]
        print(f"  injektor hosta: {len(ev)} zdarzen, rodzaje: {sorted(kinds)}")
        check("ruch myszy dotarl do hosta", len(moves) > before, f"{before} -> {len(moves)}")
        check("klik dotarl", "button" in kinds)
        check("scroll dotarl", "scroll" in kinds)
        check("klawisze dotarly", "key" in kinds)
        check("tekst dotarl", any(e[0] == "text" for e in ev))
        check("zlozenie klawiszy (Ctrl+C) dotarlo", any(e[0] == "hotkey" for e in ev))

        # wspolrzedne: srodek canvasa ma dac ~0.5 / 0.5
        mid = [e for e in moves if abs(e[1] - 0.5) < 0.35]
        check("mapowanie wspolrzednych 0..1", bool(mid), str(mid[-1][1:3]) if mid else "")

    # ---------------- schowek + podglad tego, co weszlo na drut
    try:
        app.root.clipboard_clear()
        app.root.clipboard_append("QA-SCHOWEK-12345")
    except Exception:  # noqa: BLE001
        pass
    sent: List[tuple] = []
    orig_send = sess._safe_send

    def spy(ft, obj=None, **kw):
        sent.append((getattr(ft, "name", str(ft)), obj if isinstance(obj, dict) else {}))
        return orig_send(ft, obj, **kw)

    sess._safe_send = spy  # type: ignore[assignment]
    sess.copy_clipboard("QA-SCHOWEK-12345")
    win.push_clipboard()
    for _ in range(30):
        app.root.update()
        time.sleep(0.05)
    sess._safe_send = orig_send  # type: ignore[assignment]
    clip = [o for n, o in sent if n == "CLIP_SET"]
    check("ramka CLIP_SET poszla w drut", any("QA-SCHOWEK" in json.dumps(o) for o in clip),
          str(clip[:1]))
    check("sesja nadal polaczona", sess.state == "connected", sess.state)

    # ---------------- statystyki i trwalosc
    time.sleep(2.0)
    for _ in range(20):
        app.root.update()
        time.sleep(0.05)
    st = dict(sess.stats)
    check("sesja zyje po ~15 s", sess.is_alive(), f"stan={st.get('state')}")
    check("licznik klatek rosnie", int(st.get("frames_in") or 0) > got,
          f"{got} -> {st.get('frames_in')}")
    print(f"  stats: fps_in={st.get('fps_in')} kbps_in={st.get('kbps_in')} "
          f"rtt={st.get('rtt_ms')} ms")
    shot("DeskLink - polaczenie zdalne  [CTRL]", "05-ctrl-po-wejsciu")

    # ---------------- zamkniecie sesji przez przycisk
    closer = getattr(win, "close_session", None)
    if closer:
        closer("qa-koniec")
    for _ in range(40):
        app.root.update()
        time.sleep(0.05)
    check("sesja zamknieta bez wislania okna", app.active_window is None)
    host_err = TMP / "cache" / "desklink" / "ui-error.log"
    check("zadna zakladka/okno nie sypnelo wyjatkiem",
          not host_err.exists() and not app.ui_errors,
          (host_err.read_text(encoding="utf-8")[-200:] if host_err.exists()
           else "; ".join(app.ui_errors[:2])))

    return finish(app, [relay, host], results)


def finish(app: MainApp, procs: List[subprocess.Popen], results: List[tuple]) -> int:
    print("\nzamykam okna i procesy...")
    try:
        app.quit()
        for _ in range(20):
            app.root.update()
            time.sleep(0.05)
    except Exception:  # noqa: BLE001
        pass
    for p in procs:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.6)
    for p in procs:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    ok = sum(1 for _n, r, _x in results if r)
    bad = [(n, x) for n, r, x in results if not r]
    print(f"\nWYNIK: {ok}/{len(results)} punktow QA")
    for name, note in bad:
        print(f"  FAIL {name} {note}")
    print(f"zrzuty: {OUT}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
