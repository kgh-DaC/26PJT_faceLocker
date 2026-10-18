"""Shared fixtures for vision tests (no camera / GPIO / network needed)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import numpy as np
import pytest

from facedoor.config import AppConfig, load_config
from facedoor.datatypes import (
    FaceDetection,
    FrameVerdict,
    HoldReason,
    MatchCandidate,
    MatchResult,
    QualityResult,
    RecognitionResult,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tests" / "assets"
MODELS = ROOT / "models"
HAVE_MODELS = (MODELS / "face_detection_yunet_2023mar.onnx").is_file() and \
    (MODELS / "face_recognition_sface_2021dec.onnx").is_file()

requires_models = pytest.mark.skipif(not HAVE_MODELS, reason="ONNX models missing (scripts/download_models.py)")


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_cfg(tmp_path: Path) -> Callable[..., AppConfig]:
    """AppConfig isolated in tmp_path (gallery/logs/calibration), models from the project, no local.yaml."""

    def _make(*overrides: str) -> AppConfig:
        base = [
            f"app.gallery_dir={(tmp_path / 'gallery').as_posix()}",
            f"app.logs_dir={(tmp_path / 'logs').as_posix()}",
            f"app.models_dir={MODELS.as_posix()}",
            f"app.calibration_file={(tmp_path / 'calibration.json').as_posix()}",
            f"logging.events_csv={(tmp_path / 'logs' / 'events.csv').as_posix()}",
            f"logging.app_log={(tmp_path / 'logs' / 'facedoor.log').as_posix()}",
            f"hardware.hardware_log={(tmp_path / 'logs' / 'hardware.log').as_posix()}",
            "runtime.max_fps=0",
            "display.enabled=false",
            "hardware.mode=mock",
            "hardware.deny_blink_sec=0.01",
            # default.yaml is per_locker + close_mode=limit (INTERFACES 13/15). Legacy vision/eval tests run on
            # the single-output, fixed-duration baseline; tests of the new modes pass their own overrides.
            "lockers.mode=single",
            "hardware.locker_outputs=[]",
            "hardware.solenoid_pin=17",
            "hardware.close_mode=timer",
        ]
        return load_config(None, base + list(overrides), use_local=False)

    return _make


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def fake_detection(x: int = 100, y: int = 100, w: int = 120, h: int = 140, score: float = 0.95,
                   yaw: float = 0.0) -> FaceDetection:
    """Synthetic YuNet-like detection; yaw shifts the nose x by yaw * eye distance."""
    re_ = (x + 0.3 * w, y + 0.4 * h)
    le_ = (x + 0.7 * w, y + 0.4 * h)
    eye_d = le_[0] - re_[0]
    nose = ((re_[0] + le_[0]) / 2 + yaw * eye_d, y + 0.6 * h)
    lm = np.array([re_, le_, nose, (x + 0.35 * w, y + 0.8 * h), (x + 0.65 * w, y + 0.8 * h)], dtype=np.float32)
    raw = np.concatenate([[x, y, w, h], lm.reshape(-1), [score]]).astype(np.float32)
    return FaceDetection(bbox=(x, y, w, h), landmarks=lm, score=score, raw=raw)


def make_result(verdict: FrameVerdict, person: str | None = None, score: float = 0.8, idx: int = 0,
                hold: HoldReason | None = None, ts: float = 0.0) -> RecognitionResult:
    det = fake_detection() if verdict is not FrameVerdict.NO_FACE else None
    q = QualityResult(hold is None, hold, 120, 200.0, 120.0, 0.0) if det is not None else None
    match = None
    if verdict in (FrameVerdict.MATCH, FrameVerdict.UNKNOWN):
        best = MatchCandidate(person or "someone", score, "normal")
        match = MatchResult(best, None, 1.0, 0.5, "global", "default", verdict is FrameVerdict.MATCH,
                            {best.person_id: score})
    return RecognitionResult(idx, ts, verdict, det, [det] if det else [], q, match, None, hold,
                             {"det": 1.0, "emb": 1.0, "match": 0.1, "total": 2.1})


def copy_assets(dst: Path, names: list[str], repeat: int = 1) -> Path:
    """Copy tests/assets images into a folder as 000_name, 001_name, ... (FileCamera reads in name order)."""
    dst.mkdir(parents=True, exist_ok=True)
    i = 0
    for _ in range(repeat):
        for n in names:
            src = ASSETS / n
            shutil.copyfile(src, dst / f"{i:03d}_{n}")
            i += 1
    return dst
