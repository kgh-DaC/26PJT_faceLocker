"""Cross-process mutual exclusion for the camera and gallery writes (owner: vision). Contract: INTERFACES 14.6.

- OS file locks: Windows ``msvcrt.locking`` on byte 0, POSIX ``fcntl.flock``. The OS releases them when the process
  dies, so a crashed program never leaves a stale lock (the file itself may remain; only the OS lock matters).
- Holder description ("run.py pid=1234") is written after byte 0 so other processes can read it on Windows too
  (msvcrt locks are mandatory for the locked range only).
- Non-blocking: ``acquire()`` returns False immediately when someone else holds the lock.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

# Relative to app.logs_dir (INTERFACES: "<logs_dir>/locks/camera.lock")
CAMERA_LOCK = "locks/camera.lock"
GALLERY_LOCK = "locks/gallery.lock"

_INFO_OFFSET = 1  # byte 0 is the locked byte


def camera_lock_path(logs_dir: str | Path) -> Path:
    return Path(logs_dir) / CAMERA_LOCK


def gallery_lock_path(logs_dir: str | Path) -> Path:
    return Path(logs_dir) / GALLERY_LOCK


def _program_name() -> str:
    try:
        name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    except Exception:  # noqa: BLE001
        name = ""
    return name or "python"


def _try_lock(fh: IO[bytes]) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        log.debug("unlock failed", exc_info=True)


class RunLock:
    """Non-blocking exclusive lock on ``path``. Use as a context manager or acquire()/release()."""

    def __init__(self, path: Path, description: str | None = None) -> None:
        self.path = Path(path)
        self.description = description
        self._fh: IO[bytes] | None = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        with self._mutex:
            if self._fh is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")  # create without truncating someone else's info
            if not _try_lock(fh):
                fh.close()
                return False
            info = f"{self.description or _program_name()} pid={os.getpid()}".encode("utf-8", "replace")
            try:
                fh.seek(0)
                fh.truncate(_INFO_OFFSET)  # keep the locked byte
                fh.seek(0)
                fh.write(b" ")
                fh.write(info)
                fh.flush()
            except OSError:
                log.debug("cannot write lock holder info to %s", self.path, exc_info=True)
            self._fh = fh
            return True

    def release(self) -> None:
        with self._mutex:
            fh, self._fh = self._fh, None
            if fh is None:
                return
            try:
                try:
                    fh.seek(0)
                    fh.truncate(_INFO_OFFSET)
                except OSError:
                    pass
                _unlock(fh)
            finally:
                fh.close()

    def __enter__(self) -> "RunLock":
        if not self.acquire():
            raise BlockingIOError(f"lock {self.path} is held by {lock_holder(self.path) or 'another process'}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass


def lock_holder(path: Path) -> str | None:
    """Description of the current holder ("run.py pid=1234"), or None if nobody holds the lock."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    try:
        # read the info first (outside the locked byte), then test whether the lock is free
        try:
            fh.seek(_INFO_OFFSET)
            info = fh.read(512).decode("utf-8", "replace").strip()
        except OSError:
            info = ""
    finally:
        fh.close()
    probe = RunLock(path)
    try:
        if probe._try_probe():
            return None
    except OSError:
        return info or "unknown"
    return info or "unknown"


def _try_probe(self: RunLock) -> bool:
    """True when the lock is free (lock + unlock immediately, without touching holder info)."""
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with open(self.path, "a+b") as fh:
        if _try_lock(fh):
            _unlock(fh)
            return True
        return False


RunLock._try_probe = _try_probe  # type: ignore[attr-defined]
