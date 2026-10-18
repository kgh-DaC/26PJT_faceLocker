"""models/calibration.json read/write/validation (owner: eval-engineer). Contract: docs/INTERFACES.md section 7.1.

The runtime (recognizer.ThresholdPolicy) imports this module, so it must stay lightweight:
only the standard library (json/math/logging/dataclasses) is used here.

Policy reminder (section 7.2, implemented by vision's ThresholdPolicy):
calibration can always RAISE thresholds; it can LOWER them only when ``reliable`` is true.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
GROUPS: tuple[str, ...] = ("global", "occluded")
IMPOSTOR_SOURCES: frozenset[str] = frozenset({"real", "cross", "real+cross", "none"})


@dataclass(slots=True)
class GroupThreshold:
    threshold: float
    far: float | None
    frr: float | None
    far_upper95: float | None
    n_genuine: int
    n_impostor: int
    impostor_source: str  # "real" | "cross" | "real+cross" | "none"
    synthetic_only: bool
    reliable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": _round(self.threshold),
            "far": _round(self.far),
            "frr": _round(self.frr),
            "far_upper95": _round(self.far_upper95),
            "n_genuine": int(self.n_genuine),
            "n_impostor": int(self.n_impostor),
            "impostor_source": self.impostor_source,
            "synthetic_only": bool(self.synthetic_only),
            "reliable": bool(self.reliable),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GroupThreshold":
        thr = d["threshold"]
        if not isinstance(thr, (int, float)) or isinstance(thr, bool) or not math.isfinite(float(thr)):
            raise ValueError(f"threshold must be a finite number, got {thr!r}")
        if not -1.0 <= float(thr) <= 1.0 + 1e-3:
            raise ValueError(f"threshold {thr} outside cosine range [-1, 1]")
        src = str(d.get("impostor_source", "none"))
        if src not in IMPOSTOR_SOURCES:
            raise ValueError(f"impostor_source {src!r} not in {sorted(IMPOSTOR_SOURCES)}")
        reliable = d.get("reliable", False)
        if not isinstance(reliable, bool):
            raise ValueError("reliable must be a bool")
        synthetic_only = d.get("synthetic_only", True)
        if not isinstance(synthetic_only, bool):
            raise ValueError("synthetic_only must be a bool")
        return cls(
            threshold=float(thr),
            far=_opt_float(d.get("far")),
            frr=_opt_float(d.get("frr")),
            far_upper95=_opt_float(d.get("far_upper95")),
            n_genuine=int(d.get("n_genuine", 0)),
            n_impostor=int(d.get("n_impostor", 0)),
            impostor_source=src,
            synthetic_only=synthetic_only,
            reliable=reliable,
        )


@dataclass(slots=True)
class CalibrationFile:
    schema_version: int
    created_at: str
    score: str
    embedder_backend: str
    embedder_model_id: str
    aggregation: str
    target_far: float
    margin: float | None
    thresholds: dict[str, GroupThreshold | None]  # keys "global", "occluded"
    data: dict = field(default_factory=dict)
    report_dir: str | None = None
    notes: list[str] = field(default_factory=list)

    def group(self, name: str) -> GroupThreshold | None:
        return self.thresholds.get(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "score": self.score,
            "embedder": {"backend": self.embedder_backend, "model_id": self.embedder_model_id},
            "aggregation": self.aggregation,
            "target_far": self.target_far,
            "margin": self.margin,
            "thresholds": {g: (self.thresholds.get(g).to_dict() if self.thresholds.get(g) else None) for g in GROUPS},
            "data": self.data,
            "report_dir": self.report_dir,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CalibrationFile":
        if not isinstance(d, Mapping):
            raise ValueError("calibration root must be a JSON object")
        ver = d.get("schema_version")
        if ver != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {ver!r} (expected {SCHEMA_VERSION})")
        emb = d.get("embedder")
        if not isinstance(emb, Mapping) or "backend" not in emb or "model_id" not in emb:
            raise ValueError("embedder.backend and embedder.model_id are required")
        target_far = float(d["target_far"])
        if not 0.0 < target_far < 1.0:
            raise ValueError(f"target_far {target_far} must be in (0, 1)")
        raw_thr = d.get("thresholds")
        if not isinstance(raw_thr, Mapping):
            raise ValueError("thresholds must be an object")
        unknown = set(raw_thr) - set(GROUPS)
        if unknown:
            raise ValueError(f"unknown threshold groups: {sorted(unknown)}")
        thresholds: dict[str, GroupThreshold | None] = {}
        for g in GROUPS:
            v = raw_thr.get(g)
            thresholds[g] = None if v is None else GroupThreshold.from_dict(v)
        notes = d.get("notes") or []
        if not isinstance(notes, list):
            raise ValueError("notes must be a list")
        data = d.get("data") or {}
        if not isinstance(data, Mapping):
            raise ValueError("data must be an object")
        return cls(
            schema_version=int(ver),
            created_at=str(d.get("created_at", "")),
            score=str(d.get("score", "")),
            embedder_backend=str(emb["backend"]),
            embedder_model_id=str(emb["model_id"]),
            aggregation=str(d.get("aggregation", "max")),
            target_far=target_far,
            margin=_opt_float(d.get("margin")),
            thresholds=thresholds,
            data=dict(data),
            report_dir=None if d.get("report_dir") is None else str(d.get("report_dir")),
            notes=[str(n) for n in notes],
        )


def min_impostors_for(target_far: float) -> int:
    """Rule-of-three sample size: zero FAs in N trials bounds FAR <= 3/N (95%)."""
    return int(math.ceil(3.0 / target_far - 1e-9))


def is_reliable(n_impostor: int, target_far: float, synthetic_only: bool, impostor_source: str) -> bool:
    """INTERFACES 7.1: n_impostor >= ceil(3/target_far) and not synthetic_only and impostor_source != 'none'."""
    return (
        n_impostor >= min_impostors_for(target_far)
        and not synthetic_only
        and impostor_source != "none"
    )


def now_iso() -> str:
    """Local ISO8601 with milliseconds and offset, e.g. 2026-10-16T09:30:12.345+09:00."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def load_calibration(path: Path) -> CalibrationFile | None:
    """Missing file -> None. Corrupt / schema error -> WARNING log and None (runtime falls back to defaults)."""
    path = Path(path)
    if not path.is_file():
        log.info("calibration file not found: %s (using default thresholds)", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        cal = CalibrationFile.from_dict(raw)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("calibration file %s ignored (corrupt or invalid schema): %s", path, exc)
        return None
    if cal.score != "cosine":
        # Kept (not None) so ThresholdPolicy can log why it is ignored; rule 7.2-1 handles it.
        log.warning("calibration file %s uses score=%r (expected 'cosine'); runtime will ignore it", path, cal.score)
    return cal


def save_calibration(path: Path, cal: CalibrationFile) -> None:
    """Write to a temp file in the same folder, keep the previous file as <name>.bak, then atomically replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Validate round trip before touching the existing file.
    payload = cal.to_dict()
    CalibrationFile.from_dict(json.loads(json.dumps(payload)))
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    log.info("calibration written: %s", path)


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _round(v: float | None, nd: int = 6) -> float | None:
    if v is None:
        return None
    f = float(v)
    if not math.isfinite(f):
        return None
    return round(f, nd)
