"""Gallery/probe scanning, leakage check and calibration/test split (owner: eval-engineer).

Directory contract: docs/INTERFACES.md section 6.

    data/probe/<condition>/<person_id>/*.jpg     genuine probes (true_id = person_id)
    data/probe/_impostor/<impostor_id>/*.jpg     unenrolled people, condition "normal"
    data/probe/_impostor_<condition>/<id>/*.jpg  unenrolled people under a condition
    data/probe/syn_<cond>/<person_id>/*.jpg      synthetic (is_synthetic=True)
    data/probe/_impostor_syn_<cond>/<id>/*.jpg   synthetic impostors (is_synthetic=True)
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from facedoor.evaluation._util import IMAGE_EXTS, sha256_file

log = logging.getLogger(__name__)

IMPOSTOR_LABEL = "_impostor"
_STEM_RE = re.compile(r"^(?P<tag>.+)_(?P<idx>\d+)$")

try:  # vision owns GallerySample; use it when present so types are shared.
    from facedoor.database import GallerySample  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - fallback while database.py is not available

    @dataclass(slots=True)
    class GallerySample:  # type: ignore[no-redef]
        person_id: str
        tag: str
        index: int
        npy_path: Path
        jpg_path: Path | None
        meta: dict


@dataclass(slots=True)
class ProbeSample:
    path: Path
    condition: str
    true_id: str | None  # None = impostor
    impostor_id: str | None
    is_synthetic: bool
    sha256: str

    @property
    def label(self) -> str:
        """true_id or "_impostor" (ScoreRecord.true_id convention)."""
        return self.true_id if self.true_id is not None else IMPOSTOR_LABEL

    @property
    def unit(self) -> str:
        """Resampling / split unit: the person (enrolled or impostor)."""
        return self.true_id if self.true_id is not None else f"{IMPOSTOR_LABEL}:{self.impostor_id}"


def scan_gallery(gallery_dir: Path) -> list[GallerySample]:
    """All ``<person>/<tag>_<n>.npy`` samples with their sidecar json/jpg. Missing folder -> []."""
    gallery_dir = Path(gallery_dir)
    out: list[GallerySample] = []
    if not gallery_dir.is_dir():
        return out
    for pdir in sorted(p for p in gallery_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        for npy in sorted(pdir.glob("*.npy")):
            m = _STEM_RE.match(npy.stem)
            tag, idx = (m.group("tag"), int(m.group("idx"))) if m else (npy.stem, 0)
            jpg = npy.with_suffix(".jpg")
            meta_path = npy.with_suffix(".json")
            meta: dict[str, Any] = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    log.warning("bad gallery meta %s: %s", meta_path, exc)
            out.append(GallerySample(person_id=pdir.name, tag=tag, index=idx, npy_path=npy,
                                     jpg_path=jpg if jpg.is_file() else None, meta=meta))
    return out


def _iter_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def scan_probes(probe_dir: Path, impostor_dir_name: str = "_impostor", synthetic_prefix: str = "syn_") -> list[ProbeSample]:
    """Scan ``probe_dir`` per the layout in the module docstring. Unknown hidden folders (``.x``) are skipped."""
    probe_dir = Path(probe_dir)
    out: list[ProbeSample] = []
    if not probe_dir.is_dir():
        return out
    for cdir in sorted(p for p in probe_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        name = cdir.name
        if name == impostor_dir_name or name.startswith(impostor_dir_name + "_"):
            condition = "normal" if name == impostor_dir_name else name[len(impostor_dir_name) + 1:]
            impostor = True
        elif name.startswith("_"):
            log.info("skipping probe folder %s (reserved prefix '_')", cdir)
            continue
        else:
            condition, impostor = name, False
        synthetic = condition.startswith(synthetic_prefix)
        for pdir in sorted(p for p in cdir.iterdir() if p.is_dir()):
            for img in _iter_images(pdir):
                out.append(ProbeSample(
                    path=img, condition=condition,
                    true_id=None if impostor else pdir.name,
                    impostor_id=pdir.name if impostor else None,
                    is_synthetic=synthetic, sha256=sha256_file(img)))
    return out


def check_leakage(gallery: Sequence[GallerySample], probes: Sequence[ProbeSample]) -> list[tuple[Path, Path]]:
    """(gallery file, probe file) pairs whose bytes coincide: probe sha256 vs gallery meta
    ``source_sha256`` / ``crop_sha256`` and, when the meta lacks crop_sha256, the gallery jpg hash."""
    index: dict[str, Path] = {}
    for s in gallery:
        ref = s.jpg_path or s.npy_path
        for key in ("source_sha256", "crop_sha256"):
            h = (s.meta or {}).get(key)
            if isinstance(h, str) and h:
                index.setdefault(h.lower(), ref)
        if s.jpg_path is not None and not (s.meta or {}).get("crop_sha256"):
            try:
                index.setdefault(sha256_file(s.jpg_path), s.jpg_path)
            except OSError:
                pass
    return [(index[p.sha256.lower()], p.path) for p in probes if p.sha256.lower() in index]


def split_calibration(probes: Sequence[ProbeSample], fraction: float, by: str, seed: int) -> tuple[list[ProbeSample], list[ProbeSample]]:
    """Split into (calibration, test).

    by="person": whole units (enrolled person / impostor id) go to one side. Enrolled persons and impostor
    ids are shuffled separately so both sides get impostors when there are >= 2 of them.
    by="image": every (condition, unit) cell is shuffled and split, so both sides see every person/condition.
    Rounding: each stratum contributes round(fraction * n) units to calibration but at least one unit is
    always kept for test (the test set is what gets reported).
    """
    if not 0.0 <= fraction < 1.0:
        raise ValueError("fraction must be in [0, 1)")
    rng = np.random.default_rng(seed)

    def take(n: int) -> int:
        if n <= 1:
            return 0
        return min(n - 1, int(round(fraction * n)))

    cal: list[ProbeSample] = []
    test: list[ProbeSample] = []
    if by == "person":
        for impostor in (False, True):
            units = sorted({p.unit for p in probes if (p.true_id is None) == impostor})
            order = [units[j] for j in rng.permutation(len(units))]
            cal_units = set(order[: take(len(order))])
            for p in probes:
                if (p.true_id is None) == impostor:
                    (cal if p.unit in cal_units else test).append(p)
    elif by == "image":
        cells: dict[tuple[str, str], list[ProbeSample]] = defaultdict(list)
        for p in probes:
            cells[(p.condition, p.unit)].append(p)
        cal_set: set[int] = set()
        for key in sorted(cells):
            items = cells[key]
            order = rng.permutation(len(items))
            for j in order[: take(len(items))]:
                cal_set.add(id(items[j]))
        for p in probes:
            (cal if id(p) in cal_set else test).append(p)
    else:
        raise ValueError(f"split_by must be 'person' or 'image', got {by!r}")
    return cal, test
