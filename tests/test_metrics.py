"""Hand-computed checks for facedoor.evaluation.metrics (owner: eval-engineer).

Acceptance rule everywhere: score >= threshold. FAR = impostor accepted / impostor, FRR = genuine rejected / genuine.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from facedoor.evaluation import metrics as M

# 5 genuine, 5 impostor scores used in several tests
G = np.array([0.9, 0.8, 0.7, 0.6, 0.4])
I = np.array([0.5, 0.3, 0.2, 0.1, 0.0])


# --------------------------------------------------------------------------- FAR / FRR
@pytest.mark.parametrize("thr, far, frr", [
    (0.55, 0.0, 0.2),   # impostor max 0.5 rejected; genuine 0.4 rejected
    (0.5, 0.2, 0.2),    # equality accepts (0.5 >= 0.5)
    (0.45, 0.2, 0.2),
    (0.4, 0.2, 0.0),    # 0.4 >= 0.4 accepted
    (0.0, 1.0, 0.0),
    (0.95, 0.0, 1.0),
])
def test_far_frr_at_known_values(thr: float, far: float, frr: float) -> None:
    assert M.far_frr_at(G, I, thr) == pytest.approx((far, frr))


def test_far_frr_empty_side_is_nan_not_zero() -> None:
    far, frr = M.far_frr_at(G, np.array([]), 0.5)
    assert math.isnan(far) and frr == pytest.approx(0.2)
    far, frr = M.far_frr_at([], I, 0.5)
    assert far == pytest.approx(0.2) and math.isnan(frr)


def test_far_frr_ignores_nan_scores() -> None:
    assert M.far_frr_at(np.array([0.9, np.nan]), np.array([0.1, np.nan]), 0.5) == (0.0, 0.0)


# --------------------------------------------------------------------------- ROC / DET
def test_roc_curve_known_points() -> None:
    thr, far, tar = M.roc_curve(G, I)
    assert thr[0] == np.inf and far[0] == 0.0 and tar[0] == 0.0
    assert np.all(np.diff(thr[1:]) < 0)                     # strictly descending observed scores
    assert np.all(np.diff(far) >= 0) and np.all(np.diff(tar) >= 0)
    assert far[-1] == 1.0 and tar[-1] == 1.0
    pts = {round(float(t), 6): (f, a) for t, f, a in zip(thr[1:], far[1:], tar[1:])}
    assert pts[0.6] == pytest.approx((0.0, 0.8))
    assert pts[0.5] == pytest.approx((0.2, 0.8))
    assert pts[0.4] == pytest.approx((0.2, 1.0))
    assert pts[0.0] == pytest.approx((1.0, 1.0))


def test_det_is_one_minus_tar() -> None:
    thr_r, far_r, tar = M.roc_curve(G, I)
    thr_d, far_d, frr = M.det_curve(G, I)
    np.testing.assert_array_equal(thr_r, thr_d)
    np.testing.assert_array_equal(far_r, far_d)
    np.testing.assert_allclose(frr, 1.0 - tar)


def test_roc_empty_side_returns_empty() -> None:
    thr, far, tar = M.roc_curve(G, [])
    assert thr.size == far.size == tar.size == 0


# --------------------------------------------------------------------------- EER
def test_eer_perfect_separation_is_zero() -> None:
    e, t = M.eer([0.9, 0.8], [0.1, 0.2])
    assert e == 0.0
    # highest threshold with FAR == FRR == 0 is the lowest genuine score
    assert t == pytest.approx(0.8)


def test_eer_known_overlap() -> None:
    # at thr 0.5 and 0.4: FAR = FRR = 0.2 -> tie broken towards the higher (more secure) threshold
    e, t = M.eer(G, I)
    assert e == pytest.approx(0.2)
    assert t == pytest.approx(0.5)


def test_eer_inverted_scores() -> None:
    # impostors score above every genuine: at thr 0.8 FAR = FRR = 1 -> EER 1 (worst possible)
    e, t = M.eer([0.1, 0.2], [0.8, 0.9])
    assert e == pytest.approx(1.0) and t == pytest.approx(0.8)


def test_eer_empty_nan() -> None:
    assert all(math.isnan(x) for x in M.eer([], I))


# --------------------------------------------------------------------------- threshold at FAR
def test_threshold_at_far_small_sample_is_above_max_impostor() -> None:
    thr = M.threshold_at_far(I, 0.001)       # floor(0.005) = 0 accepts allowed
    assert thr == pytest.approx(0.5 + 1e-6)
    assert M.far_frr_at(G, I, thr)[0] == 0.0


def test_threshold_at_far_allows_k_accepts() -> None:
    imp = np.linspace(0.0, 0.99, 100)        # 100 distinct scores
    thr = M.threshold_at_far(imp, 0.05)      # 5 accepts allowed -> just above the 6th highest
    far, _ = M.far_frr_at([], imp, thr)
    assert far == pytest.approx(0.05)
    assert M.far_frr_at([], imp, thr - 2e-6)[0] > 0.05   # it is the smallest such threshold


def test_threshold_at_far_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        M.threshold_at_far(np.array([]), 0.01)
    with pytest.raises(ValueError):
        M.threshold_at_far(I, 1.5)


# --------------------------------------------------------------------------- d', CIs
def test_d_prime_known_value() -> None:
    g, i = np.array([1.0, 3.0]), np.array([-1.0, 1.0])  # means 2 and 0, var(ddof=1) 2 and 2
    assert M.d_prime(g, i) == pytest.approx(2.0 / math.sqrt(2.0))
    assert math.isnan(M.d_prime([1.0], [0.0, 0.1]))


def test_rule_of_three() -> None:
    assert M.rule_of_three(300) == pytest.approx(0.01)
    assert M.rule_of_three(2) == 1.0
    assert M.rule_of_three(0) == 1.0


def test_clopper_pearson_known_values() -> None:
    lo, hi = M.clopper_pearson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(1 - 0.025 ** (1 / 10), abs=1e-6)   # 0.3085
    lo, hi = M.clopper_pearson(10, 10)
    assert hi == 1.0 and lo == pytest.approx(0.025 ** (1 / 10), abs=1e-6)
    lo, hi = M.clopper_pearson(5, 10)                                             # textbook 0.1871 .. 0.8129
    assert lo == pytest.approx(0.1871, abs=1e-3) and hi == pytest.approx(0.8129, abs=1e-3)
    assert M.clopper_pearson(0, 0) == (0.0, 1.0)


def test_rate_upper95_uses_rule_of_three_for_zero_events() -> None:
    assert M.rate_upper95(0, 1000) == pytest.approx(0.003)
    assert M.rate_upper95(5, 10) == pytest.approx(M.clopper_pearson(5, 10)[1])
    assert M.rate_upper95(0, 0) is None


def test_bootstrap_ci_contains_point_and_needs_two_groups() -> None:
    groups = [np.full(10, v) for v in (0.1, 0.2, 0.3, 0.4, 0.5)]
    stat = lambda gs: float(np.mean(np.concatenate(gs)))  # noqa: E731
    lo, hi = M.bootstrap_ci(stat, groups, 500, 0.95, 0)
    assert lo <= 0.3 <= hi and 0.1 <= lo < hi <= 0.5
    assert M.bootstrap_ci(stat, groups, 500, 0.95, 0) == (lo, hi)    # seeded -> reproducible
    assert all(math.isnan(x) for x in M.bootstrap_ci(stat, groups[:1], 100, 0.95, 0))


def test_bootstrap_ratio_ci() -> None:
    lo, hi = M.bootstrap_ratio_ci([1, 2, 3, 4], [10, 10, 10, 10], 2000, 0.95, 1)
    assert lo <= 0.25 <= hi
    assert all(math.isnan(x) for x in M.bootstrap_ratio_ci([1], [10], 100, 0.95, 0))


# --------------------------------------------------------------------------- confusion / system rates
def test_confusion_matrix_counts() -> None:
    labels = ["alice", "bob", "_impostor", "Unknown", "HOLD"]
    m = M.confusion_matrix(["alice", "alice", "bob", "_impostor"], ["alice", "bob", "HOLD", "Unknown"], labels)
    assert m[0, 0] == 1 and m[0, 1] == 1 and m[1, 4] == 1 and m[2, 3] == 1 and m.sum() == 4
    with pytest.raises(ValueError):
        M.confusion_matrix(["carol"], ["alice"], labels)


def test_system_counts_and_rates() -> None:
    true = ["a", "a", "a", "a", "b", "_impostor", "_impostor", "_impostor", "_impostor"]
    verd = ["MATCH", "MATCH", "HOLD", "UNKNOWN", "MATCH", "MATCH", "UNKNOWN", "HOLD", "NO_FACE"]
    acc = ["a", "b", "", "", "b", "a", "", "", ""]
    c = M.system_counts(true, verd, acc)
    assert c == dict(n_genuine=5, g_match_self=2, g_misid=1, g_hold=1, g_unknown=1,
                     n_impostor=4, i_fa=1, i_hold=2, i_unknown=1)
    r = M.system_rates(c, count_hold_as_reject=True)
    assert r["far"] == pytest.approx(1 / 4)
    assert r["misid_rate"] == pytest.approx(1 / 5)
    assert r["frr"] == pytest.approx(3 / 5)           # 5 genuine, 2 own MATCH
    assert r["frr_matcher"] == pytest.approx(2 / 4)   # HOLD excluded
    assert r["tar"] == pytest.approx(2 / 5)
    assert r["hold_rate"] == pytest.approx(3 / 9)
    assert M.system_rates(c, count_hold_as_reject=False)["frr"] == pytest.approx(2 / 4)


def test_system_rates_no_impostor_is_nan() -> None:
    r = M.system_rates(M.system_counts(["a"], ["MATCH"], ["a"]))
    assert math.isnan(r["far"]) and r["frr"] == 0.0


def test_nan_to_none() -> None:
    out = M.nan_to_none({"a": float("nan"), "b": [np.float32(0.5), np.int64(3), float("inf")], "c": np.bool_(True)})
    assert out == {"a": None, "b": [0.5, 3, None], "c": True}
