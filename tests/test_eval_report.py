"""metrics.json / report.html tests (owner: eval-engineer): synthetic share is stated, synthetic results are
marked as reference only (참고용), small samples are flagged, and non-computable metrics are null + reason."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from facedoor.evaluation.dataset import ProbeSample, split_calibration
from facedoor.evaluation.report import build_report
from facedoor.evaluation.scoring import ScoreRecord, fill_derived, read_scores_csv
from facedoor.evaluation.simulate import simulate_records
from facedoor.evaluation.summary import compute_metrics

CFG = {"evaluation": {"bootstrap_iters": 100}}


def rec(true_id: str, scores: dict[str, float], verdict: str, cond: str = "normal", syn: bool = False,
        accepted: str = "", path: str | None = None) -> ScoreRecord:
    r = ScoreRecord(probe_path=path or f"p/{cond}/{true_id}/x.jpg", condition=cond, is_synthetic=syn,
                    true_id=true_id, split="test", verdict=verdict, hold_reason="", best_id="", best_score=None,
                    second_id="", second_score=None, own_score=None, max_other_score=None, matched_tag="normal",
                    threshold=0.5, threshold_group="global", accepted_id=accepted, latency_ms=20.0, scores=scores)
    return fill_derived(r)


def mixed_records() -> list[ScoreRecord]:
    real = [rec("alice", {"alice": 0.8, "bob": 0.1}, "MATCH", accepted="alice"),
            rec("bob", {"alice": 0.2, "bob": 0.45}, "UNKNOWN"),
            rec("_impostor", {"alice": 0.3, "bob": 0.2}, "UNKNOWN", path="p/_impostor/x1/0.jpg")]
    syn = [replace(r, condition="syn_mask", is_synthetic=True, probe_path=r.probe_path.replace("normal", "syn_mask"))
           for r in real[:2]] * 3
    return real + syn


# --------------------------------------------------------------------------- metrics.json content
def test_synthetic_fraction_and_overall_uses_real_only() -> None:
    m = compute_metrics(mixed_records(), bootstrap_iters=50)
    sc = m["sample_counts"]
    assert sc["synthetic_fraction"] == pytest.approx(6 / 9)
    assert sc["per_condition"]["syn_mask"]["synthetic"] is True
    assert sc["per_condition"]["normal"]["synthetic"] is False
    assert m["overall"]["n_genuine"] == 2                     # real only
    assert m["overall_including_synthetic"]["n_genuine"] == 8
    assert any("실촬영 표본" in w for w in m["warnings"])


def test_all_synthetic_warns_reference_only() -> None:
    recs = simulate_records(n_persons=3, n_impostors=3, probes_per_cell=3, impostor_probes_per_id=3,
                            conditions=["normal", "mask"])
    m = compute_metrics(recs, bootstrap_iters=50, data_origin="simulation", split=None)
    assert m["sample_counts"]["synthetic_fraction"] == 1.0
    assert any("참고용" in w for w in m["warnings"])
    assert any("시뮬레이션" in w for w in m["warnings"])


def test_small_impostor_sample_flagged() -> None:
    m = compute_metrics(mixed_records(), target_far=0.001, bootstrap_iters=50)
    assert any("3/target_far = 3000" in w for w in m["warnings"])
    assert m["overall"]["far"] == 0.0 and m["overall"]["far_upper95"] == pytest.approx(1.0)  # 3/1 capped


def test_no_impostor_far_is_not_computable() -> None:
    recs = [r for r in mixed_records() if not r.is_impostor]
    m = compute_metrics(recs, bootstrap_iters=50)
    assert m["overall"]["far"] != m["overall"]["far"]   # NaN -> null in JSON
    assert any("FAR 계산 불가" in w for w in m["warnings"])


def test_single_person_warns_ci_not_computable() -> None:
    recs = [rec("alice", {"alice": 0.8}, "MATCH", accepted="alice") for _ in range(3)]
    m = compute_metrics(recs, bootstrap_iters=50)
    assert any("1명뿐" in w for w in m["warnings"])
    assert any("ROC/DET/EER" in w for w in m["warnings"])     # no impostor scores at all


# --------------------------------------------------------------------------- report artifacts
def test_build_report_marks_synthetic_reference_only(tmp_path) -> None:
    recs = mixed_records()
    m = compute_metrics(recs, bootstrap_iters=50)
    html_path = build_report(tmp_path, recs, m, CFG)
    for name in ("metrics.json", "per_condition.csv", "scores.csv", "roc.png", "det.png", "score_dist.png",
                 "confusion.png", "condition_bar.png", "report.html", "config_snapshot.yaml"):
        assert (tmp_path / name).is_file(), name
    html = html_path.read_text(encoding="utf-8")
    assert "합성 데이터 포함 — 참고용" in html
    assert "합성 비중 66.7%" in html
    assert "합성 조건 (syn_*) — 참고용" in html
    assert html.index("참고용") < html.index("1. 요약")       # stated before the results
    mj = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert mj["sample_counts"]["synthetic_fraction"] == pytest.approx(6 / 9)
    assert len(read_scores_csv(tmp_path / "scores.csv")) == len(recs)


def test_real_only_report_has_no_synthetic_banner(tmp_path) -> None:
    recs = [r for r in mixed_records() if not r.is_synthetic]
    html = build_report(tmp_path, recs, compute_metrics(recs, bootstrap_iters=50), CFG).read_text(encoding="utf-8")
    assert "합성 데이터 포함" not in html


def test_comparison_section(tmp_path) -> None:
    recs = mixed_records()
    m = compute_metrics(recs, bootstrap_iters=50)
    prev = json.loads(json.dumps(m, default=str))
    prev["overall"]["frr"] = 0.1
    html = build_report(tmp_path, recs, m, CFG, previous_metrics=prev).read_text(encoding="utf-8")
    assert "이전 → 현재" in html
    assert "comparison" in json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- split hygiene
def test_person_split_keeps_people_on_one_side(tmp_path) -> None:
    probes = [ProbeSample(tmp_path / f"{p}{k}.jpg", "normal", p, None, False, f"{p}{k}") for p in "abcdef" for k in range(3)]
    probes += [ProbeSample(tmp_path / f"x{j}{k}.jpg", "normal", None, f"x{j}", False, f"x{j}{k}")
               for j in range(4) for k in range(2)]
    cal, test = split_calibration(probes, 0.5, "person", 42)
    assert {p.unit for p in cal}.isdisjoint({p.unit for p in test})
    assert any(p.true_id is None for p in cal) and any(p.true_id is None for p in test)
    assert len(cal) + len(test) == len(probes)
