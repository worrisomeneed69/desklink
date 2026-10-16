"""DeskLink - konfiguracja i tozsamosc zapisywane na dysku.

Windows (takze pod Wine!): %APPDATA%\\DeskLink\\config.json
Linux/macOS:               ~/.config/desklink/config.json
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .crypto import Identity, check_password, hash_password_hex, new_salt

APP_NAME = "DeskLink"
DEFAULT_RELAY_HOST = os.environ.get("DESKLINK_RELAY", "")
DEFAULT_RELAY_PORT = int(os.environ.get("DESKLINK_RELAY_PORT", "7001"))
DEFAULT_LISTEN_PORT = int(os.environ.get("DESKLINK_LISTEN_PORT", "7000"))
ID_DIGITS = 9


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "desklink"


def cache_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "desklink"


def machine_name() -> str:
    name = socket.gethostname().split(".")[0]
    return name or "bez-nazwy"


def format_id(desk_id: int) -> str:
    """123456789 -> '123 456 789' (czytelnie jak w AnyDesk)."""
    s = str(int(desk_id))
    if len(s) <= 3:
        return s
    groups = [s[i : i + 3] for i in range(0, len(s), 3)]
    return " ".join(groups)


def parse_id(text: str) -> Optional[int]:
    """Znosi spacje, myślniki i 'ID: 123 456'."""
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    if len(digits) < 3:
        return None
    return int(digits)


def looks_like_address(text: str) -> bool:
    """Czy zamiast ID uzytkownik wpisal host:port / adres bezposredni."""
    t = (text or "").strip()
    if not t:
        return False
    if ":" in t:
        return True
    host = t.split(":")[0]
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-/]+", host)) and not t.isdigit() and ("." in host or host in {"localhost"})


@dataclass
class Recent:
    desk_id: int = 0
    name: str = ""
    fp: str = ""
    ts: float = 0.0
    addr: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Config:
    # --- tozsamosc / siec
    identity: Identity = field(default_factory=Identity.create)
    relay_host: str = DEFAULT_RELAY_HOST
    relay_port: int = DEFAULT_RELAY_PORT
    relay_tls: bool = False
    relay_tls_insecure: bool = False   # self-signed certyfikat wlasnego relaya
    relay_key: str = ""                # SECRET, jesli relay startowal z --require-auth
    listen_enabled: bool = True
    listen_port: int = DEFAULT_LISTEN_PORT
    lan_discovery: bool = True
    # --- jakosc obrazu (strona udostepniajaca ekran)
    fps: int = 20
    quality: int = 72
    scale: float = 1.0
    monitor: int = 1
    encoding: str = "jpeg"
    adaptive: bool = True
    # --- uprawnienia (jak host odpowiada na przychodzace polaczenia)
    password_salt: str = ""
    password_hash: str = ""
    confirm_incoming: bool = True
    allow_input: bool = True
    allow_files: bool = False
    clipboard_sync: bool = True
    view_only_default: bool = False
    auto_lock_local: bool = False
    # --- UI / ufnosc
    theme: str = "dark"
    trusted: dict = field(default_factory=dict)   # desk_id -> fingerprint
    recent: List[dict] = field(default_factory=list)
    window: List[int] = field(default_factory=lambda: [1080, 700, 100, 80])
    show_cursor_remote: bool = True
    log_level: str = "info"

    # ------------------------------------------------------------------ haslo
    @property
    def has_password(self) -> bool:
        return bool(self.password_hash and self.password_salt)

    def set_password(self, password: str) -> None:
        if not password:
            self.password_salt = ""
            self.password_hash = ""
            return
        self.password_salt = new_salt()
        self.password_hash = hash_password_hex(password, self.password_salt)

    def verify_password(self, password: str) -> bool:
        if not self.has_password:
            return False
        return check_password(password, self.password_salt, self.password_hash)

    @property
    def unattended(self) -> bool:
        return self.has_password

    # ------------------------------------------------------------------ trwa
    def remember_peer(self, desk_id: int, name: str, fp: str, addr: str = "") -> None:
        entry = Recent(desk_id=desk_id, name=name, fp=fp, ts=time.time(), addr=addr).as_dict()
        self.recent = [r for r in self.recent if int(r.get("desk_id") or 0) != desk_id]
        self.recent.insert(0, entry)
        del self.recent[12:]

    def trust(self, desk_id: int, fp: str) -> None:
        self.trusted[str(int(desk_id))] = fp

    def is_trusted(self, desk_id: int, fp: str) -> bool:
        saved = self.trusted.get(str(int(desk_id)))
        return saved is None or saved == fp

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or (config_dir() / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data["identity"] = self.identity.as_dict()
        data["recent"] = self.recent
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or (config_dir() / "config.json")
        cfg = cls()
        if not path.exists():
            cfg.identity = Identity.create()
            cfg.save(path)
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - uszkodzony plik nie moze zablokowac apki
            backup = path.with_suffix(".json.bad")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            cfg.identity = Identity.create()
            cfg.save(path)
            return cfg
        known = {f.name for f in cls.__dataclass_fields__.values()}
        for key, value in raw.items():
            if key not in known:
                continue
            if key == "identity":
                cfg.identity = Identity.load(value or {})
                continue
            if key == "recent":
                cfg.recent = list(value or [])
                continue
            setattr(cfg, key, value)
        if not cfg.identity.secret:
            from .crypto import id_secret

            cfg.identity.secret = id_secret()
        if not cfg.identity.public_b64:
            fresh = Identity.create()
            cfg.identity.private_hex = fresh.private_hex
            cfg.identity.public_b64 = fresh.public_b64
            cfg.identity.fp = fresh.fp
        return cfg


class ConfigStore:
    """Wrapper z lockiem + deferred save (GUI klika 'zmien' setki razy)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (config_dir() / "config.json")
        self._lock = threading.RLock()
        self._dirty = False
        self.config = Config.load(self.path)
        self._last_saved = 0.0

    def __getattr__(self, item):  # wygoda: store.fps itp.
        return getattr(self.config, item)

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if key not in Config.__dataclass_fields__:
                    raise AttributeError(f"nieznana opcja: {key}")
                setattr(self.config, key, value)
            self._dirty = True

    def flush(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            self.config.save(self.path)
            self._dirty = False
            self._last_saved = time.time()

    def tick(self) -> None:
        """Wywolywane z petli GUI - zapisuje nie rzadziej niz co 2 s."""
        if self._dirty and time.time() - self._last_saved > 2.0:
            self.flush()
