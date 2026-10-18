"""Simulated ScoreRecords (owner: eval-engineer).

Purpose: exercise metrics/calibration/report end-to-end without models, cameras or face images.
Every record is flagged ``is_synthetic=True`` and callers must pass ``data_origin="simulation"`` to
``summary.compute_metrics`` so the report shows the simulation banner. The numbers mean nothing about
the real system; per-condition score means are only rough guesses chosen to make weak conditions visible.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from facedoor.evaluation.dataset import IMPOSTOR_LABEL
from facedoor.evaluation.scoring import ScoreRecord, fill_derived

# condition -> (genuine mean, genuine sd, other-person mean, HOLD probability)
DEFAULT_PROFILES: dict[str, tuple[float, float, float, float]] = {
    "normal": (0.70, 0.08, 0.08, 0.03),
    "glasses": (0.64, 0.09, 0.09, 0.04),
    "noglasses": (0.68, 0.08, 0.08, 0.03),
    "lowlight": (0.56, 0.12, 0.11, 0.35),
    "backlight": (0.58, 0.11, 0.11, 0.25),
    "angle": (0.54, 0.12, 0.09, 0.30),
    "mask": (0.47, 0.10, 0.17, 0.05),
    "scarf": (0.52, 0.10, 0.14, 0.05),
}


def simulate_records(*, n_persons: int = 5, n_impostors: int = 8, probes_per_cell: int = 12,
                     impostor_probes_per_id: int = 10, conditions: Sequence[str] | None = None,
                     profiles: Mapping[str, tuple[float, float, float, float]] | None = None,
                     thresholds: Mapping[str, float] | None = None, margin: float = 0.05,
                     occluded_conditions: Sequence[str] = ("mask", "scarf"), seed: int = 42,
                     cal_fraction: float = 0.5) -> list[ScoreRecord]:
    """Simulated records for ``n_persons`` enrolled people (genuine probes per condition) and impostors
    (``_impostor`` = normal condition, plus ``_impostor_mask``-like probes for occluded conditions).
    Verdicts apply the runtime rule: best >= thr(group) and margin >= ``margin``; HOLD at random."""
    rng = np.random.default_rng(seed)
    prof = dict(DEFAULT_PROFILES)
    prof.update(profiles or {})
    conds = list(conditions or prof.keys())
    thr = {"global": 0.50, "occluded": 0.55}
    thr.update(thresholds or {})
    persons = [f"p{j:02d}" for j in range(1, n_persons + 1)]
    imp_ids = [f"x{j:02d}" for j in range(1, n_impostors + 1)]
    # person-level split (same rule as dataset.split_calibration by="person")
    cal_p = set(rng.permutation(persons)[: int(round(cal_fraction * n_persons))].tolist())
    cal_i = set(rng.permutation(imp_ids)[: int(round(cal_fraction * n_impostors))].tolist())
    out: list[ScoreRecord] = []

    def make(true_id: str, unit: str, cond: str, own_mean: float | None, own_sd: float, other_mean: float,
             hold_p: float, split: str, k: int) -> ScoreRecord:
        scores = {p: float(np.clip(rng.normal(other_mean, 0.085), -1, 1)) for p in persons}
        if own_mean is not None:
            scores[true_id] = float(np.clip(rng.normal(own_mean, own_sd), -1, 1))
        occluded = cond in occluded_conditions and rng.random() < 0.85
        tag = "mask" if occluded else "normal"
        group = "occluded" if occluded else "global"
        lat = float(rng.lognormal(np.log(45.0), 0.25))
        folder = IMPOSTOR_LABEL if true_id == IMPOSTOR_LABEL and cond == "normal" else (
            f"{IMPOSTOR_LABEL}_{cond}" if true_id == IMPOSTOR_LABEL else cond)
        path = f"sim/{folder}/{unit}/{k:03d}.jpg"
        base = dict(probe_path=path, condition=cond, is_synthetic=True, true_id=true_id, split=split, latency_ms=lat)
        if rng.random() < hold_p:
            return ScoreRecord(**base, verdict="HOLD", hold_reason="too_dark" if "light" in cond else "pose",
                               best_id="", best_score=None, second_id="", second_score=None, own_score=None,
                               max_other_score=None, matched_tag="", threshold=None, threshold_group="", accepted_id="",
                               scores=scores)
        items = sorted(scores.items(), key=lambda kv: -kv[1])
        (b_id, b_sc), (s_id, s_sc) = items[0], items[1] if len(items) > 1 else ("", None)
        mg = b_sc - s_sc if s_sc is not None else 1.0
        accepted = b_sc >= thr[group] and mg >= margin
        rec = ScoreRecord(**base, verdict="MATCH" if accepted else "UNKNOWN", hold_reason="",
                          best_id=b_id, best_score=b_sc, second_id=s_id, second_score=s_sc, own_score=None,
                          max_other_score=None, matched_tag=tag, threshold=thr[group], threshold_group=group,
                          accepted_id=b_id if accepted else "", scores=scores)
        return fill_derived(rec)

    for cond in conds:
        gm, gsd, om, hp = prof[cond]
        for p in persons:
            for k in range(probes_per_cell):
                out.append(make(p, p, cond, gm, gsd, om, hp, "cal" if p in cal_p else "test", k))
        imp_conds = ("normal",) if cond not in occluded_conditions else (cond,)
        if cond in imp_conds:
            for x in imp_ids:
                for k in range(impostor_probes_per_id):
                    out.append(make(IMPOSTOR_LABEL, x, cond, None, 0.0, om + 0.04, hp, "cal" if x in cal_i else "test", k))
    return out
