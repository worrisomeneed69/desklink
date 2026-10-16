"""DeskLink - krypto sesji.

Model (zbliżony do AnyDesk, ale uproszczony i jawny):

1. Kontroler i host robia wymiane kluczy X25519 (jednorazowa para na sesje).
2. Z shared secret przez HKDF powstaja 4 klucze: auth/data x kierunek (c2s/s2c).
   Osobne klucze na kierunek = brak kolizji nonce AES-GCM w pelnym duplexie.
3. Host zna tylko weryfikator hasla (PBKDF2). Kontroler dowodzi, ze zna haslo, przez
   HMAC kluczem auth nad challenge - haslo nigdy nie leci po drucie.
4. Po AUTH wszystkie ramki idza AES-256-GCM z nonce = pelny licznik sekwencji.
5. Odcisk palca (fingerprint) klucza publicznego hosta = to co pokazujemy uzytkownikowi
   i zapamietujemy przy pierwszym kontakcie (TOFU), tak jak AnyDesk pokazuje 'key changed'.

Jezeli brak biblioteki `cryptography`, sesja pada do trybu PLAIN (ostrzezenie w UI) -
przydatne tylko w zaufanej sieci lokalnej.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from typing import Optional, Tuple

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    HAS_CRYPTO = True
except Exception:  # pragma: no cover - brak zaleznosci
    HAS_CRYPTO = False

NONCE_LEN = 12
PBKDF2_ROUNDS = 240_000
_SEQ_MASK = 0xFFFFFFFF


class CryptoUnavailable(RuntimeError):
    pass


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text.encode("ascii") + pad.encode("ascii"))


def fingerprint(raw_public: bytes) -> str:
    """SHA-256 klucza publicznego, format AA:BB:CC... (jak w AnyDesk)."""
    digest = hashlib.sha256(b"desklink-key-v1" + raw_public).digest()
    return ":".join(f"{b:02X}" for b in digest[:16])


@dataclass
class EphemeralKey:
    """Jednorazowa para X25519 na sesje."""

    private: object = None
    public_bytes: bytes = b""

    @property
    def public_b64(self) -> str:
        return b64e(self.public_bytes)

    @classmethod
    def generate(cls) -> "EphemeralKey":
        if not HAS_CRYPTO:
            return cls(None, b"")
        priv = X25519PrivateKey.generate()
        return cls(priv, raw_public(priv.public_key()))

    def shared(self, peer_public_b64: str) -> bytes:
        if not HAS_CRYPTO or self.private is None:
            raise CryptoUnavailable("brak biblioteki cryptography")
        peer = X25519PublicKey.from_public_bytes(b64d(peer_public_b64))
        return self.private.exchange(peer)


def hkdf(secret: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    if not HAS_CRYPTO:
        # Deterministyczna alternatywa (SHA-256 based) gdy brak AES - tylko dla trybu plain.
        return hashlib.sha256(secret + info + salt).digest()[:length]
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


@dataclass
class SessionKeys:
    auth_c2s: bytes = b""
    auth_s2c: bytes = b""
    data_c2s: bytes = b""
    data_s2c: bytes = b""
    peer_fingerprint: str = ""
    plain: bool = False

    def for_role(self, is_controller: bool) -> Tuple[bytes, bytes]:
        """(klucz wysylania, klucz odbioru) dla danej strony."""
        if is_controller:
            return self.data_c2s, self.data_s2c
        return self.data_s2c, self.data_c2s

    def auth_for_role(self, is_controller: bool) -> Tuple[bytes, bytes]:
        if is_controller:
            return self.auth_c2s, self.auth_s2c
        return self.auth_s2c, self.auth_c2s


def derive_session_keys(shared: bytes, transcript: bytes) -> SessionKeys:
    """Wiaze transcript handshake - atakujacy w srodku nie podmieni kluczy."""
    salt = hashlib.sha256(b"desklink-salt-v1" + transcript).digest()[:16]
    mk = hkdf(shared, b"desklink-master-v1", 32, salt=salt)
    return SessionKeys(
        auth_c2s=hkdf(mk, b"auth-c2s", 32, salt=salt),
        auth_s2c=hkdf(mk, b"auth-s2c", 32, salt=salt),
        data_c2s=hkdf(mk, b"data-c2s", 32, salt=salt),
        data_s2c=hkdf(mk, b"data-s2c", 32, salt=salt),
    )


class SessionCrypto:
    """AEAD dla jednego kierunku pary (tx, rx)."""

    def __init__(self, tx_key: bytes, rx_key: bytes):
        if not HAS_CRYPTO:
            raise CryptoUnavailable("brak cryptography")
        self._tx = AESGCM(tx_key)
        self._rx = AESGCM(rx_key)
        self._tx_full = 0
        self._rx_full = 0

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return (counter & _SEQ_MASK).to_bytes(NONCE_LEN, "big")

    def sync(self, tx_seq: int, rx_seq: int) -> None:
        """Ustaw pelne liczniki nonce na wartosc, od ktorej startuje kierunek."""
        self._tx_full = int(tx_seq) & _SEQ_MASK
        self._rx_full = int(rx_seq) & _SEQ_MASK

    def encrypt(self, plain: bytes, seq: int, flags: int = 0) -> bytes:
        aad = bytes([flags & 0xFF]) + (seq & _SEQ_MASK).to_bytes(4, "big")
        ct = self._tx.encrypt(self._nonce(self._tx_full), plain, aad)
        self._tx_full = (self._tx_full + 1) & _SEQ_MASK
        return ct

    def decrypt(self, blob: bytes, seq: int, flags: int = 0) -> bytes:
        expected = self._rx_full & _SEQ_MASK
        if seq != expected:
            raise ValueError(f"ramka poza kolejnoscia (rx {expected} != {seq})")
        aad = bytes([flags & 0xFF]) + (seq & _SEQ_MASK).to_bytes(4, "big")
        plain = self._rx.decrypt(self._nonce(self._rx_full), blob, aad)
        self._rx_full = (self._rx_full + 1) & _SEQ_MASK
        return plain


# ------------------------------------------------------------------ haslo / weryfikatory
def new_salt() -> str:
    return b64e(secrets.token_bytes(16))


def hash_password(password: str, salt: str, rounds: int = PBKDF2_ROUNDS) -> bytes:
    """PBKDF2-HMAC-SHA256 jako bajty - tego uzywa dowod znalosci hasla."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), b64d(salt), int(rounds), dklen=32
    )


def hash_password_hex(password: str, salt: str) -> str:
    return hash_password(password, salt).hex()


def check_password(password: str, salt: str, verifier: str) -> bool:
    if not verifier:
        return False
    return hmac.compare_digest(hash_password_hex(password, salt), verifier)


def auth_proof(auth_key: bytes, challenge: bytes, client_fp: str, pw_hash: bytes) -> bytes:
    """HMAC nad challenge + hashem hasla.

    Haslo (i jego hash w jasnej formie) nigdy nie leci po drucie; host liczy
    dowod ze swojego zapisanego weryfikatora, wiec nie potrzebuje hasla jawnego.
    """
    msg = b"desklink-auth-v1" + challenge + client_fp.encode("utf-8", "replace") + pw_hash
    return hmac.new(auth_key, msg, hashlib.sha256).digest()


def id_secret() -> str:
    """Tajny token wlasciciela ID - pozwala odkazac swoje ID po restarcie."""
    return b64e(secrets.token_bytes(24))


def raw_public(key) -> bytes:
    """32-bajtowy klucz publiczny X25519 (Encoding.Raw - dziala od cryptography 37+)."""
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def raw_private(key) -> bytes:
    """Surowy sekret X25519 bezszyfrowany; trzymamy go jako hex w config.json (0600)."""
    return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def private_from_hex(hex_str: str):
    if not HAS_CRYPTO or not hex_str:
        return None
    return X25519PrivateKey.from_private_bytes(bytes.fromhex(hex_str))


@dataclass
class Identity:
    """Trwala tozsamosc tej maszyny (klucz long-lived + nadane ID)."""

    private_hex: str = ""
    public_b64: str = ""
    fp: str = ""
    desk_id: int = 0
    secret: str = ""
    alias: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def create(cls) -> "Identity":
        if not HAS_CRYPTO:
            seed = os.urandom(32)
            return cls(public_b64=b64e(seed), fp=fingerprint(seed), secret=id_secret())
        priv = X25519PrivateKey.generate()
        pub = raw_public(priv.public_key())
        return cls(
            private_hex=raw_private(priv).hex(),
            public_b64=b64e(pub),
            fp=fingerprint(pub),
            secret=id_secret(),
        )

    @classmethod
    def load(cls, data: dict) -> "Identity":
        return cls(
            private_hex=data.get("private_hex", ""),
            public_b64=data.get("public_b64", ""),
            fp=data.get("fp", ""),
            desk_id=int(data.get("desk_id") or 0),
            secret=data.get("secret", ""),
            alias=data.get("alias", ""),
            extra=data.get("extra", {}),
        )

    def as_dict(self) -> dict:
        return {
            "private_hex": self.private_hex,
            "public_b64": self.public_b64,
            "fp": self.fp,
            "desk_id": self.desk_id,
            "secret": self.secret,
            "alias": self.alias,
            "extra": self.extra,
        }

    def long_term_shared(self, peer_public_b64: str) -> bytes:
        if not HAS_CRYPTO or not self.private_hex:
            raise CryptoUnavailable("brak klucza long-term")
        priv = private_from_hex(self.private_hex)
        return priv.exchange(X25519PublicKey.from_public_bytes(b64d(peer_public_b64)))
