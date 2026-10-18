"""Probe scoring through the runtime FramePipeline, and score arrays for metrics (owner: eval-engineer).

Contract: docs/INTERFACES.md section 8 (ScoreRecord, score_probes, verification_scores, openset_scores)
and section 8.1 (scores.csv columns = ScoreRecord fields in order, ``scores`` -> ``scores_json``).

Records can come from three sources, all feeding the same metrics/report code:
1. ``score_probes``         - images through the real FramePipeline (same code as the door).
2. ``records_from_events``  - labelled rows of logs/events.csv (field use; only top-2 scores known).
3. ``evaluation.simulate``  - simulated scores (pipeline-free demo/testing; flagged as simulation).
"""

from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

import numpy as np

from facedoor.evaluation.dataset import IMPOSTOR_LABEL, ProbeSample

if TYPE_CHECKING:  # pragma: no cover
    from facedoor.pipeline import FramePipeline

log = logging.getLogger(__name__)

SCORED_VERDICTS = ("MATCH", "UNKNOWN")  # verdicts for which the system actually compared scores to thresholds


@dataclass(slots=True)
class ScoreRecord:
    probe_path: str
    condition: str
    is_synthetic: bool
    true_id: str            # person_id or "_impostor"
    split: str              # "cal" | "test"
    verdict: str            # FrameVerdict value
    hold_reason: str
    best_id: str
    best_score: float | None
    second_id: str
    second_score: float | None
    own_score: float | None          # genuine: score vs own identity; impostor: None
    max_other_score: float | None    # genuine: best score among other identities; impostor: overall best
    matched_tag: str
    threshold: float | None
    threshold_group: str
    accepted_id: str
    latency_ms: float
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def is_impostor(self) -> bool:
        return self.true_id == IMPOSTOR_LABEL

    @property
    def unit(self) -> str:
        """Bootstrap/split unit. Impostor records are grouped by their source folder when known."""
        if not self.is_impostor:
            return self.true_id
        p = Path(self.probe_path)
        return f"{IMPOSTOR_LABEL}:{p.parent.name}" if self.probe_path else IMPOSTOR_LABEL


CSV_COLUMNS: tuple[str, ...] = tuple(f.name if f.name != "scores" else "scores_json" for f in fields(ScoreRecord))


# --------------------------------------------------------------------------- derived fields
def fill_derived(rec: ScoreRecord) -> ScoreRecord:
    """Recompute own_score / max_other_score / best / second from ``rec.scores`` when it is non-empty."""
    if not rec.scores:
        return rec
    items = sorted(rec.scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if not rec.best_id:
        rec.best_id, rec.best_score = items[0][0], float(items[0][1])
    if not rec.second_id and len(items) > 1:
        rec.second_id, rec.second_score = items[1][0], float(items[1][1])
    if rec.is_impostor:
        rec.own_score = None
        rec.max_other_score = float(items[0][1])
    else:
        rec.own_score = float(rec.scores[rec.true_id]) if rec.true_id in rec.scores else None
        others = [v for k, v in items if k != rec.true_id]
        rec.max_other_score = float(max(others)) if others else None
    return rec


# --------------------------------------------------------------------------- pipeline scoring
def _enum_value(v: Any) -> str:
    if v is None:
        return ""
    return str(getattr(v, "value", v))


def _hold_scores(pipeline: Any, image: np.ndarray, det: Any) -> dict[str, float]:
    """Matcher scores for a HOLD frame (diagnostics only; HOLD frames never open the door)."""
    try:
        emb = pipeline.embedder.embed(image, det)
        return {k: float(v[0]) for k, v in pipeline.recognizer.score_all(emb).items()}
    except Exception as exc:  # pragma: no cover - diagnostic path only
        log.debug("could not score HOLD frame: %s", exc)
        return {}


def score_probes(pipeline: "FramePipeline", probes: Sequence[ProbeSample], split: Mapping[Path, str],
                 enrolled: Iterable[str] | None = None, score_hold: bool = True) -> list[ScoreRecord]:
    """Run every probe through ``pipeline.process_image`` and turn the RecognitionResult into a ScoreRecord.

    - ``split`` maps probe path -> "cal" | "test" (missing -> "test").
    - ``enrolled``: enrolled person ids. A genuine probe whose person is not enrolled is re-labelled as an
      impostor (it can only be a false accept) and a warning is logged.
    - ``score_hold``: for HOLD frames with a detection, also compute matcher scores (diagnostic; verdict stays HOLD).
    """
    from facedoor.imageio import imread  # vision module; unicode-safe

    enrolled_set = set(enrolled) if enrolled is not None else None
    if enrolled_set is None:
        db = getattr(pipeline.recognizer, "db", None)
        if db is not None and hasattr(db, "persons"):
            enrolled_set = set(db.persons())
    out: list[ScoreRecord] = []
    for i, p in enumerate(probes):
        true_id = p.label
        if true_id != IMPOSTOR_LABEL and enrolled_set is not None and true_id not in enrolled_set:
            log.warning("probe %s: person %r not enrolled -> treated as impostor", p.path, true_id)
            true_id = IMPOSTOR_LABEL
        img = imread(p.path)
        base = dict(probe_path=str(p.path), condition=p.condition, is_synthetic=p.is_synthetic,
                    true_id=true_id, split=split.get(p.path, "test"))
        if img is None:
            log.warning("unreadable probe image %s -> recorded as NO_FACE", p.path)
            out.append(ScoreRecord(**base, verdict="NO_FACE", hold_reason="unreadable", best_id="", best_score=None,
                                   second_id="", second_score=None, own_score=None, max_other_score=None,
                                   matched_tag="", threshold=None, threshold_group="", accepted_id="", latency_ms=float("nan")))
            continue
        res = pipeline.process_image(img, index=i)
        out.append(record_from_result(res, base, pipeline=pipeline if score_hold else None, image=img))
    return out


def record_from_result(res: Any, base: Mapping[str, Any], pipeline: Any = None, image: np.ndarray | None = None) -> ScoreRecord:
    """RecognitionResult -> ScoreRecord. ``base`` holds probe_path/condition/is_synthetic/true_id/split."""
    verdict = _enum_value(res.verdict)
    m = res.match
    scores: dict[str, float] = {}
    best_id = second_id = matched_tag = threshold_group = ""
    best_score = second_score = threshold = None
    if m is not None:
        scores = {str(k): float(v) for k, v in (m.scores or {}).items()}
        if m.best is not None:
            best_id, best_score, matched_tag = m.best.person_id, float(m.best.score), m.best.tag
        if m.second is not None:
            second_id, second_score = m.second.person_id, float(m.second.score)
        threshold, threshold_group = float(m.threshold), str(m.threshold_group)
    elif pipeline is not None and image is not None and res.detection is not None:
        scores = _hold_scores(pipeline, image, res.detection)
    accepted_id = best_id if verdict == "MATCH" else ""
    rec = ScoreRecord(**base, verdict=verdict, hold_reason=_enum_value(res.hold_reason),
                      best_id=best_id, best_score=best_score, second_id=second_id, second_score=second_score,
                      own_score=None, max_other_score=None, matched_tag=matched_tag, threshold=threshold,
                      threshold_group=threshold_group, accepted_id=accepted_id,
                      latency_ms=float((res.timings_ms or {}).get("total", float("nan"))), scores=scores)
    return fill_derived(rec)


# --------------------------------------------------------------------------- score arrays
def _usable(r: ScoreRecord, include_hold: bool) -> bool:
    return include_hold or r.verdict in SCORED_VERDICTS


def verification_scores(records: Sequence[ScoreRecord], include_hold: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """1:1 genuine = own_score; impostor = every (probe, other enrolled person) pair score. For ROC/DET/EER.
    HOLD/NO_FACE records are excluded unless ``include_hold`` (they never reach the threshold at runtime)."""
    gen: list[float] = []
    imp: list[float] = []
    for r in records:
        if not _usable(r, include_hold):
            continue
        if not r.is_impostor and r.own_score is not None:
            gen.append(r.own_score)
        if r.scores:
            imp.extend(v for k, v in r.scores.items() if k != r.true_id)
        elif r.max_other_score is not None:  # events-derived records only know the top-2
            imp.append(r.max_other_score)
    return np.asarray(gen, dtype=np.float64), np.asarray(imp, dtype=np.float64)


def openset_scores(records: Sequence[ScoreRecord], include_hold: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """genuine = own_score; impostor = max_other_score (best wrong identity per probe = door-opening risk).
    Used for threshold calibration."""
    gen: list[float] = []
    imp: list[float] = []
    for r in records:
        if not _usable(r, include_hold):
            continue
        if not r.is_impostor and r.own_score is not None:
            gen.append(r.own_score)
        if r.max_other_score is not None:
            imp.append(r.max_other_score)
    return np.asarray(gen, dtype=np.float64), np.asarray(imp, dtype=np.float64)


def impostor_source(records: Sequence[ScoreRecord], include_hold: bool = False) -> str:
    """"real" (only unenrolled-person probes), "cross" (only enrolled-vs-others), "real+cross", "none"."""
    real = any(r.is_impostor and r.max_other_score is not None and _usable(r, include_hold) for r in records)
    cross = any((not r.is_impostor) and r.max_other_score is not None and _usable(r, include_hold) for r in records)
    return {(True, True): "real+cross", (True, False): "real", (False, True): "cross"}.get((real, cross), "none")


# --------------------------------------------------------------------------- CSV I/O
def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return "" if not math.isfinite(v) else f"{v:.6f}"
    return str(v)


def write_scores_csv(path: Path, records: Sequence[ScoreRecord]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in records:
            row = []
            for f in fields(ScoreRecord):
                v = getattr(r, f.name)
                row.append(json.dumps({k: round(float(x), 6) for k, x in v.items()}, ensure_ascii=False)
                           if f.name == "scores" else _fmt(v))
            w.writerow(row)


def _opt(v: str) -> float | None:
    v = (v or "").strip()
    return float(v) if v else None


def read_scores_csv(path: Path) -> list[ScoreRecord]:
    out: list[ScoreRecord] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(ScoreRecord(
                probe_path=row["probe_path"], condition=row["condition"], is_synthetic=row["is_synthetic"] in ("1", "True", "true"),
                true_id=row["true_id"], split=row["split"], verdict=row["verdict"], hold_reason=row["hold_reason"],
                best_id=row["best_id"], best_score=_opt(row["best_score"]), second_id=row["second_id"],
                second_score=_opt(row["second_score"]), own_score=_opt(row["own_score"]),
                max_other_score=_opt(row["max_other_score"]), matched_tag=row["matched_tag"],
                threshold=_opt(row["threshold"]), threshold_group=row["threshold_group"], accepted_id=row["accepted_id"],
                latency_ms=_opt(row["latency_ms"]) if row["latency_ms"] else float("nan"),
                scores={k: float(v) for k, v in json.loads(row.get("scores_json") or "{}").items()}))
    return out


# --------------------------------------------------------------------------- events.csv
FRAME_EVENTS: tuple[str, ...] = ("frame", "open", "deny")   # INTERFACES 5: open/deny rows ARE the decision frame rows


def records_from_events(events_csv: Path, ignore_label: str = "_ignore") -> tuple[list[ScoreRecord], list[str]]:
    """Labelled frame rows of logs/events.csv -> ScoreRecords (split="test", is_synthetic=False).

    Frame rows are ``event=frame`` plus ``event=open``/``deny``: the logger writes the decision frame itself with
    event=open/deny instead of frame (INTERFACES 5), so skipping them would drop exactly the MATCH frames that
    opened the door and bias FAR downwards. Columns are read by name, so 33-column (v1.2) and 35-column (v1.3)
    files are both accepted (missing columns = "").

    Only the top-2 scores are logged, so own_score is known only when the labelled person is best or second,
    and verification (1:1) impostor scores are limited to max_other_score. Returns (records, warnings).
    """
    recs: list[ScoreRecord] = []
    warns: list[str] = []
    n_unlabelled = n_ignored = 0
    with open(events_csv, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, restval=""):
            if row.get("event") not in FRAME_EVENTS:
                continue
            label = (row.get("label") or "").strip()
            if not label:
                n_unlabelled += 1
                continue
            if label == ignore_label:
                n_ignored += 1
                continue
            scores: dict[str, float] = {}
            for k_id, k_sc in (("best_id", "best_score"), ("second_id", "second_score")):
                pid, sc = (row.get(k_id) or "").strip(), _opt(row.get(k_sc, ""))
                if pid and sc is not None:
                    scores[pid] = sc
            verdict = row.get("verdict", "")
            best_id = (row.get("best_id") or "").strip()
            rec = ScoreRecord(
                probe_path=f"events:{row.get('session_id', '')}:{row.get('frame_idx', '')}",
                condition=(row.get("condition_tag") or "live").strip() or "live", is_synthetic=False,
                true_id=label, split="test", verdict=verdict, hold_reason=row.get("hold_reason", ""),
                best_id=best_id, best_score=_opt(row.get("best_score", "")), second_id=(row.get("second_id") or "").strip(),
                second_score=_opt(row.get("second_score", "")), own_score=None, max_other_score=None,
                matched_tag=row.get("matched_tag", ""), threshold=_opt(row.get("threshold", "")),
                threshold_group=row.get("threshold_group", ""), accepted_id=best_id if verdict == "MATCH" else "",
                latency_ms=_opt(row.get("total_ms", "")) or float("nan"), scores=scores)
            recs.append(fill_derived(rec))
    if n_unlabelled:
        warns.append(f"events.csv: 라벨 없는 frame/open/deny 행 {n_unlabelled}개는 제외됨")
    if n_ignored:
        warns.append(f"events.csv: _ignore 라벨 행 {n_ignored}개 제외됨")
    if recs:
        warns.append("events.csv 기반 평가: 로그에는 상위 2명 점수만 있으므로 ROC/DET/EER는 부분 정보이고, "
                     "본인 점수가 상위 2위 밖이면 genuine 점수가 누락됨. 시스템 지표(FAR/FRR)는 판정 그대로 계산됨.")
    return recs, warns
