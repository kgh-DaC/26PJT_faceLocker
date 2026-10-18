"""Face detection with OpenCV YuNet (owner: vision-engineer). Contract: INTERFACES section 3.2."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

from facedoor.config import DetectorSection
from facedoor.datatypes import FaceDetection
from facedoor.errors import ModelError
from facedoor.imageio import ascii_safe_path

log = logging.getLogger(__name__)

MODEL_HINT = "run: python scripts/download_models.py"


@runtime_checkable
class FaceDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[FaceDetection]: ...


def detection_from_raw(row: np.ndarray, frame_w: int, frame_h: int) -> FaceDetection:
    """Build a FaceDetection from a YuNet-format (15,) row in original-frame coordinates."""
    raw = np.asarray(row, dtype=np.float32).reshape(-1)[:15].copy()
    x, y, w, h = (float(v) for v in raw[:4])
    x0 = int(max(0, np.floor(x)))
    y0 = int(max(0, np.floor(y)))
    x1 = int(min(frame_w, np.ceil(x + w)))
    y1 = int(min(frame_h, np.ceil(y + h)))
    bbox = (x0, y0, max(0, x1 - x0), max(0, y1 - y0))
    lm = raw[4:14].reshape(5, 2).astype(np.float32)
    return FaceDetection(bbox=bbox, landmarks=lm, score=float(raw[14]), raw=raw)


class YuNetDetector:
    def __init__(self, model_path: Path, score_threshold: float = 0.85, nms_threshold: float = 0.3,
                 top_k: int = 50, input_scale: float = 1.0) -> None:
        model_path = Path(model_path)
        if not model_path.is_file():
            raise ModelError(f"YuNet model not found: {model_path} ({MODEL_HINT})")
        if not hasattr(cv2, "FaceDetectorYN"):
            raise ModelError(f"OpenCV {cv2.__version__} has no FaceDetectorYN; need opencv-python>=4.8")
        try:
            self._net = cv2.FaceDetectorYN.create(str(ascii_safe_path(model_path)), "", (320, 320),
                                                  float(score_threshold), float(nms_threshold), int(top_k))
        except cv2.error as e:
            raise ModelError(f"failed to load YuNet model {model_path}: {e} ({MODEL_HINT} --force)") from e
        self.model_path = model_path
        self.score_threshold = float(score_threshold)
        self.input_scale = float(input_scale)
        self._size: tuple[int, int] | None = None

    def detect(self, image: np.ndarray) -> list[FaceDetection]:
        h, w = image.shape[:2]
        s = self.input_scale
        img = image if s == 1.0 else cv2.resize(image, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                                                interpolation=cv2.INTER_AREA)
        ih, iw = img.shape[:2]
        if self._size != (iw, ih):
            self._net.setInputSize((iw, ih))
            self._size = (iw, ih)
        _, faces = self._net.detect(img)
        if faces is None or len(faces) == 0:
            return []
        out: list[FaceDetection] = []
        for row in faces:
            row = np.asarray(row, dtype=np.float32).copy()
            if s != 1.0:
                sx, sy = w / iw, h / ih
                row[0:14:2] *= sx
                row[1:14:2] *= sy
            d = detection_from_raw(row, w, h)
            if d.bbox[2] > 0 and d.bbox[3] > 0:
                out.append(d)
        out.sort(key=lambda d: d.score, reverse=True)
        return out


def create_detector(cfg: DetectorSection, models_dir: Path) -> FaceDetector:
    if cfg.backend != "yunet":
        raise ModelError(f"unsupported detector backend {cfg.backend!r} (only yunet)")
    p = Path(cfg.model_file)
    if not p.is_absolute():
        p = Path(models_dir) / p
    return YuNetDetector(p, cfg.score_threshold, cfg.nms_threshold, cfg.top_k, cfg.input_scale)
