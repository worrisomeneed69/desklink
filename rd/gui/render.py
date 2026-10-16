"""DeskLink GUI - konwersja klatki PIL na obraz do okna Tk.

ImageTk jest najszybszy, ale kolaze Pillowa pod Linuxa bywaja zbudowane bez
_tkinter (a pod Wine bywa odwrotnie), wiec mamy sciezke awaryjna: kodowanie
PPM + tk.PhotoImage(data=...). Awaryjna sciezka jest ~2x wolniejsza, nadal
wystarczajaca do 25 fps.
"""

from __future__ import annotations

import base64
import tkinter as tk
from typing import Optional

from PIL import Image

try:  # pragma: no cover - zalezy od tego, jak skompilowano Pillow
    from PIL import ImageTk

    _IMAGETK = True
except Exception:  # noqa: BLE001
    ImageTk = None
    _IMAGETK = False


def _ppm(img: Image.Image) -> bytes:
    """PPM "P6" - Tk 8.6 czyta go sam, wiec nie potrzebujemy ImageTku."""
    head = f"P6\n{img.width} {img.height}\n255\n".encode("ascii")
    return head + img.convert("RGB").tobytes()


class FrameView:
    """Trzyma PhotoImage i rozmiary, zeby przeliczac wspolrzedne myszy."""

    def __init__(self, master: tk.Misc):
        self.master = master
        self.photo = None
        self.backend = "imagetk" if _IMAGETK else "ppm"
        self.source_size = (0, 0)
        self.drawn_size = (0, 0)

    def update(self, img: Image.Image, max_w: int, max_h: int,
               keep_aspect: bool = True) -> Optional[object]:
        """Dopasuj do ramy i zwroć image do canvas.create_image()."""
        if img is None or max_w < 8 or max_h < 8:
            return None
        sw, sh = img.size
        self.source_size = (sw, sh)
        if keep_aspect:
            ratio = min(max_w / sw, max_h / sh)
            tw, th = max(1, int(sw * ratio)), max(1, int(sh * ratio))
        else:
            tw, th = max_w, max_h
        if (tw, th) != (sw, sh):
            shown = img.resize((tw, th), Image.BILINEAR)
        else:
            shown = img
        self.drawn_size = (tw, th)
        if _IMAGETK and self.backend == "imagetk":
            try:
                self.photo = ImageTk.PhotoImage(shown, master=self.master)
                return self.photo
            except Exception:  # noqa: BLE001 - ImageTk padl: przechodzimy na PPM
                self.backend = "ppm"
        if self.backend == "ppm":
            self.photo = tk.PhotoImage(data=base64.b64encode(_ppm(shown)).decode("ascii"),
                                       master=self.master)
            return self.photo
        return None

    @staticmethod
    def available_backend() -> str:
        return "ImageTk" if _IMAGETK else "PPM (awaryjnie)"
