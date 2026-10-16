#!/usr/bin/env python3
"""desklink-relay - serwer rendezvous/relay dla DeskLink.

Czysty stdlib (python3, zero pip), zeby postawic go byle gdzie: tani VPS,
kontener, Raspberry Pi, drugi komputer. Robi trzy rzeczy:

1. Wydaje 9-cyfrowe ID (jak AnyDesk) i zapamietuje wlasciciela po tajnym sekrecie,
   dzieki temu ten sam komputer dostaje to samo ID po restarcie.
2. Trzyma presence (kto online) na dlugim polaczeniu sygnalizacji.
3. Po zaakceptowaniu zaproszenia dokleja DRUGIE polaczenie TCP kazdej strony i
   przepuszcza miedzy nimi rure z bajtami. Ruch jest AES-GCM po stronie klientow
   (E2E) - relay nie widzi ekranu, hasla ani klawiszy, tylko liczniki bajtow.

Dwa gniazda na klienta (sygnalizacja + tunel) powoduja, ze ID przezywa koncie sesji.

Uruchomienie:   python3 desklink_relay.py --port 7001
Klucz wstepu:   python3 desklink_relay.py --require-auth --secret JAKISS
TLS:            python3 desklink_relay.py --cert fullchain.pem --key privkey.pem
Systemd:        ExecStart=/usr/bin/python3 /opt/desklink/desklink_relay.py --port 7001
"""

from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

ID_MIN = 100_000_000
ID_MAX = 999_999_999
ALIVE_TIMEOUT = 95.0
RING_TIMEOUT = float(os.environ.get("DESKLINK_RING_TIMEOUT", "75"))
JOIN_TIMEOUT = 75.0
MAX_CLIENTS = int(os.environ.get("DESKLINK_MAX_CLIENTS", "2000"))
MAX_TUNNELS = int(os.environ.get("DESKLINK_MAX_TUNNELS", "500"))
_LOG_LOCK = threading.Lock()


def log(*args) -> None:
    with _LOG_LOCK:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]", *args, flush=True)


class LineBuf:
    """Bufor linii JSON nad gniazdem TCP (z pozostala nadwyzka bajtow)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.closed = False

    def readline(self, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[:nl]).decode("utf-8", "replace")
                del self.buf[: nl + 1]
                return line
            if self.closed:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                self.sock.settimeout(min(left, 0.4))
                chunk = self.sock.recv(16384)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                self.closed = True
                return None
            if not chunk:
                self.closed = True
                return None
            self.buf.extend(chunk)


@dataclass
class Client:
    desk_id: int
    sock: socket.socket
    addr: str
    name: str = ""
    fp: str = ""
    pub: str = ""
    secret: str = ""
    alias: str = ""
    avail: bool = True
    busy: bool = False
    last_seen: float = field(default_factory=time.time)
    peer_id: int = 0

    def send(self, obj: dict) -> bool:
        try:
            self.sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False


@dataclass
class Invite:
    token: str
    caller: Client
    target: Client
    created: float = field(default_factory=time.time)
    accepted: bool = False
    joins: Dict[str, socket.socket] = field(default_factory=dict)
    closed: bool = False


class Relay:
    def __init__(self, require_auth: bool = False, key: str = ""):
        self.clients: Dict[int, Client] = {}
        self.aliases: Dict[str, int] = {}
        self.by_secret: Dict[str, int] = {}
        self.invites: Dict[str, Invite] = {}
        self.require_auth = require_auth
        self.key = key
        self.lock = threading.RLock()
        self.tunnels = 0
        self.total_tunnels = 0

    # ------------------------------------------------------------------ ID
    def new_id(self) -> int:
        with self.lock:
            while True:
                candidate = random.randint(ID_MIN, ID_MAX)
                if candidate not in self.clients:
                    return candidate

    def reclaim(self, want: int, secret: str) -> Optional[int]:
        """Ten sam komputer -> to samo ID (para ID<->sekret przezywa restart).

        ID zapisane pod sekretem oddajemy nawet wtedy, gdy klient nie podal
        'want', zeby restart agenta nie zmienil mu adresu. Nie oddajemy nic,
        gdy stare ID jest nadal zajete albo gdy klient prosi o inne.
        """
        if not secret:
            return None
        with self.lock:
            known = self.by_secret.get(secret)
            if known is None:
                return None
            if known in self.clients:
                return None
            if want and want != known:
                # prosi o inne ID niz zapisane pod tym sekretem - nie oddajemy
                return None
            return known

    def register_secret(self, desk_id: int, secret: str) -> None:
        if secret:
            with self.lock:
                self.by_secret[secret] = desk_id

    def resolve(self, desk_id: int, alias: str) -> Optional[Client]:
        with self.lock:
            target = self.clients.get(int(desk_id)) if desk_id else None
            if target is None and alias:
                target = self.clients.get(self.aliases.get(alias, 0))
            if target is None or not target.avail:
                return None
            return target

    def drop(self, client: Client) -> None:
        with self.lock:
            if self.clients.get(client.desk_id) is client:
                del self.clients[client.desk_id]
            if client.alias and self.aliases.get(client.alias) == client.desk_id:
                del self.aliases[client.alias]
            client.avail = False
            for invite in list(self.invites.values()):
                if invite.caller is client or invite.target is client:
                    self.close_invite(invite, "gone")
            for other in list(self.clients.values()):
                if other.peer_id == client.desk_id:
                    other.send({"t": "fail", "peer": client.desk_id, "reason": "gone"})

    # ------------------------------------------------------------------ zaproszenia
    def ring(self, caller: Client, target: Client, want_input: bool, view_only: bool) -> None:
        token = secrets.token_urlsafe(16)
        invite = Invite(token=token, caller=caller, target=target)
        with self.lock:
            self.invites[token] = invite
            caller.peer_id = target.desk_id
            target.peer_id = caller.desk_id
        caller.send({"t": "ringing", "peer": target.desk_id, "token": token})
        target.send(
            {
                "t": "ring",
                "peer": caller.desk_id,
                "peer_name": caller.name or "?",
                "peer_fp": caller.fp,
                "pub": caller.pub,
                "token": token,
                "input": want_input,
                "view_only": view_only,
            }
        )
        log(f"dzwoni {caller.desk_id} -> {target.desk_id} (token {token[:6]})")
        threading.Timer(RING_TIMEOUT, self._ring_expired, args=(token,)).start()

    def _ring_expired(self, token: str) -> None:
        invite = self.invites.get(token)
        if invite is None or invite.closed:
            return
        if not invite.accepted:
            invite.caller.send({"t": "fail", "peer": invite.target.desk_id, "reason": "timeout"})
            invite.target.send({"t": "ring_cancel", "peer": invite.caller.desk_id})
            self.close_invite(invite, "timeout")

    def accept(self, target: Client, token: str) -> None:
        invite = self.invites.get(token)
        if invite is None or invite.closed or invite.target is not target:
            target.send({"t": "error", "msg": "zaproszenie nieaktualne"})
            return
        invite.accepted = True
        target.send({"t": "join", "token": token, "peer": invite.caller.desk_id, "role": "host"})
        invite.caller.send({"t": "join", "token": token, "peer": invite.target.desk_id, "role": "controller"})
        threading.Timer(JOIN_TIMEOUT, self._join_expired, args=(token,)).start()

    def _join_expired(self, token: str) -> None:
        invite = self.invites.get(token)
        if invite is None or invite.closed or len(invite.joins) == 2:
            return
        invite.caller.send({"t": "fail", "peer": invite.target.desk_id, "reason": "join-timeout"})
        invite.target.send({"t": "ring_cancel", "peer": invite.caller.desk_id})
        self.close_invite(invite, "join timeout")

    def decline(self, target: Client, token: str, reason: str) -> None:
        invite = self.invites.get(token)
        if invite is None or invite.closed:
            return
        invite.caller.send({"t": "fail", "peer": target.desk_id, "reason": reason})
        self.close_invite(invite, reason)

    def close_invite(self, invite: Invite, reason: str = "") -> None:
        with self.lock:
            self.invites.pop(invite.token, None)
            invite.closed = True
            sockets = list(invite.joins.values())
            invite.joins.clear()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        if reason:
            log(f"zaproszenie {invite.token[:6]}: {reason}")

    # ------------------------------------------------------------------ tunel
    def join(self, token: str, role: str, sock: socket.socket) -> bool:
        """Dokleja drugie polaczenie; gdy oba sa - startuje pompke bajtow."""
        invite = self.invites.get(token)
        if invite is None or invite.closed or not invite.accepted:
            sock.sendall(b'{"t":"error","msg":"token nieaktualny"}\n')
            return False
        if role not in ("caller", "target"):
            sock.sendall(b'{"t":"error","msg":"zla role w join"}\n')
            return False
        with self.lock:
            if role in invite.joins:
                return False
            invite.joins[role] = sock
            both = len(invite.joins) == 2
            full = self.tunnels >= MAX_TUNNELS
            if both and not full:
                self.tunnels += 1
                self.total_tunnels += 1
                invite.caller.busy = True
                invite.target.busy = True
                invite.caller.peer_id = invite.target.desk_id
                invite.target.peer_id = invite.caller.desk_id
        if not both:
            sock.sendall(b'{"t":"waiting"}\n')
            return True
        caller_sock = invite.joins["caller"]
        target_sock = invite.joins["target"]
        if full:
            for s in (caller_sock, target_sock):
                try:
                    s.sendall(b'{"t":"error","msg":"relay pelny"}\n')
                except OSError:
                    pass
            invite.caller.send({"t": "fail", "peer": invite.target.desk_id, "reason": "relay-busy"})
            self.close_invite(invite, "relay pelny")
            return False
        for s, peer, side in (
            (caller_sock, invite.target.desk_id, "caller"),
            (target_sock, invite.caller.desk_id, "target"),
        ):
            try:
                s.sendall(json.dumps({"t": "joined", "peer": peer, "side": side}).encode() + b"\n")
            except OSError:
                self.close_invite(invite, "join-io")
                return False
        invite.caller.send({"t": "tunnel", "status": "open", "peer": invite.target.desk_id})
        invite.target.send({"t": "tunnel", "status": "open", "peer": invite.caller.desk_id})
        log(f"tunel {invite.caller.desk_id} <-> {invite.target.desk_id} (aktywnych {self.tunnels})")
        threading.Thread(target=self._pump, args=(caller_sock, target_sock, invite), name="pump", daemon=True).start()
        return True

    def _pump(self, a: socket.socket, b: socket.socket, invite: Invite) -> None:
        stop = threading.Event()
        counters = {"a": 0, "b": 0}

        def pipe(src: socket.socket, dst: socket.socket, key: str) -> None:
            while not stop.is_set():
                try:
                    data = src.recv(65536)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                if not data:
                    break
                counters[key] += len(data)
                try:
                    dst.sendall(data)
                except OSError:
                    break
            stop.set()

        t1 = threading.Thread(target=pipe, args=(a, b, "a"), daemon=True)
        t2 = threading.Thread(target=pipe, args=(b, a, "b"), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass
        with self.lock:
            self.tunnels = max(0, self.tunnels - 1)
            invite.caller.busy = False
            invite.target.busy = False
            invite.caller.peer_id = 0
            invite.target.peer_id = 0
        invite.caller.send({"t": "tunnel", "status": "closed", "peer": invite.target.desk_id})
        invite.target.send({"t": "tunnel", "status": "closed", "peer": invite.caller.desk_id})
        self.close_invite(invite, "koniec")
        log(
            f"tunel {invite.caller.desk_id} <-> {invite.target.desk_id} zamkniety "
            f"({counters['a']} B / {counters['b']} B)"
        )

    def status(self) -> str:
        with self.lock:
            return (
                f"status: klientow {len(self.clients)}, tuneli {self.tunnels}, "
                f"zaproszen {len(self.invites)}, par lacznie {self.total_tunnels}"
            )


# --------------------------------------------------------------------------- obsluga polaczenia
def do_signaling(relay: Relay, conn: socket.socket, lines: LineBuf, addr: str, hello: dict) -> None:
    if relay.require_auth and hello.get("key") != relay.key:
        conn.sendall(b'{"t":"error","msg":"relay key wymagany"}\n')
        return
    if len(relay.clients) >= MAX_CLIENTS:
        conn.sendall(b'{"t":"error","msg":"relay pelny"}\n')
        return
    want = int(hello.get("want_id") or 0)
    secret = str(hello.get("secret") or "")
    desk_id = relay.reclaim(want, secret) or relay.new_id()
    client = Client(
        desk_id=desk_id,
        sock=conn,
        addr=addr,
        name=str(hello.get("name") or "")[:64],
        fp=str(hello.get("fp") or "")[:128],
        pub=str(hello.get("pub") or ""),
        secret=secret,
    )
    with relay.lock:
        relay.clients[desk_id] = client
    relay.register_secret(desk_id, secret)
    alias = str(hello.get("alias") or "")[:32]
    if alias:
        with relay.lock:
            taken = relay.aliases.get(alias)
            if not taken or taken not in relay.clients:
                relay.aliases[alias] = desk_id
                client.alias = alias
    client.send(
        {
            "t": "welcome",
            "id": desk_id,
            "secret": secret or secrets.token_urlsafe(24),
            "reclaimed": bool(want and desk_id == want),
            "alias": client.alias,
            "server": "desklink-relay/1",
        }
    )
    log(f"+ {desk_id} ({client.name or '?'}) z {addr}")
    try:
        while True:
            line = lines.readline(1.0)
            if line is None:
                if lines.closed:
                    break
                if time.time() - client.last_seen > ALIVE_TIMEOUT:
                    log(f"- {client.desk_id}: brak zycia")
                    break
                continue
            client.last_seen = time.time()
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            dispatch(relay, client, msg)
    finally:
        relay.drop(client)
        log(f"- {client.desk_id} ({client.name or '?'})")


def handle(relay: Relay, conn: socket.socket, addr: str) -> None:
    """Jedno wejście dla obu rodzajów połączeń: pierwsza linia decyduje."""
    conn.settimeout(None)
    lines = LineBuf(conn)
    deadline = time.monotonic() + 20.0
    first: Optional[dict] = None
    while first is None and time.monotonic() < deadline:
        line = lines.readline(max(0.1, deadline - time.monotonic()))
        if line is None:
            if lines.closed:
                conn.close()
                return
            continue
        try:
            first = json.loads(line)
        except json.JSONDecodeError:
            conn.sendall(b'{"t":"error","msg":"bad json"}\n')
            conn.close()
            return
    if first is None:
        conn.close()
        return
    kind = first.get("t")
    if kind == "join":
        if relay.require_auth and first.get("key") != relay.key:
            conn.sendall(b'{"t":"error","msg":"relay key wymagany"}\n')
            conn.close()
            return
        role = "target" if first.get("role") == "host" else "caller"
        ok = relay.join(str(first.get("token") or ""), role, conn)
        if not ok:
            try:
                conn.close()
            except OSError:
                pass
        return
    if kind == "hello":
        do_signaling(relay, conn, lines, addr, first)
        return
    conn.sendall(b'{"t":"error","msg":"witaj, potrzebujesz hello albo join"}\n')
    conn.close()


def dispatch(relay: Relay, client: Client, msg: dict) -> None:
    kind = msg.get("t")
    if kind == "ping":
        state = msg.get("state") or {}
        if isinstance(state, dict):
            client.avail = bool(state.get("avail", True))
            if state.get("name"):
                client.name = str(state["name"])[:64]
        client.send({"t": "pong", "now": time.time()})
    elif kind == "state":
        client.avail = bool(msg.get("avail", True))
    elif kind == "alias":
        alias = str(msg.get("alias") or "")[:32]
        if not alias:
            client.send({"t": "error", "msg": "pusty alias"})
            return
        with relay.lock:
            taken = relay.aliases.get(alias)
            if taken and taken != client.desk_id and taken in relay.clients:
                client.send({"t": "error", "msg": f"alias '{alias}' zajety"})
                return
            if client.alias and relay.aliases.get(client.alias) == client.desk_id:
                del relay.aliases[client.alias]
            relay.aliases[alias] = client.desk_id
            client.alias = alias
        client.send({"t": "alias_ok", "alias": alias, "id": client.desk_id})
        log(f"alias {alias} -> {client.desk_id}")
    elif kind == "lookup":
        target = relay.resolve(int(msg.get("id") or 0), str(msg.get("alias") or ""))
        if target is None:
            client.send({"t": "lookup_result", "online": False, "id": msg.get("id")})
        else:
            client.send({"t": "lookup_result", "online": True, "id": target.desk_id, "busy": target.busy})
    elif kind == "open":
        peer_id = int(msg.get("peer") or 0)
        target = relay.resolve(peer_id, str(msg.get("alias") or ""))
        if target is None:
            client.send({"t": "fail", "peer": peer_id, "reason": "offline"})
            return
        if target is client:
            client.send({"t": "fail", "peer": peer_id, "reason": "siebie"})
            return
        if target.busy:
            client.send({"t": "fail", "peer": target.desk_id, "reason": "busy"})
            return
        if any(i.caller is client or i.target is client for i in relay.invites.values()):
            client.send({"t": "fail", "peer": target.desk_id, "reason": "already-dialing"})
            return
        relay.ring(client, target, bool(msg.get("input", True)), bool(msg.get("view_only", False)))
    elif kind == "accept":
        relay.accept(client, str(msg.get("token") or ""))
    elif kind == "decline":
        relay.decline(client, str(msg.get("token") or ""), str(msg.get("reason") or "declined"))
    elif kind == "cancel":
        for invite in list(relay.invites.values()):
            if invite.caller is client:
                invite.target.send({"t": "ring_cancel", "peer": client.desk_id})
                relay.close_invite(invite, "cancelled")
    elif kind == "bye":
        # klient wychodzi swiadomie: natychmiast zwolnij jego ID i ewent. rure
        relay.drop(client)
    else:
        client.send({"t": "error", "msg": f"nieznana komenda {kind}"})


# --------------------------------------------------------------------------- start
def serve_forever(relay: Relay, host: str, port: int, cert: str, key: str) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(64)
    ctx: Optional[ssl.SSLContext] = None
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    tls_note = "TLS" if ctx else "plain TCP (i tak E2E po stronie klientow)"
    log(f"desklink-relay slucha na {host}:{port} ({tls_note})")
    if relay.require_auth:
        log("wymagam klucza wstepu (--require-auth --secret)")

    stop = threading.Event()

    def sweeper() -> None:
        while not stop.is_set():
            time.sleep(20)
            now = time.time()
            with relay.lock:
                dead = [c for c in relay.clients.values() if now - c.last_seen > ALIVE_TIMEOUT]
            for client in dead:
                log(f"pad {client.desk_id}: brak zycia, zamykam")
                try:
                    client.sock.close()
                except OSError:
                    pass
            log(relay.status())

    threading.Thread(target=sweeper, name="sweeper", daemon=True).start()
    try:
        while True:
            conn, addr = listener.accept()
            if ctx is not None:
                try:
                    conn = ctx.wrap_socket(conn, server_side=True)
                except ssl.SSLError as exc:
                    log(f"TLS odrzucone z {addr[0]}: {exc}")
                    continue
            threading.Thread(target=handle, args=(relay, conn, f"{addr[0]}:{addr[1]}"), daemon=True).start()
    except OSError as exc:
        log(f"listener zamkniety: {exc}")
    finally:
        stop.set()
        listener.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="DeskLink relay/rendezvous (tylko stdlib)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DESKLINK_RELAY_PORT", "7001")))
    ap.add_argument("--cert", default=os.environ.get("DESKLINK_TLS_CERT", ""))
    ap.add_argument("--key", default=os.environ.get("DESKLINK_TLS_KEY", ""))
    ap.add_argument("--require-auth", action="store_true", help="wymagaj --secret od kazdego klienta")
    ap.add_argument("--secret", default=os.environ.get("DESKLINK_RELAY_SECRET", ""))
    args = ap.parse_args()
    relay = Relay(require_auth=args.require_auth, key=args.secret)
    try:
        serve_forever(relay, args.host, args.port, args.cert, args.key)
    except KeyboardInterrupt:
        log("stop (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
