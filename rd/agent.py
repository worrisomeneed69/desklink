"""DeskLink - orkiestrator (agent).

Trzyma zycie: polaczenie sygnalizacyjne z relay, nasluchiwanie bezposrednie,
odkrywanie w LAN i biezaca sesje. GUI nic nie wie o sieci - odbiera tylko
zdarzenia z kolejki `ui` i woła metody agenta.

Jednoczesnie obslugujemy JEDNA sesje (jak AnyDesk): zajety host dostaje 'busy'.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, List, Optional

from . import net
from .crypto import HAS_CRYPTO
from .osutil import describe as platform_desc
from .protocol import Pipe
from .session import Session, machine_name_cached
from .settings import ConfigStore, format_id, looks_like_address, parse_id


class Agent:
    def __init__(self, store: ConfigStore, ui: Optional["queue.Queue[Tuple]"] = None):
        self.store = store
        self.cfg = store.config
        self.ui = ui if ui is not None else queue.Queue()
        self.link: Optional[net.RelayLink] = None
        self.direct: Optional[net.DirectListener] = None
        self.lan: Optional[net.LanDiscovery] = None
        self.session: Optional[Session] = None
        self.pending_ring: Optional[dict] = None
        self.dialing: Optional[dict] = None
        # haslo trzymamy TYLKO w pamieci i tylko na czas wywolanego numeru
        self._dial_password: str = ""
        self._planned: dict = {}
        self.available = True
        self.relay_status = "wylaczony"
        self.last_error = ""
        self.started_at = time.time()
        self._event_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.history: List[dict] = []

    # ================================================================== start
    def emit(self, kind: str, data: Any = None) -> None:
        self.ui.put((kind, data))

    def start_direct(self) -> bool:
        if self.direct is not None:
            return self.direct.active
        if not self.cfg.listen_enabled:
            return False
        self.direct = net.DirectListener(self.cfg.listen_port, self._on_direct_pipe)
        wanted = int(self.cfg.listen_port)
        ok = self.direct.start()
        if not ok:
            self.last_error = self.direct.error
            self.emit("log", f"nasluchiwanie bezposrednie: {self.direct.error}")
            return False
        if int(self.direct.port) != wanted:
            # port zajety -> agent przesiedl sie na nastepny wolny; UI ma grac prawde
            self.cfg.listen_port = int(self.direct.port)
            self.store.flush(force=True)
            self.emit("log", f"port {wanted} zajety - nasluchuje na {self.direct.port}")
            self.emit("listen", {"port": self.direct.port})
        return True

    def needs_relay_restart(self, host: str = "", port: Optional[int] = None,
                            tls: Optional[bool] = None, key: str = "") -> bool:
        """Czy watek relay wisi na czym innym niz wlasnie zapisana konfiguracja.

        Naprawia realny blad: bez tego `start_relay` wywolywalismy tylko gdy
        `link is None`. Ktos, kto wpisal w Ustawieniach zly adres (np. numer
        portu w polu hosta), dostawal watek probujacy laczyc sie z "7002" w
        nieskonczonosc; pozniejsza poprawka na wlasciwy host byla zapisywana do
        pliku, ale nigdy nie trafiala do agenta - w UI wisialo "offline" az do
        restartu aplikacji.
        """
        host = host or self.cfg.relay_host
        port = self.cfg.relay_port if port is None else port
        tls = self.cfg.relay_tls if tls is None else tls
        key = key or getattr(self.cfg, "relay_key", "") or ""
        link = self.link
        if link is None:
            return bool(host)
        try:
            cur = (str(link.host), int(link.port), bool(link.tls), str(link.key or ""))
            want = (str(host), int(port), bool(tls), str(key))
        except (TypeError, ValueError):
            return True
        return cur != want

    def start_relay(self, host: str = "", port: Optional[int] = None, tls: Optional[bool] = None,
                   key: str = "") -> bool:
        host = host if host else self.cfg.relay_host
        if not host:
            self.relay_status = "brak relay w konfiguracji"
            return False
        port = port if port is not None else self.cfg.relay_port
        tls = tls if tls is not None else self.cfg.relay_tls
        key = key or getattr(self.cfg, "relay_key", "") or ""
        with self._lock:
            if self.link is not None:
                self.link.stop.set()
        self.link = net.RelayLink(
            host,
            int(port),
            self.cfg.identity,
            name=machine_name_cached(),
            tls=bool(tls),
            insecure_tls=bool(getattr(self.cfg, "relay_tls_insecure", False)),
            key=key or "",
            on_state=lambda: {"avail": self.available, "name": machine_name_cached()},
        )
        self.link.start()
        self.relay_status = f"polaczenie z {host}:{port}"
        self._event_thread = threading.Thread(target=self._relay_events, name="relay-events", daemon=True)
        self._event_thread.start()
        self.emit("relay", {"status": self.relay_status, "host": host, "port": port})
        return True

    def start_lan(self) -> None:
        if self.lan is not None or not self.cfg.lan_discovery:
            return
        self.lan = net.LanDiscovery(self._lan_info)
        self.lan.responder_start()

    def _lan_info(self) -> dict:
        return {
            "name": machine_name_cached(),
            "port": self.cfg.listen_port,
            "desk_id": self.cfg.identity.desk_id,
            "platform": platform_desc().get("os", ""),
        }

    # ================================================================== zdarzenia relay
    def _relay_events(self) -> None:
        link = self.link
        if link is None:
            return
        while not self._stop.is_set():
            if link is not self.link:
                # start_relay podmienil link (zmiana adresu w Ustawieniach) - ten watek
                # ma sie rozejsc, bo jego zdarzenia dotycza juz nieaktualnej pary.
                break
            try:
                kind, data = link.events.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._on_relay_event(kind, data)
            except Exception as exc:  # noqa: BLE001
                self.emit("log", f"relay event {kind}: {exc}")
        link.close()

    def _on_relay_event(self, kind: str, data: dict) -> None:
        if kind == "welcome":
            self.cfg.identity.desk_id = int(data.get("id") or 0)
            self.store.flush(force=True)
            self.relay_status = "online"
            self.emit("address", {"id": self.cfg.identity.desk_id, "alias": data.get("alias") or self.cfg.identity.alias,
                                  "reclaimed": data.get("reclaimed")})
            self.emit("log", f"relay: moje ID {format_id(self.cfg.identity.desk_id)}")
        elif kind == "connected":
            self.relay_status = "online"
        elif kind == "closed":
            self.relay_status = "reconnect..."
            self.last_error = str(data.get("error") or "")
            self.emit("relay", {"status": self.relay_status, "error": self.last_error})
        elif kind == "ring":
            self._on_ring(data)
        elif kind == "ringing":
            self.dialing = {"peer": int(data.get("peer") or 0), "since": time.time()}
            self.emit("dialing", {"peer": self.dialing["peer"]})
        elif kind == "ring_cancel":
            self.pending_ring = None
            self.emit("incoming_cancel", data)
        elif kind == "join":
            pass  # RelayLink sam otwiera drugie gniazdo
        elif kind == "joined":
            self._on_joined(data)
        elif kind == "tunnel":
            if data.get("status") == "closed" and self.session is not None:
                self.session.stop("tunnel-closed")
        elif kind == "fail":
            reason = str(data.get("reason") or "")
            self.dialing = None
            self._dial_password = ""
            self.last_error = reason
            self.emit("connect_failed", {"peer": data.get("peer"), "reason": reason})
            self.emit("log", f"relay: nie poszlo ({reason})")
        elif kind == "error":
            self.last_error = str(data.get("msg") or "")
            self.emit("log", f"relay: {self.last_error}")
        elif kind in ("alias_ok", "lookup_result", "pong"):
            self.emit(kind, data)

    def _on_ring(self, data: dict) -> None:
        peer = {
            "desk_id": int(data.get("peer") or 0),
            "name": str(data.get("peer_name") or "nieznany"),
            "fp": str(data.get("peer_fp") or ""),
        }
        ring = {
            "peer": peer,
            "token": str(data.get("token") or ""),
            "want_input": bool(data.get("input", True)),
            "view_only": bool(data.get("view_only", False)),
            "pub": str(data.get("pub") or ""),
            "via": "relay",
        }
        if self.session is not None and self.session.is_alive():
            if self.link:
                self.link.decline(peer["desk_id"], ring["token"], "busy")
            self.emit("log", "zajety - odrzucilem przychodzace")
            return
        unattended = self.cfg.has_password and not self.cfg.confirm_incoming
        if unattended:
            self._accept_unattended(ring)
            return
        self.pending_ring = ring
        self.emit("incoming", ring)

    def _accept_unattended(self, ring: dict) -> None:
        if self.link:
            self.link.accept(ring["peer"]["desk_id"], ring["token"])
        self.pending_ring = None
        self._planned = {"ring": ring, "input": bool(self.cfg.allow_input and ring["want_input"])}

    def _on_joined(self, data: dict) -> None:
        pipe = data.get("pipe")
        if not isinstance(pipe, Pipe):
            return
        side = data.get("side") or ""
        peer_id = int(data.get("peer") or 0)
        role = "controller" if side == "caller" else "host"
        pre = None
        want_input = True
        view_only = False
        password = ""
        if role == "host":
            planned = getattr(self, "_planned", None) or {}
            ring = planned.get("ring") or (self.pending_ring if self.pending_ring and self.pending_ring["peer"]["desk_id"] == peer_id else None)
            if ring:
                pre = {"input": bool(planned.get("input", ring["want_input"]))}
                want_input = bool(ring["want_input"])
                view_only = bool(ring.get("view_only"))
                self.pending_ring = None
            self._planned = {}
        elif self.dialing:
            want_input = bool(self.dialing.get("input", True))
            view_only = bool(self.dialing.get("view_only", False))
            # haslo z connect() - bez niego host wyliczy dowod na pustym hasle
            password = self._dial_password
            self._dial_password = ""   # nie trzymamy jawneho hasla dluzej niz trzeba
        hint = None
        if role == "controller":
            rec = next((r for r in self.cfg.recent if int(r.get("desk_id") or 0) == peer_id), None)
            if rec:
                hint = {"desk_id": peer_id, "name": rec.get("name", ""), "fp": rec.get("fp", "")}
        self.start_session(
            pipe,
            role,
            peer_hint=hint,
            preapproved=pre,
            password=password,
            want_input=want_input,
            view_only=view_only,
            dialing=role == "controller",
        )

    # ================================================================== sesje
    def start_session(self, pipe: Pipe, role: str, peer_hint: Optional[dict] = None,
                      preapproved: Optional[dict] = None, password: str = "", want_input: bool = True,
                      view_only: bool = False, dialing: bool = False,
                      source: Any = None, injector: Any = None) -> Session:
        with self._lock:
            if self.session is not None:
                try:
                    self.session.stop("zastapiona")
                except Exception:  # noqa: BLE001
                    pass
                self.session = None
            # overridey umozliwiaja testy bez realnego ekranu i bez ruszania kursora
            source = source if source is not None else getattr(self, "source_override", None)
            injector = injector if injector is not None else getattr(self, "injector_override", None)
            session = Session(
                pipe,
                role,
                self.store,
                ui=self.ui,
                password=password,
                want_input=want_input,
                view_only=view_only,
                peer_hint=peer_hint,
                preapproved=preapproved,
                source=source,
                injector=injector,
            )
            self.session = session
            session.start()
        self.emit("session_started", {"role": role, "peer": peer_hint or {}, "dialing": dialing})
        return session

    def _on_direct_pipe(self, pipe: Pipe) -> None:
        """Polaczenie bezposrednie po IP - od razu rola hosta."""
        if self.session is not None and self.session.is_alive():
            self.emit("log", "zajety - odrzucilem polaczenie bezposrednie")
            pipe.abort()
            return
        self.start_session(pipe, "host")

    def connect(self, target: str, password: str = "", view_only: bool = False) -> Dict[str, Any]:
        """target = '123 456 789' albo '192.168.1.20:7000'."""
        target = (target or "").strip()
        if not target:
            return {"ok": False, "reason": "pusty adres"}
        if not HAS_CRYPTO:
            self.emit("log", "UWAGA: brak biblioteki cryptography - sesja bez szyfrowania")
        # haslo tylko do pamieci, do czasu az 'joined' uruchomi Sesje
        self._dial_password = password or ""
        if looks_like_address(target):
            try:
                pipe = net.connect_direct(target)
            except OSError as exc:
                self.last_error = str(exc)
                self.emit("connect_failed", {"addr": target, "reason": f"nie osiagnalem {target}"})
                return {"ok": False, "reason": f"{exc}"}
            self.start_session(pipe, "controller", password=password, view_only=view_only,
                               want_input=not view_only, dialing=True)
            self._dial_password = ""
            return {"ok": True, "mode": "direct", "addr": target}
        desk_id = parse_id(target)
        if desk_id is None:
            return {"ok": False, "reason": "to nie jest ID ani adres"}
        if desk_id == self.cfg.identity.desk_id:
            return {"ok": False, "reason": "nie mozesz polaczyc sie ze sobą"}
        if self.link is None or not self.link.connected:
            if self.cfg.relay_host:
                self.start_relay()
            if self.link is None or not self.link.connected:
                return {"ok": False, "reason": "relay offline - sprawdz polaczenie z internetem"}
        self.dialing = {"peer": desk_id, "since": time.time(), "input": not view_only, "view_only": view_only}
        self.link.open(desk_id, want_input=not view_only, view_only=view_only)
        self.emit("dialing", {"peer": desk_id})
        return {"ok": True, "mode": "relay", "id": desk_id}

    def answer_incoming(self, allow: bool, allow_input: bool = True) -> None:
        ring = self.pending_ring
        if not ring:
            return
        self.pending_ring = None
        if not allow:
            if self.link:
                self.link.decline(ring["peer"]["desk_id"], ring["token"], "declined")
            self.emit("log", "odrzucono przychodzace polaczenie")
            return
        self._planned = {"ring": ring, "input": bool(allow_input and ring["want_input"])}
        if self.link:
            self.link.accept(ring["peer"]["desk_id"], ring["token"])
        self.emit("log", f"przyjete: {format_id(ring['peer']['desk_id'])}")

    def cancel_dial(self) -> None:
        if self.link:
            self.link.cancel()
        self.dialing = None
        self._dial_password = ""

    def close_session(self, reason: str = "user") -> None:
        with self._lock:
            if self.session is not None:
                self.session.stop(reason)
                self.session = None
        self.emit("session_closed", {"reason": reason})

    def grant_input(self, allow: bool) -> None:
        if self.session:
            self.session.set_input(allow)

    def answer_input_request(self, allow: bool) -> None:
        if self.session:
            self.session.answer_input_request(allow)

    def set_available(self, avail: bool) -> None:
        self.available = bool(avail)
        self.emit("log", "jestem dostepny" if avail else "offline (nowe polaczenia zablokowane)")

    def set_alias(self, alias: str) -> None:
        alias = (alias or "").strip()[:32]
        self.cfg.identity.alias = alias
        self.store.flush(force=True)
        if self.link:
            self.link.set_alias(alias)
        self.emit("alias", {"alias": alias})

    # ================================================================== status
    def status(self) -> dict:
        sess = self.session
        session_info: Optional[dict] = None
        if sess is not None:
            session_info = {
                "role": sess.role,
                "state": sess.state,
                "peer": sess.peer.as_dict(),
                "granted_input": sess.granted_input,
                "peer_controls_me": sess.peer_controls_me,
                "share_screen": sess.share_screen,
                "stats": dict(sess.stats),
                "error": sess.error,
                "plain": bool(sess.keys and sess.keys.plain),
            }
        return {
            "id": self.cfg.identity.desk_id,
            "alias": self.cfg.identity.alias,
            "fingerprint": self.cfg.identity.fp,
            "name": machine_name_cached(),
            "platform": platform_desc(),
            "relay": {
                "host": self.cfg.relay_host,
                "port": self.cfg.relay_port,
                "connected": bool(self.link and self.link.connected),
                "status": self.relay_status,
                "error": self.link.last_error if self.link else "",
            },
            "direct": {
                "enabled": self.cfg.listen_enabled,
                "active": bool(self.direct and self.direct.active),
                "port": self.cfg.listen_port,
                "error": self.direct.error if self.direct else "",
            },
            "available": self.available,
            "encrypted": bool(HAS_CRYPTO),
            "pending_ring": self.pending_ring,
            "dialing": self.dialing,
            "session": session_info,
            "uptime": round(time.time() - self.started_at, 1),
        }

    def scan_lan(self) -> List[net.LanHost]:
        return net.LanDiscovery.scan()

    def stop(self) -> None:
        self._stop.set()
        if self.session is not None:
            self.session.stop("agent-stop")
        if self.link is not None:
            self.link.stop.set()
            self.link.close()
        if self.direct is not None:
            self.direct.stop()
        if self.lan is not None:
            self.lan.responder_stop()
