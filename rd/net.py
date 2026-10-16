"""DeskLink - transport.

Trzy sposoby dotarcia do drugiej maszyny:

1. RELAY (dziala przez kazde WiFi/NAT, bez przekierowywania portow) - oba kompy
   wychodzaco lacza sie do desklink-relay, serwer przypina numeryczne ID w stylu
   AnyDesk i po zaakceptowaniu zaproszenia dokleja DRUGIE polaczenie, przez ktore
   płynie rura z bajtami. Rura jest AES-GCM E2E, więc relay nic nie widzi.
2. ŁĄCZENIE BEZPOŚREDNIE (ip:port) - bez serwera: LAN albo przekierowany port.
3. ODKRYWANIE W LAN - broadcast UDP, zobacz komputery obok bez znajomosci ID.
"""

from __future__ import annotations

import json
import queue
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .protocol import Pipe

LAN_PORT = 51255
LAN_MAGIC = b"DESKLINK-DISCOVER-V1"
KEEPALIVE = 22.0
RELAY_TIMEOUT = 95.0
DEFAULT_DIRECT_PORT = 7000


def _tls_context(insecure: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class LineBuf:
    """Bufor nad gniazdem: czyta linie JSON, a przy handover oddaje nadwyzke bajtow."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.closed = False

    def readline(self, timeout: Optional[float] = 2.0) -> Optional[str]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[:nl]).decode("utf-8", "replace")
                del self.buf[: nl + 1]
                return line
            if self.closed:
                return None
            # cap 1 s: watek reader nigdy nie trzyma 'zajetego' gniazda dlugo,
            # dzieki temu stop()/close() reaguje od razu
            left = 0.4 if deadline is None else min(1.0, max(0.05, deadline - time.monotonic()))
            try:
                self.sock.settimeout(left)
                chunk = self.sock.recv(32 * 1024)
            except (socket.timeout, TimeoutError):
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                continue
            except OSError:
                self.closed = True
                return None
            self.sock.settimeout(None)
            if not chunk:
                self.closed = True
                return None
            self.buf.extend(chunk)

    def take(self) -> bytes:
        rest = bytes(self.buf)
        self.buf.clear()
        return rest


class SignalingError(Exception):
    pass


class RelayLink:
    """Klient serwera relay/rendezvous.

    Watek `run` utrzymuje dlugie polaczenie sygnalizacyjne i wrzuca do `events`:
      ('welcome', msg)      - dostalismy ID
      ('ringing', msg)      - nasz wywolanie zostalo przyiete (oczekuj 'join')
      ('ring', msg)         - ktos dzwoni do nas
      ('ring_cancel', msg)  - wywolajacy odwolal
      ('join', msg)         - zaproszenie przyjęte, otwieramy drugie gniazdo (potem 'joined')
      ('joined', msg)       - rura gotowa: msg['pipe'] to Pipe do sesji
      ('tunnel', msg)       - status otwarcia/zamkniecia rury
      ('fail', msg)         - offline / busy / timeout / declined
      ('error', msg)
      ('closed', msg)       - sygnalizacja padla (auto-reconnect w tle)
    """

    def __init__(
        self,
        host: str,
        port: int,
        identity,
        name: str,
        tls: bool = False,
        insecure_tls: bool = False,
        key: str = "",
        on_state: Optional[Callable[[], dict]] = None,
    ):
        self.host = host
        self.port = port
        self.identity = identity
        self.name = name
        self.tls = tls
        self.insecure_tls = insecure_tls
        self.key = key
        self.on_state = on_state or (lambda: {})
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.connected = False
        self.last_error = ""
        self.desk_id = int(identity.desk_id or 0)
        self.stop = threading.Event()
        self.alias = getattr(identity, "alias", "") or ""
        self._lines: Optional[LineBuf] = None
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._last_rx = time.time()
        self._thread: Optional[threading.Thread] = None
        self._joining: Dict[str, bool] = {}

    # ------------------------------------------------------------------ zycie
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="relay-link", daemon=True)
        self._thread.start()

    def _emit(self, kind: str, data) -> None:
        self.events.put((kind, data))

    def run(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self._connect_and_serve()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = str(exc)
                self._emit("closed", {"error": str(exc)})
            if self.stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    def _new_socket(self) -> socket.socket:
        s = socket.create_connection((self.host, self.port), timeout=10)
        s.settimeout(None)
        if self.tls:
            s = _tls_context(self.insecure_tls).wrap_socket(s, server_hostname=self.host)
        return s

    def _connect_and_serve(self) -> None:
        sock = self._new_socket()
        self._sock = sock
        self._lines = LineBuf(sock)
        self.connected = True
        self.last_error = ""
        self._send(
            {
                "t": "hello",
                "ver": 1,
                "name": self.name,
                "pub": self.identity.public_b64,
                "fp": self.identity.fp,
                "want_id": self.desk_id or None,
                "secret": self.identity.secret,
                "alias": self.alias,
                "key": self.key,
                "platform": _os_name(),
            }
        )
        self._last_rx = time.time()
        while not self.stop.is_set():
            if time.time() - self._last_rx > RELAY_TIMEOUT:
                raise ConnectionError("relay milczy (timeout)")
            line = self._lines.readline(timeout=KEEPALIVE)
            if line is None:
                if self._lines.closed:
                    raise ConnectionError("relay zamknal polaczenie")
                self._send({"t": "ping", "state": self.on_state()})
                continue
            line = line.strip()
            if not line:
                continue
            self._last_rx = time.time()
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle(msg)
        self._cleanup()

    def _handle(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "welcome":
            self.desk_id = int(msg.get("id") or 0)
            self.identity.desk_id = self.desk_id
            if msg.get("secret"):
                self.identity.secret = msg["secret"]
            self.alias = msg.get("alias") or self.alias
            self._emit("welcome", msg)
        elif kind == "ring":
            self._emit("ring", msg)
        elif kind == "ringing":
            self._emit("ringing", msg)
        elif kind == "ring_cancel":
            self._emit("ring_cancel", msg)
        elif kind == "join":
            # zaproszenie zaakceptowane - otwieramy DRUGIE polaczenie (rura)
            token = str(msg.get("token") or "")
            role = str(msg.get("role") or "host")
            if not self._joining.get(token):
                self._joining[token] = True
                threading.Thread(
                    target=self._open_join, args=(token, role, int(msg.get("peer") or 0)), daemon=True
                ).start()
        elif kind == "tunnel":
            self._emit("tunnel", msg)
            if msg.get("status") == "closed":
                self._joining.clear()
        elif kind in ("fail",):
            self._emit("fail", msg)
        elif kind in ("error",):
            self._emit("error", msg)
        elif kind in ("alias_ok", "lookup_result", "pong", "state", "busy", "peer"):
            self._emit(kind, msg)
        else:
            self._emit("unknown", msg)

    def _open_join(self, token: str, role: str, peer_id: int) -> None:
        """Drugie polaczenie: 'join' -> waiting -> joined -> od tego momentu bajty."""
        try:
            sock = self._new_socket()
        except OSError as exc:
            self._emit("fail", {"peer": peer_id, "reason": f"join-connect: {exc}"})
            return
        lines = LineBuf(sock)
        try:
            sock.sendall((json.dumps({"t": "join", "token": token, "role": role, "key": self.key}) + "\n").encode())
        except OSError as exc:
            self._emit("fail", {"peer": peer_id, "reason": f"join-send: {exc}"})
            sock.close()
            return
        deadline = time.monotonic() + 70.0
        while time.monotonic() < deadline and not self.stop.is_set():
            line = lines.readline(timeout=2.0)
            if line is None:
                if lines.closed:
                    self._emit("fail", {"peer": peer_id, "reason": "join-zero"})
                    try:
                        sock.close()
                    except OSError:
                        pass
                    return
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("t") == "waiting":
                continue
            if msg.get("t") == "joined":
                self._joining.pop(token, None)
                pipe = Pipe(sock, lines.take(), name=f"relay:{msg.get('peer')}")
                self._emit("joined", {"pipe": pipe, "peer": int(msg.get("peer") or peer_id), "side": msg.get("side", role)})
                return
            if msg.get("t") == "error":
                self._emit("fail", {"peer": peer_id, "reason": msg.get("msg", "join error")})
                try:
                    sock.close()
                except OSError:
                    pass
                return
        self._emit("fail", {"peer": peer_id, "reason": "join-timeout"})
        try:
            sock.close()
        except OSError:
            pass

    def _cleanup(self) -> None:
        self.connected = False
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._lines = None

    # ------------------------------------------------------------------ wysylka
    def _send(self, obj: dict) -> None:
        sock = self._sock
        if sock is None:
            raise SignalingError("brak polaczenia z relay")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            try:
                sock.sendall(data)
            except OSError as exc:
                raise SignalingError(str(exc)) from exc

    def _send_quiet(self, obj: dict) -> None:
        try:
            self._send(obj)
        except SignalingError:
            pass

    def open(self, peer_id: int, want_input: bool = True, view_only: bool = False) -> None:
        self._send_quiet({"t": "open", "peer": int(peer_id), "input": bool(want_input),
                          "view_only": bool(view_only), "pub": self.identity.public_b64})

    def accept(self, peer_id: int, token: str) -> None:
        self._send_quiet({"t": "accept", "peer": int(peer_id), "token": token})

    def decline(self, peer_id: int, token: str, reason: str = "declined") -> None:
        self._send_quiet({"t": "decline", "peer": int(peer_id), "token": token, "reason": reason})

    def cancel(self) -> None:
        self._send_quiet({"t": "cancel"})

    def set_alias(self, alias: str) -> None:
        self.alias = alias or ""
        self._send_quiet({"t": "alias", "alias": self.alias})

    def lookup(self, peer_id: int) -> None:
        self._send_quiet({"t": "lookup", "id": int(peer_id)})

    def close(self) -> None:
        """Zamkniecie sygnalizacji.

        `shutdown()` jest tu obowiazkowe: jezeli watek reader wlasnie blokuje sie
        na `recv()`, to CPython odklada fizyczne zamkniecie deskryptora do konca
        tej operacji. Bez shutdown FIN nie idzie do relay, relay dalej trzyma
        nasze ID i po reconnexion dostajemy nowe (ID 'uciekalo').
        """
        self.stop.set()
        self._send_quiet({"t": "bye"})
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class DirectListener:
    """Słucha na TCP (domyślnie 7000) - połączenia bezpośrednie po IP."""

    def __init__(self, port: int, on_pipe: Callable[[Pipe], None], host: str = "0.0.0.0"):
        self.port = port
        self.host = host
        self.on_pipe = on_pipe
        self.error = ""
        self.active = False
        self._srv: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def start(self, retries: int = 4) -> bool:
        """Zalagmiamy nasluch. Zajety port nie moze unieruchamiac agenta - gdy
        siedzi na nim druga instancja (host + kontroler na jednej maszynie),
        probujemy nastepnych kilku numerow i zapisujemy ten, ktory wyszedl."""
        last: Optional[OSError] = None
        for off in range(max(1, int(retries) + 1)):
            port = int(self.port) + off
            srv = None
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((self.host, port))
                srv.listen(8)
                srv.settimeout(1.0)
            except OSError as exc:
                last = exc
                if srv is not None:
                    try:
                        srv.close()
                    except OSError:
                        pass
                continue
            self.port = port
            self._srv = srv
            self.active = True
            self.error = ""
            self._thread = threading.Thread(target=self._loop, name="direct-listener",
                                            daemon=True)
            self._thread.start()
            return True
        self.error = f"port {self.port}: {last}"
        return False

    def _loop(self) -> None:
        assert self._srv is not None
        while not self._stopping.is_set():
            try:
                client, addr = self._srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            client.settimeout(None)
            self.on_pipe(Pipe(client, name=f"{addr[0]}:{addr[1]}"))
        self.active = False

    def stop(self) -> None:
        self._stopping.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None


def connect_direct(address: str, timeout: float = 8.0) -> Pipe:
    """'192.168.1.20:7000' albo '192.168.1.20' -> Pipe (bez sygnalizacji)."""
    host, _, port_s = address.partition(":")
    port = int(port_s) if port_s else DEFAULT_DIRECT_PORT
    sock = socket.create_connection((host.strip(), port), timeout=timeout)
    sock.settimeout(None)
    return Pipe(sock, name=address)


def local_addresses() -> List[str]:
    """Adresy IPv4 tej maszyny, zeby uzytkownik widzial, co wpisac przy
    polaczeniu bezposrednim. Zero zaleznosci: najpierw trik z UDP connect
    (tylko glowny interfejs), potem uzupelnienie po nazwie hosta.
    """
    out: List[str] = []

    def add(value: str) -> None:
        if value and value not in out and not value.startswith("127."):
            out.append(value)

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))
        add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        import socket as _s

        for info in _s.getaddrinfo(_s.gethostname(), None, _s.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return out


@dataclass
class LanHost:
    name: str
    addr: str
    port: int
    desk_id: int = 0
    platform: str = ""

    def key(self) -> str:
        return self.addr


class LanDiscovery:
    """Odpowiada na broadcast UDP i skanuje siec wokol."""

    def __init__(self, info_fn: Callable[[], dict], port: int = LAN_PORT):
        self.info_fn = info_fn
        self.port = port
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def responder_start(self) -> None:
        self._thread = threading.Thread(target=self._responder, name="lan-discovery", daemon=True)
        self._thread.start()

    def responder_stop(self) -> None:
        self._stop.set()

    def _responder(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", self.port))
        except OSError:
            return
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if not data.startswith(LAN_MAGIC):
                continue
            try:
                info = dict(self.info_fn())
                info.setdefault("port", DEFAULT_DIRECT_PORT)
                sock.sendto(json.dumps(info).encode("utf-8"), addr)
            except Exception:  # noqa: BLE001
                pass
        try:
            sock.close()
        except OSError:
            pass

    @staticmethod
    def scan(timeout: float = 2.2, port: int = LAN_PORT) -> List[LanHost]:
        found: Dict[str, LanHost] = {}
        targets = ["255.255.255.255", "127.0.0.1"]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 53))
            local = probe.getsockname()[0]
            net = ".".join(local.split(".")[:3])
            targets.append(f"{net}.255")
        except OSError:
            pass
        finally:
            probe.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.35)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for tgt in targets:
                try:
                    sock.sendto(LAN_MAGIC + b"\x00", (tgt, port))
                except OSError:
                    pass
            try:
                data, addr = sock.recvfrom(4096)
                info = json.loads(data.decode("utf-8"))
                host = LanHost(
                    name=str(info.get("name", "bez-nazwy")),
                    addr=f"{addr[0]}:{int(info.get('port', DEFAULT_DIRECT_PORT))}",
                    port=int(info.get("port", DEFAULT_DIRECT_PORT)),
                    desk_id=int(info.get("desk_id") or 0),
                    platform=str(info.get("platform", "")),
                )
                found[host.key()] = host
            except (socket.timeout, TimeoutError, ValueError, OSError):
                continue
        try:
            sock.close()
        except OSError:
            pass
        return sorted(found.values(), key=lambda h: h.name.lower())


def _os_name() -> str:
    try:
        import sys

        if sys.platform.startswith("win"):
            import platform as _p

            return f"Windows {_p.release()}"
        import platform as _p

        return f"{_p.system()} {_p.release()}"
    except Exception:  # noqa: BLE001
        return "unknown"
