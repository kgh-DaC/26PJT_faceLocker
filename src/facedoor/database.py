"""Face gallery on disk (owner: vision-engineer). Contract: INTERFACES sections 3.5 and 6.

data/gallery/_meta.json, data/gallery/<person_id>/{person.json, <tag>_<nnn>.npy|.jpg|.json}
person.json may also carry display_name (Korean allowed) and locker_id (INTERFACES section 12).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from facedoor.errors import GalleryError
from facedoor.imageio import imwrite, sha256_file
from facedoor.timeutil import now_iso

log = logging.getLogger(__name__)

META_FILE = "_meta.json"
PERSON_FILE = "person.json"
_STEM_RE = re.compile(r"^(?P<tag>[A-Za-z0-9_]+)_(?P<n>\d{3,})$")


@dataclass(slots=True)
class GallerySample:
    person_id: str
    tag: str
    index: int
    npy_path: Path
    jpg_path: Path | None
    meta: dict = field(default_factory=dict)


def _write_json(path: Path, data: dict) -> None:
    """Atomic write: unique temp file in the same folder + fsync + os.replace (readers never see half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cannot read %s: %s", path, e)
        return {}


class FaceDatabase:
    def __init__(self, gallery_dir: Path, model_id: str | None = None) -> None:
        self.gallery_dir = Path(gallery_dir)
        self.model_id: str | None = model_id
        self.dim: int | None = None
        self._samples: list[GallerySample] = []
        self._embs: list[np.ndarray] = []
        self._matrix: np.ndarray | None = None

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, gallery_dir: Path, expected_model_id: str | None = None) -> "FaceDatabase":
        gallery_dir = Path(gallery_dir)
        db = cls(gallery_dir)
        if not gallery_dir.is_dir():
            db.model_id = expected_model_id
            return db
        meta_path = gallery_dir / META_FILE
        meta = _read_json(meta_path) if meta_path.is_file() else {}
        db.model_id = meta.get("model_id")
        db.dim = meta.get("dim")
        for pdir in sorted(p for p in gallery_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
            for npy in sorted(pdir.glob("*.npy")):
                m = _STEM_RE.match(npy.stem)
                if not m:
                    log.warning("ignoring gallery file with unexpected name: %s", npy)
                    continue
                try:
                    emb = np.load(npy).astype(np.float32).reshape(-1)
                except (OSError, ValueError) as e:
                    raise GalleryError(f"cannot read gallery embedding {npy}: {e}") from e
                if db.dim is None:
                    db.dim = int(emb.shape[0])
                if emb.shape[0] != db.dim:
                    raise GalleryError(f"embedding dim mismatch in {npy}: {emb.shape[0]} != {db.dim}. "
                                       "Run: python scripts/enroll.py --reembed")
                n = float(np.linalg.norm(emb))
                if n < 1e-12 or not np.isfinite(n):
                    raise GalleryError(f"invalid (zero/non-finite) embedding {npy}")
                jpg = npy.with_suffix(".jpg")
                js = npy.with_suffix(".json")
                smeta = _read_json(js) if js.is_file() else {}
                db._samples.append(GallerySample(pdir.name, m.group("tag"), int(m.group("n")), npy,
                                                 jpg if jpg.is_file() else None, smeta))
                db._embs.append(emb / n)
        if db._samples and db.model_id is None:
            raise GalleryError(f"{meta_path} missing or has no model_id; run: python scripts/enroll.py --reembed")
        if expected_model_id is not None and db._samples and db.model_id != expected_model_id:
            raise GalleryError(f"gallery was built with embedder {db.model_id!r} but current embedder is "
                               f"{expected_model_id!r}. Run: python scripts/enroll.py --reembed")
        if not db._samples and db.model_id is None:
            db.model_id = expected_model_id
        log.info("gallery %s: %d samples, %d persons, model_id=%s", gallery_dir, len(db._samples),
                 len(db.persons()), db.model_id)
        return db

    # ------------------------------------------------------------------ views
    @property
    def matrix(self) -> np.ndarray:
        if self._matrix is None:
            if self._embs:
                self._matrix = np.stack(self._embs).astype(np.float32)
            else:
                self._matrix = np.zeros((0, self.dim or 0), dtype=np.float32)
        return self._matrix

    @property
    def labels(self) -> list[str]:
        return [s.person_id for s in self._samples]

    @property
    def tags(self) -> list[str]:
        return [s.tag for s in self._samples]

    def persons(self) -> list[str]:
        """Sorted unique person_ids that have at least one sample."""
        return sorted(set(self.labels))

    def all_person_ids(self) -> list[str]:
        """Persons with samples or with only a person.json (e.g. enrollment in progress)."""
        ids = set(self.labels)
        if self.gallery_dir.is_dir():
            ids |= {p.name for p in self.gallery_dir.iterdir()
                    if p.is_dir() and not p.name.startswith("_") and (p / PERSON_FILE).is_file()}
        return sorted(ids)

    def samples(self, person_id: str | None = None) -> list[GallerySample]:
        return [s for s in self._samples if person_id is None or s.person_id == person_id]

    def embedding(self, sample: GallerySample) -> np.ndarray:
        return self._embs[self._samples.index(sample)]

    def __len__(self) -> int:
        return len(self._samples)

    # ------------------------------------------------------------------ person info (section 12)
    def person_info(self, person_id: str) -> dict:
        p = self.gallery_dir / person_id / PERSON_FILE
        return _read_json(p) if p.is_file() else {}

    def set_person_info(self, person_id: str, **fields: object) -> dict:
        info = self.person_info(person_id)
        if not info:
            info = {"person_id": person_id, "created_at": now_iso()}
        info.update({k: v for k, v in fields.items()})
        info["updated_at"] = now_iso()
        _write_json(self.gallery_dir / person_id / PERSON_FILE, info)
        return info

    def display_name(self, person_id: str) -> str:
        return str(self.person_info(person_id).get("display_name") or person_id)

    def locker_id(self, person_id: str) -> str | None:
        """person.json locker_id. Missing / null / "" -> None. A non-string value (number, list: manual edit
        error) is NOT converted with str(): returns None with a WARNING, so per_locker can never route a
        malformed value to a channel (INTERFACES 3.5; LockerMap reports it as invalid_locker_id)."""
        v = self.person_info(person_id).get("locker_id")
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            log.warning("person %s: locker_id %r is not a string (type %s); treated as no locker",
                        person_id, v, type(v).__name__)
            return None
        return v

    # ------------------------------------------------------------------ mutation
    def _write_meta(self) -> None:
        _write_json(self.gallery_dir / META_FILE, {"schema_version": 1, "model_id": self.model_id,
                                                   "dim": self.dim, "updated_at": now_iso()})

    def _next_index(self, person_id: str, tag: str) -> int:
        pdir = self.gallery_dir / person_id
        used = [s.index for s in self._samples if s.person_id == person_id and s.tag == tag]
        if pdir.is_dir():
            for f in pdir.glob(f"{tag}_*.npy"):
                m = _STEM_RE.match(f.stem)
                if m and m.group("tag") == tag:
                    used.append(int(m.group("n")))
        return max(used) + 1 if used else 0

    def add(self, person_id: str, tag: str, embedding: np.ndarray, crop: np.ndarray | None,
            meta: dict) -> GallerySample:
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(emb))
        if n < 1e-12 or not np.isfinite(n):
            raise GalleryError("refusing to store zero/non-finite embedding")
        emb = emb / n
        if self.dim is not None and len(self._samples) and emb.shape[0] != self.dim:
            raise GalleryError(f"embedding dim {emb.shape[0]} != gallery dim {self.dim}")
        mid = meta.get("model_id")
        if mid and self.model_id and mid != self.model_id and len(self._samples):
            raise GalleryError(f"sample model_id {mid} != gallery model_id {self.model_id}")
        if mid and (self.model_id is None or not len(self._samples)):
            self.model_id = mid
        self.dim = int(emb.shape[0])
        if not re.fullmatch(r"[A-Za-z0-9_]+", tag):
            raise GalleryError(f"invalid tag {tag!r}")
        pdir = self.gallery_dir / person_id
        pdir.mkdir(parents=True, exist_ok=True)
        if not (pdir / PERSON_FILE).is_file():
            self.set_person_info(person_id)
        idx = self._next_index(person_id, tag)
        stem = f"{tag}_{idx:03d}"
        npy = pdir / f"{stem}.npy"
        np.save(npy, emb)
        jpg: Path | None = None
        full_meta = dict(meta)
        full_meta.update({"person_id": person_id, "tag": tag, "index": idx})
        full_meta.setdefault("created_at", now_iso())
        full_meta.setdefault("model_id", self.model_id)
        if crop is not None:
            jpg = pdir / f"{stem}.jpg"
            if not imwrite(jpg, crop, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise GalleryError(f"failed to write crop {jpg}")
            full_meta["crop_sha256"] = sha256_file(jpg)
        _write_json(pdir / f"{stem}.json", full_meta)
        sample = GallerySample(person_id, tag, idx, npy, jpg, full_meta)
        self._samples.append(sample)
        self._embs.append(emb)
        self._matrix = None
        self._write_meta()
        return sample

    def remove_sample(self, sample: GallerySample) -> None:
        for p in (sample.npy_path, sample.jpg_path, sample.npy_path.with_suffix(".json")):
            if p is not None and Path(p).exists():
                Path(p).unlink()
        for i, s in enumerate(self._samples):
            if s.npy_path == sample.npy_path:
                del self._samples[i]
                del self._embs[i]
                break
        self._matrix = None

    def remove_person(self, person_id: str) -> None:
        pdir = self.gallery_dir / person_id
        if pdir.is_dir():
            shutil.rmtree(pdir)
        keep = [i for i, s in enumerate(self._samples) if s.person_id != person_id]
        self._samples = [self._samples[i] for i in keep]
        self._embs = [self._embs[i] for i in keep]
        self._matrix = None
        if self.gallery_dir.is_dir():
            self._write_meta()

    def replace_embedding(self, sample: GallerySample, embedding: np.ndarray, model_id: str) -> None:
        """Used by re-embedding: overwrite .npy and meta.model_id in place."""
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        emb = emb / float(np.linalg.norm(emb))
        np.save(sample.npy_path, emb)
        sample.meta["model_id"] = model_id
        _write_json(sample.npy_path.with_suffix(".json"), sample.meta)
        i = [s.npy_path for s in self._samples].index(sample.npy_path)
        self._embs[i] = emb
        self._matrix = None

    def set_model(self, model_id: str, dim: int) -> None:
        self.model_id, self.dim = model_id, int(dim)
        self.gallery_dir.mkdir(parents=True, exist_ok=True)
        self._write_meta()

    def find_outliers(self, person_id: str, min_self_score: float) -> list[GallerySample]:
        idx = [i for i, s in enumerate(self._samples) if s.person_id == person_id]
        if len(idx) < 2:
            return []
        m = self.matrix[idx]
        sims = m @ m.T
        mean_other = (sims.sum(axis=1) - np.diag(sims)) / (len(idx) - 1)
        return [self._samples[i] for i, v in zip(idx, mean_other) if float(v) < min_self_score]

    def self_score_mean(self, person_id: str) -> float | None:
        idx = [i for i, s in enumerate(self._samples) if s.person_id == person_id]
        if len(idx) < 2:
            return None
        m = self.matrix[idx]
        sims = m @ m.T
        k = len(idx)
        return float((sims.sum() - np.trace(sims)) / (k * (k - 1)))
