"""DeskLink - wspolne startowanie CLI i GUI.

Modul jest celowo "cienki": nic nie robi sam, tylko sklada konfiguracje i
wskazuje wlasciwy kod (agent / relay / GUI). Dzieki temu `python -m rd` dziala
tak samo natywnie na Windows 11, pod Wine i na Linuxie.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from .settings import ConfigStore, format_id


class DryInjector:
    """Injektor 'na sucho' - zapisuje wejscie zamiast ruszac prawdziwym kursorem.

    Uzywane w testach GUI i przy `--dry-inject`, zeby zdalna sesja byla w
    pełni funkcjonalna, ale nic nie kliknęła na pulpicie operatora.
    """

    def __init__(self, log_path: str = "") -> None:
        self.events: List[tuple] = []
        self.applied = 0
        self.rejected = 0
        self.last_error = ""
        self.enabled = True
        # DESKLINK_DRY_LOG: plik, do ktory dopisujemy zdarzenia - dzieki temu test
        # GUI w jednym procesie moze sprawdzic, co faktycznie dotarlo do hosta.
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None

    def _note(self, event: tuple) -> None:
        self.events.append(event)
        if self._log:
            import json

            self._log.write(json.dumps(list(event), ensure_ascii=False, default=str) + "\n")
            self._log.flush()

    def set_area(self, left, top, width, height) -> None:
        self._note(("area", left, top, width, height))

    def move(self, fx, fy) -> None:
        self.applied += 1
        self._note(("move", round(fx, 3), round(fy, 3)))

    def button(self, name, action, fx=None, fy=None, count=1) -> None:
        self.applied += 1
        self._note(("button", name, action, fx, fy))

    def scroll(self, dx, dy, fx=None, fy=None) -> None:
        self.applied += 1
        self._note(("scroll", dx, dy))

    def key(self, canon, action, char=None) -> None:
        self.applied += 1
        self._note(("key", canon, action, char))

    def hotkey(self, combo) -> None:
        self.applied += 1
        self._note(("hotkey", combo))

    def type_text(self, text, interval=0.008) -> None:
        self.applied += 1
        self._note(("text", text))

    def release_all(self) -> None:
        self._note(("release",))


# --------------------------------------------------------------------------- konfiguracja
def make_store(args: argparse.Namespace) -> ConfigStore:
    """ConfigStore + nadpisania z linii polecen (zeby nie edytowac JSONa recznie)."""
    if getattr(args, "config_dir", None):
        os.environ["XDG_CONFIG_HOME"] = str(Path(args.config_dir))
        if sys.platform.startswith("win"):
            os.environ["APPDATA"] = str(Path(args.config_dir))
    store = ConfigStore(Path(args.config) if getattr(args, "config", None) else None)
    cfg = store.config

    if getattr(args, "relay", None):
        host, _, port = str(args.relay).partition(":")
        cfg.relay_host = host
        if port:
            cfg.relay_port = int(port)
    if getattr(args, "relay_port", None):
        cfg.relay_port = int(args.relay_port)
    if getattr(args, "listen_port", None):
        cfg.listen_port = int(args.listen_port)
    if getattr(args, "no_relay", False):
        cfg.relay_host = ""
    if getattr(args, "lan", None) is not None:
        cfg.lan_discovery = bool(args.lan)
    if getattr(args, "fps", None):
        cfg.fps = int(args.fps)
    if getattr(args, "quality", None):
        cfg.quality = int(args.quality)
    if getattr(args, "scale", None):
        cfg.scale = float(args.scale)
    if getattr(args, "password", None):
        cfg.set_password(args.password)
    elif getattr(args, "clear_password", False):
        cfg.set_password("")
    if getattr(args, "auto_accept", False):
        cfg.confirm_incoming = False
        cfg.allow_input = True
        cfg.allow_files = True
    if getattr(args, "view_only", False):
        cfg.view_only_default = True
    store.flush(force=True)
    return store


def apply_overrides(agent, args: argparse.Namespace) -> None:
    """Wstrzykuje syntetyczny ekran / injektor 'na sucho' do Agenta."""
    if getattr(args, "dry_inject", False):
        agent.injector_override = DryInjector(os.environ.get("DESKLINK_DRY_LOG", ""))
    if getattr(args, "synthetic", False):
        from .capture import SyntheticSource

        agent.source_override = SyntheticSource(width=1280, height=720)


# --------------------------------------------------------------------------- polecenia
def cmd_gui(args: argparse.Namespace) -> int:
    store = make_store(args)
    try:
        from .agent import Agent
        from .gui.app import run_gui
    except ImportError as exc:  # np. brak python3-tk
        print("Nie moge odpalic GUI:", exc, file=sys.stderr)
        print("Zainstaluj Tkinter (Windows: installer Pythona ma go domyslnie, "
              "Linux: sudo apt install python3-tk, Wine: winetricks python3).",
              file=sys.stderr)
        return 2
    agent = Agent(store)
    apply_overrides(agent, args)
    run_gui(store=store, agent=agent, demo=bool(getattr(args, "demo", False)))
    return 0


def cmd_host(args: argparse.Namespace) -> int:
    """Agent bez GUI - nasluchuje i akceptuje polaczenia (do serwera / testow)."""
    store = make_store(args)
    from .agent import Agent

    agent = Agent(store)
    agent.start_direct()
    if store.config.relay_host:
        agent.start_relay()
    if store.config.lan_discovery:
        agent.start_lan()
    apply_overrides(agent, args)
    if not store.config.has_password and not store.config.confirm_incoming:
        print("UWAGA: brak hasla i brak zgody - kazdy z Twoim ID ma pelny dostep.",
              file=sys.stderr)
    print(f"DeskLink host  ID: {format_id(store.config.identity.desk_id)}   "
          f"port bezposredni: {store.config.listen_port}   "
          f"relay: {store.config.relay_host or '(brak)'}")
    print("Ctrl-C konczy sesje.")
    try:
        while True:
            try:
                kind, data = agent.ui.get(timeout=1.0)
                if kind == "address":
                    print(f"[relay] ID {format_id(int(data.get('id') or 0))}")
                elif kind in ("incoming", "permission", "grant", "closed", "session_closed",
                              "auth_failed", "log"):
                    print(f"[{kind}] {data}")
            except Exception:  # noqa: BLE001 - queue.Empty
                continue
    except KeyboardInterrupt:
        print("\nstop...")
    finally:
        agent.stop()
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    """Kontroler: laczy sie z ID lub ip:port. Domyślnie otwiera GUI sesji."""
    store = make_store(args)
    # '123 456 789' to trzy argumenty - skladamy je z powrotem w jedno ID
    target = " ".join(args.target) if isinstance(args.target, (list, tuple)) else args.target
    if getattr(args, "no_gui", False):
        return _connect_headless(store, target, args)
    from .gui.app import MainApp

    app = MainApp(store=store)
    app.entry_target.set_value(target)
    if args.password:
        app.entry_password.set_value(args.password)
    if args.view_only:
        app.chk_view_only.set(True)
    app.root.after(400, app.connect_now)
    app.run()
    return 0


def _connect_headless(store: ConfigStore, target: str, args: argparse.Namespace) -> int:
    from .agent import Agent

    agent = Agent(store)
    apply_overrides(agent, args)
    if store.config.relay_host:
        agent.start_relay()
    time.sleep(0.6)
    res = agent.connect(target, password=args.password or "", view_only=args.view_only)
    if not res.get("ok"):
        print("nie udalo sie:", res, file=sys.stderr)
        return 1
    print("polaczenie zainicjowane:", res)
    session = agent.session
    deadline = time.monotonic() + float(getattr(args, "for_", None) or 30)
    frames = 0
    try:
        while time.monotonic() < deadline:
            if session is None:
                session = agent.session
                continue
            img, _seq = session.pull_image()
            if img is not None:
                frames += 1
                if frames == 1:
                    print(f"pierwsza klatka {img.size[0]}x{img.size[1]}")
            if getattr(args, "click", None):
                fx, fy = (float(v) for v in args.click.split(","))
                session.send_mouse(fx, fy)
            if getattr(args, "type", None) and frames > 3:
                session.send_text(args.type)
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"klatek: {frames}")
        agent.stop()
    return 0


def cmd_id(args: argparse.Namespace) -> int:
    store = make_store(args)
    cfg = store.config
    print(f"ID:        {format_id(cfg.identity.desk_id)}")
    print(f"odcisk:    {cfg.identity.fp}")
    print(f"alias:     {cfg.identity.alias or '-'}")
    print(f"relay:     {cfg.relay_host or '(brak)'}:{cfg.relay_port}"
          f"{' (TLS)' if cfg.relay_tls else ''}")
    print(f"haslo:     {'ustawione' if cfg.has_password else 'brak'}")
    print(f"bezposr.:  port {cfg.listen_port} {'wlaczony' if cfg.listen_enabled else 'wylaczony'}")
    print(f"plik cfg:  {store.path}")
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    """Wlasny serwer rendezvous (ten sam kod co server/desklink_relay.py).

    Szukamy pliku w katalogu projektu, a gdy jestesmy spakowani PyInstallerem -
    w 'server' obok .exe albo wewnatrz archiwum (_MEIPASS). Zeby to drugie
    dzialalo, build_windows.sh pakuje katalog 'server' przez --add-data.
    """
    here = Path(__file__).resolve().parent.parent
    roots: List[Path] = [here / "server", Path(sys.executable).resolve().parent / "server"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.insert(1, Path(meipass) / "server")
    seen, uniq = set(), []
    for r in roots:
        if str(r) not in seen:
            seen.add(str(r))
            uniq.append(r)
    roots = uniq
    for cand in roots:
        if (cand / "desklink_relay.py").exists():
            sys.path.insert(0, str(cand))
            break
    else:
        print("Brak serwera relay (szukalem w: " + ", ".join(str(r) for r in roots) + ")",
              file=sys.stderr)
        return 2
    try:
        import desklink_relay  # noqa: WPS433
    except ImportError as exc:
        print("Brak serwera relay:", exc, file=sys.stderr)
        return 2
    argv = ["desklink_relay", "--host", args.bind, "--port", str(args.port)]
    if args.tls_cert:
        argv += ["--cert", args.tls_cert]
    if args.tls_key:
        argv += ["--key", args.tls_key]
    if args.secret:
        argv += ["--secret", args.secret, "--require-auth"]
    sys.argv = argv
    try:
        desklink_relay.main()
    except KeyboardInterrupt:
        print("\nrelay zatrzymany")
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="desklink",
        description="DeskLink - zdalny pulpit przez ID (klon AnyDesk) na Windows 11, Wine i Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="przyklady:\n"
               "  python -m rd                      # GUI\n"
               "  python -m rd id                   # moje ID i odcisk klucza\n"
               "  python -m rd relay --port 7001    # wlasny serwer rendezvous\n"
               "  python -m rd host --relay 10.0.0.5:7001 --password sekret\n"
               "  python -m rd connect 123 456 789 --relay 10.0.0.5\n"
               "  python -m rd connect 192.168.1.20:7000 --no-gui --for 20\n",
    )
    p.add_argument("--config-dir", metavar="DIR", help="katalog konfiguracji (izolowany profil)")
    p.add_argument("--config", metavar="FILE", help="sciezka do config.json")

    sub = p.add_subparsers(dest="command")

    gui = sub.add_parser("gui", help="uruchom interfejs graficzny (podglad i sterowanie)")
    _common(gui)
    gui.add_argument("--demo", action="store_true",
                     help="zamiast ekranu puszcz syntetyczna animacje (do testow UI)")

    host = sub.add_parser("host", help="agent bez GUI, nasluchuje i udostepnia ekran")
    _common(host)

    conn = sub.add_parser("connect", help="polacz sie z ID albo ip:port")
    conn.add_argument("target", nargs="+", help="'123 456 789' albo '192.168.1.20:7000'")
    _common(conn)
    conn.add_argument("--no-gui", action="store_true", help="bez okna: statystyki do terminala")
    conn.add_argument("--for", dest="for_", type=int, default=30, help="sekundy sesji (z --no-gui)")
    conn.add_argument("--click", metavar="X,Y", help="wspolrzedne 0..1, np. 0.5,0.5")
    conn.add_argument("--type", metavar="TEKST", help="wpisz tekst po drugiej stronie")


    ident = sub.add_parser("id", help="pokaz moje ID, odcisk i ustawienia")
    _config_opts(ident)

    relay = sub.add_parser("relay", help="uruchom serwer rendezvous (ID + tunel)")
    _config_opts(relay)
    relay.add_argument("--bind", default="0.0.0.0", help="adres nasluchu (domyslnie wszystkie)")
    relay.add_argument("--port", type=int, default=7001, help="port (domyslnie 7001)")
    relay.add_argument("--tls-cert", dest="tls_cert",
                       help="certyfikat TLS (w parze z --tls-key)")
    relay.add_argument("--tls-key", dest="tls_key", help="klucz TLS")
    relay.add_argument("--secret",
                       help="klucz wstepu; jesli podany, relay wymaga go od kazdego klienta"
                            " (po stronie agenta: 'relay_key')")
    return p


def _config_opts(sub: argparse.ArgumentParser) -> None:
    """Te same opcje co w parserze glownym, ale z SUPPRESS.

    Bez tego podparser nadpisalby 'default' i --config-dir podany przed
    'gui' znikalby. SUPPRESS = nie ruszamy wartosci, jesli flagi nie ma.
    """
    sub.add_argument("--config-dir", metavar="DIR", default=argparse.SUPPRESS,
                     help="katalog konfiguracji (izolowany profil)")
    sub.add_argument("--config", metavar="FILE", default=argparse.SUPPRESS,
                     help="sciezka do config.json")


def _common(sub: argparse.ArgumentParser) -> None:
    _config_opts(sub)
    sub.add_argument("--relay", help="serwer rendezvous: host albo host:port")
    sub.add_argument("--relay-port", type=int, help="port relaya")
    sub.add_argument("--listen-port", type=int, help="port polaczen bezposrednich")
    sub.add_argument("--no-relay", action="store_true", help="wyłącz relay (tylko LAN/IP)")
    sub.add_argument("--lan", type=int, choices=(0, 1), help="odkrywanie w sieci lokalnej (0/1)")
    sub.add_argument("--fps", type=int, help="klatek na sekunde przy nadawaniu")
    sub.add_argument("--quality", type=int, help="jakosc JPEG 20..95")
    sub.add_argument("--scale", type=float, help="skala obrazu 0.3..1.0")
    sub.add_argument("--view-only", action="store_true",
                     help="domyslnie tylko podglad (bez sterowania)")
    sub.add_argument("--password", help="ustaw haslo dostepu (puste = usun za pomoca --clear-password)")
    sub.add_argument("--clear-password", action="store_true", help="usun haslo dostepu")
    sub.add_argument("--auto-accept", action="store_true",
                     help="przyjmuj polaczenia bez pytania (uwaga: pelny dostep)")
    sub.add_argument("--dry-inject", action="store_true",
                     help="nie ruszaj prawdziwej myszki - zapisuj wejscie (testy)")
    sub.add_argument("--synthetic", action="store_true",
                     help="nadawaj syntetyczna animacje zamiast prawdziwego ekranu")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 'python -m rd 123 456 789' ->connect
    known = {"gui", "host", "connect", "id", "relay", "-h", "--help"}
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        argv = ["connect"] + argv
    if not argv:
        argv = ["gui"]
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"gui": cmd_gui, "host": cmd_host, "connect": cmd_connect,
                "id": cmd_id, "relay": cmd_relay}
    handler = handlers.get(args.command or "gui")
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args) or 0)
