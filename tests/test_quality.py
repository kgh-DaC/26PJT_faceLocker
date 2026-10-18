"""Quality gate (INTERFACES section 3.3) on synthetic images."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import fake_detection
from facedoor.config import QualitySection
from facedoor.datatypes import HoldReason
from facedoor.quality import assess_quality, yaw_ratio

CFG = QualitySection()


def textured(h: int = 480, w: int = 640, mean: float = 128.0, amp: float = 60.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.clip(mean + rng.uniform(-amp, amp, size=(h, w)), 0, 255).astype(np.uint8)
    return np.dstack([img] * 3)


def test_good_face_passes():
    q = assess_quality(textured(), fake_detection(), CFG)
    assert q.ok and q.reason is None
    assert q.face_px == 120 and q.blur_var > CFG.min_blur_var
    assert CFG.min_brightness < q.brightness < CFG.max_brightness


@pytest.mark.parametrize("img,det,reason", [
    (textured(), fake_detection(x=1), HoldReason.BORDER),
    (textured(), fake_detection(w=60, h=70), HoldReason.TOO_SMALL),
    (textured(), fake_detection(score=0.87), HoldReason.LOW_DET_SCORE),
    (textured(), fake_detection(yaw=0.6), HoldReason.POSE),
    (textured(mean=20, amp=15), fake_detection(), HoldReason.TOO_DARK),
    (textured(mean=240, amp=10), fake_detection(), HoldReason.TOO_BRIGHT),
    (np.full((480, 640, 3), 128, np.uint8), fake_detection(), HoldReason.BLUR),
])
def test_failure_reasons(img, det, reason):
    q = assess_quality(img, det, CFG)
    assert not q.ok and q.reason is reason


def test_first_failure_wins_order():
    # small AND low score AND dark -> too_small is checked first
    q = assess_quality(textured(mean=20, amp=10), fake_detection(w=50, h=50, score=0.86), CFG)
    assert q.reason is HoldReason.TOO_SMALL


def test_border_uses_unclipped_raw_box():
    det = fake_detection(x=10)
    det.raw[0] = -30.0  # face extends beyond the left edge before clipping
    assert assess_quality(textured(), det, CFG).reason is HoldReason.BORDER


def test_yaw_override_for_enrollment():
    det = fake_detection(yaw=0.6)
    assert assess_quality(textured(), det, CFG).reason is HoldReason.POSE
    assert assess_quality(textured(), det, CFG, max_abs_yaw_ratio=0.8).ok


def test_yaw_ratio_sign():
    assert yaw_ratio(fake_detection(yaw=0.3).landmarks) == pytest.approx(0.3, abs=1e-5)
    assert yaw_ratio(fake_detection(yaw=-0.3).landmarks) == pytest.approx(-0.3, abs=1e-5)
