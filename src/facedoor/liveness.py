"""Liveness (anti-spoofing) hook — default no-op (owner: vision-engineer). Contract: INTERFACES section 3.6.

WARNING: with the default NoopLiveness this system can be opened with a photo / phone screen / video replay.
Set liveness.required=true to verify fail-closed behaviour (nothing opens until a real checker is plugged in).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from facedoor.config import LivenessSection
from facedoor.datatypes import FaceDetection, Frame


@dataclass(slots=True)
class LivenessResult:
    is_live: bool | None
    score: float | None
    method: str


@runtime_checkable
class LivenessChecker(Protocol):
    def check(self, frame: Frame, det: FaceDetection, track_id: int | None = None) -> LivenessResult: ...
    def reset(self) -> None: ...


class NoopLiveness:
    def check(self, frame: Frame, det: FaceDetection, track_id: int | None = None) -> LivenessResult:
        return LivenessResult(None, None, "none")

    def reset(self) -> None:
        return None


def create_liveness(cfg: LivenessSection) -> LivenessChecker:
    if cfg.backend == "none":
        return NoopLiveness()
    raise ValueError(f"unsupported liveness backend {cfg.backend!r}")


def liveness_label(res: LivenessResult | None) -> str:
    """events.csv 'liveness' column: none|live|spoof|unknown."""
    if res is None or res.method == "none":
        return "none"
    if res.is_live is True:
        return "live"
    if res.is_live is False:
        return "spoof"
    return "unknown"
