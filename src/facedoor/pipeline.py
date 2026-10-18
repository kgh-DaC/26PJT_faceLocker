"""One frame -> RecognitionResult, shared by runtime and evaluation (owner: vision-engineer). INTERFACES section 3.8.

Runtime and evaluation MUST use this same class so calibrated thresholds mean the same thing at the door.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from facedoor.config import AppConfig, QualitySection
from facedoor.database import FaceDatabase
from facedoor.datatypes import FaceDetection, Frame, FrameVerdict, HoldReason, QualityResult, RecognitionResult
from facedoor.detector import FaceDetector, create_detector
from facedoor.embedder import Embedder, create_embedder
from facedoor.errors import GalleryError
from facedoor.liveness import LivenessChecker, LivenessResult, create_liveness
from facedoor.quality import assess_quality
from facedoor.recognizer import Recognizer, ThresholdPolicy

if TYPE_CHECKING:  # pragma: no cover
    from facedoor.calibration import CalibrationFile

log = logging.getLogger(__name__)

_AUTO: Any = object()  # sentinel: load calibration from cfg.app.calibration_file


class FramePipeline:
    def __init__(self, detector: FaceDetector, embedder: Embedder, recognizer: Recognizer,
                 quality_cfg: QualitySection, multi_face_policy: str = "largest",
                 liveness: LivenessChecker | None = None, liveness_required: bool = False) -> None:
        if multi_face_policy not in ("largest", "hold"):
            raise ValueError(f"multi_face_policy must be largest|hold, got {multi_face_policy!r}")
        self.detector = detector
        self.embedder = embedder
        self.recognizer = recognizer
        self.quality_cfg = quality_cfg
        self.multi_face_policy = multi_face_policy
        self.liveness = liveness
        self.liveness_required = bool(liveness_required)

    def _result(self, frame: Frame, verdict: FrameVerdict, det: FaceDetection | None, dets: list[FaceDetection],
                q: QualityResult | None, match: Any, live: LivenessResult | None, reason: HoldReason | None,
                timings: dict[str, float], t0: float) -> RecognitionResult:
        timings.setdefault("det", 0.0)
        timings.setdefault("emb", 0.0)
        timings.setdefault("match", 0.0)
        timings["total"] = (time.perf_counter() - t0) * 1000.0
        return RecognitionResult(frame.index, frame.ts, verdict, det, dets, q, match, live, reason, timings)

    def process(self, frame: Frame) -> RecognitionResult:
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        img = frame.image
        dets = self.detector.detect(img)
        timings["det"] = (time.perf_counter() - t0) * 1000.0
        if not dets:
            return self._result(frame, FrameVerdict.NO_FACE, None, [], None, None, None, None, timings, t0)

        largest = max(dets, key=lambda d: d.bbox[2] * d.bbox[3])
        det, q = largest, assess_quality(img, largest, self.quality_cfg)
        if self.multi_face_policy == "hold" and len(dets) > 1:
            passing = [(d, assess_quality(img, d, self.quality_cfg)) for d in dets]
            passing = [(d, qq) for d, qq in passing if qq.ok]
            if len(passing) >= 2:
                return self._result(frame, FrameVerdict.HOLD, det, dets, q, None, None, HoldReason.MULTI_FACE,
                                    timings, t0)
            if len(passing) == 1:
                det, q = passing[0]
        if not q.ok:
            return self._result(frame, FrameVerdict.HOLD, det, dets, q, None, None, q.reason, timings, t0)

        live: LivenessResult | None = None
        if self.liveness is not None:
            live = self.liveness.check(frame, det)
        if (live is not None and live.is_live is False) or (
                self.liveness_required and (live is None or live.is_live is not True)):
            return self._result(frame, FrameVerdict.HOLD, det, dets, q, None, live, HoldReason.LIVENESS, timings, t0)

        t1 = time.perf_counter()
        emb = self.embedder.embed(img, det)
        timings["emb"] = (time.perf_counter() - t1) * 1000.0
        t2 = time.perf_counter()
        match = self.recognizer.identify(emb)
        timings["match"] = (time.perf_counter() - t2) * 1000.0
        verdict = FrameVerdict.MATCH if match.accepted else FrameVerdict.UNKNOWN
        return self._result(frame, verdict, det, dets, q, match, live, None, timings, t0)

    def process_image(self, image: np.ndarray, index: int = 0) -> RecognitionResult:
        return self.process(Frame(image=image, index=index, ts=time.monotonic(), source="image"))

    @classmethod
    def from_config(cls, cfg: AppConfig, calibration: "CalibrationFile | None" = _AUTO,
                    allow_empty_gallery: bool | None = None) -> "FramePipeline":
        """Load models, gallery (expected_model_id=embedder.model_id) and ThresholdPolicy.

        calibration omitted -> load_calibration(cfg.app.calibration_file); explicit None -> defaults only.
        Raises ModelError / GalleryError (fail-closed)."""
        detector = create_detector(cfg.detector, cfg.app.models_dir)
        embedder = create_embedder(cfg.embedder, cfg.app.models_dir)
        db = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=embedder.model_id)
        allow_empty = cfg.app.allow_empty_gallery if allow_empty_gallery is None else allow_empty_gallery
        if len(db) == 0 and not allow_empty:
            raise GalleryError(f"gallery {cfg.app.gallery_dir} is empty: enroll someone first "
                               "(python scripts/enroll.py --name <id> --tag normal) or set app.allow_empty_gallery")
        if calibration is _AUTO:
            from facedoor.calibration import load_calibration
            calibration = load_calibration(cfg.app.calibration_file)
        policy = ThresholdPolicy.from_sources(cfg.recognition, embedder.backend, embedder.model_id, calibration)
        rec = Recognizer(db, policy, cfg.recognition.aggregation, cfg.recognition.topk)
        return cls(detector, embedder, rec, cfg.quality, cfg.recognition.multi_face_policy,
                   create_liveness(cfg.liveness), cfg.liveness.required)
