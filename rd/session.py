"""DeskLink - silnik sesji (jedna klasa, dwie role).

`Session` to jeden strumien miedzy dwoema maszynami. Role:

* ``controller`` - inicjuje handshake, pyta o ekran, wysyla wejscie.
* ``host``       - odpowiada klatkami ekranu i wykonuje wejscie (jesli dostanie zgod).

Ta sama instancja moze robic obie rzeczy naraz (two-way), bo strumien jest
symetryczny: kazdy nadaje kiedy dostanie SCREEN_REQ, kazdy przyjmuje wejscie
kiedy ma GRANT. Stad 'oba komputery steruja sie nawzajem' wychodzi darmowo.

Handshake (wszystko po AES-GCM, poza dwoma pierwszymi ramkami KEX):

    controller -> HELLO_KEX      (plain, X25519 pub + preferencje)
    host       -> HELLO_KEX_ACK  (plain, X25519 pub + challenge + salt + rozdzielczosc)
    controller -> AUTH_REQ       (auth_key, dowod znalosci hasla)
    host       -> AUTH_OK|AUTH_NO
    ...                        (data_key: FRAME / MOUSE / KEY / GRANT / PING ...)
"""

from __future__ import annotations

import io
import os
import queue
import struct
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image

from . import crypto as dcrypto
from .capture import ScreenCapture, frame_payload
from .input_remote import InputInjector, LocalInputBlock
from .keys import normalize_pynput_key
from .osutil import describe as platform_desc, enable_dpi_awareness
from .protocol import FT, FLAG_KEYFRAME, FLAG_PLAIN, Frame, Pipe, ProtocolError, unpack_frame_header
from .settings import ConfigStore, format_id

CHUNK = 128 * 1024
PERMISSION_TIMEOUT = 45.0
AUTH_TIMEOUT = 60.0
HANDSHAKE_TIMEOUT = 30.0
FILE_DIR_NAME = "DeskLink"


@dataclass
class Peer:
    desk_id: int = 0
    name: str = ""
    fp: str = ""
    platform: str = ""
    width: int = 0
    height: int = 0
    offset_x: int = 0
    offset_y: int = 0
    monitors: int = 1

    def as_dict(self) -> dict:
        return {
            "desk_id": self.desk_id,
            "name": self.name,
            "fp": self.fp,
            "platform": self.platform,
            "width": self.width,
            "height": self.height,
            "monitors": self.monitors,
        }


class SessionClosed(Exception):
    pass


class Session:
    def __init__(
        self,
        pipe: Pipe,
        role: str,
        store: ConfigStore,
        ui: Optional["queue.Queue[Tuple]"] = None,
        source: Any = None,
        password: str = "",
        want_input: bool = True,
        view_only: bool = False,
        peer_hint: Optional[dict] = None,
        preapproved: Optional[dict] = None,
        injector: Optional[InputInjector] = None,
    ):
        assert role in ("controller", "host")
        self.pipe = pipe
        self.role = role
        self.store = store
        self.cfg = store.config
        self.ui: "queue.Queue[Tuple]" = ui if ui is not None else queue.Queue()
        self.source = source if source is not None else ScreenCapture(self.cfg.monitor)
        self.injector = injector if injector is not None else InputInjector()
        self.blocker = LocalInputBlock()
        self.password = password or ""
        self.want_input = want_input
        self.view_only = view_only
        # decyzja podjeta przy 'przychodzącym połączeniu' (relay) - nie pytamy drugi raz
        self.preapproved = preapproved
        self.peer = Peer(**(peer_hint or {})) if peer_hint else Peer()

        # stan sesji
        self.state = "handshake"          # handshake|connected|closing|closed
        self.error = ""
        self.reason = ""
        self.stop_event = threading.Event()
        self.connected_at = 0.0
        self.eph = dcrypto.EphemeralKey.generate()
        self.challenge = os.urandom(16)
        self.keys: Optional[dcrypto.SessionKeys] = None
        self.remote_pw_ok = False

        # video
        self.latest: Optional[Image.Image] = None
        self.gen = 0
        self.stream_monitor: Optional[int] = None
        self.stream_over: dict = {}   # pilot nadpisuje jakosc swojego obrazu
        self._video_lock = threading.Lock()
        self._force_key = True
        self.share_screen = False
        self._out_quality = max(35, int(self.cfg.quality))
        self._out_fps = max(3, int(self.cfg.fps))
        self._out_scale = float(self.cfg.scale)
        self._out_fmt = self.cfg.encoding

        # uprawnienia
        self.granted_input = False        # my sterujemy druga strona
        self.peer_controls_me = False     # druga strona steruje mną
        self._perm_event = threading.Event()
        self._perm_answer: Optional[dict] = None
        self._auth_event = threading.Event()
        self._auth_result: Optional[dict] = None
        self._pending_pw: Optional[str] = None

        # watki
        self._threads: List[threading.Thread] = []
        self._send_lock = threading.Lock()
        self._last_send_ms = 0.0

        # statystyki
        self.stats: Dict[str, Any] = {
            "rtt_ms": 0.0,
            "fps_in": 0.0,
            "kbps_out": 0.0,
            "kbps_in": 0.0,
            "quality": self._out_quality,
            "dropped": 0,
            "frames_in": 0,
            "local_fps": 0.0,
        }
        self._bytes_in_at = 0.0
        self._bytes_out_at = 0.0
        self._frames_in_at = 0

        # pliki
        self._files: Dict[int, dict] = {}
        self._next_fid = 1
        self._in_cfg_pw = False
        self.caps_peer: dict = {}

    # ================================================================== zycie
    def start(self) -> None:
        enable_dpi_awareness()
        self._threads.append(threading.Thread(target=self._run, name=f"rd-session-{self.role}", daemon=True))
        self._threads[0].start()

    def _emit(self, kind: str, data: Any = None) -> None:
        try:
            self.ui.put_nowait((kind, data))
        except queue.Full:  # pragma: no cover - kolejka nieograniczona
            pass

    def _run(self) -> None:
        try:
            ok = self._handshake_controller() if self.role == "controller" else self._handshake_host()
            if not ok:
                self._teardown(self.error or "handshake nie udany")
                return
            self.state = "connected"
            self.connected_at = time.time()
            self._emit("connected", {"peer": self.peer.as_dict(), "role": self.role})
            self._spawn_workers()
            self._recv_loop()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            self._teardown(self.error)

    def _spawn_workers(self) -> None:
        self._threads.append(threading.Thread(target=self._stream_loop, name="rd-stream", daemon=True))
        self._threads.append(threading.Thread(target=self._stats_loop, name="rd-stats", daemon=True))
        self._threads[-2].start()
        self._threads[-1].start()

    def stop(self, reason: str = "user") -> None:
        if self.state in ("closing", "closed"):
            return
        self.reason = reason
        try:
            self.pipe.send_json(FT.BYE, {"reason": reason}, flags=FLAG_PLAIN)
        except Exception:  # noqa: BLE001
            pass
        self.stop_event.set()
        self._perm_event.set()
        self._auth_event.set()
        self.pipe.abort()

    def _teardown(self, reason: str = "") -> None:
        if self.state == "closed":
            return
        self.state = "closed"
        self.reason = reason or self.reason
        self.stop_event.set()
        try:
            self.injector.release_all()
        except Exception:  # noqa: BLE001
            pass
        self.blocker.ungrab()
        self.granted_input = False
        self.peer_controls_me = False
        self.share_screen = False
        self.pipe.abort()
        self._emit("closed", {"reason": self.reason, "stats": dict(self.stats)})

    # ================================================================== handshake
    def _prefs(self) -> dict:
        return {
            "want_input": bool(self.want_input and not self.view_only),
            "view_only": bool(self.view_only),
            "clipboard": bool(self.cfg.clipboard_sync),
            "files": bool(self.cfg.allow_files),
            "fps": int(self.cfg.fps),
            "quality": int(self.cfg.quality),
            "scale": float(self.cfg.scale),
        }

    def _my_screen_info(self) -> dict:
        try:
            mons = self.source.monitors()
            cur = self.source.current_monitor()
            w, h = self.source.native_size()
        except Exception:  # noqa: BLE001
            mons, cur, (w, h) = [], None, self.source.native_size()
        return {
            "width": int(w),
            "height": int(h),
            "x": int(getattr(cur, "left", 0) or 0),
            "y": int(getattr(cur, "top", 0) or 0),
            "monitors": [{"i": m.index, "w": m.width, "h": m.height, "name": m.name} for m in (mons or [])],
            "platform": platform_desc(),
        }

    def _recv_plain(self, expect: Tuple[int, ...], timeout: float) -> Frame:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.pipe.recv(timeout=min(2.0, deadline - time.monotonic()))
            if frame.type in expect:
                return frame
            if frame.type == FT.BYE:
                raise SessionClosed("druga strona zamknela podczas handshake")
            if frame.type == FT.ERROR:
                raise SessionClosed(f"blad drugiej strony: {frame.json.get('msg')}")
            # PING w trakcie handshake - odpowiedz i jedz dalej
            if frame.type == FT.PING:
                self._safe_send(FT.PONG, {"ts": frame.json.get("ts")}, plain=True)
        raise TimeoutError("czekanie na ramke handshake")

    def _safe_send(self, ftype: int, payload: Any = None, plain: bool = False, flags: int = 0) -> bool:
        if plain:
            flags |= FLAG_PLAIN
        try:
            with self._send_lock:
                if isinstance(payload, (bytes, bytearray)):
                    self.pipe.send(ftype, bytes(payload), flags)
                else:
                    self.pipe.send_json(ftype, payload or {}, flags)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            return False
        return True

    def _handshake_controller(self) -> bool:
        self._emit("log", "handshake: wysylam HELLO_KEX")
        self._safe_send(
            FT.HELLO_KEX,
            {
                "v": 1,
                "role": "controller",
                "pub": self.eph.public_b64,
                "fp": self.cfg.identity.fp,
                "id": self.cfg.identity.desk_id,
                "name": machine_name_cached(),
                "prefs": self._prefs(),
            },
            plain=True,
        )
        frame = self._recv_plain((FT.HELLO_KEX_ACK,), HANDSHAKE_TIMEOUT)
        ack = frame.json
        if int(ack.get("v", 0)) != 1:
            self.error = "nieznana wersja protokolu po drugiej stronie"
            return False
        peer_pub = ack.get("pub", "")
        peer_fp = ack.get("fp", "")
        self.peer.desk_id = int(ack.get("id") or self.peer.desk_id or 0)
        self.peer.name = str(ack.get("name") or self.peer.name)
        self.peer.fp = peer_fp
        screen = ack.get("screen") or {}
        self.peer.width = int(screen.get("width") or 0)
        self.peer.height = int(screen.get("height") or 0)
        self.peer.offset_x = int(screen.get("x") or 0)
        self.peer.offset_y = int(screen.get("y") or 0)
        self.peer.monitors = len(screen.get("monitors") or []) or 1
        self.peer.platform = ((screen.get("platform") or {}).get("os")) or ""
        if not dcrypto.HAS_CRYPTO:
            self.keys = dcrypto.SessionKeys(plain=True)
        else:
            try:
                shared = self.eph.shared(peer_pub)
            except Exception as exc:  # noqa: BLE001
                self.error = f"KEX nie udany: {exc}"
                return False
            transcript = "|".join(sorted([self.eph.public_b64, peer_pub])) + f"|{ack.get('challenge','')}"
            self.keys = dcrypto.derive_session_keys(shared, transcript.encode())
            self.challenge = dcrypto.b64d(ack.get("challenge", ""))
        # TOFU: sprawdz odcisk palca
        if self.peer.desk_id and self.peer.fp:
            trusted = self.cfg.is_trusted(self.peer.desk_id, self.peer.fp)
            if not trusted:
                self._emit("fp_changed", {"peer": self.peer.as_dict()})
                self.error = "odcisk klucza niezgodny z zapisanym - polaczenie przerwane"
                self._safe_send(FT.ERROR, {"msg": self.error}, plain=True)
                return False
            self.cfg.trust(self.peer.desk_id, self.peer.fp)
            self.store.flush(force=True)
        self.cfg.remember_peer(self.peer.desk_id, self.peer.name, self.peer.fp)
        self.store.flush(force=True)
        self._emit("peer_info", self.peer.as_dict())

        if self.keys and not self.keys.plain:
            tx, rx = self.keys.auth_for_role(True)
            self.pipe.attach_crypto(dcrypto.SessionCrypto(tx, rx))
        proof = self._make_proof(ack.get("salt", ""), int(ack.get("rounds", dcrypto.PBKDF2_ROUNDS)))
        self._safe_send(
            FT.AUTH_REQ,
            {"proof": dcrypto.b64e(proof), "pub": self.cfg.identity.public_b64, "prefs": self._prefs()},
        )
        self._emit("log", "handshake: czekam na zgoda / haslo")
        frame = self._recv_plain((FT.AUTH_OK, FT.AUTH_NO), AUTH_TIMEOUT)
        if frame.type == FT.AUTH_NO:
            info = frame.json
            self.error = f"odmowa: {info.get('reason', 'nieznany')}"
            self._emit("auth_failed", info)
            return False
        ok = frame.json
        tx, rx = self.keys.for_role(True) if (self.keys and not self.keys.plain) else (b"", b"")
        if self.keys and not self.keys.plain:
            self.pipe.attach_crypto(dcrypto.SessionCrypto(tx, rx))
        self._apply_caps(ok.get("caps") or {})
        self.granted_input = bool((ok.get("caps") or {}).get("input", False))
        self.remote_pw_ok = True
        # kontroler prosi o klatki - host zacznie nadawac dopiero po SCREEN_REQ
        self.request_screen(True, force=True)
        return True

    def _make_proof(self, salt: str, rounds: int) -> bytes:
        if not salt or self.keys is None or self.keys.plain:
            return b""
        auth_tx, _ = self.keys.auth_for_role(True)
        pw_hash = dcrypto.hash_password(self.password or "", salt, rounds) if self.password else b"\x00" * 32
        return dcrypto.auth_proof(auth_tx, self.challenge, self.cfg.identity.fp, pw_hash)

    def _handshake_host(self) -> bool:
        frame = self._recv_plain((FT.HELLO_KEX,), HANDSHAKE_TIMEOUT)
        hello = frame.json
        if hello.get("role") != "controller":
            self.error = "oczekiwalem kontrolera"
            return False
        peer_pub = hello.get("pub", "")
        self.peer.desk_id = int(hello.get("id") or 0)
        self.peer.name = str(hello.get("name") or "nieznany")
        self.peer.fp = str(hello.get("fp") or "")
        prefs = hello.get("prefs") or {}
        self.want_input = bool(prefs.get("want_input", True))
        if not dcrypto.HAS_CRYPTO:
            self.keys = dcrypto.SessionKeys(plain=True)
        else:
            try:
                shared = self.eph.shared(peer_pub)
            except Exception as exc:  # noqa: BLE001
                self.error = f"KEX nie udany: {exc}"
                return False
            transcript = "|".join(sorted([self.eph.public_b64, peer_pub])) + f"|{dcrypto.b64e(self.challenge)}"
            self.keys = dcrypto.derive_session_keys(shared, transcript.encode())
        self._emit("peer_info", self.peer.as_dict())
        self._safe_send(
            FT.HELLO_KEX_ACK,
            {
                "v": 1,
                "pub": self.eph.public_b64,
                "fp": self.cfg.identity.fp,
                "id": self.cfg.identity.desk_id,
                "name": machine_name_cached(),
                "challenge": dcrypto.b64e(self.challenge),
                "salt": self.cfg.password_salt,
                "rounds": dcrypto.PBKDF2_ROUNDS,
                "need_password": self.cfg.has_password,
                "screen": self._my_screen_info(),
                "caps": self._my_caps(),
            },
            plain=True,
        )
        if self.keys and not self.keys.plain:
            tx, rx = self.keys.auth_for_role(False)
            self.pipe.attach_crypto(dcrypto.SessionCrypto(tx, rx))
        try:
            frame = self._recv_plain((FT.AUTH_REQ,), AUTH_TIMEOUT)
        except TimeoutError:
            self._safe_send(FT.AUTH_NO, {"reason": "timeout"})
            self.error = "brak proby logowania"
            return False
        req = frame.json
        pw_ok = self._check_proof(req.get("proof", ""))
        if self.cfg.has_password and not pw_ok:
            self._safe_send(FT.AUTH_NO, {"reason": "password", "retry": True})
            self.error = "złe haslo"
            return False
        if not self.cfg.has_password and not pw_ok:
            self.remote_pw_ok = False
        else:
            self.remote_pw_ok = pw_ok
        # decyzja uzytkownika (monit jak w AnyDesk). 'confirm_incoming' jest jedynym
        # przelacznikiem: wylaczony = tryb bez nadzoru (haslo albo swiadomie bez hasla),
        # tak samo jak przy 'dzwonku' po relayu w Agent._on_ring.
        input_ok = bool(self.cfg.allow_input and self.want_input and not prefs.get("view_only"))
        if self.preapproved is not None:
            # zgoda zapadla juz przy 'przychodzace polaczenie' (relay) - nie pytamy drugi raz
            input_ok = input_ok and bool(self.preapproved.get("input", True))
        elif self.cfg.confirm_incoming:
            self._emit(
                "permission",
                {
                    "peer": self.peer.as_dict(),
                    "want_input": self.want_input,
                    "password_ok": pw_ok,
                    "unattended": self.cfg.unattended,
                    "clipboard": bool(prefs.get("clipboard")),
                    "files": bool(prefs.get("files")),
                },
            )
            answered = self._perm_event.wait(PERMISSION_TIMEOUT)
            ans = self._perm_answer or {}
            if not answered or not ans.get("allow"):
                reason = "declined" if answered else "timeout"
                self._safe_send(FT.AUTH_NO, {"reason": reason})
                self.error = f"uzytkownik: {reason}"
                return False
            input_ok = input_ok and bool(ans.get("input", True))
        caps = self._my_caps()
        caps["input"] = input_ok
        caps["clipboard"] = bool(caps["clipboard"] and prefs.get("clipboard"))
        caps["files"] = bool(caps["files"] and prefs.get("files"))
        self._safe_send(FT.AUTH_OK, {"caps": caps, "screen": self._my_screen_info()})
        tx, rx = self.keys.for_role(False) if (self.keys and not self.keys.plain) else (b"", b"")
        if self.keys and not self.keys.plain:
            self.pipe.attach_crypto(dcrypto.SessionCrypto(tx, rx))
        self.peer_controls_me = input_ok
        if input_ok and self.cfg.auto_lock_local:
            self.blocker.grab()
        self._apply_caps(req.get("prefs") or {})
        return True

    def _check_proof(self, proof_b64: str) -> bool:
        if not proof_b64 or self.keys is None or self.keys.plain:
            return False
        if not self.cfg.has_password:
            return False
        try:
            proof = dcrypto.b64d(proof_b64)
        except Exception:  # noqa: BLE001
            return False
        _, auth_rx = self.keys.auth_for_role(False)
        expect = dcrypto.auth_proof(
            auth_rx, self.challenge, self.peer.fp, bytes.fromhex(self.cfg.password_hash)
        )
        import hmac

        return hmac.compare_digest(proof, expect)

    def answer_permission(self, allow: bool, input: bool = True, password_ok: bool = None) -> None:  # noqa: A002
        self._perm_answer = {"allow": bool(allow), "input": bool(input)}
        self._perm_event.set()

    def _my_caps(self) -> dict:
        return {
            "input": bool(self.cfg.allow_input),
            "clipboard": bool(self.cfg.clipboard_sync),
            "files": bool(self.cfg.allow_files),
            "fps": int(self.cfg.fps),
            "quality": int(self.cfg.quality),
            "scale": float(self.cfg.scale),
            "view_only_default": bool(self.cfg.view_only_default),
            "screen": True,
        }

    def _apply_caps(self, caps: dict) -> None:
        if not caps:
            return
        self.caps_peer = caps
        if self.role == "host":
            # kontroler podaje jakosc, jaka chce zobaczyc - to on jest 'pilotem'
            fps = caps.get("fps")
            if fps:
                self._out_fps = int(max(3, min(60, int(fps))))
            q = caps.get("quality")
            if q:
                self._out_quality = int(max(20, min(95, int(q))))
            sc = caps.get("scale")
            if sc:
                self._out_scale = float(max(0.25, min(1.0, float(sc))))
            # pilot moze prosic o inny monitor nadajacego (wielomonitorowosc)
            mon = caps.get("monitor")
            if mon is not None and int(mon) != int(getattr(self.source, "monitor_index", 0) or 0):
                self.set_monitor(int(mon))

    # ================================================================== stream ekranu
    def _start_streaming(self, want: bool = True) -> None:
        if want:
            self.share_screen = True
            self._force_key = True
            try:
                w, h = self.source.native_size()
                self.injector.set_area(0, 0, w, h)
            except Exception:  # noqa: BLE001
                pass

    def _stream_loop(self) -> None:
        """Nadajnik wlasnego ekranu + adaptacyjna jakosc (chroni przed zatkaniem laca)."""
        target_native = None
        while not self.stop_event.is_set():
            if not self.share_screen:
                time.sleep(0.2)
                continue
            started = time.monotonic()
            if target_native != self.source.native_size():
                target_native = self.source.native_size()
                self._send_screen_info()
            try:
                cap = self.source.grab(
                    quality=self._out_quality,
                    scale=self._out_scale,
                    fmt=self._out_fmt,
                    force=self._force_key,
                )
            except Exception as exc:  # noqa: BLE001 - ekran zniknal (monitor odlaczony?)
                self.error = f"capture: {exc}"
                self._emit("log", f"capture blad: {exc}")
                try:
                    self.source.reset_diff()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1.0)
                continue
            self._force_key = False
            if cap is not None:
                t0 = time.monotonic()
                if not self._safe_send(FT.FRAME, frame_payload(cap), flags=FLAG_KEYFRAME):
                    return
                self._last_send_ms = (time.monotonic() - t0) * 1000.0
                self._tune()
            period = 1.0 / max(1, self._out_fps)
            elapsed = time.monotonic() - started
            if elapsed < period:
                time.sleep(period - elapsed)

    def _tune(self) -> None:
        """Sterowana przepustowosc: wolny wysyl = mniej klatek/jakosci, szybki = wroc do celu."""
        if not self.cfg.adaptive:
            return
        goal_q = int(self.cfg.quality)
        goal_fps = int(self.cfg.fps)
        if self._last_send_ms > 180.0:
            self._out_fps = max(3, self._out_fps - 2)
            self._out_quality = max(28, self._out_quality - 6)
            self.stats["dropped"] += 1
        elif self._last_send_ms < 25.0:
            self._out_fps = min(goal_fps, self._out_fps + 1)
            self._out_quality = min(goal_q, self._out_quality + 2)
        self.stats["quality"] = self._out_quality
        self.stats["fps_out"] = self._out_fps

    def _send_screen_info(self) -> None:
        self._safe_send(FT.SCREEN_INFO, self._my_screen_info())

    # ================================================================== odbior
    def _recv_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.pipe.recv(timeout=1.0)
            except TimeoutError:
                continue
            except (ConnectionError, ProtocolError, OSError) as exc:
                self.error = str(exc)
                break
            try:
                self._dispatch(frame)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self._teardown(self.error or "polaczenie zerwane")

    def _dispatch(self, frame) -> None:  # noqa: C901 - switche w jednym miejscu
        ftype = frame.type
        if ftype == FT.FRAME:
            self._on_frame(frame)
        elif ftype == FT.SCREEN_INFO:
            info = frame.json
            self.peer.width = int(info.get("width") or self.peer.width)
            self.peer.height = int(info.get("height") or self.peer.height)
            self.peer.offset_x = int(info.get("x") or 0)
            self.peer.offset_y = int(info.get("y") or 0)
            mons = info.get("monitors") or []
            self.peer.monitors = len(mons) or 1
            self.peer.platform = (info.get("platform") or {}).get("os", self.peer.platform)
            self.injector.set_area(0, 0, self.peer.width or 1920, self.peer.height or 1080)
            self._emit("screen_info", info)
        elif ftype == FT.SCREEN_REQ:
            req = frame.json
            self._apply_caps(req)
            self._start_streaming(bool(req.get("want", True)))
            if not req.get("want", True):
                self.share_screen = False
        elif ftype in (FT.MOUSE, FT.KEY, FT.TEXT, FT.SCROLL):
            self._on_input(frame)
        elif ftype == FT.GRANT:
            obj = frame.json
            if obj.get("by", "host") != self.role:  # nasze wlasne echo ignorujemy
                if self.role == "controller":
                    self.granted_input = bool(obj.get("input"))
                    if not self.granted_input:
                        self._emit("log", "host odbil mi kontrole myszy i klawiatury")
                else:
                    self.peer_controls_me = bool(obj.get("input")) and bool(self.cfg.allow_input)
                    if not self.peer_controls_me:
                        self.injector.release_all()
                        self.blocker.ungrab()
            self._emit("grant", obj)
        elif ftype == FT.PING:
            self._safe_send(FT.PONG, {"ts": frame.json.get("ts"), "stats": self._out_stats_brief()})
        elif ftype == FT.PONG:
            obj = frame.json
            sent = float(obj.get("ts") or 0.0)
            if sent:
                self.stats["rtt_ms"] = round((time.time() - sent) * 1000.0, 1)
            self.stats["peer"] = obj.get("stats") or {}
        elif ftype == FT.CLIP_SET:
            if self.cfg.clipboard_sync:
                self._emit("clipboard", frame.json.get("s", ""))
        elif ftype == FT.CLIP_GET:
            self._emit("clipboard_get", {})
        elif ftype in (FT.FILE_META, FT.FILE_DATA, FT.FILE_DONE):
            self._on_file(frame)
        elif ftype == FT.CAPS:
            obj = frame.json
            if obj.get("input_request") and self.role == "host":
                self._emit("input_request", {"peer": self.peer.as_dict()})
            else:
                self._apply_caps(obj)
                self._emit("caps", obj)
        elif ftype == FT.BYE:
            self.error = f"druga strona rozlaczyla: {frame.json.get('reason', '')}"
            self.stop_event.set()
        elif ftype == FT.ERROR:
            self._emit("log", f"peer error: {frame.json.get('msg')}")
        elif ftype == FT.AUTH_NO and self.state != "connected":
            self._emit("auth_failed", frame.json)
            self.stop_event.set()

    def _on_frame(self, frame) -> None:
        x, y, w, h, fmt, q, data = unpack_frame_header(frame.payload)
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:  # noqa: BLE001
            self.stats["dropped"] += 1
            self._emit("log", f"dekodowanie klatki: {exc}")
            return
        if img.mode != "RGB":
            img = img.convert("RGB")
        with self._video_lock:
            self.latest = img
            self.gen += 1
        self.stats["frames_in"] += 1
        self.stats["frame_w"] = w
        self.stats["frame_h"] = h

    def _on_input(self, frame) -> None:
        if self.role != "host" or not self.peer_controls_me or not self.cfg.allow_input:
            self.injector.rejected += 1
            return
        obj = frame.json
        if frame.type == FT.MOUSE:
            fx = float(obj.get("fx", 0.0))
            fy = float(obj.get("fy", 0.0))
            act = obj.get("act", "move")
            if act == "move":
                self.injector.move(fx, fy)
            else:
                self.injector.button(obj.get("btn", "left"), act, fx, fy, int(obj.get("n", 1)))
        elif frame.type == FT.SCROLL:
            self.injector.scroll(float(obj.get("dx", 0)), float(obj.get("dy", 0)),
                                 float(obj.get("fx", 0)), float(obj.get("fy", 0)))
        elif frame.type == FT.KEY:
            canon = obj.get("canon")
            if obj.get("combo"):
                self.injector.hotkey(obj["combo"])
            else:
                self.injector.key(canon, obj.get("act", "press"), obj.get("char"))
        elif frame.type == FT.TEXT:
            self.injector.type_text(obj.get("s", ""))

    # ================================================================== wyjscie (kontroler)
    def _guard(self) -> bool:
        return self.state == "connected" and not self.stop_event.is_set()

    def _can_input(self) -> bool:
        """Kontroler wysyla wejscie tylko gdy host mu przyznal GRANT."""
        return self._guard() and bool(self.granted_input) and self.role == "controller"

    def send_mouse(self, fx: float, fy: float, btn: Optional[str] = None, act: str = "move", n: int = 1) -> None:
        if not self._can_input():
            return
        payload = {"fx": round(fx, 5), "fy": round(fy, 5), "act": act}
        if btn:
            payload["btn"] = btn
            payload["n"] = n
        self._safe_send(FT.MOUSE, payload)

    def send_key(self, canon: Optional[str], act: str = "press", char: Optional[str] = None,
                 combo: Optional[str] = None) -> None:
        if not self._can_input():
            return
        payload = {"act": act}
        if combo:
            payload["combo"] = combo
        else:
            payload["canon"] = canon
            if char:
                payload["char"] = char
        self._safe_send(FT.KEY, payload)

    def send_text(self, text: str) -> None:
        if not self._can_input() or not text:
            return
        self._safe_send(FT.TEXT, {"s": text})

    def send_scroll(self, dx: float, dy: float, fx: float, fy: float) -> None:
        if not self._can_input():
            return
        self._safe_send(FT.SCROLL, {"dx": round(dx, 3), "dy": round(dy, 3),
                                    "fx": round(fx, 5), "fy": round(fy, 5)})

    def set_monitor(self, index: int) -> None:
        """Zmiana monitora po stronie nadajacej (host)."""
        try:
            self.source.monitor_index = int(index)
            self.source.reset_diff()
            self._force_key = True
            self._send_screen_info()
            self.cfg.monitor = int(index)
            self.store.flush(force=True)
        except Exception as exc:  # noqa: BLE001
            self._emit("log", f"monitor: {exc}")

    def set_input(self, on: bool) -> None:
        """Przywrocenie/odebranie kontroli.

        Bezpieczenstwo: kontroler MOZE tylko oddac sterowanie albo o nie poprosic.
        Decyzje 'input=True' zawsze wysyla host - inaczej pilot podnosilby sobie
        uprawnienia sam.
        """
        on = bool(on)
        if self.role == "host":
            self.peer_controls_me = on and bool(self.cfg.allow_input)
            self._safe_send(FT.GRANT, {"input": self.peer_controls_me, "by": "host"})
            if not self.peer_controls_me:
                self.injector.release_all()
                self.blocker.ungrab()
            elif self.cfg.auto_lock_local:
                self.blocker.grab()
            self._emit("grant", {"input": self.peer_controls_me, "by": "host"})
        else:
            if not on:
                self.granted_input = False
                self._safe_send(FT.GRANT, {"input": False, "by": "controller"})
                self._emit("grant", {"input": False, "by": "controller"})
            else:
                self._safe_send(FT.CAPS, {"input_request": True})
                self._emit("log", "poproszylem o ponowne przyznanie kontroli")

    def answer_input_request(self, allow: bool) -> None:
        """Host odpowiada na probe kontrolera o odzyskanie sterowania."""
        if self.role != "host":
            return
        self.set_input(bool(allow))

    def request_screen(self, want: bool = True, force: bool = False) -> None:
        """Prosba o klatki. Wolane tez w handshake, wiec nie pilnuje stanu 'connected'."""
        if self.stop_event.is_set():
            return
        payload = {
            "want": bool(want),
            "force": bool(force),
            "fps": int(self.store.config.fps),
            "quality": int(self.store.config.quality),
            "scale": float(self.store.config.scale),
        }
        if self.stream_monitor is not None:
            payload["monitor"] = int(self.stream_monitor)
        for key in ("fps", "quality", "scale"):
            if key in self.stream_over:
                payload[key] = self.stream_over[key]
        self._safe_send(FT.SCREEN_REQ, payload)
        if force:
            self._force_key = True

    def set_sharing(self, on: bool) -> None:
        """Nadajacy moze w kazdej chwili wcisnac 'przestan nadawac' (prywatnosc)."""
        on = bool(on)
        self.share_screen = on
        if on:
            self._start_streaming(True)
        self._emit("sharing", {"on": on})

    def local_monitors(self) -> list:
        """Lokalne monitory nadajacego - UI buduje z tego menu wyboru."""
        try:
            return [{"i": m.index, "w": m.width, "h": m.height, "name": m.name}
                    for m in (self.source.monitors() or [])]
        except Exception:  # noqa: BLE001
            return []

    def select_monitor(self, index: int) -> None:
        """Kontroler prosi o inny monitor nadajacego (0 = pierwszy)."""
        self.stream_monitor = int(index)
        self.request_screen(True, force=True)
        self._emit("log", f"monitor {index} - prosba wyslana")

    def set_stream_params(self, quality: int = None, fps: int = None, scale: float = None,
                          encoding: str = None) -> None:
        """Usluga lokalna - to my nadajemy swoj ekran, wiec zmieniamy sobie parametry."""
        if quality is not None:
            self._out_quality = int(max(20, min(95, quality)))
        if fps is not None:
            self._out_fps = int(max(3, min(60, fps)))
        if scale is not None:
            self._out_scale = float(max(0.25, min(1.0, scale)))
        if encoding:
            self._out_fmt = encoding
        if self.role == "controller":
            # pilot nie nadaje ekranu - on tylko mowi hostowi, jaki obraz chce dostac
            for key, val in (("fps", fps), ("quality", quality), ("scale", scale)):
                if val is not None:
                    self.stream_over[key] = val
            self.request_screen(True, force=True)
        self._force_key = True

    def copy_clipboard(self, text: str) -> None:
        if not self._guard() or not self.cfg.clipboard_sync:
            return
        self._safe_send(FT.CLIP_SET, {"s": text[:200000]})

    # ================================================================== pliki
    def send_file(self, path: str | os.PathLike) -> bool:
        p = Path(path)
        if not p.is_file():
            self._emit("log", f"plik nie istnieje: {p}")
            return False
        fid = self._next_fid
        self._next_fid += 1
        size = p.stat().st_size
        self._files[fid] = {"dir": "out", "path": p, "sent": 0, "size": size, "name": p.name}
        self._safe_send(FT.FILE_META, {"fid": fid, "name": p.name, "size": size, "chunks": -1})
        threading.Thread(target=self._send_file_worker, args=(fid, p, size), name="rd-file-send", daemon=True).start()
        return True

    def _send_file_worker(self, fid: int, p: Path, size: int) -> None:
        try:
            with p.open("rb") as fh:
                while not self.stop_event.is_set():
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    payload = struct.pack(">I", fid) + chunk
                    if not self._safe_send(FT.FILE_DATA, payload):
                        return
                    st = self._files.get(fid)
                    if st:
                        st["sent"] += len(chunk)
                        self._emit("file_progress", {"fid": fid, "dir": "out", "name": p.name,
                                                     "sent": st["sent"], "size": size})
                    time.sleep(0.001)
            self._safe_send(FT.FILE_DONE, {"fid": fid, "size": size})
        except OSError as exc:
            self._emit("log", f"wysylanie pliku: {exc}")

    def _incoming_dir(self) -> Path:
        from .settings import cache_dir

        base = cache_dir() / "incoming"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _on_file(self, frame) -> None:
        if not self.cfg.allow_files:
            return
        if frame.type == FT.FILE_META:
            meta = frame.json
            fid = int(meta.get("fid") or 0)
            target = self._incoming_dir() / str(meta.get("name", "plik.bin"))
            i = 1
            while target.exists():
                target = target.with_name(f"{target.stem}({i}){target.suffix}")
                i += 1
            self._files[fid] = {"dir": "in", "path": target, "sent": 0, "size": int(meta.get("size") or 0),
                                "name": target.name, "fh": open(target, "wb")}
            self._emit("file_started", {"fid": fid, "name": target.name, "size": meta.get("size")})
        elif frame.type == FT.FILE_DATA:
            fid = struct.unpack(">I", frame.payload[:4])[0]
            st = self._files.get(fid)
            if not st or st["dir"] != "in":
                return
            try:
                st["fh"].write(frame.payload[4:])
                st["sent"] += len(frame.payload) - 4
                self._emit("file_progress", {"fid": fid, "dir": "in", "name": st["name"],
                                             "sent": st["sent"], "size": st["size"]})
            except OSError as exc:
                self._emit("log", f"zapis pliku: {exc}")
        elif frame.type == FT.FILE_DONE:
            fid = int(frame.json.get("fid") or 0)
            st = self._files.pop(fid, None)
            if st and st["dir"] == "in":
                try:
                    st["fh"].close()
                except OSError:
                    pass
                self._emit("file_done", {"name": st["name"], "path": str(st["path"]), "size": st["sent"]})

    # ================================================================== statystyki
    def _out_stats_brief(self) -> dict:
        return {"fps": self._out_fps, "quality": self._out_quality, "kbps": round(self.stats.get("kbps_out", 0), 1)}

    def _stats_loop(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(1.0)
            now = time.monotonic()
            dt = max(0.001, now - self._bytes_in_at) if self._bytes_in_at else 0
            b_in = self.pipe.bytes_in
            b_out = self.pipe.bytes_out
            if self._bytes_in_at:
                self.stats["kbps_in"] = round((b_in - getattr(self, "_b_in", 0)) * 8 / 1000 / dt, 1)
                self.stats["kbps_out"] = round((b_out - getattr(self, "_b_out", 0)) * 8 / 1000 / dt, 1)
                f_in = self.stats["frames_in"]
                self.stats["fps_in"] = round((f_in - getattr(self, "_f_in", 0)) / dt, 1)
            self._b_in, self._b_out, self._f_in, self._bytes_in_at = b_in, b_out, self.stats["frames_in"], now
            self.stats["uptime"] = round(time.time() - self.connected_at, 1) if self.connected_at else 0
            self.stats["state"] = self.state
            self.stats["input"] = self.granted_input or self.peer_controls_me
            self._emit("stats", dict(self.stats))
            if self.role == "controller":
                self._safe_send(FT.PING, {"ts": time.time()})
        self._stats_done = True

    # ================================================================== API dla GUI
    def pull_image(self) -> Tuple[Optional[Image.Image], int]:
        with self._video_lock:
            return self.latest, self.gen

    def is_alive(self) -> bool:
        return self.state == "connected" and not self.stop_event.is_set()

    def normalize_key(self, key) -> Tuple[Optional[str], Optional[str]]:
        return normalize_pynput_key(key)


_MACHINE_NAME: Optional[str] = None


def machine_name_cached() -> str:
    global _MACHINE_NAME
    if _MACHINE_NAME is None:
        import socket

        _MACHINE_NAME = socket.gethostname().split(".")[0] or "bez-nazwy"
    return _MACHINE_NAME
