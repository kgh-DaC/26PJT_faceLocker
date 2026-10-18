"""Face quality gate (owner: vision-engineer). Contract: INTERFACES section 3.3.

Failing quality -> HOLD (no score computed), so dark/blurry noise can neither match an impostor
nor cause repeated UNKNOWN denials for a genuine user.
"""

from __future__ import annotations

import cv2
import numpy as np

from facedoor.config import QualitySection
from facedoor.datatypes import FaceDetection, HoldReason, QualityResult


def yaw_ratio(landmarks: np.ndarray) -> float:
    """(nose_x - eye_mid_x) / inter-eye distance, sign kept. landmarks (5,2): r_eye, l_eye, nose, r_mouth, l_mouth."""
    lm = np.asarray(landmarks, dtype=np.float64).reshape(5, 2)
    re, le, nose = lm[0], lm[1], lm[2]
    dist = float(np.hypot(*(le - re)))
    if dist < 1e-6:
        return float("inf")
    return float((nose[0] - (re[0] + le[0]) / 2.0) / dist)


def face_stats(image: np.ndarray, det: FaceDetection) -> tuple[float, float]:
    """(blur_var, brightness) over the face bbox region (grayscale)."""
    x, y, w, h = det.bbox
    crop = image[y:y + h, x:x + w]
    if crop.size == 0:
        return 0.0, 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return blur, float(gray.mean())


def assess_quality(image: np.ndarray, det: FaceDetection, cfg: QualitySection,
                   max_abs_yaw_ratio: float | None = None) -> QualityResult:
    """Order (first failure is the reason): border -> too_small -> low_det_score -> pose -> dark/bright -> blur."""
    H, W = image.shape[:2]
    x, y, w, h = det.bbox
    face_px = int(min(w, h))
    blur, bright = face_stats(image, det)
    yr = yaw_ratio(det.landmarks)
    max_yaw = cfg.max_abs_yaw_ratio if max_abs_yaw_ratio is None else max_abs_yaw_ratio
    m = cfg.border_margin_px

    # Unclipped raw box (if available) detects faces cut by the frame edge.
    rx, ry, rw, rh = (float(v) for v in det.raw[:4]) if det.raw is not None and len(det.raw) >= 4 else (x, y, w, h)
    reason: HoldReason | None = None
    if min(x, rx) < m or min(y, ry) < m or max(x + w, rx + rw) > W - m or max(y + h, ry + rh) > H - m:
        reason = HoldReason.BORDER
    elif face_px < cfg.min_face_px:
        reason = HoldReason.TOO_SMALL
    elif det.score < cfg.min_det_score:
        reason = HoldReason.LOW_DET_SCORE
    elif not np.isfinite(yr) or abs(yr) > max_yaw:
        reason = HoldReason.POSE
    elif bright < cfg.min_brightness:
        reason = HoldReason.TOO_DARK
    elif bright > cfg.max_brightness:
        reason = HoldReason.TOO_BRIGHT
    elif blur < cfg.min_blur_var:
        reason = HoldReason.BLUR
    return QualityResult(ok=reason is None, reason=reason, face_px=face_px, blur_var=blur,
                         brightness=bright, yaw_ratio=yr if np.isfinite(yr) else 99.0)
