"""Synthetic probe conditions (owner: eval-engineer).

These are crude, reproducible approximations of real conditions (lighting, noise, blur, occlusion overlays).
Results on them are reported as "합성 조건" and never replace real captures.

All functions are pure: they return a new BGR uint8 image and never modify the input.
Landmarks (5, 2) follow YuNet order: right eye, left eye, nose, mouth right, mouth left (image coordinates).
Without landmarks a centred, upright face covering ~60% of the image is assumed (fine for face crops).
"""

from __future__ import annotations

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - opencv is a hard dependency of the project
    cv2 = None  # type: ignore[assignment]

SYNTHETIC_CONDITIONS: tuple[str, ...] = (
    "syn_lowlight", "syn_bright", "syn_backlight", "syn_noise", "syn_blur",
    "syn_mask", "syn_glasses", "syn_scarf",
)


def _check(image: np.ndarray) -> None:
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("expected BGR uint8 image of shape (H, W, 3)")


def default_landmarks(shape: tuple[int, ...]) -> np.ndarray:
    """Heuristic 5-point landmarks for a centred frontal face."""
    h, w = shape[:2]
    cx, cy = w / 2.0, h / 2.0
    s = min(h, w) * 0.6  # assumed face box size
    return np.array([
        [cx - 0.18 * s, cy - 0.12 * s],  # right eye (image left)
        [cx + 0.18 * s, cy - 0.12 * s],  # left eye
        [cx, cy + 0.05 * s],             # nose
        [cx - 0.14 * s, cy + 0.22 * s],  # mouth right
        [cx + 0.14 * s, cy + 0.22 * s],  # mouth left
    ], dtype=np.float32)


def _geometry(lm: np.ndarray) -> dict[str, float]:
    re_, le, nose, mr, ml = (lm[i].astype(np.float64) for i in range(5))
    eye_c = (re_ + le) / 2.0
    eye_d = float(np.linalg.norm(le - re_)) or 1.0
    mouth_c = (mr + ml) / 2.0
    return dict(eye_cx=eye_c[0], eye_cy=eye_c[1], eye_d=eye_d, nose_x=nose[0], nose_y=nose[1],
                mouth_cx=mouth_c[0], mouth_cy=mouth_c[1], re_x=re_[0], re_y=re_[1], le_x=le[0], le_y=le[1])


def _gamma(image: np.ndarray, gamma: float, gain: float = 1.0) -> np.ndarray:
    x = image.astype(np.float32) / 255.0
    y = np.power(x, gamma) * gain
    return np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _noise(image: np.ndarray, sigma: float, rng: np.random.Generator) -> np.ndarray:
    n = rng.normal(0.0, sigma, size=image.shape)
    return np.clip(image.astype(np.float32) + n, 0, 255).astype(np.uint8)


def _blur(image: np.ndarray, k: int) -> np.ndarray:
    k = max(3, k | 1)
    if cv2 is not None:
        return cv2.GaussianBlur(image, (k, k), 0)
    return image.copy()  # pragma: no cover


def _face_weight(shape: tuple[int, ...], g: dict[str, float]) -> np.ndarray:
    """Soft (H, W) mask, 1 at the face centre falling to 0 ~2 eye distances away."""
    h, w = shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = g["nose_x"], g["nose_y"]
    r = 1.6 * g["eye_d"]
    d2 = ((xx - cx) / r) ** 2 + ((yy - cy) / (1.3 * r)) ** 2
    return np.exp(-d2).astype(np.float32)


def _lower_face_polygon(g: dict[str, float], top_y: float, shape: tuple[int, ...]) -> np.ndarray:
    h, w = shape[:2]
    d = g["eye_d"]
    cx = (g["eye_cx"] + g["mouth_cx"]) / 2.0
    chin_y = min(h - 1.0, g["mouth_cy"] + 0.95 * d)
    half_w_top = 1.05 * d
    half_w_bottom = 0.75 * d
    pts = [
        (cx - half_w_top, top_y), (cx + half_w_top, top_y),
        (cx + 1.0 * d, (top_y + chin_y) / 2.0), (cx + half_w_bottom, chin_y),
        (cx, chin_y + 0.15 * d), (cx - half_w_bottom, chin_y), (cx - 1.0 * d, (top_y + chin_y) / 2.0),
    ]
    arr = np.array(pts, dtype=np.float64)
    arr[:, 0] = np.clip(arr[:, 0], 0, w - 1)
    arr[:, 1] = np.clip(arr[:, 1], 0, h - 1)
    return np.round(arr).astype(np.int32)


def _overlay_mask(image: np.ndarray, g: dict[str, float], rng: np.random.Generator) -> np.ndarray:
    out = image.copy()
    top = g["nose_y"] - 0.15 * g["eye_d"]  # covers the nose bridge downward, like a surgical mask
    poly = _lower_face_polygon(g, top, image.shape)
    shade = int(rng.integers(200, 245))
    color = (shade, shade, int(min(255, shade + 5)))  # pale blue/white surgical mask (BGR)
    if cv2 is not None:
        cv2.fillPoly(out, [poly], color, lineType=cv2.LINE_AA)
        # pleats
        d = g["eye_d"]
        for k in (0.25, 0.5, 0.75):
            y = int(top + k * (poly[:, 1].max() - top))
            x0, x1 = int(g["mouth_cx"] - 0.8 * d), int(g["mouth_cx"] + 0.8 * d)
            cv2.line(out, (x0, y), (x1, y), (shade - 40,) * 3, 1, cv2.LINE_AA)
    return out


def _overlay_scarf(image: np.ndarray, g: dict[str, float], rng: np.random.Generator) -> np.ndarray:
    out = image.copy()
    top = (g["nose_y"] + g["mouth_cy"]) / 2.0  # up to just below the nose
    poly = _lower_face_polygon(g, top, image.shape)
    h = image.shape[0]
    # extend down to the image bottom (scarf around neck)
    d = g["eye_d"]
    ext = np.array([[poly[:, 0].min() - int(0.3 * d), h - 1], [poly[:, 0].max() + int(0.3 * d), h - 1]], dtype=np.int32)
    full = np.vstack([poly, ext[::-1]])
    base = np.array([int(rng.integers(20, 90)), int(rng.integers(20, 60)), int(rng.integers(90, 180))])  # dark red-ish
    if cv2 is not None:
        hull = cv2.convexHull(full)
        cv2.fillPoly(out, [hull], tuple(int(c) for c in base), lineType=cv2.LINE_AA)
        stripe = tuple(int(min(255, c + 60)) for c in base)
        y0, y1 = int(top), h - 1
        step = max(4, int(0.25 * d))
        layer = out.copy()
        for y in range(y0, y1, step * 2):
            cv2.line(layer, (0, y), (image.shape[1] - 1, y), stripe, max(1, step // 2))
        m = np.zeros(image.shape[:2], np.uint8)
        cv2.fillPoly(m, [hull], 255)
        out = np.where(m[..., None] > 0, layer, out)
    return out


def _overlay_glasses(image: np.ndarray, g: dict[str, float], rng: np.random.Generator) -> np.ndarray:
    out = image.copy()
    d = g["eye_d"]
    rx, ry = int(0.32 * d), int(0.22 * d)
    thick = max(1, int(0.06 * d))
    tint = float(rng.uniform(0.0, 0.35))  # 0 = clear lens, >0 = tinted
    if cv2 is not None:
        if tint > 0.05:
            lens = np.zeros(image.shape[:2], np.uint8)
            for ex, ey in ((g["re_x"], g["re_y"]), (g["le_x"], g["le_y"])):
                cv2.ellipse(lens, (int(ex), int(ey)), (rx, ry), 0, 0, 360, 255, -1, cv2.LINE_AA)
            f = 1.0 - tint * (lens.astype(np.float32) / 255.0)
            out = np.clip(out.astype(np.float32) * f[..., None], 0, 255).astype(np.uint8)
        frame = (20, 20, 20)
        for ex, ey in ((g["re_x"], g["re_y"]), (g["le_x"], g["le_y"])):
            cv2.ellipse(out, (int(ex), int(ey)), (rx, ry), 0, 0, 360, frame, thick, cv2.LINE_AA)
        # bridge and temples
        cv2.line(out, (int(g["re_x"] + rx), int(g["re_y"])), (int(g["le_x"] - rx), int(g["le_y"])), frame, thick, cv2.LINE_AA)
        cv2.line(out, (int(g["re_x"] - rx), int(g["re_y"])), (int(g["re_x"] - rx - 0.4 * d), int(g["re_y"] - 0.05 * d)), frame, thick, cv2.LINE_AA)
        cv2.line(out, (int(g["le_x"] + rx), int(g["le_y"])), (int(g["le_x"] + rx + 0.4 * d), int(g["le_y"] - 0.05 * d)), frame, thick, cv2.LINE_AA)
    return out


def _backlight(image: np.ndarray, g: dict[str, float], rng: np.random.Generator) -> np.ndarray:
    """Bright background, darkened face: blend towards white outside the face, gamma-darken inside."""
    wgt = _face_weight(image.shape, g)[..., None]
    x = image.astype(np.float32)
    dark = _gamma(image, float(rng.uniform(1.6, 2.2)), float(rng.uniform(0.55, 0.75))).astype(np.float32)
    bright = np.clip(x * 0.4 + 255.0 * 0.75, 0, 255)
    y = wgt * dark + (1.0 - wgt) * bright
    return np.clip(y, 0, 255).astype(np.uint8)


def apply_condition(image: np.ndarray, condition: str, rng: np.random.Generator,
                    landmarks: np.ndarray | None = None) -> np.ndarray:
    """Return a synthetic version of ``image`` for ``condition`` (one of SYNTHETIC_CONDITIONS)."""
    _check(image)
    lm = default_landmarks(image.shape) if landmarks is None else np.asarray(landmarks, dtype=np.float32).reshape(5, 2)
    g = _geometry(lm)
    if condition == "syn_lowlight":
        y = _gamma(image, float(rng.uniform(1.8, 2.6)), float(rng.uniform(0.30, 0.45)))
        return _noise(y, float(rng.uniform(3.0, 7.0)), rng)
    if condition == "syn_bright":
        return _gamma(image, float(rng.uniform(0.45, 0.65)), float(rng.uniform(1.15, 1.35)))
    if condition == "syn_backlight":
        return _backlight(image, g, rng)
    if condition == "syn_noise":
        return _noise(image, float(rng.uniform(12.0, 22.0)), rng)
    if condition == "syn_blur":
        k = int(max(3, round(g["eye_d"] * float(rng.uniform(0.12, 0.2)))))
        return _blur(image, k)
    if condition == "syn_mask":
        return _overlay_mask(image, g, rng)
    if condition == "syn_glasses":
        return _overlay_glasses(image, g, rng)
    if condition == "syn_scarf":
        return _overlay_scarf(image, g, rng)
    raise ValueError(f"unknown synthetic condition {condition!r}; expected one of {SYNTHETIC_CONDITIONS}")
