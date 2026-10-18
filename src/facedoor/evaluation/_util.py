"""Small shared helpers for the evaluation package (private)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def cfg_get(cfg: Any, dotted: str, default: Any = None) -> Any:
    """Read ``a.b.c`` from an AppConfig-like object (attributes) or a nested dict. Missing -> default."""
    cur = cfg
    for part in dotted.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        else:
            if not hasattr(cur, part):
                return default
            cur = getattr(cur, part)
    return cur


def cfg_raw(cfg: Any) -> dict:
    """Merged raw config dict (AppConfig.raw) or the dict itself."""
    if isinstance(cfg, dict):
        return cfg
    raw = getattr(cfg, "raw", None)
    return raw if isinstance(raw, dict) else {}


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp"})
