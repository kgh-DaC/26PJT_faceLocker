"""ScoreRecords -> metrics.json dict (owner: eval-engineer). Contract: docs/INTERFACES.md section 8.1.

Pure computation (no I/O, no matplotlib). Every undefined metric is NaN here and becomes ``null`` in JSON,
with the reason appended to ``warnings``.

Definitions (frame level, before N-of-M voting, actual thresholds as applied by the pipeline):
- FAR       = impostor probes that got MATCH (anyone) / impostor probes
- misid     = genuine probes that got MATCH as a different person / genuine probes
- FRR(sys)  = genuine probes not MATCHed as themselves / genuine probes (HOLD counted, count_hold_as_reject)
- FRR(match)= same, HOLD/NO_FACE excluded
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Sequence

import numpy as np

from facedoor.calibration import min_impostors_for, now_iso
from facedoor.evaluation import metrics as M
from facedoor.evaluation.scoring import ScoreRecord, impostor_source, openset_scores, verification_scores

MIN_GENUINE_PER_CONDITION = 30  # below this a per-condition FRR is flagged as "표본 부족"
RATE_KEYS = ("far", "frr", "frr_matcher", "hold_rate", "misid_rate")
KNOWN_CONDITIONS = ("normal", "glasses", "noglasses", "hat", "lowlight", "backlight", "angle", "mask", "scarf", "live")
# hat (INTERFACES 14.5): global threshold group, real captures only (no syn_hat). Measured per condition so that a
# FAR clearly above the global one can justify adding it to recognition.occluded_tags.


def per_person_metrics(records: Sequence[ScoreRecord], count_hold_as_reject: bool = True) -> dict[str, dict[str, Any]]:
    """Genuine frame rates per enrolled person (INTERFACES 14.2: GUI '보강 필요' uses ``frr`` > gui.weak_frr).
    Rates are NaN (-> null) when the person has no genuine record."""
    out: dict[str, dict[str, Any]] = {}
    for pid in sorted({r.true_id for r in records if not r.is_impostor}):
        sub = [r for r in records if r.true_id == pid]
        c = M.system_counts([r.true_id for r in sub], [r.verdict for r in sub], [r.accepted_id for r in sub])
        rates = M.system_rates(c, count_hold_as_reject)
        out[pid] = {"n_genuine": c["n_genuine"], "frr": rates["frr"], "frr_matcher": rates["frr_matcher"],
                    "hold_rate": rates["hold_rate"], "misid_rate": rates["misid_rate"],
                    "conditions": sorted({r.condition for r in sub}),
                    "low_sample": c["n_genuine"] < MIN_GENUINE_PER_CONDITION}
    return out


def condition_order(conds: Any) -> list[str]:
    """Real conditions first (normal first, then the documented order, then others alphabetically), then syn_*."""
    def key(c: str) -> tuple:
        base = c[4:] if c.startswith("syn_") else c
        rank = KNOWN_CONDITIONS.index(base) if base in KNOWN_CONDITIONS else len(KNOWN_CONDITIONS)
        return (c.startswith("syn_"), rank, c)
    return sorted(set(conds), key=key)


def _unit_counts(records: Sequence[ScoreRecord]) -> dict[str, dict[str, int]]:
    per: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in records:
        c = M.system_counts([r.true_id], [r.verdict], [r.accepted_id])
        for k, v in c.items():
            per[r.unit][k] += v
    return per


def _ci_block(records: Sequence[ScoreRecord], count_hold_as_reject: bool, iters: int, ci: float, seed: int) -> dict[str, list[float | None]]:
    per = list(_unit_counts(records).values())
    g = lambda k: [u.get(k, 0) for u in per]  # noqa: E731
    ng, ni = np.array(g("n_genuine")), np.array(g("n_impostor"))
    ok, hold, mis, fa, ihold = (np.array(g(k)) for k in ("g_match_self", "g_hold", "g_misid", "i_fa", "i_hold"))
    pairs = {
        "far": (fa, ni),
        "frr": ((ng - ok), ng) if count_hold_as_reject else ((ng - hold - ok), (ng - hold)),
        "frr_matcher": ((ng - hold - ok), (ng - hold)),
        "hold_rate": ((hold + ihold), (ng + ni)),
        "misid_rate": (mis, ng),
    }
    out: dict[str, list[float | None]] = {}
    for j, (k, (num, den)) in enumerate(pairs.items()):
        lo, hi = M.bootstrap_ratio_ci(num, den, iters, ci, seed + j)
        out[k] = [lo, hi]
    return out


def block_metrics(records: Sequence[ScoreRecord], *, count_hold_as_reject: bool = True, bootstrap_iters: int = 1000,
                  ci: float = 0.95, seed: int = 42) -> dict[str, Any]:
    """System rates + matcher curves summary for one subset of records."""
    counts = M.system_counts([r.true_id for r in records], [r.verdict for r in records], [r.accepted_id for r in records])
    rates = M.system_rates(counts, count_hold_as_reject)
    vg, vi = verification_scores(records)
    og, oi = openset_scores(records)
    e, e_thr = M.eer(vg, vi)
    real_imp = sum(1 for r in records if r.is_impostor)
    block: dict[str, Any] = dict(rates)
    block.update({
        "eer": e, "eer_threshold": e_thr, "d_prime": M.d_prime(vg, vi),
        "far_upper95": M.rate_upper95(counts["i_fa"], counts["n_impostor"]),
        "n_genuine": counts["n_genuine"], "n_impostor": counts["n_impostor"], "n_real_impostor": real_imp,
        "n_verification_genuine": int(vg.size), "n_verification_impostor": int(vi.size),
        "n_openset_impostor": int(oi.size), "d_prime_openset": M.d_prime(og, oi),
        "counts": counts,
        "ci": _ci_block(records, count_hold_as_reject, bootstrap_iters, ci, seed),
        "synthetic": bool(records) and all(r.is_synthetic for r in records),
    })
    return block


def compute_metrics(records: Sequence[ScoreRecord], *, target_far: float = 0.001, count_hold_as_reject: bool = True,
                    bootstrap_iters: int = 1000, ci: float = 0.95, seed: int = 42, data_origin: str = "pipeline",
                    embedder: dict | None = None, thresholds: dict | None = None,
                    warnings: Sequence[str] = (), split: str | None = "test") -> dict[str, Any]:
    """Full metrics.json dict for ``records`` (only ``split`` records are evaluated when split is given)."""
    warns: list[str] = []
    all_records = list(records)
    recs = [r for r in all_records if split is None or r.split == split]
    n_cal = sum(1 for r in all_records if r.split == "cal")

    if data_origin == "simulation":
        warns.append("시뮬레이션 데이터: 이 리포트의 모든 점수는 실제 얼굴 이미지가 아니라 난수로 생성한 합성/시뮬레이션 점수다. "
                     "리포트 생성 경로 시연용이며 시스템 성능을 나타내지 않는다.")
    real = [r for r in recs if not r.is_synthetic]
    syn_frac = (sum(1 for r in recs if r.is_synthetic) / len(recs)) if recs else float("nan")
    overall_src = real if real else recs
    if recs and not real:
        warns.append("실촬영 표본이 없어 전체(overall) 지표가 합성 조건만으로 계산됨(참고용). 실촬영 결과를 대체하지 못한다.")
    elif real and len(real) < len(recs):
        warns.append(f"전체(overall) 지표는 실촬영 표본({len(real)}개)만으로 계산. 합성 조건 포함 값은 overall_including_synthetic 참고.")

    kw = dict(count_hold_as_reject=count_hold_as_reject, bootstrap_iters=bootstrap_iters, ci=ci, seed=seed)
    overall = block_metrics(overall_src, **kw)
    overall_all = block_metrics(recs, **kw) if len(overall_src) != len(recs) else None

    conds = condition_order({r.condition for r in recs})
    per_condition: dict[str, Any] = {}
    per_counts: dict[str, Any] = {}
    for c in conds:
        sub = [r for r in recs if r.condition == c]
        b = block_metrics(sub, **kw)
        per_condition[c] = b
        per_counts[c] = {"genuine": b["n_genuine"], "impostor": b["n_impostor"],
                         "synthetic": b["synthetic"], "persons": len({r.true_id for r in sub if not r.is_impostor})}
        if b["n_impostor"] == 0:
            warns.append(f"조건 '{c}': 이 조건의 미등록자(impostor) probe가 없어 FAR 계산 불가 (misid_rate로 등록자 간 오인식만 확인)")
        if 0 < b["n_genuine"] < MIN_GENUINE_PER_CONDITION:
            warns.append(f"조건 '{c}': genuine 표본 {b['n_genuine']}개 < {MIN_GENUINE_PER_CONDITION} — FRR 신뢰구간이 넓음")

    # Global sample-size warnings (security first: FAR)
    need = min_impostors_for(target_far)
    ni = overall["n_impostor"]
    if ni == 0:
        warns.append("미등록자(_impostor) probe가 없어 시스템 FAR 계산 불가. 등록자 간 교차 비교(misid, open-set 점수)만 있으며 "
                     "실제 미등록자보다 낙관적이다.")
    elif ni < need:
        warns.append(f"impostor 표본 {ni}개 < 3/target_far = {need}: 목표 FAR {target_far:.3%}를 검증할 수 없음 "
                     f"(관측 FA 0건이어도 95% 상한 = {M.rule_of_three(ni):.2%}).")
    if overall["n_genuine"] == 0:
        warns.append("genuine probe가 없어 FRR/TAR 계산 불가")
    persons = {r.true_id for r in recs if not r.is_impostor}
    if len(persons) < 2:
        warns.append(f"등록자 표본이 {len(persons)}명뿐: 등록자 간 교차 impostor 점수가 없거나 매우 적고, 부트스트랩 CI 계산 불가/불안정")
    if overall["n_verification_impostor"] == 0 or overall["n_verification_genuine"] == 0:
        warns.append("1:1 genuine 또는 impostor 점수가 없어 ROC/DET/EER/d′ 계산 불가")
    src = impostor_source(recs)
    if src == "cross":
        warns.append("FAR 관련 점수 분포는 등록자 교차 비교만 사용(낙관적).")
    warns.append("모든 지표는 프레임 단위(N-of-M 투표 전)다. 투표 적용 시 실제 오개방률은 이보다 낮지만 프레임 간 상관 때문에 독립 가정만큼 낮아지지는 않는다.")
    warns.extend(warnings)

    lat = [r.latency_ms for r in recs if r.latency_ms is not None and math.isfinite(r.latency_ms)]
    latency = M.percentiles(lat) if lat else {"mean": float("nan"), "p50": float("nan"), "p95": float("nan")}
    if not lat:
        warns.append("지연시간 정보 없음(시뮬레이션/로그에 값 없음)")

    used_thr: dict[str, Any] = dict(thresholds or {})
    observed = defaultdict(set)
    for r in recs:
        if r.threshold is not None and r.threshold_group:
            observed[r.threshold_group].add(round(r.threshold, 6))
    used_thr["observed"] = {g: sorted(v) for g, v in observed.items()}

    out = {
        "schema_version": 1,
        "created_at": now_iso(),
        "data_origin": data_origin,
        "target_far": target_far,
        "embedder": embedder or {},
        "thresholds": used_thr,
        "sample_counts": {
            "genuine": sum(1 for r in recs if not r.is_impostor),
            "impostor": int(openset_scores(recs)[1].size),  # open-set impostor scores (real + cross)
            "real_impostor": sum(1 for r in recs if r.is_impostor),
            "synthetic_fraction": syn_frac,
            "persons": len(persons),
            "records_test": len(recs), "records_cal": n_cal,
            "impostor_source": src,
            "per_condition": per_counts,
        },
        "overall": overall,
        "overall_including_synthetic": overall_all,
        "per_condition": per_condition,
        "per_person": per_person_metrics(overall_src, count_hold_as_reject),
        "latency_ms": latency,
        "warnings": warns,
    }
    return out


def compare_metrics(current: dict, previous: dict) -> list[dict[str, Any]]:
    """Per-condition deltas (current - previous) for the main rates. Missing values -> None."""
    rows: list[dict[str, Any]] = []
    keys = ("far", "misid_rate", "frr", "frr_matcher", "hold_rate", "eer")
    pc, pp = current.get("per_condition", {}), previous.get("per_condition", {})
    for cond in ["overall"] + sorted(set(pc) | set(pp)):
        cur = current.get("overall") if cond == "overall" else pc.get(cond)
        prev = previous.get("overall") if cond == "overall" else pp.get(cond)
        row: dict[str, Any] = {"condition": cond}
        for k in keys:
            a = None if cur is None else cur.get(k)
            b = None if prev is None else prev.get(k)
            a = None if a is None or (isinstance(a, float) and math.isnan(a)) else a
            b = None if b is None or (isinstance(b, float) and math.isnan(b)) else b
            row[k] = a
            row[k + "_prev"] = b
            row[k + "_delta"] = None if a is None or b is None else a - b
        rows.append(row)
    return rows
