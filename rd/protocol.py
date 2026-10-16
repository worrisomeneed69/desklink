"""DeskLink - binarna warstwa ramek (protokół sesji).

Format ramki (13 bajtow naglowka, big-endian):

    magic 'DL'  ver  type  flags  seq(u32)  length(u32) | payload

`seq` to licznik per-kierunek, uzywany jako nonce AES-GCM - dzieki temu nonce
nigdy sie nie powtarza. 32 bity = przy 30 klatkach/s rekeying by potrebowal
dopiero po ~45 latach, wiec nie ma sensu robic skomplikowanego restartu strumienia.
Kierunki maja osobne klucze (c2s / s2c), wiec AEAD jest bezpieczne w pelnym duplexie.
"""

from __future__ import annotations

import enum
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Optional, Tuple

PROTOCOL_VERSION = 1
MAGIC = b"DL"
HEADER = struct.Struct(">2sBBBII")
HEADER_LEN = HEADER.size  # 13
MAX_PAYLOAD = 32 * 1024 * 1024
COMPRESS_MIN = 900
SEQ_MASK = 0xFFFFFFFF


class FT(enum.IntEnum):
    """Typy ramek."""

    # negotiated / control
    HELLO_KEX = 0x01        # plain: propozycja X25519 od kontrolera
    HELLO_KEX_ACK = 0x02    # plain: odpowiedź hosta + challenge
    AUTH_REQ = 0x03         # plain(auth_key): proof hasła + preferencje
    AUTH_OK = 0x04          # auth_key: sesja zaakceptowana
    AUTH_NO = 0x05          # auth_key: odmowa / zły powód
    CAPS = 0x06             # data_key: negocjacja możliwości
    PING = 0x07
    PONG = 0x08
    BYE = 0x09
    ERROR = 0x0A
    # obraz
    SCREEN_INFO = 0x10
    SCREEN_REQ = 0x11
    FRAME = 0x12
    CURSOR = 0x13
    # wejście
    MOUSE = 0x20
    KEY = 0x21
    TEXT = 0x22
    SCROLL = 0x23
    GRANT = 0x24            # przyznano/odebrano kontrolę myszy i klawiatury
    # schowek
    CLIP_GET = 0x30
    CLIP_SET = 0x31
    # pliki
    FILE_META = 0x40
    FILE_DATA = 0x41
    FILE_DONE = 0x42
    # diagnostyka
    SYSINFO = 0x50
    STATUS = 0x51
    RAW = 0xFE


FLAG_PLAIN = 0x01        # nie szyfruj (handshake)
FLAG_COMPRESSED = 0x02   # payload zlib
FLAG_KEYFRAME = 0x04     # pełna klatka
FLAG_PARTIAL = 0x08      # ramka regionu (dirty rect)

FRAME_HEADER = struct.Struct(">HHHHBB")  # x, y, w, h, fmt, quality
FRAME_HEADER_LEN = FRAME_HEADER.size


class ProtocolError(Exception):
    pass


@dataclass
class Frame:
    type: int
    payload: bytes = b""
    flags: int = 0
    seq: int = 0
    ts: float = 0.0

    @property
    def json(self) -> dict:
        import json

        if not self.payload:
            return {}
        return json.loads(self.payload.decode("utf-8", "replace"))


def encode_json(obj: dict) -> bytes:
    import json

    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pack_frame(ftype: int, payload: bytes, flags: int = 0, seq: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload za duzy: {len(payload)}")
    if flags & FLAG_COMPRESSED:
        payload = zlib.compress(payload, 6)
    return HEADER.pack(MAGIC, PROTOCOL_VERSION, ftype, flags, seq & SEQ_MASK, len(payload)) + payload


def unpack_frame(raw: bytes) -> Tuple[Frame, bytes]:
    """Rozbierz jedną ramkę z bufora; zwróć (ramka, resztę bufora)."""
    if len(raw) < HEADER_LEN:
        raise ProtocolError("bufor za krotki na naglowek")
    magic, ver, ftype, flags, seq, length = HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise ProtocolError(f"zly magic: {magic!r}")
    if ver != PROTOCOL_VERSION:
        raise ProtocolError(f"nieznana wersja protokolu: {ver}")
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"ramka za duza: {length}")
    if len(raw) < HEADER_LEN + length:
        raise ProtocolError("niekompletna ramka")
    payload = raw[HEADER_LEN : HEADER_LEN + length]
    if flags & FLAG_COMPRESSED:
        payload = zlib.decompress(payload)
    return Frame(ftype, payload, flags, seq, time.time()), raw[HEADER_LEN + length :]


class Pipe:
    """Uchwyt na gniazdo TCP z ramkowaniem, opcjonalnym AEAD i wysyłką thread-safe."""

    def __init__(self, sock: socket.socket, buffer: bytes = b"", name: str = "peer"):
        self.sock = sock
        self.name = name
        self._buf = bytearray(buffer)
        self._send_lock = threading.Lock()
        self._tx_seq = 0
        self._rx_seq_expected: Optional[int] = None
        self.crypto = None          # rd.crypto.SessionCrypto
        self.closed = False
        self.bytes_out = 0
        self.bytes_in = 0
        self.frames_out = 0
        self.frames_in = 0
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    # ---------------------------------------------------------------- niskopoziomowe
    def _read(self, n: int, timeout: Optional[float]) -> bytes:
        """Dokładnie n bajtów z bufora/gniazda; timeout None = blokująco."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self._buf) < n:
            if self.closed:
                raise ConnectionError("pipe zamkniety")
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("timeout odczytu ramki")
                self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(64 * 1024)
            except (socket.timeout, TimeoutError):
                raise TimeoutError("timeout odczytu ramki")
            except OSError as exc:
                raise ConnectionError(f"odczyt przerwany: {exc}") from exc
            if not chunk:
                self.closed = True
                raise ConnectionError("kolejny koniec polaczenia (peer zamknal)")
            self._buf.extend(chunk)
            self.bytes_in += len(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_frame(self, frame: Frame) -> None:
        if self.closed:
            raise ConnectionError("pipe zamkniety")
        flags = frame.flags
        payload = frame.payload
        # kompresja ma sens tylko dla JSON-ów (duże, powtarzalne słownictwo)
        if payload and not (flags & FLAG_PLAIN) and len(payload) > COMPRESS_MIN and frame.type != FT.FRAME:
            comp = zlib.compress(payload, 6)
            if len(comp) < len(payload):
                payload, flags = comp, flags | FLAG_COMPRESSED
        seq = self._tx_seq
        if self.crypto is not None and not (flags & FLAG_PLAIN):
            payload = self.crypto.encrypt(payload, seq, flags)
            # tylko ramki szyfrowane zjadaja seq - dzieki temu nonce i licznik
            # po drugiej stronie zawsze ida 1:1, nawet gdy w srodek wpadnie BYE
            self._tx_seq = (self._tx_seq + 1) & SEQ_MASK
        raw = HEADER.pack(MAGIC, PROTOCOL_VERSION, frame.type, flags, seq, len(payload)) + payload
        with self._send_lock:
            try:
                self.sock.sendall(raw)
            except OSError as exc:
                self.closed = True
                raise ConnectionError(f"wysylka przerwana: {exc}") from exc
        self.bytes_out += len(raw)
        self.frames_out += 1

    def send(self, ftype: int, payload: bytes = b"", flags: int = 0) -> None:
        self.send_frame(Frame(ftype, payload, flags))

    def send_json(self, ftype: int, obj: dict, flags: int = 0) -> None:
        self.send(ftype, encode_json(obj), flags)

    def attach_crypto(self, crypto) -> None:
        """Podpinanie AEAD w trakcie sesji (auth_key -> data_key).

        Liczniki nonce musza startowac od aktualnego seq, inaczej druga strona
        dostanie 'ramka poza kolejnoscia'.
        """
        crypto.sync(self._tx_seq, self._rx_seq_expected or 0)
        self.crypto = crypto

    def recv(self, timeout: Optional[float] = None) -> Frame:
        head = self._read(HEADER_LEN, timeout)
        magic, ver, ftype, flags, seq, length = HEADER.unpack_from(head, 0)
        if magic != MAGIC:
            raise ProtocolError(f"zly magic: {magic!r}")
        if ver != PROTOCOL_VERSION:
            raise ProtocolError(f"wersja {ver} nieobslugiwana")
        if length > MAX_PAYLOAD:
            raise ProtocolError("ramka za duza")
        payload = self._read(length, timeout) if length else b""
        if flags & FLAG_PLAIN:
            # ramki jawne (BYE/ERROR) nie sa szyfrowane i nie zjadaja numeru seq,
            # wiec liczniki AEAD po obu stronach zostaja spojne
            pass
        elif self.crypto is not None:
            payload = self.crypto.decrypt(payload, seq, flags)
            if self._rx_seq_expected is not None and seq != self._rx_seq_expected:
                raise ProtocolError(f"sekwencja rozjedziona: oczekiwano {self._rx_seq_expected}, jest {seq}")
            self._rx_seq_expected = (seq + 1) & SEQ_MASK
        if flags & FLAG_COMPRESSED:
            payload = zlib.decompress(payload)
        self.frames_in += 1
        return Frame(ftype, payload, flags, seq, time.time())

    def recv_into_callback(self, on_frame, on_stop=None) -> None:
        """Pętla odbiorcza dla watku czytajacego."""
        while not self.closed:
            try:
                frame = self.recv(timeout=1.0)
            except TimeoutError:
                continue
            except (ConnectionError, ProtocolError, zlib.error) as exc:
                self.closed = True
                if on_stop:
                    on_stop(exc)
                return
            try:
                on_frame(frame)
            except Exception:  # noqa: BLE001 - nie zabijaj petli bledem handlera
                import traceback

                traceback.print_exc()

    def set_timeout(self, t: Optional[float]) -> None:
        try:
            self.sock.settimeout(t)
        except OSError:
            pass

    def abort(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def close(self) -> None:
        self.abort()

    # handshake
    def pending(self) -> bytes:
        return bytes(self._buf)


def pack_frame_header(x: int, y: int, w: int, h: int, fmt: int = 0, quality: int = 70) -> bytes:
    return FRAME_HEADER.pack(x, y, w, h, fmt, quality)


def unpack_frame_header(payload: bytes) -> Tuple[int, int, int, int, int, int, bytes]:
    x, y, w, h, fmt, q = FRAME_HEADER.unpack_from(payload, 0)
    return x, y, w, h, fmt, q, payload[FRAME_HEADER.size :]


def connect_tcp(host: str, port: int, timeout: float = 8.0, ssl_ctx=None) -> socket.socket:
    """Connect z obsluga IPv4/IPv6 i opcjonalnym TLS."""
    last: Optional[OSError] = None
    for family, stype, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        try:
            s = socket.socket(family, stype, proto)
            s.settimeout(timeout)
            s.connect(addr)
            s.settimeout(None)
            if ssl_ctx is not None:
                s = ssl_ctx.wrap_socket(s, server_hostname=host)
            return s
        except OSError as exc:
            last = exc
            try:
                s.close()
            except OSError:
                pass
    raise ConnectionError(f"nie moglem polaczyc sie z {host}:{port}: {last}")


def make_listener(host: str, port: int, backlog: int = 16) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(backlog)
    return srv
