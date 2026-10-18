"""Shared data types (owner: vision-engineer). Contract: docs/INTERFACES.md section 1.

This module must stay dependency-light (numpy + stdlib only) because hardware/eval import it in parallel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    from facedoor.liveness import LivenessResult


class FrameVerdict(str, Enum):
    """Verdict for a single frame."""

    MATCH = "MATCH"
    UNKNOWN = "UNKNOWN"
    HOLD = "HOLD"
    NO_FACE = "NO_FACE"


class HoldReason(str, Enum):
    TOO_SMALL = "too_small"
    LOW_DET_SCORE = "low_det_score"
    BLUR = "blur"
    TOO_DARK = "too_dark"
    TOO_BRIGHT = "too_bright"
    POSE = "pose"
    BORDER = "border"
    MULTI_FACE = "multi_face"
    LIVENESS = "liveness"


class DecisionState(str, Enum):
    IDLE = "IDLE"
    VERIFYING = "VERIFYING"
    HOLD = "HOLD"
    OPEN = "OPEN"
    DENIED = "DENIED"
    REARM = "REARM"


@dataclass(slots=True)
class Frame:
    image: np.ndarray
    index: int
    ts: float
    source: str


@dataclass(slots=True)
class FaceDetection:
    bbox: tuple[int, int, int, int]
    landmarks: np.ndarray
    score: float
    raw: np.ndarray

    @property
    def area(self) -> int:
        return int(self.bbox[2]) * int(self.bbox[3])


@dataclass(slots=True)
class QualityResult:
    ok: bool
    reason: HoldReason | None
    face_px: int
    blur_var: float
    brightness: float
    yaw_ratio: float


@dataclass(slots=True)
class MatchCandidate:
    person_id: str
    score: float
    tag: str


@dataclass(slots=True)
class MatchResult:
    best: MatchCandidate | None
    second: MatchCandidate | None
    margin: float
    threshold: float
    threshold_group: str
    threshold_source: str
    accepted: bool
    scores: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RecognitionResult:
    frame_index: int
    ts: float
    verdict: FrameVerdict
    detection: FaceDetection | None
    all_detections: list[FaceDetection]
    quality: QualityResult | None
    match: MatchResult | None
    liveness: "LivenessResult | None"
    hold_reason: HoldReason | None
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def matched_id(self) -> str | None:
        """person_id if verdict is MATCH, else None."""
        if self.verdict is FrameVerdict.MATCH and self.match is not None and self.match.best is not None:
            return self.match.best.person_id
        return None


@dataclass(slots=True)
class DecisionOutput:
    state: DecisionState
    should_open: bool
    should_deny: bool
    person_id: str | None
    votes: int
    window: int
    best_score: float | None
    reason: str
