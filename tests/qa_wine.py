"""QA miedzy systemami: host pod Wine (Windows Python) + kontroler pod Linuksem.

Dokladnie scenariusz z zadania: dwa komputery, dwa systemy, inne sieci. 'Inny
komputer' gra tu proces Windowsowego Pythona 3.11 w IZOLOWANYM prefiksie Wine
(.wine-desklink), a 'inne wifi' to fakt, ze strony nic o sobie nie wiedza -
spotykaja sie przez relay po 9-cyfrowym ID, tak jak w AnyDesk.

Sprawdza to, czego nie da test na jednym systemie:
  1. czy koder obrazu i capture dzialaja po stronie 'Windows' (u nas: Wine + mss),
  2. czy uklad ekranu drugiej strony (inny OS = inna rozdzielczosc i offsety) jest
     przeliczany na wspolrzedne 0..1,
  3. czy wejscie leci w druga strone i wpada do injektora na Windowsie,
  4. czy E2E szyfrowanie zgadza sie miedzy implementacjami (ten sam kod, ale
     inne biblioteki: cryptography-wheel pod Windows vs systemowy pod Linuxem).

Wymaga: wine + wystawiony prefiks (scripts/setup_wine_windows.sh), DISPLAY,
'import' z ImageMagicka.
Uruchom:  DISPLAY=:0 python3 tests/qa_wine.py
          DISPLAY=:0 DESKLINK_QA_WINE_EXE=1 python3 tests/qa_wine.py
          # z ta druga zmienna hostem jest zamrozony dist/gui/DeskLink.exe
          # (scripts/build_windows.sh) - dowod, ze dziala to, co trafia na Windows 11
Zrzuty:   qa_out/wine-*.png
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import qa_gui as Q                      # noqa: E402  (helpery: shot, wait_host_id, Ev)
from rd.agent import Agent              # noqa: E402
from rd.gui.app import MainApp          # noqa: E402
from rd.settings import ConfigStore, format_id  # noqa: E402

OUT = ROOT / "qa_out"
PREFIX = ROOT / ".wine-desklink"
PY_WIN = "C:\\Python311\\python.exe"
# DeskLINK_QA_WINE_EXE=1 -> hostem jest zamrozony plik .exe z dist/, nie python.exe.
# Dowod, ze to co buduje scripts/build_windows.sh dziala tak samo jak kod z repo.
EXE_WIN = "C:\\desklink\\dist\\gui\\DeskLink.exe"
USE_EXE = os.environ.get("DESKLINK_QA_WINE_EXE", "").strip() in ("1", "true", "yes")
HOST_PASSWORD = "wine-qa-123"
WINE_TMP = PREFIX / "drive_c" / "tmp"


def have_wine() -> bool:
    if shutil.which("wine") is None:
        return False
    if USE_EXE:
        return (ROOT / "dist" / "gui" / "DeskLink.exe").exists()
    return (PREFIX / "drive_c" / "Python311" / "python.exe").exists()


def host_cmd(relay_addr: str) -> List[str]:
    """Komenda startowa hosta 'Windows' - .exe (jedno klikniecie) albo modul rd."""
    args = ["gui", "--config-dir", "C:\\tmp\\qa-host", "--relay", relay_addr,
            "--password", HOST_PASSWORD, "--auto-accept", "--dry-inject", "--lan", "0"]
    if USE_EXE:
        return ["wine", EXE_WIN] + args
    return ["wine", PY_WIN, "-m", "rd"] + args


def main() -> int:
    OUT.mkdir(exist_ok=True)
    if not have_wine():
        print("POMINIALEM: brak wine albo brak Windows Pythona w prefiksie.")
        print("  wystaw go:  ./scripts/setup_wine_windows.sh")
        return 0

    Q.kill_strays()          # hosty i releje po przerwanych biegach
    tmp = Path(tempfile.mkdtemp(prefix="desklink-qa-wine-"))
    results: List[tuple] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        results.append((name, ok, note))
        print(("PASS  " if ok else "FAIL  ") + name + (f" - {note}" if note else ""))

    def wait_id_win(profile: Path, timeout: float = 90.0) -> int:
        """Pod Windowsem katalog nazywa sie 'DeskLink', pod Linuxem 'desklink'."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for sub in ("DeskLink", "desklink"):
                cfg = profile / sub / "config.json"
                try:
                    data = json.loads(cfg.read_text(encoding="utf-8"))
                    did = int(data.get("identity", {}).get("desk_id") or 0)
                    if did:
                        return did
                except Exception:  # noqa: BLE001 - plik bywa w polowie zapisu
                    pass
            time.sleep(0.4)
        return 0

    relay_port = Q.free_port()
    env_base = {**os.environ,
                "WINEPREFIX": str(PREFIX),
                "WINEDEBUG": "-all",
                "DISPLAY": os.environ.get("DISPLAY", ":0")}
    print(f"relay 127.0.0.1:{relay_port}, Wine prefiks {PREFIX.name}")

    relay = subprocess.Popen([sys.executable, str(ROOT / "server" / "desklink_relay.py"),
                              "--host", "127.0.0.1", "--port", str(relay_port)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1.2)

    # ---- host 'Windows': profile i log injektora trzymamy w C:\tmp prefiksu
    WINE_TMP.mkdir(parents=True, exist_ok=True)
    dry_win = "C:\\tmp\\wine-input.jsonl"
    dry_unix = WINE_TMP / "wine-input.jsonl"
    profile_unix = WINE_TMP / "qa-host"
    shutil.rmtree(profile_unix, ignore_errors=True)
    host = subprocess.Popen(
        host_cmd(f"127.0.0.1:{relay_port}"),
        cwd=str(ROOT), env={**env_base, "DESKLINK_TITLE": "WINE",
                            "DESKLINK_DRY_LOG": dry_win})
    # onefile najpierw rozpakowuje sie do tempa - dajemy wiecej czasu na pierwsze ID
    host_id = wait_id_win(profile_unix, 150.0 if USE_EXE else 90.0)
    check("host pod Wine dostal ID od relaya", host_id > 0, format_id(host_id) if host_id else "brak")
    # Wine maluje duze okno z opoznieniem po pierwszym mapowaniu - bez tego oddechu
    # zrzut wychodzi ciemnym pustym prostokatem z jednym paskiem logu.
    time.sleep(3.0)
    shot_path = Q.shot("DeskLink - zdalny pulpit  [WINE]", "wine-01-host")
    ncol = Q.painted(shot_path)
    check("okno hosta pod Wine jest wymalowane", ncol >= 40,
          f"{ncol} roznych kolorow na zrzucie")

    if host.poll() is not None:
        out = host.stdout.read().decode("utf-8", "replace") if host.stdout else ""
        print("  wine host padl:\n" + out[-2000:])
        return 1

    # ---- kontroler: ten proces (Linux)
    ctrl_profile = tmp / "ctrl"
    ctrl_profile.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    store = ConfigStore(ctrl_profile / "desklink" / "config.json")
    store.config.relay_host = "127.0.0.1"
    store.config.relay_port = relay_port
    store.config.lan_discovery = False
    store.config.confirm_incoming = False
    store.config.window = [1180, 740, 40, 40]
    store.flush(force=True)
    os.environ["DESKLINK_TITLE"] = "CTRL"
    agent = Agent(store)
    app = MainApp(store=store, agent=agent)
    app.root.update()
    time.sleep(0.3)

    app.entry_target.set_value(format_id(host_id))
    app.entry_password.set_value(HOST_PASSWORD)
    app.connect_now()
    print(f"dzwonie na Wine host {format_id(host_id)}...")

    win = None
    for _ in range(200):
        app.root.update()
        time.sleep(0.1)
        if app.active_window is not None:
            win = app.active_window
            break
    check("sesja z Wine otwarta", win is not None, type(win).__name__ if win else "brak")
    if win is None:
        return Q.finish(app, [relay, host], results)

    got = 0
    size = (0, 0)
    # Wine plus Python z /mnt potrafi startowac znacznie dluzej niz zamrozony
    # .exe, wiec czekamy do 60 s, ale wychodzimy przy pierwszych 8 klatkach.
    for i in range(1200):
        app.root.update()
        time.sleep(0.05)
        sess = agent.session
        if sess is not None:
            size = (int(sess.stats.get("frame_w") or 0), int(sess.stats.get("frame_h") or 0))
            if int(sess.stats.get("frames_in") or 0) >= 8:
                got = int(sess.stats.get("frames_in"))
                break
        if i and i % 200 == 0:
            f = int((agent.session.stats.get("frames_in") or 0)) if agent.session else 0
            print(f"  ...bez klatek po {i * 0.05:.0f} s (frames_in={f})")
    check("Wine nadaje obraz ekranu", got >= 8, f"frames_in={got} rozmiar={size[0]}x{size[1]}")
    time.sleep(1.2)
    app.root.update()
    Q.shot("DeskLink - polaczenie zdalne  [CTRL]", "wine-02-podglad")
    Q.shot("DeskLink - udostepniasz ekran  [WINE]", "wine-03-panel-hosta")

    # ---- wejscie w strone Wine: ruch + klik + klawisz
    sess = agent.session
    canvas = getattr(win, "canvas", None)
    if sess is not None and canvas is not None:
        time.sleep(1.0)
        for _ in range(30):
            app.root.update()
            time.sleep(0.05)
        w, h = int(canvas.cget("width")), int(canvas.cget("height"))
        for i in range(5):
            win._on_motion(Q.Ev(x=int(w * (0.3 + 0.08 * i)), y=int(h * 0.4), widget=canvas))
            app.root.update()
            time.sleep(0.08)
        win._on_button(Q.Ev(x=int(w * 0.5), y=int(h * 0.5), widget=canvas), "left", "down")
        win._on_button(Q.Ev(x=int(w * 0.5), y=int(h * 0.5), widget=canvas), "left", "up")
        for _ in range(40):
            app.root.update()
            time.sleep(0.05)
        evs = Q.dry_events_from(dry_unix)
        kinds = {e[0] for e in evs}
        check("wejscie dotarlo do injektora na Windowsie", "move" in kinds and "button" in kinds,
              f"{len(evs)} zdarzen: {sorted(kinds)}")
        check("wspolrzedne z Wine w zakresie 0..1",
              all(0.0 <= float(e[1]) <= 1.0 and 0.0 <= float(e[2]) <= 1.0
                  for e in evs if e[0] == "move") if evs else False)
        st = dict(sess.stats)
        e2e = bool(getattr(sess, "keys", None) and not sess.keys.plain)
        print(f"  stats: fps_in={st.get('fps_in')} kbps_in={st.get('kbps_in')} "
              f"rtt={st.get('rtt_ms')} ms e2e={'tak' if e2e else 'NIE'}")
        check("sesja Wine jest szyfrowana E2E", e2e)

    # ---- zamkniecie
    closer = getattr(win, "close_session", None)
    if closer:
        closer("qa-wine-koniec")
    for _ in range(40):
        app.root.update()
        time.sleep(0.05)
    check("sesja zamknieta czysto", app.active_window is None)

    rc = Q.finish(app, [relay, host], results)
    # 'wine ...' to proces-uniksa; Windowsowa strona przezywa terminate() (trzyma ja
    # wineserver), wiec sprzatamy dopiero teraz - po zrzutach i po werdykcie.
    Q.kill_strays()
    print(f"  profile QA: {tmp}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
