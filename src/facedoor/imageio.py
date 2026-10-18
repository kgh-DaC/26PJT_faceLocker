"""Unicode-path-safe image/model file I/O (owner: vision-engineer). Contract: INTERFACES section 1.3.

cv2.imread/imwrite and cv2.*.create(path) fail on non-ASCII paths on Windows (project folder contains Korean).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

from facedoor.errors import FaceDoorError, ModelError


def imread(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Read an image via np.fromfile + cv2.imdecode. Returns None if missing/undecodable."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        buf = np.fromfile(str(p), dtype=np.uint8)
    except OSError:
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, flags)


def imwrite(path: str | Path, image: np.ndarray, params: list[int] | None = None) -> bool:
    """Write an image via cv2.imencode + tofile. Creates parent folders."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix or ".jpg"
    ok, buf = cv2.imencode(ext, image, params or [])
    if not ok:
        return False
    buf.tofile(str(p))
    return True


def read_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cache_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(base) / "facedoor" / "cache"
    else:
        d = Path.home() / ".cache" / "facedoor"
    return d


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _needs_ascii_copy() -> bool:
    """Only Windows OpenCV builds fail on non-ASCII paths; Linux/macOS OpenCV takes UTF-8 paths directly."""
    return sys.platform.startswith("win")


def ascii_safe_path(path: str | Path, error_cls: type[FaceDoorError] = ModelError) -> Path:
    """Return a path OpenCV can open.

    - Non-Windows, or ASCII path: the resolved path unchanged (no copy; works under systemd ProtectHome).
    - Windows + non-ASCII: a copy in an ASCII cache dir named by sha256 (keeps suffix).
    Any OSError while copying (missing file, PermissionError, disk full) is raised as `error_cls`
    (default ModelError, a FaceDoorError) so app.main() maps it to an exit code instead of a traceback.
    """
    p = Path(path).resolve()
    if not _needs_ascii_copy() or _is_ascii(str(p)):
        return p
    try:
        if not p.is_file():
            raise FileNotFoundError(str(p))
        digest = sha256_file(p)
        cache = _cache_dir()
        if not _is_ascii(str(cache)):
            # Fallback for non-ASCII user profile names.
            cache = Path(os.environ.get("SystemDrive", "C:") + os.sep) / "facedoor_cache"
        cache.mkdir(parents=True, exist_ok=True)
        dst = cache / f"{digest}{p.suffix}"
        if not dst.is_file() or dst.stat().st_size != p.stat().st_size:
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copyfile(p, tmp)
            os.replace(tmp, dst)
        return dst
    except OSError as e:
        raise error_cls(f"cannot prepare ASCII-safe copy of {p}: {type(e).__name__}: {e}") from e
