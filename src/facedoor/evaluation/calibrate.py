"""Threshold calibration on the calibration split only (owner: eval-engineer). Contract: INTERFACES 7.1 / 8.

Impostor scores = open-set "best wrong identity" per probe (unenrolled probes + enrolled probes vs others),
which is exactly the score that could open the door. The threshold is the smallest value with
FAR <= target_far on those scores (``metrics.threshold_at_far``). HOLD/NO_FACE probes are excluded because the
runtime never compares them with a threshold.

Whether the runtime may use a value lower than the conservative default is decided by ``reliable``
(enough real impostors, not synthetic-only) and enforced by vision's ThresholdPolicy (INTERFACES 7.2).
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np

from facedoor.calibration import CalibrationFile, GroupThreshold, SCHEMA_VERSION, is_reliable, min_impostors_for, now_iso
from facedoor.evaluation import metrics as M
from facedoor.evaluation._util import cfg_get
from facedoor.evaluation.scoring import SCORED_VERDICTS, ScoreRecord, impostor_source, openset_scores

log = logging.getLogger(__name__)

DEFAULT_OCCLUDED_CONDITIONS = ("mask", "scarf", "syn_mask", "syn_scarf")


def calibrate_group(records: Sequence[ScoreRecord], target_far: float, name: str, notes: list[str]) -> GroupThreshold | None:
    """GroupThreshold from ``records`` or None (with a note) when there are no impostor scores."""
    g, i = openset_scores(records)
    used = [r for r in records if r.verdict in SCORED_VERDICTS and (r.own_score is not None or r.max_other_score is not None)]
    if i.size == 0:
        notes.append(f"{name}: impostor 점수 없음 → 보정 불가(null, 런타임은 기본값 사용)")
        return None
    thr = M.threshold_at_far(i, target_far)
    k = int(np.sum(i >= thr))
    far = k / i.size
    frr = float(np.mean(g < thr)) if g.size else None
    synthetic_only = bool(used) and all(r.is_synthetic for r in used)
    src = impostor_source(records)
    reliable = is_reliable(int(i.size), target_far, synthetic_only, src)
    need = min_impostors_for(target_far)
    if i.size < need:
        notes.append(f"{name}: impostor 표본 {i.size} < 3/target_far={need} → reliable=false "
                     f"(임계값={thr:.4f}는 관측 최고 impostor 점수 바로 위 값에 가깝고 과적합 위험)")
    if synthetic_only:
        notes.append(f"{name}: 합성 조건만으로 보정됨 → reliable=false")
    if src == "cross":
        notes.append(f"{name}: 미등록자 probe 없이 등록자 교차 비교만 사용(낙관적)")
    if g.size == 0:
        notes.append(f"{name}: genuine 점수 없음 → FRR 계산 불가")
    return GroupThreshold(threshold=float(thr), far=float(far), frr=frr,
                          far_upper95=M.rate_upper95(k, int(i.size)), n_genuine=int(g.size), n_impostor=int(i.size),
                          impostor_source=src, synthetic_only=synthetic_only, reliable=reliable)


def calibrate(cal_records: Sequence[ScoreRecord], target_far: float, embedder: Any, cfg: Any, *,
              report_dir: str | None = None, data: dict | None = None) -> CalibrationFile:
    """Build a CalibrationFile from calibration-split records. ``embedder`` needs ``backend`` and ``model_id``."""
    recs = [r for r in cal_records if r.split == "cal"] or list(cal_records)
    if len(recs) != len(cal_records):
        log.warning("calibrate(): %d non-'cal' records ignored", len(cal_records) - len(recs))
    occl = set(cfg_get(cfg, "evaluation.occluded_conditions", DEFAULT_OCCLUDED_CONDITIONS) or ())
    notes: list[str] = []
    glob_recs = [r for r in recs if r.condition not in occl]
    occ_recs = [r for r in recs if r.condition in occl]
    thresholds: dict[str, GroupThreshold | None] = {
        "global": calibrate_group(glob_recs, target_far, "global", notes),
        "occluded": calibrate_group(occ_recs, target_far, "occluded", notes) if occ_recs else None,
    }
    if not occ_recs:
        notes.append("occluded: 가림 조건 probe 없음 → null (런타임은 기본값/global 이상 사용)")
    notes.append("FRR은 보정셋에서 margin·품질 게이트를 제외한 점수 기준(HOLD 제외). 시스템 FRR은 리포트 참고.")
    syn_frac = (sum(r.is_synthetic for r in recs) / len(recs)) if recs else None
    data_out = {
        "gallery_dir": str(cfg_get(cfg, "app.gallery_dir", "data/gallery")),
        "probe_dir": str(cfg_get(cfg, "app.probe_dir", "data/probe")),
        "split_by": cfg_get(cfg, "evaluation.split_by", "person"),
        "calibration_fraction": cfg_get(cfg, "evaluation.calibration_fraction", 0.5),
        "seed": cfg_get(cfg, "evaluation.seed", 42),
        "synthetic_fraction": None if syn_frac is None else round(syn_frac, 4),
        "n_records": len(recs),
    }
    if data:
        data_out.update(data)
    return CalibrationFile(
        schema_version=SCHEMA_VERSION, created_at=now_iso(), score="cosine",
        embedder_backend=str(getattr(embedder, "backend", "")), embedder_model_id=str(getattr(embedder, "model_id", "")),
        aggregation=str(cfg_get(cfg, "recognition.aggregation", "max")), target_far=float(target_far),
        margin=cfg_get(cfg, "recognition.margin", None), thresholds=thresholds, data=data_out,
        report_dir=report_dir, notes=notes)
