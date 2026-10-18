"""Enrollment: multi-condition gallery building (owner: vision-engineer). Contract: INTERFACES sections 3.12 and 12.

Per person we store several embeddings per condition tag (normal/glasses/noglasses/mask/scarf/lowlight/backlight)
and poses (front/left/right/up/down). Condition diversity is the first line of robustness.
Quality-failing frames are rejected automatically; near-duplicates are dropped; outliers are reported.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import cv2
import numpy as np

from facedoor.camera import Camera
from facedoor.config import EnrollSection, QualitySection
from facedoor.database import FaceDatabase, GallerySample
from facedoor.datatypes import FaceDetection
from facedoor.detector import FaceDetector
from facedoor.embedder import Embedder
from facedoor.imageio import imread, sha256_file
from facedoor.quality import assess_quality
from facedoor.timeutil import now_iso

log = logging.getLogger(__name__)

POSE_GUIDE = {
    "front": ("Look straight at the camera", "정면을 바라보세요"),
    "left": ("Turn your head slightly LEFT", "고개를 왼쪽으로 살짝 돌리세요"),
    "right": ("Turn your head slightly RIGHT", "고개를 오른쪽으로 살짝 돌리세요"),
    "up": ("Tilt your head slightly UP", "고개를 살짝 드세요"),
    "down": ("Tilt your head slightly DOWN", "고개를 살짝 숙이세요"),
}
TAG_GUIDE = {
    "normal": "평소 모습(안경/마스크 없이 또는 평소대로)으로 촬영합니다",
    "glasses": "안경을 착용하세요",
    "noglasses": "안경을 벗으세요",
    "mask": "마스크를 착용하세요 (코와 입을 가림)",
    "scarf": "목도리로 턱/입 주변을 가리세요",
    "lowlight": "조명을 어둡게 한 상태에서 촬영합니다",
    "backlight": "뒤쪽에 밝은 빛(창문 등)이 있는 상태에서 촬영합니다",
    "hat": "모자를 쓰세요 (챙이 눈을 가리지 않게)",
    "other": "기타 조건으로 촬영합니다",
}
LIVE_POSE_TIMEOUT_SEC = 20.0
LIVE_POSE_SETTLE_SEC = 1.0


@dataclass(slots=True)
class EnrollReport:
    person_id: str
    tag: str
    saved: int
    rejected: dict[str, int] = field(default_factory=dict)
    outliers: list[Path] = field(default_factory=list)
    self_score_mean: float | None = None
    cancelled: bool = False          # (v1.3, section 14.3) cancel event was set during capture
    rolled_back: bool = False        # (v1.3) new-user capture removed again by AdminService
    warnings: list[str] = field(default_factory=list)   # (v1.3) user-facing (Korean) notes from AdminService

    def summary(self) -> str:
        rej = ", ".join(f"{k}={v}" for k, v in sorted(self.rejected.items())) or "none"
        ss = "n/a" if self.self_score_mean is None else f"{self.self_score_mean:.3f}"
        extra = (" CANCELLED" if self.cancelled else "") + (" ROLLED-BACK" if self.rolled_back else "")
        return (f"{self.person_id}/{self.tag}: saved {self.saved}, rejected [{rej}], "
                f"self-score mean {ss}, outliers {len(self.outliers)}{extra}")


@dataclass(slots=True)
class EnrollProgress:
    """Live-capture progress for the admin GUI (INTERFACES section 14.3). Emitted from the capture thread:
    the callback must not touch tkinter (put it in a queue)."""
    phase: str                       # "capturing" | "pose_change" | "done" | "cancelled"
    pose: str | None
    pose_index: int                  # 0-based index of the current pose
    n_poses: int
    saved: int                       # total saved so far in this capture
    target: int                      # n_poses * samples_per_pose
    last_reject: str | None          # HoldReason value | "duplicate" | "no_face" | "multi_face"
    frame: np.ndarray | None         # BGR copy for preview (nothing drawn on it)
    bbox: tuple[int, int, int, int] | None
    countdown_sec: float | None = None   # (addition) remaining pose countdown; None while capturing
    just_saved: bool = False             # (addition) this frame produced a saved sample


def validate_person_id(person_id: str, pattern: str) -> None:
    if not isinstance(person_id, str) or not re.fullmatch(pattern, person_id) or person_id.startswith("_"):
        raise ValueError(f"invalid person_id {person_id!r}: must match {pattern} (ASCII) and not start with '_'")


def validate_locker_id(locker_id: str, pattern: str) -> None:
    """locker_id stored in person.json (INTERFACES section 12); must fully match lockers.locker_id_pattern."""
    if not isinstance(locker_id, str) or not re.fullmatch(pattern, locker_id):
        raise ValueError(f"invalid locker_id {locker_id!r}: must match {pattern}")


# ---------------------------------------------------------------- Korean name -> ASCII person_id
_CHO = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_JUNG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi",
         "yu", "eu", "ui", "i"]
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t",
         "ng", "t", "t", "k", "t", "p", "t"]


def romanize(text: str) -> str:
    """Simplified Revised Romanization of Hangul syllables (no sound-change rules); other chars kept/NFKD'd."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            s = code - 0xAC00
            out.append(_CHO[s // 588] + _JUNG[(s % 588) // 28] + _JONG[s % 28])
        else:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii") or
                       ("_" if ch.isspace() else ""))
    return "".join(out)


def make_person_id(display_name: str, existing: Sequence[str] = (), pattern: str = "^[A-Za-z0-9_-]{1,32}$") -> str:
    """ASCII person_id from a (possibly Korean) display name, unique against existing ids."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", romanize(display_name.strip())).strip("_-").lower()[:24] or "user"
    taken = {e.lower() for e in existing}
    pid, n = base, 2
    while pid.lower() in taken:
        pid = f"{base}_{n}"
        n += 1
    validate_person_id(pid, pattern)
    return pid


def expand_crop(image: np.ndarray, det: FaceDetection, margin: float) -> np.ndarray:
    H, W = image.shape[:2]
    x, y, w, h = det.bbox
    mx, my = int(round(w * margin)), int(round(h * margin))
    x0, y0 = max(0, x - mx), max(0, y - my)
    x1, y1 = min(W, x + w + mx), min(H, y + h + my)
    return image[y0:y1, x0:x1].copy()


class Enroller:
    def __init__(self, detector: FaceDetector, embedder: Embedder, db: FaceDatabase,
                 quality_cfg: QualitySection, enroll_cfg: EnrollSection,
                 notify: Callable[[str], None] | None = None) -> None:
        self.detector = detector
        self.embedder = embedder
        self.db = db
        self.quality_cfg = quality_cfg
        self.cfg = enroll_cfg
        self.notify = notify or (lambda s: log.info("%s", s))
        # The model_id check lives in _check_model() (called by enroll_*), not here, so that reembed_all()
        # can be run on a gallery built with a different embedder.

    def _check_model(self) -> None:
        db, emb = self.db, self.embedder
        if db.model_id is not None and len(db) and db.model_id != emb.model_id:
            from facedoor.errors import GalleryError
            raise GalleryError(f"gallery model_id {db.model_id} != embedder {emb.model_id}; "
                               "run: python scripts/enroll.py --reembed")

    # ------------------------------------------------------------------ core
    def _try_add(self, image: np.ndarray, person_id: str, tag: str, pose: str | None, source: str,
                 source_sha256: str | None, session: list[np.ndarray], note: str | None = None,
                 ) -> tuple[GallerySample | None, str, FaceDetection | None]:
        dets = self.detector.detect(image)
        if not dets:
            return None, "no_face", None
        if len(dets) > 1:
            return None, "multi_face", None
        det = dets[0]
        q = assess_quality(image, det, self.quality_cfg, max_abs_yaw_ratio=self.cfg.max_abs_yaw_ratio)
        if not q.ok:
            return None, (q.reason.value if q.reason else "quality"), det
        emb = self.embedder.embed(image, det)
        if session and max(float(e @ emb) for e in session) >= self.cfg.dedup_score:
            return None, "duplicate", det
        crop = expand_crop(image, det, self.cfg.crop_margin)
        meta = {"person_id": person_id, "tag": tag, "pose": pose, "source": source,
                "source_sha256": source_sha256, "model_id": self.embedder.model_id,
                "det_score": round(float(det.score), 4),
                "quality": {"face_px": q.face_px, "blur_var": round(q.blur_var, 2),
                            "brightness": round(q.brightness, 2), "yaw_ratio": round(q.yaw_ratio, 4)},
                "created_at": now_iso()}
        if note:
            meta["note"] = str(note)
        sample = self.db.add(person_id, tag, emb, crop, meta)
        session.append(emb)
        return sample, "saved", det

    def _finish(self, person_id: str, tag: str, saved: int, rejected: dict[str, int]) -> EnrollReport:
        outliers = [s.npy_path for s in self.db.find_outliers(person_id, self.cfg.outlier_min_self_score)]
        rep = EnrollReport(person_id, tag, saved, rejected, outliers, self.db.self_score_mean(person_id))
        log.info("enroll done: %s", rep.summary())
        return rep

    def _check_tag(self, person_id: str, tag: str) -> None:
        self._check_model()
        validate_person_id(person_id, self.cfg.person_id_pattern)
        base = tag[len("synthetic_"):] if tag.startswith("synthetic_") else tag
        if base not in self.cfg.tags:
            raise ValueError(f"unknown tag {tag!r}; allowed: {self.cfg.tags} (+ synthetic_<tag>)")

    # ------------------------------------------------------------------ public API
    def enroll_images(self, person_id: str, tag: str, paths: Sequence[Path]) -> EnrollReport:
        self._check_tag(person_id, tag)
        rejected: dict[str, int] = {}
        saved = 0
        session: list[np.ndarray] = []
        for p in paths:
            p = Path(p)
            img = imread(p)
            if img is None:
                rejected["unreadable"] = rejected.get("unreadable", 0) + 1
                log.warning("unreadable image %s", p)
                continue
            sample, why, _ = self._try_add(img, person_id, tag, None, f"file:{p}", sha256_file(p), session)
            if sample is None:
                rejected[why] = rejected.get(why, 0) + 1
                log.info("rejected %s: %s", p.name, why)
            else:
                saved += 1
        return self._finish(person_id, tag, saved, rejected)

    def enroll_live(self, camera: Camera, person_id: str, tag: str, display: object | None = None,
                    poses: Sequence[str] | None = None, samples_per_pose: int | None = None, *,
                    progress: Callable[[EnrollProgress], None] | None = None,
                    cancel: threading.Event | None = None,
                    pose_countdown_sec: float = 0.0,
                    note: str | None = None) -> EnrollReport:
        """Guided capture. camera must already be open. Display (optional) needs show_image(image, lines)->key.
        For non-live (file) cameras every frame is a candidate and no timing/settle/countdown delays apply.

        v1.3 (section 14.3, keyword-only, backward compatible):
        - progress: called from this (worker) thread on each pose change and each processed frame.
        - cancel: when set, capture stops before the next frame; report.cancelled=True.
          Samples already saved are kept here (rollback of a new user is AdminService's decision).
        - pose_countdown_sec: per-pose wait before capturing starts (live cameras only; >= the 1 s settle time).
        - note: stored as sample meta "note" (tag "other")."""
        self._check_tag(person_id, tag)
        poses = list(poses or self.cfg.poses)
        spp = int(samples_per_pose or self.cfg.samples_per_pose)
        target = len(poses) * spp
        rejected: dict[str, int] = {}
        saved = 0
        session: list[np.ndarray] = []
        live = bool(getattr(camera, "is_live", True))
        show = getattr(display, "show_image", None)
        settle = max(LIVE_POSE_SETTLE_SEC, float(pose_countdown_sec or 0.0))
        self.notify(f"[{tag}] {TAG_GUIDE.get(tag, tag)}")
        aborted = False
        cancelled = False

        def emit(phase: str, pose: str | None, pi: int, last_reject: str | None = None,
                 image: np.ndarray | None = None, det: FaceDetection | None = None,
                 countdown: float | None = None, just_saved: bool = False) -> None:
            if progress is None:
                return
            try:
                progress(EnrollProgress(phase, pose, pi, len(poses), saved, target, last_reject,
                                        None if image is None else image.copy(),
                                        None if det is None else tuple(int(v) for v in det.bbox),
                                        countdown, just_saved))
            except Exception:  # noqa: BLE001 - a broken UI callback must not break the capture loop
                log.exception("enroll progress callback failed")

        pi = 0
        for pi, pose in enumerate(poses):
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            en, ko = POSE_GUIDE.get(pose, (pose, pose))
            self.notify(f"  - {pose}: {ko} ({spp}장)")
            emit("pose_change", pose, pi)
            got = 0
            t0 = time.monotonic()
            last_cap = 0.0
            while got < spp:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                try:
                    frame = camera.read()
                except StopIteration:
                    aborted = True
                    break
                now = time.monotonic()
                status = ""
                if live and (now - t0 < settle or now - last_cap < self.cfg.capture_interval_sec):
                    waiting = now - t0 < settle
                    status = "get ready" if waiting else ""
                    if progress is not None:
                        dets = self.detector.detect(frame.image) if waiting else []
                        emit("capturing", pose, pi, None, frame.image, dets[0] if len(dets) == 1 else None,
                             countdown=max(0.0, settle - (now - t0)) if waiting else None)
                else:
                    sample, why, det = self._try_add(frame.image, person_id, tag, pose,
                                                     f"live:{frame.source}" if live else frame.source, None,
                                                     session, note)
                    if sample is None:
                        rejected[why] = rejected.get(why, 0) + 1
                        status = f"rejected: {why}"
                        emit("capturing", pose, pi, why, frame.image, det)
                    else:
                        got += 1
                        saved += 1
                        last_cap = now
                        status = f"saved {got}/{spp}"
                        emit("capturing", pose, pi, None, frame.image, det, just_saved=True)
                if callable(show):
                    key = show(frame.image, [f"[{tag}] {pose}: {en}", status, "q/ESC: cancel"])
                    if key in (ord("q"), 27):
                        aborted = True
                        break
                if live and now - t0 > LIVE_POSE_TIMEOUT_SEC + settle - LIVE_POSE_SETTLE_SEC:
                    self.notify(f"    시간 초과: {pose} {got}/{spp}장만 저장")
                    break
            if aborted or cancelled:
                break
        if cancelled:
            self.notify("  촬영이 취소되었습니다")
        elif aborted:
            self.notify("  촬영이 중단되었거나 입력이 끝났습니다")
        rep = self._finish(person_id, tag, saved, rejected)
        rep.cancelled = cancelled
        emit("cancelled" if cancelled else "done", None, pi)
        return rep

    def reembed_all(self) -> int:
        """Re-detect + re-embed every stored crop with the current embedder; update _meta.json model_id.
        Samples whose crop is missing or has no detectable face are moved to <gallery>/_unembeddable/."""
        done = 0
        failed: list[GallerySample] = []
        for s in list(self.db.samples()):
            img = imread(s.jpg_path) if s.jpg_path is not None else None
            dets = self.detector.detect(img) if img is not None else []
            if not dets:
                failed.append(s)
                continue
            det = max(dets, key=lambda d: d.bbox[2] * d.bbox[3])
            emb = self.embedder.embed(img, det)
            self.db.replace_embedding(s, emb, self.embedder.model_id)
            done += 1
        for s in failed:
            dst = self.db.gallery_dir / "_unembeddable" / s.person_id
            dst.mkdir(parents=True, exist_ok=True)
            for p in (s.npy_path, s.jpg_path, s.npy_path.with_suffix(".json")):
                if p is not None and Path(p).exists():
                    Path(p).replace(dst / Path(p).name)
            self.db.remove_sample(s)
            log.warning("re-embed failed for %s; moved to %s", s.npy_path.name, dst)
        self.db.set_model(self.embedder.model_id, self.embedder.dim)
        log.info("re-embedded %d samples (%d moved to _unembeddable) with %s", done, len(failed),
                 self.embedder.model_id)
        return done
