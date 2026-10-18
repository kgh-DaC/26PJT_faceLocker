"""Face embedding: SFace (OpenCV, default) or ArcFace (onnxruntime, optional). Contract: INTERFACES section 3.4.

All embeddings are float32 (D,) L2-normalized; cosine similarity == dot product.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

import cv2
import numpy as np

from facedoor.config import EmbedderSection
from facedoor.datatypes import FaceDetection
from facedoor.errors import ModelError
from facedoor.imageio import ascii_safe_path, sha256_file

log = logging.getLogger(__name__)

MODEL_HINT = "run: python scripts/download_models.py"

# ArcFace 112x112 5-point template (image-left eye first; same order as YuNet landmarks).
ARCFACE_TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                             [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


@runtime_checkable
class Embedder(Protocol):
    backend: str
    dim: int
    model_id: str

    def embed(self, image: np.ndarray, det: FaceDetection) -> np.ndarray: ...
    def align(self, image: np.ndarray, det: FaceDetection) -> np.ndarray: ...


def l2_normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n < 1e-12:
        raise ValueError("cannot normalize a zero/non-finite embedding")
    return (v / n).astype(np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine for already-normalized vectors."""
    return float(np.asarray(a, dtype=np.float32) @ np.asarray(b, dtype=np.float32))


def apply_clahe(bgr: np.ndarray, clip: float, grid: int) -> np.ndarray:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l_, a, b = cv2.split(lab)
    l_ = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(int(grid), int(grid))).apply(l_)
    return cv2.cvtColor(cv2.merge([l_, a, b]), cv2.COLOR_LAB2BGR)


def model_id_for(backend: str, model_path: Path) -> str:
    return f"{backend}:{sha256_file(model_path)[:12]}"


def align_arcface(image: np.ndarray, landmarks: np.ndarray, size: int = 112) -> np.ndarray:
    src = np.asarray(landmarks, dtype=np.float32).reshape(5, 2)
    M, _ = cv2.estimateAffinePartial2D(src, ARCFACE_TEMPLATE * (size / 112.0), method=cv2.LMEDS)
    if M is None:
        raise ValueError("cannot estimate alignment transform from landmarks")
    return cv2.warpAffine(image, M, (size, size), borderValue=0)


class SFaceEmbedder:
    backend = "sface"
    dim = 128

    def __init__(self, model_path: Path, clahe: bool = False, clahe_clip: float = 2.0, clahe_grid: int = 8) -> None:
        model_path = Path(model_path)
        if not model_path.is_file():
            raise ModelError(f"SFace model not found: {model_path} ({MODEL_HINT})")
        try:
            self._net = cv2.FaceRecognizerSF.create(str(ascii_safe_path(model_path)), "")
        except cv2.error as e:
            raise ModelError(f"failed to load SFace model {model_path}: {e} ({MODEL_HINT} --force)") from e
        self.model_path = model_path
        self.model_id = model_id_for(self.backend, model_path)
        self.clahe, self.clahe_clip, self.clahe_grid = clahe, clahe_clip, clahe_grid

    def align(self, image: np.ndarray, det: FaceDetection) -> np.ndarray:
        raw = np.asarray(det.raw, dtype=np.float32).reshape(1, -1)
        return self._net.alignCrop(image, raw)

    def embed(self, image: np.ndarray, det: FaceDetection) -> np.ndarray:
        aligned = self.align(image, det)
        if self.clahe:
            aligned = apply_clahe(aligned, self.clahe_clip, self.clahe_grid)
        feat = self._net.feature(aligned)
        return l2_normalize(feat)


class ArcFaceEmbedder:
    backend = "arcface"
    dim = 512

    def __init__(self, model_path: Path, providers: Sequence[str] = ("CPUExecutionProvider",),
                 clahe: bool = False, clahe_clip: float = 2.0, clahe_grid: int = 8) -> None:
        model_path = Path(model_path)
        if not model_path.is_file():
            raise ModelError(f"ArcFace model not found: {model_path}. Place an InsightFace w600k_r50 ONNX file there "
                             "(non-commercial license, not auto-downloaded).")
        try:
            import onnxruntime as ort  # lazy: optional dependency
        except ImportError as e:
            raise ModelError("embedder.backend=arcface requires onnxruntime: pip install onnxruntime>=1.17") from e
        try:
            self._sess = ort.InferenceSession(str(model_path), providers=list(providers))
        except Exception as e:  # noqa: BLE001
            raise ModelError(f"failed to load ArcFace model {model_path}: {e}") from e
        self._input = self._sess.get_inputs()[0].name
        out_shape = self._sess.get_outputs()[0].shape
        if out_shape and isinstance(out_shape[-1], int):
            self.dim = int(out_shape[-1])
        self.model_path = model_path
        self.model_id = model_id_for(self.backend, model_path)
        self.clahe, self.clahe_clip, self.clahe_grid = clahe, clahe_clip, clahe_grid

    def align(self, image: np.ndarray, det: FaceDetection) -> np.ndarray:
        return align_arcface(image, det.landmarks)

    def embed(self, image: np.ndarray, det: FaceDetection) -> np.ndarray:
        aligned = self.align(image, det)
        if self.clahe:
            aligned = apply_clahe(aligned, self.clahe_clip, self.clahe_grid)
        rgb = cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB).astype(np.float32)
        blob = ((rgb - 127.5) / 127.5).transpose(2, 0, 1)[None]
        out = self._sess.run(None, {self._input: blob})[0]
        return l2_normalize(out)


def create_embedder(cfg: EmbedderSection, models_dir: Path) -> Embedder:
    def _p(name: str) -> Path:
        p = Path(name)
        return p if p.is_absolute() else Path(models_dir) / p

    if cfg.backend == "sface":
        return SFaceEmbedder(_p(cfg.sface_model_file), cfg.clahe, cfg.clahe_clip, cfg.clahe_grid)
    if cfg.backend == "arcface":
        return ArcFaceEmbedder(_p(cfg.arcface_model_file), cfg.onnx_providers, cfg.clahe, cfg.clahe_clip,
                               cfg.clahe_grid)
    raise ModelError(f"unknown embedder backend {cfg.backend!r}")
