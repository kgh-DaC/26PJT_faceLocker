"""Gallery matching and threshold policy (owner: vision-engineer). Contract: INTERFACES sections 3.7 and 7.2.

Thresholds never live in code: they come from configs (conservative defaults) and models/calibration.json.
Calibration may always RAISE a threshold; it may LOWER it only when the group is reliable (enough real impostors).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from facedoor.config import RecognitionSection
from facedoor.database import FaceDatabase
from facedoor.datatypes import MatchCandidate, MatchResult

if TYPE_CHECKING:  # pragma: no cover
    from facedoor.calibration import CalibrationFile

log = logging.getLogger(__name__)

GROUPS = ("global", "occluded")
SRC_DEFAULT = "default"
SRC_CAL = "calibration"
SRC_MAX = "max(calibration,default)"
# occluded threshold raised to the global value: "global:<global source>", e.g. "global:calibration"
SRC_RAISED_PREFIX = "global:"


class ThresholdPolicy:
    def __init__(self, thresholds: dict[str, float], sources: dict[str, str], margin: float,
                 occluded_tags: frozenset[str], occluded_policy: str = "threshold",
                 notes: list[str] | None = None) -> None:
        for g in GROUPS:
            if g not in thresholds or g not in sources:
                raise ValueError(f"threshold group {g!r} missing")
        self._thr = {g: float(thresholds[g]) for g in GROUPS}
        self._src = {g: str(sources[g]) for g in GROUPS}
        self.margin = float(margin)
        self.occluded_tags = frozenset(occluded_tags)
        self.occluded_policy = occluded_policy
        self.notes = list(notes or [])

    @classmethod
    def from_sources(cls, rec_cfg: RecognitionSection, embedder_backend: str, embedder_model_id: str,
                     calibration: "CalibrationFile | None") -> "ThresholdPolicy":
        b = embedder_backend
        if b not in rec_cfg.default_thresholds or b not in rec_cfg.threshold_floor:
            raise ValueError(f"no default thresholds/floor configured for backend {b!r}")
        defaults = rec_cfg.default_thresholds[b]
        floor = float(rec_cfg.threshold_floor[b])
        notes: list[str] = []
        usable = calibration is not None
        if calibration is None:
            notes.append("no calibration file -> conservative defaults")
        elif calibration.score != "cosine":
            usable = False
            notes.append(f"calibration score {calibration.score!r} != cosine -> ignored")
        elif calibration.embedder_model_id != embedder_model_id:
            usable = False
            notes.append(f"calibration model_id {calibration.embedder_model_id!r} != current {embedder_model_id!r}"
                         " -> ignored")
            log.warning(notes[-1])
        thr: dict[str, float] = {}
        src: dict[str, str] = {}
        for g in GROUPS:
            D = float(defaults[g])
            gt = calibration.thresholds.get(g) if (usable and calibration is not None) else None
            if gt is None:
                thr[g], src[g] = D, SRC_DEFAULT
            elif gt.reliable:
                thr[g], src[g] = max(float(gt.threshold), floor), SRC_CAL
            elif rec_cfg.unreliable_calibration == "max_with_default":
                thr[g], src[g] = max(float(gt.threshold), D, floor), SRC_MAX
                notes.append(f"{g}: calibration not reliable -> max(cal={gt.threshold:.4f}, default={D:.4f})")
            else:
                thr[g], src[g] = D, SRC_DEFAULT
                notes.append(f"{g}: calibration not reliable and policy=ignore -> default")
        # rule 4: occluded may go below global only with reliable, real (non-synthetic) occluded calibration
        if thr["occluded"] < thr["global"]:
            occ = calibration.thresholds.get("occluded") if (usable and calibration is not None) else None
            if not (occ is not None and occ.reliable and not occ.synthetic_only):
                notes.append(f"occluded {thr['occluded']:.4f} < global {thr['global']:.4f} without reliable real "
                             "calibration -> raised to global")
                # label makes clear the occluded value was NOT used; global's value (and its origin) was
                thr["occluded"], src["occluded"] = thr["global"], f"{SRC_RAISED_PREFIX}{src['global']}"
        pol = cls(thr, src, rec_cfg.margin, frozenset(rec_cfg.occluded_tags), rec_cfg.occluded_policy, notes)
        log.info("threshold policy: %s", pol.describe())
        return pol

    def group_for(self, matched_tag: str) -> str:
        base = matched_tag[len("synthetic_"):] if matched_tag.startswith("synthetic_") else matched_tag
        return "occluded" if (matched_tag in self.occluded_tags or base in self.occluded_tags) else "global"

    def threshold_for(self, group: str) -> float:
        return self._thr[group]

    def source_for(self, group: str) -> str:
        return self._src[group]

    def describe(self) -> dict:
        return {"global": {"threshold": round(self._thr["global"], 4), "source": self._src["global"]},
                "occluded": {"threshold": round(self._thr["occluded"], 4), "source": self._src["occluded"]},
                "margin": self.margin, "occluded_tags": sorted(self.occluded_tags),
                "occluded_policy": self.occluded_policy, "notes": list(self.notes)}


class Recognizer:
    def __init__(self, db: FaceDatabase, policy: ThresholdPolicy, aggregation: str = "max", topk: int = 3) -> None:
        if aggregation not in ("max", "topk_mean"):
            raise ValueError(f"aggregation must be max|topk_mean, got {aggregation!r}")
        self.db = db
        self.policy = policy
        self.aggregation = aggregation
        self.topk = max(1, int(topk))
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the per-person index after the gallery changed."""
        self._M = self.db.matrix
        self._labels = np.array(self.db.labels, dtype=object)
        self._tags = list(self.db.tags)
        self._persons = self.db.persons()
        self._rows = {p: np.flatnonzero(self._labels == p) for p in self._persons}

    def score_all(self, embedding: np.ndarray) -> dict[str, tuple[float, str]]:
        if not self._persons:
            return {}
        e = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if e.shape[0] != self._M.shape[1]:
            raise ValueError(f"embedding dim {e.shape[0]} != gallery dim {self._M.shape[1]}")
        sims = self._M @ e
        out: dict[str, tuple[float, str]] = {}
        for p, rows in self._rows.items():
            s = sims[rows]
            j = int(np.argmax(s))
            if self.aggregation == "max":
                score = float(s[j])
            else:
                k = min(self.topk, len(s))
                score = float(np.mean(np.sort(s)[-k:]))
            out[p] = (score, self._tags[int(rows[j])])
        return out

    def identify(self, embedding: np.ndarray) -> MatchResult:
        allsc = self.score_all(embedding)
        pol = self.policy
        if not allsc:
            return MatchResult(None, None, 1.0, pol.threshold_for("global"), "global", pol.source_for("global"),
                               False, {})
        ranked = sorted(allsc.items(), key=lambda kv: kv[1][0], reverse=True)
        bp, (bs, bt) = ranked[0]
        best = MatchCandidate(bp, bs, bt)
        second = MatchCandidate(ranked[1][0], ranked[1][1][0], ranked[1][1][1]) if len(ranked) > 1 else None
        margin = bs - second.score if second is not None else 1.0
        group = pol.group_for(bt)
        thr = pol.threshold_for(group)
        accepted = bool(bs >= thr and margin >= pol.margin
                        and not (group == "occluded" and pol.occluded_policy == "deny"))
        return MatchResult(best, second, float(margin), thr, group, pol.source_for(group), accepted,
                           {p: float(v[0]) for p, v in allsc.items()})
