"""Preview frames for tkinter without PIL (INTERFACES section 14.4).

Worker thread: encode_preview() (pure OpenCV: resize, colored face box only - no text, encode PPM or PNG)
-> LatestQueue (size 1, old frames dropped). Main thread: photo_from_preview() -> tk.PhotoImage.
tkinter is imported lazily inside the functions that need it.
"""

from __future__ import annotations

import base64
import logging
import queue
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

log = logging.getLogger(__name__)

# BGR colors: saved / waiting / rejected
COLOR_SAVED = (0, 200, 0)
COLOR_WAIT = (0, 215, 255)
COLOR_REJECT = (0, 0, 230)


@dataclass(slots=True)
class PreviewImage:
    data: bytes          # PPM binary (P6) or PNG bytes
    fmt: str             # "ppm" | "png"
    width: int
    height: int


def draw_box(image: np.ndarray, bbox: tuple[int, int, int, int] | None, color: tuple[int, int, int],
             thickness: int = 3) -> np.ndarray:
    """Rectangle only (Korean text is never drawn on frames: cv2.putText cannot render Hangul)."""
    if bbox is None:
        return image
    x, y, w, h = (int(v) for v in bbox)
    cv2.rectangle(image, (x, y), (x + w, y + h), color, thickness)
    return image


def encode_preview(frame: np.ndarray, width: int, bbox: tuple[int, int, int, int] | None = None,
                   color: tuple[int, int, int] = COLOR_WAIT, fmt: str = "ppm") -> PreviewImage:
    """Resize to `width` (keeping aspect), draw the face box, encode. Safe to call from a worker thread."""
    img = frame.copy()
    draw_box(img, bbox, color, thickness=max(2, img.shape[1] // 200))
    h, w = img.shape[:2]
    if width > 0 and w != width:
        scale = width / float(w)
        img = cv2.resize(img, (int(width), max(1, int(round(h * scale)))),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    ext = ".png" if fmt == "png" else ".ppm"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise ValueError(f"cv2.imencode({ext}) failed")
    return PreviewImage(buf.tobytes(), "png" if fmt == "png" else "ppm", img.shape[1], img.shape[0])


class LatestQueue:
    """queue.Queue(maxsize=1) that replaces the old item instead of blocking (worker never waits for the UI)."""

    def __init__(self) -> None:
        self._q: queue.Queue[Any] = queue.Queue(maxsize=1)

    def put(self, item: Any) -> None:
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def get(self) -> Any | None:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None


_PPM_OK: bool | None = None   # learned at runtime: does this Tk accept binary PPM data?


def preferred_format() -> str:
    return "png" if _PPM_OK is False else "ppm"


def photo_from_preview(img: PreviewImage, master: Any = None) -> Any:
    """Main thread only. tk.PhotoImage from PPM bytes; falls back to PNG+base64 (Tk >= 8.6) if this Tk build
    rejects binary PPM data (the worker then switches to PNG via preferred_format())."""
    global _PPM_OK
    import tkinter as tk

    if img.fmt == "ppm":
        try:
            photo = tk.PhotoImage(master=master, data=img.data, format="PPM")
            _PPM_OK = True
            return photo
        except tk.TclError as e:
            log.warning("Tk rejected binary PPM preview (%s); switching to PNG", e)
            _PPM_OK = False
            return None
    return tk.PhotoImage(master=master, data=base64.b64encode(img.data).decode("ascii"), format="png")
