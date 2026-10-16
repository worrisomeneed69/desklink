"""DeskLink - przechwytywanie ekranu.

Backend: `mss` (GDI na Windows/Wine, XCB na Linux) - najszybszy i nie wymaga
zadnych okraglych objetosci. Fallback: PIL.ImageGrab, a na ostatek skrot przez
xdotool/import. Klatki sa kodowane do JPEG (PNG do trybu 'tekst/czytelnosc').

Wykrywanie statycznego ekranu: porownujemy male glowki (thumbnail 48x27) -
jesli sie nie zmienily, nie wysylamy nic. To jest to, co w AnyDesk daje
'ekran stoi, a polaczenie nie obciaza laca'.
"""

from __future__ import annotations

import io
import struct
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image

from .osutil import IS_LINUX, IS_WINDOWS

FMT_JPEG = 0
FMT_PNG = 1
FMT_NAMES = {"jpeg": FMT_JPEG, "png": FMT_PNG}
THUMB_W, THUMB_H = 48, 27


class CaptureError(RuntimeError):
    pass


@dataclass
class Monitor:
    index: int
    left: int
    top: int
    width: int
    height: int
    name: str = ""
    primary: bool = False


@dataclass
class Captured:
    data: bytes
    x: int
    y: int
    width: int
    height: int
    fmt: int
    quality: int
    changed: bool = True
    native_w: int = 0
    native_h: int = 0
    encode_ms: float = 0.0
    capture_ms: float = 0.0


class ScreenCapture:
    def __init__(self, monitor_index: int = 1):
        self.monitor_index = monitor_index
        self._local = threading.local()
        self.backend = self._pick_backend()
        self._prev_thumb: Optional[bytes] = None
        self._static_skips = 0
        self.last_error: str = ""
        self.stats = {"frames": 0, "skipped": 0, "bytes": 0, "fps": 0.0}
        self._t_start = time.monotonic()

    # ------------------------------------------------------------------ backend
    @staticmethod
    def _pick_backend() -> str:
        try:
            import mss  # noqa: F401

            return "mss"
        except Exception:  # noqa: BLE001
            pass
        try:
            from PIL import ImageGrab  # noqa: F401

            return "pil"
        except Exception:  # noqa: BLE001
            return "none"

    def _sct(self):
        """mss nie jest thread-safe - jedna instancja na watek."""
        if self.backend != "mss":
            return None
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss

            sct = mss.mss()
            self._local.sct = sct
        return sct

    # ------------------------------------------------------------------ monitory
    def monitors(self) -> List[Monitor]:
        sct = self._sct()
        out: List[Monitor] = []
        if sct is not None:
            try:
                raw = sct.monitors
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                raw = []
            for i, mon in enumerate(raw):
                if i == 0 and len(raw) > 2:
                    continue  # pod mss index 0 = cala wirtualna przestrzen
                out.append(
                    Monitor(
                        index=i,
                        left=int(mon["left"]),
                        top=int(mon["top"]),
                        width=int(mon["width"]),
                        height=int(mon["height"]),
                        name=("Glowowny" if i == 1 else f"Monitor {i}"),
                        primary=(i == 1),
                    )
                )
        if not out:
            w, h = self._pil_size()
            out = [Monitor(1, 0, 0, w, h, "Glowowny", True)]
        return out

    def _pil_size(self) -> Tuple[int, int]:
        try:
            if IS_WINDOWS:
                import ctypes

                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            import pyautogui

            size = pyautogui.size()
            return int(size[0]), int(size[1])
        except Exception:  # noqa: BLE001
            return 1920, 1080

    def current_monitor(self) -> Monitor:
        mons = self.monitors()
        for m in mons:
            if m.index == self.monitor_index:
                return m
        return mons[0]

    def native_size(self) -> Tuple[int, int]:
        mon = self.current_monitor()
        return mon.width, mon.height

    # ------------------------------------------------------------------ grab
    def _grab_image(self) -> Tuple[Image.Image, Monitor]:
        mon = self.current_monitor()
        if self.backend == "mss":
            sct = self._sct()
            if sct is not None:
                shot = sct.grab({"left": mon.left, "top": mon.top, "width": mon.width, "height": mon.height})
                try:
                    img = Image.frombuffer("RGBA", shot.size, shot.bgra, "raw", "BGRX")
                except Exception:  # noqa: BLE001
                    img = Image.frombytes("RGB", shot.size, shot.rgb, "raw", "RGB")
                return img, mon
        if self.backend in ("mss", "pil"):
            try:
                from PIL import ImageGrab

                if IS_WINDOWS or IS_LINUX:
                    box = (mon.left, mon.top, mon.left + mon.width, mon.top + mon.height)
                    return ImageGrab.grab(bbox=box, all_screens=False), mon
                return ImageGrab.grab(), mon
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"ImageGrab: {exc}"
        raise CaptureError(f"brak dzialajacego backendu przechwytywania ({self.backend}): {self.last_error}")

    @staticmethod
    def _thumb(img: Image.Image) -> bytes:
        return img.convert("L").resize((THUMB_W, THUMB_H), Image.BILINEAR).tobytes()

    @staticmethod
    def _thumb_diff(a: bytes, b: bytes) -> float:
        total = 0
        for x, y in zip(a, b):
            total += x - y if x >= y else y - x
        return total / (len(a) * 255.0)

    def grab(
        self,
        quality: int = 72,
        scale: float = 1.0,
        fmt: str = "jpeg",
        force: bool = False,
        threshold: float = 0.004,
    ) -> Optional[Captured]:
        """Zwraca klatke albo None, gdy ekran sie nie zmienil (i nie wymuszono)."""
        t0 = time.monotonic()
        img, mon = self._grab_image()
        t1 = time.monotonic()
        thumb = self._thumb(img)
        changed = True
        if not force and self._prev_thumb is not None:
            changed = self._thumb_diff(thumb, self._prev_thumb) > threshold
        self._prev_thumb = thumb
        if not changed:
            self._static_skips += 1
            self.stats["skipped"] += 1
            return None
        self._static_skips = 0

        target_w = max(16, int(round(img.width * scale))) if scale != 1.0 else img.width
        target_h = max(16, int(round(img.height * scale))) if scale != 1.0 else img.height
        if (target_w, target_h) != (img.width, img.height):
            img = img.resize((target_w, target_h), Image.BILINEAR)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        code = FMT_NAMES.get(fmt, FMT_JPEG)
        buf = io.BytesIO()
        if code == FMT_PNG:
            img.save(buf, "PNG", optimize=False, compress_level=4)
        else:
            img.save(buf, "JPEG", quality=int(max(15, min(95, quality))))
        blob = buf.getvalue()
        t2 = time.monotonic()
        self.stats["frames"] += 1
        self.stats["bytes"] += len(blob)
        elapsed = t2 - self._t_start
        if elapsed > 0.5:
            self.stats["fps"] = round(self.stats["frames"] / elapsed, 1)
        return Captured(
            data=blob,
            x=mon.left,
            y=mon.top,
            width=target_w,
            height=target_h,
            fmt=code,
            quality=int(quality),
            changed=True,
            native_w=mon.width,
            native_h=mon.height,
            encode_ms=(t2 - t1) * 1000,
            capture_ms=(t1 - t0) * 1000,
        )

    def reset_diff(self) -> None:
        self._prev_thumb = None


class SyntheticSource:
    """Generator klatek do testow bez displaya / do demo."""

    def __init__(self, width: int = 1280, height: int = 720, fps: int = 10):
        self.width, self.height, self.fps = width, height, fps
        self._n = 0
        self.stats = {"frames": 0, "skipped": 0, "bytes": 0, "fps": 0.0}

    def native_size(self) -> Tuple[int, int]:
        return self.width, self.height

    def monitors(self) -> List[Monitor]:
        return [Monitor(1, 0, 0, self.width, self.height, "Syntetyczny", True)]

    def reset_diff(self) -> None:
        pass

    def grab(self, quality: int = 72, scale: float = 1.0, fmt: str = "jpeg", force: bool = False, **_kw):
        from PIL import ImageDraw

        self._n += 1
        img = Image.new("RGB", (self.width, self.height))
        d = ImageDraw.Draw(img)
        for band in range(12):
            hue = (band * 21 + self._n * 7) % 256
            d.rectangle([0, band * (self.height // 12), self.width, (band + 1) * (self.height // 12)],
                         fill=(hue, (hue * 2) % 256, 255 - hue))
        d.ellipse(
            [
                (self._n * 23) % (self.width - 120),
                (self._n * 11) % (self.height - 120),
                ((self._n * 23) % (self.width - 120)) + 120,
                ((self._n * 11) % (self.height - 120)) + 120,
            ],
            fill=(255, 255, 255),
        )
        d.text((24, 24), f"DeskLink SYNTH frame {self._n}", fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=int(quality))
        self.stats["frames"] += 1
        return Captured(
            data=buf.getvalue(),
            x=0, y=0,
            width=self.width, height=self.height,
            fmt=FMT_JPEG, quality=int(quality), changed=True,
            native_w=self.width, native_h=self.height,
        )


def frame_payload(cap: Captured) -> bytes:
    """Naglow regionu (x, y, w, h, fmt, quality) + dane obrazu (ramka FT.FRAME)."""
    head = struct.pack(">HHHHBB", cap.x & 0xFFFF, cap.y & 0xFFFF, cap.width & 0xFFFF,
                       cap.height & 0xFFFF, cap.fmt, cap.quality & 0xFF)
    return head + cap.data
