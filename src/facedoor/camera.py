"""Camera sources (owner: vision-engineer). Contract: INTERFACES section 3.1.

OpenCVCamera: live device (Windows CAP_DSHOW, Linux CAP_V4L2). FileCamera: image folder or video file.
Read failures are retried read_retries times then raise CameraError so the app closes the door (fail-closed).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

from facedoor.config import CameraSection
from facedoor.datatypes import Frame
from facedoor.errors import CameraError
from facedoor.imageio import ascii_safe_path, imread

log = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}


@runtime_checkable
class Camera(Protocol):
    def open(self) -> None: ...
    def read(self) -> Frame: ...
    def close(self) -> None: ...
    def __enter__(self) -> "Camera": ...
    def __exit__(self, *exc: object) -> None: ...
    @property
    def is_live(self) -> bool: ...


def _backend_flag(name: str) -> int:
    name = (name or "auto").lower()
    if name == "auto":
        if sys.platform.startswith("win"):
            return cv2.CAP_DSHOW
        if sys.platform.startswith("linux"):
            return cv2.CAP_V4L2
        return cv2.CAP_ANY
    return {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF, "v4l2": cv2.CAP_V4L2, "any": cv2.CAP_ANY}.get(
        name, cv2.CAP_ANY)


class OpenCVCamera:
    def __init__(self, cfg: CameraSection, source: str | int | None = None) -> None:
        self.cfg = cfg
        src = cfg.source if source is None else source
        if isinstance(src, str) and src.strip().lstrip("-").isdigit():
            src = int(src)
        self.source: str | int = src
        self._cap: cv2.VideoCapture | None = None
        self._index = 0

    @property
    def is_live(self) -> bool:
        return True

    def open(self) -> None:
        flag = _backend_flag(self.cfg.backend)
        cap = cv2.VideoCapture(self.source, flag)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"cannot open camera {self.source!r} (backend={self.cfg.backend}). "
                              "Check the device index / v4l2-ctl --list-devices / that no other app uses it.")
        if self.cfg.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc[:4].ljust(4)))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        if self.cfg.buffer_size > 0:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, self.cfg.buffer_size)
        log.info("camera %r opened: %dx%d @ %.1f fps (requested %dx%d @ %s, fourcc=%s)", self.source,
                 int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                 cap.get(cv2.CAP_PROP_FPS), self.cfg.width, self.cfg.height, self.cfg.fps, self.cfg.fourcc)
        self._cap = cap

    def read(self) -> Frame:
        if self._cap is None:
            raise CameraError("camera not opened")
        for attempt in range(self.cfg.read_retries):
            ok, img = self._cap.read()
            if ok and img is not None and img.size > 0:
                if img.ndim == 2:
                    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                f = Frame(image=img, index=self._index, ts=time.monotonic(), source=f"cam:{self.source}")
                self._index += 1
                return f
            log.warning("camera read failed (%d/%d)", attempt + 1, self.cfg.read_retries)
            time.sleep(self.cfg.retry_delay_sec)
        raise CameraError(f"camera {self.source!r}: {self.cfg.read_retries} consecutive read failures")

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "OpenCVCamera":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FileCamera:
    """Image folder (name order, jpg/png) or a video file. End of stream -> StopIteration."""

    def __init__(self, path: str | Path, loop: bool = False, fps: float = 0.0) -> None:
        self.path = Path(path)
        self.loop = loop
        self.fps = float(fps)
        self._files: list[Path] = []
        self._pos = 0
        self._cap: cv2.VideoCapture | None = None
        self._index = 0
        self._last_t: float | None = None
        self._opened = False

    @property
    def is_live(self) -> bool:
        return False

    def open(self) -> None:
        if self.path.is_dir():
            self._files = sorted(p for p in self.path.iterdir() if p.suffix.lower() in IMAGE_EXTS)
            if not self._files:
                raise CameraError(f"no images (jpg/png/bmp) in folder {self.path}")
        elif self.path.is_file():
            if self.path.suffix.lower() in IMAGE_EXTS:
                self._files = [self.path]
            else:
                cap = cv2.VideoCapture(str(ascii_safe_path(self.path, error_cls=CameraError)))
                if not cap.isOpened():
                    cap.release()
                    raise CameraError(f"cannot open video file {self.path}")
                self._cap = cap
        else:
            raise CameraError(f"camera source not found: {self.path}")
        self._pos = 0
        self._opened = True

    def _throttle(self) -> None:
        if self.fps > 0:
            now = time.monotonic()
            if self._last_t is not None:
                wait = 1.0 / self.fps - (now - self._last_t)
                if wait > 0:
                    time.sleep(wait)
            self._last_t = time.monotonic()

    def _next_image(self) -> tuple[np.ndarray, str]:
        while True:
            if self._cap is not None:
                ok, img = self._cap.read()
                if ok and img is not None:
                    return img, f"file:{self.path}#{self._pos}"
                if self.loop:
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, img = self._cap.read()
                    if ok and img is not None:
                        return img, f"file:{self.path}#0"
                raise StopIteration
            if self._pos >= len(self._files):
                if self.loop and self._files:
                    self._pos = 0
                else:
                    raise StopIteration
            p = self._files[self._pos]
            self._pos += 1
            img = imread(p)
            if img is None:
                log.warning("skipping unreadable image %s", p)
                continue
            return img, f"file:{p}"

    def read(self) -> Frame:
        if not self._opened:
            raise CameraError("camera not opened")
        img, src = self._next_image()
        if self._cap is not None:
            self._pos += 1
        self._throttle()
        f = Frame(image=img, index=self._index, ts=time.monotonic(), source=src)
        self._index += 1
        return f

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._opened = False

    def __enter__(self) -> "FileCamera":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def create_camera(cfg: CameraSection, source: str | None = None) -> Camera:
    src = cfg.source if source is None else str(source)
    s = str(src).strip()
    if s.lstrip("-").isdigit() or s.startswith("/dev/video") or "://" in s:
        return OpenCVCamera(cfg, s)
    return FileCamera(Path(s), loop=cfg.file_loop, fps=cfg.file_fps)
