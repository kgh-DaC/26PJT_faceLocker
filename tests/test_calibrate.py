"""Calibration tests (owner: eval-engineer): reliable flag, split hygiene, and the INTERFACES 7.2 rule that a
calibration can always RAISE a threshold but may LOWER it only when it is reliable (enough real impostors)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from facedoor.calibration import CalibrationFile, GroupThreshold, load_calibration, min_impostors_for, save_calibration
from facedoor.config import RecognitionSection
from facedoor.evaluation.calibrate import calibrate
from facedoor.evaluation.scoring import ScoreRecord, fill_derived
from facedoor.recognizer import ThresholdPolicy

EMB = SimpleNamespace(backend="sface", model_id="sface:test0000")
CFG = {"evaluation": {"occluded_conditions": ["mask", "syn_mask"]}, "recognition": {"aggregation": "max", "margin": 0.05}}


def rec(true_id: str, scores: dict[str, float], *, cond: str = "normal", syn: bool = False, split: str = "cal",
        verdict: str = "UNKNOWN", path: str | None = None) -> ScoreRecord:
    r = ScoreRecord(probe_path=path or f"p/{cond}/{true_id}/x.jpg", condition=cond, is_synthetic=syn, true_id=true_id,
                    split=split, verdict=verdict, hold_reason="", best_id="", best_score=None, second_id="",
                    second_score=None, own_score=None, max_other_score=None, matched_tag="normal", threshold=0.5,
                    threshold_group="global", accepted_id="", latency_ms=10.0, scores=dict(scores))
    return fill_derived(r)


def population(n_imp: int, *, imp_mean: float = 0.15, syn: bool = False, cond: str = "normal", seed: int = 0,
               split: str = "cal") -> list[ScoreRecord]:
    rng = np.random.default_rng(seed)
    out = [rec("alice", {"alice": float(rng.normal(0.7, 0.05)), "bob": float(rng.normal(0.1, 0.05))},
               cond=cond, syn=syn, split=split) for _ in range(20)]
    out += [rec("_impostor", {"alice": float(rng.normal(imp_mean, 0.08)), "bob": float(rng.normal(imp_mean, 0.08))},
                cond=cond, syn=syn, split=split, path=f"p/_impostor/x{k % 50:02d}/{k}.jpg") for k in range(n_imp)]
    return out


# --------------------------------------------------------------------------- calibrate()
def test_small_sample_is_unreliable_with_note() -> None:
    cal = calibrate(population(40), 0.001, EMB, CFG)
    g = cal.thresholds["global"]
    assert g is not None and g.reliable is False
    assert g.n_impostor == 40 + 20          # 40 real impostors + 20 genuine probes' best-other (cross)
    assert g.impostor_source == "real+cross"
    assert g.far == 0.0 and g.far_upper95 == pytest.approx(3 / 60)
    assert any("reliable=false" in n for n in cal.notes)
    assert cal.thresholds["occluded"] is None   # no occluded probes
    assert cal.embedder_model_id == EMB.model_id and cal.score == "cosine"


def test_threshold_is_just_above_max_impostor_when_k_is_zero() -> None:
    recs = population(30)
    cal = calibrate(recs, 0.001, EMB, CFG)
    max_imp = max(r.max_other_score for r in recs)
    assert cal.thresholds["global"].threshold == pytest.approx(max_imp + 1e-6)


def test_enough_real_impostors_is_reliable() -> None:
    need = min_impostors_for(0.01)   # 300
    cal = calibrate(population(need), 0.01, EMB, CFG)
    g = cal.thresholds["global"]
    assert g.n_impostor >= need and g.reliable is True and g.synthetic_only is False
    assert g.far <= 0.01


def test_synthetic_only_is_never_reliable() -> None:
    cal = calibrate(population(400, syn=True, cond="syn_lowlight"), 0.01, EMB, CFG)
    g = cal.thresholds["global"]
    assert g.synthetic_only is True and g.reliable is False
    assert cal.data["synthetic_fraction"] == 1.0
    assert any("합성" in n for n in cal.notes)


def test_cross_only_impostors_flagged() -> None:
    recs = [r for r in population(0)]
    cal = calibrate(recs, 0.001, EMB, CFG)
    assert cal.thresholds["global"].impostor_source == "cross"
    assert any("교차" in n for n in cal.notes)


def test_calibrate_ignores_test_split() -> None:
    cal_recs = population(30, seed=1)
    test_recs = [rec("_impostor", {"alice": 0.95, "bob": 0.9}, split="test", path="p/_impostor/t/0.jpg")]
    cal = calibrate(cal_recs + test_recs, 0.001, EMB, CFG)
    assert cal.thresholds["global"].threshold < 0.9   # the 0.95 test impostor did not leak into calibration
    assert cal.data["n_records"] == len(cal_recs)


def test_hold_records_are_excluded() -> None:
    recs = population(30)
    recs.append(rec("_impostor", {"alice": 0.99}, verdict="HOLD", path="p/_impostor/h/0.jpg"))
    cal = calibrate(recs, 0.001, EMB, CFG)
    assert cal.thresholds["global"].threshold < 0.99


def test_occluded_group_uses_occluded_conditions() -> None:
    recs = population(30) + population(30, cond="mask", imp_mean=0.3, seed=2)
    cal = calibrate(recs, 0.001, EMB, CFG)
    occ = cal.thresholds["occluded"]
    assert occ is not None and occ.n_genuine == 20
    assert occ.threshold > cal.thresholds["global"].threshold


def test_calibration_roundtrip(tmp_path) -> None:
    cal = calibrate(population(40), 0.001, EMB, CFG)
    p = tmp_path / "calibration.json"
    save_calibration(p, cal)
    save_calibration(p, cal)                   # second write keeps a .bak
    back = load_calibration(p)
    assert back is not None and back.thresholds["global"].reliable is False
    assert back.thresholds["global"].threshold == pytest.approx(cal.thresholds["global"].threshold, abs=1e-6)
    assert (tmp_path / "calibration.json.bak").is_file()


# --------------------------------------------------------------------------- 7.2: only raise unless reliable
REC = RecognitionSection()   # sface default global 0.50, occluded 0.55, floor 0.40


def calfile(thr: float, reliable: bool, *, occ: float | None = None, occ_reliable: bool = False,
            synthetic_only: bool = False, model_id: str = EMB.model_id) -> CalibrationFile:
    def gt(t: float, r: bool) -> GroupThreshold:
        return GroupThreshold(threshold=t, far=0.0, frr=0.1, far_upper95=0.01, n_genuine=100,
                              n_impostor=5000 if r else 50, impostor_source="real+cross",
                              synthetic_only=synthetic_only, reliable=r)
    return CalibrationFile(schema_version=1, created_at="", score="cosine", embedder_backend="sface",
                           embedder_model_id=model_id, aggregation="max", target_far=0.001, margin=0.05,
                           thresholds={"global": gt(thr, reliable), "occluded": None if occ is None else gt(occ, occ_reliable)})


def policy(cal: CalibrationFile | None) -> ThresholdPolicy:
    return ThresholdPolicy.from_sources(REC, "sface", EMB.model_id, cal)


def test_no_calibration_uses_defaults() -> None:
    p = policy(None)
    assert p.threshold_for("global") == 0.50 and p.source_for("global") == "default"


def test_unreliable_lower_calibration_does_not_lower_threshold() -> None:
    p = policy(calfile(0.30, reliable=False))
    assert p.threshold_for("global") == pytest.approx(0.50)
    assert p.source_for("global") == "max(calibration,default)"


def test_unreliable_higher_calibration_raises_threshold() -> None:
    p = policy(calfile(0.62, reliable=False))
    assert p.threshold_for("global") == pytest.approx(0.62)


def test_reliable_calibration_may_lower_but_not_below_floor() -> None:
    assert policy(calfile(0.45, reliable=True)).threshold_for("global") == pytest.approx(0.45)
    assert policy(calfile(0.20, reliable=True)).threshold_for("global") == pytest.approx(0.40)   # floor


def test_occluded_cannot_go_below_global_without_reliable_real_calibration() -> None:
    p = policy(calfile(0.60, reliable=True, occ=0.52, occ_reliable=False))
    assert p.threshold_for("occluded") >= p.threshold_for("global")
    p = policy(calfile(0.60, reliable=True, occ=0.52, occ_reliable=True, synthetic_only=True))
    assert p.threshold_for("occluded") >= p.threshold_for("global")


def test_model_mismatch_ignores_calibration() -> None:
    p = policy(calfile(0.9, reliable=True, model_id="sface:other"))
    assert p.threshold_for("global") == 0.50 and p.source_for("global") == "default"


@pytest.mark.parametrize("seed", range(5))
def test_small_sample_calibration_never_lowers_runtime_threshold(seed: int) -> None:
    """End-to-end: calibrate() on a small (unreliable) sample whose ideal threshold is LOW (well separated
    impostors) -> the runtime policy stays at the conservative default."""
    cal = calibrate(population(60, imp_mean=-0.2, seed=seed), 0.001, EMB, CFG)
    assert cal.thresholds["global"].threshold < 0.50 and not cal.thresholds["global"].reliable
    p = policy(cal)
    assert p.threshold_for("global") >= 0.50
    assert p.threshold_for("occluded") >= 0.55
