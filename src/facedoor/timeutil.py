"""Timestamp helpers shared by logs/metadata (local ISO8601 with milliseconds and TZ offset)."""

from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    """e.g. 2026-10-16T09:30:12.345+09:00"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")
