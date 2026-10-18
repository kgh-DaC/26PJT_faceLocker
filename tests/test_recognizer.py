"""Recognizer + ThresholdPolicy (INTERFACES sections 3.7 and 7.2)."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import unit
from facedoor.calibration import CalibrationFile, GroupThreshold
from facedoor.config import RecognitionSection
from facedoor.database import FaceDatabase
from facedoor.recognizer import Recognizer, ThresholdPolicy

MID = "sface:test0000000"
E = np.eye(8, dtype=np.float32)


def gt(thr: float, reliable: bool = False, synthetic_only: bool = False) -> GroupThreshold:
    return GroupThreshold(thr, 0.0, 0.1, 0.002, 100, 5000 if reliable else 100, "real", synthetic_only, reliable)


def cal(global_: GroupThreshold | None, occluded: GroupThreshold | None = None, model_id: str = MID,
        score: str = "cosine") -> CalibrationFile:
    return CalibrationFile(1, "2026-10-16T10:00:00+09:00", score, "sface", model_id, "max", 0.001, 0.05,
                           {"global": global_, "occluded": occluded})


def policy(c: CalibrationFile | None, **kw) -> ThresholdPolicy:
    return ThresholdPolicy.from_sources(RecognitionSection(**kw), "sface", MID, c)


# ------------------------------------------------------------------ threshold policy (section 7.2)
def test_no_calibration_uses_defaults():
    p = policy(None)
    assert p.threshold_for("global") == 0.50 and p.source_for("global") == "default"
    assert p.threshold_for("occluded") == 0.55 and p.source_for("occluded") == "default"


@pytest.mark.parametrize("c", [cal(gt(0.45, True), model_id="sface:other"), cal(gt(0.45, True), score="l2")])
def test_mismatched_calibration_ignored(c):
    p = policy(c)
    assert p.threshold_for("global") == 0.50 and p.source_for("global") == "default"


def test_reliable_calibration_can_lower_but_not_below_floor():
    assert policy(cal(gt(0.45, True))).threshold_for("global") == pytest.approx(0.45)
    p = policy(cal(gt(0.30, True)))
    assert p.threshold_for("global") == pytest.approx(0.40) and p.source_for("global") == "calibration"


def test_unreliable_calibration_only_raises():
    p = policy(cal(gt(0.45, False)))
    assert p.threshold_for("global") == 0.50 and p.source_for("global") == "max(calibration,default)"
    p = policy(cal(gt(0.62, False)))
    assert p.threshold_for("global") == pytest.approx(0.62)
    p = policy(cal(gt(0.45, False)), unreliable_calibration="ignore")
    assert p.threshold_for("global") == 0.50 and p.source_for("global") == "default"


def test_occluded_not_below_global_without_reliable_real_calibration():
    # synthetic-only occluded calibration cannot go below global
    p = policy(cal(gt(0.60, True), gt(0.45, True, synthetic_only=True)))
    assert p.threshold_for("occluded") == pytest.approx(0.60)
    assert p.source_for("occluded") == "global:calibration"
    # reliable real occluded may go below global
    p = policy(cal(gt(0.60, True), gt(0.45, True)))
    assert p.threshold_for("occluded") == pytest.approx(0.45)
    assert p.source_for("occluded") == "calibration"


def test_occluded_raised_to_global_without_occluded_calibration_is_labelled():
    # QA B-4: no occluded calibration, global raised by unreliable calibration -> label must say "global:..."
    p = policy(cal(gt(0.60, False), None))
    assert p.threshold_for("occluded") == pytest.approx(0.60)
    assert p.source_for("occluded") == "global:max(calibration,default)"
    assert p.source_for("global") == "max(calibration,default)"
    assert p.describe()["occluded"]["source"].startswith("global:")


def test_group_for_tags():
    p = policy(None)
    assert p.group_for("mask") == "occluded" and p.group_for("synthetic_scarf") == "occluded"
    assert p.group_for("glasses") == "global"


# ------------------------------------------------------------------ recognizer
@pytest.fixture
def db(tmp_path):
    d = FaceDatabase.load(tmp_path / "g", expected_model_id=MID)
    meta = {"model_id": MID}
    d.add("alice", "normal", E[0], None, meta)
    d.add("alice", "mask", unit(E[0] + E[1]), None, meta)
    d.add("bob", "normal", E[2], None, meta)
    return d


def test_identify_accepts_clear_match(db):
    r = Recognizer(db, policy(None)).identify(unit(E[0] + 0.1 * E[3]))
    assert r.best.person_id == "alice" and r.best.tag == "normal"
    assert r.second.person_id == "bob"
    assert r.accepted and r.threshold == 0.50 and r.threshold_group == "global"
    assert set(r.scores) == {"alice", "bob"}
    assert r.margin == pytest.approx(r.best.score - r.second.score)


def test_threshold_boundary_is_inclusive(db):
    rec = Recognizer(db, policy(None, margin=0.0))
    # cosine with E[0] exactly 0.5 (gallery alice/normal), others lower
    v = np.zeros(8, np.float32)
    v[0], v[4] = 0.5, np.sqrt(1 - 0.25)
    r = rec.identify(v)
    assert r.best.score == pytest.approx(0.5, abs=1e-6)
    thr = r.threshold
    r2 = Recognizer(db, ThresholdPolicy({"global": float(r.best.score), "occluded": 0.9},
                                        {"global": "default", "occluded": "default"}, 0.0,
                                        frozenset({"mask"}))).identify(v)
    assert r2.accepted  # score >= threshold (equality accepted)
    r3 = Recognizer(db, ThresholdPolicy({"global": float(r.best.score) + 1e-4, "occluded": 0.9},
                                        {"global": "default", "occluded": "default"}, 0.0,
                                        frozenset({"mask"}))).identify(v)
    assert not r3.accepted
    assert thr == 0.5


def test_below_threshold_is_rejected(db):
    r = Recognizer(db, policy(None)).identify(unit(E[0] + 2.0 * E[5]))
    assert r.best.person_id == "alice" and r.best.score < 0.5 and not r.accepted


def test_margin_rejects_ambiguous(db):
    # equally similar to alice and bob -> margin ~0 < 0.05
    r = Recognizer(db, policy(None)).identify(unit(E[0] + E[2]))
    assert r.best.score >= 0.5 and r.margin < 0.05 and not r.accepted


def test_occluded_group_and_deny_policy(db):
    probe = unit(E[0] + E[1] + 0.05 * E[6])  # best sample = alice/mask
    r = Recognizer(db, policy(None)).identify(probe)
    assert r.best.tag == "mask" and r.threshold_group == "occluded" and r.threshold == 0.55 and r.accepted
    r = Recognizer(db, policy(None, occluded_policy="deny")).identify(probe)
    assert not r.accepted


def test_single_person_margin_is_one(tmp_path):
    d = FaceDatabase.load(tmp_path / "g1", expected_model_id=MID)
    d.add("solo", "normal", E[0], None, {"model_id": MID})
    r = Recognizer(d, policy(None)).identify(E[0])
    assert r.second is None and r.margin == 1.0 and r.accepted


def test_empty_gallery_never_accepts(tmp_path):
    d = FaceDatabase.load(tmp_path / "empty", expected_model_id=MID)
    r = Recognizer(d, policy(None)).identify(E[0])
    assert r.best is None and not r.accepted


def test_topk_mean_aggregation(db):
    db.add("alice", "glasses", unit(E[0] + E[7]), None, {"model_id": MID})
    rec_max = Recognizer(db, policy(None), "max")
    rec_k = Recognizer(db, policy(None), "topk_mean", topk=2)
    s_max = rec_max.score_all(E[0])["alice"][0]
    s_k = rec_k.score_all(E[0])["alice"][0]
    assert s_max == pytest.approx(1.0) and s_k == pytest.approx((1.0 + 1 / np.sqrt(2)) / 2, abs=1e-5)
