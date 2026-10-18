"""Pure metric functions (owner: eval-engineer). Contract: docs/INTERFACES.md section 8.

Conventions
- Scores are cosine similarities; acceptance is always ``score >= thr``.
- ``genuine`` = scores of a probe against its own identity, ``impostor`` = scores against someone else.
- FAR(thr) = mean(impostor >= thr), FRR(thr) = mean(genuine < thr), TAR = 1 - FRR.
- Empty inputs produce NaN (never a made-up number). Callers turn NaN into ``null`` + a warning.

No matplotlib, no scipy: everything is numpy + math so it can be unit-tested by hand.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

NAN = float("nan")


def _arr(x: Iterable[float] | np.ndarray) -> np.ndarray:
    a = np.asarray(list(x) if not isinstance(x, np.ndarray) else x, dtype=np.float64).ravel()
    return a[np.isfinite(a)]


# --------------------------------------------------------------------------- basic rates
def far_frr_at(genuine: np.ndarray, impostor: np.ndarray, thr: float) -> tuple[float, float]:
    """(FAR, FRR) at threshold ``thr`` with the rule ``score >= thr`` accepts. NaN for an empty side."""
    g, i = _arr(genuine), _arr(impostor)
    far = float(np.mean(i >= thr)) if i.size else NAN
    frr = float(np.mean(g < thr)) if g.size else NAN
    return far, frr


def _candidate_thresholds(g: np.ndarray, i: np.ndarray) -> np.ndarray:
    """Descending thresholds: +inf (accept nobody) followed by every distinct observed score."""
    uniq = np.unique(np.concatenate([g, i]))[::-1]
    return np.concatenate([[np.inf], uniq])


def _rates_at(g: np.ndarray, i: np.ndarray, thr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised FAR and TAR for many thresholds (sorted search, O((n+m) log)."""
    gs, is_ = np.sort(g), np.sort(i)
    # count of scores >= t  ==  n - (index of first element >= t)
    n_g_acc = g.size - np.searchsorted(gs, thr, side="left")
    n_i_acc = i.size - np.searchsorted(is_, thr, side="left")
    far = n_i_acc / i.size if i.size else np.full(thr.shape, np.nan)
    tar = n_g_acc / g.size if g.size else np.full(thr.shape, np.nan)
    return far.astype(np.float64), tar.astype(np.float64)


def roc_curve(genuine: np.ndarray, impostor: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(thresholds, far, tar), thresholds descending starting at +inf (far=tar=0); far/tar non-decreasing."""
    g, i = _arr(genuine), _arr(impostor)
    if g.size == 0 or i.size == 0:
        e = np.array([], dtype=np.float64)
        return e, e, e
    thr = _candidate_thresholds(g, i)
    far, tar = _rates_at(g, i, thr)
    return thr, far, tar


def det_curve(genuine: np.ndarray, impostor: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(thresholds, far, frr) on the same thresholds as roc_curve."""
    thr, far, tar = roc_curve(genuine, impostor)
    return thr, far, 1.0 - tar


def eer(genuine: np.ndarray, impostor: np.ndarray) -> tuple[float, float]:
    """(EER, threshold). Evaluated on observed thresholds: picks the threshold minimising |FAR-FRR|
    (ties -> the higher, i.e. more secure, threshold) and reports (FAR+FRR)/2 there. NaN if a side is empty."""
    g, i = _arr(genuine), _arr(impostor)
    if g.size == 0 or i.size == 0:
        return NAN, NAN
    thr = np.unique(np.concatenate([g, i]))[::-1]  # descending, finite only
    far, tar = _rates_at(g, i, thr)
    frr = 1.0 - tar
    diff = np.abs(far - frr)
    k = int(np.argmin(diff))  # first minimum in descending order = highest threshold
    return float((far[k] + frr[k]) / 2.0), float(thr[k])


def threshold_at_far(impostor: np.ndarray, target_far: float) -> float:
    """Smallest threshold with FAR(thr) <= target_far, i.e. at most floor(target_far * N) impostors accepted.

    With N impostors sorted descending s1 >= s2 >= ..., k = floor(target_far*N) allowed accepts,
    the threshold is s_{k+1} + 1e-6 (just above the (k+1)-th score). When k == 0 (sample too small to
    ever allow one accept) this equals max(impostor) + 1e-6. Raises ValueError on empty input.
    """
    i = _arr(impostor)
    if i.size == 0:
        raise ValueError("threshold_at_far: no impostor scores")
    if not 0.0 <= target_far < 1.0:
        raise ValueError("target_far must be in [0, 1)")
    s = np.sort(i)[::-1]
    k = int(math.floor(target_far * s.size + 1e-12))
    if k >= s.size:
        return float(s[-1])
    return float(s[k] + 1e-6)


def d_prime(genuine: np.ndarray, impostor: np.ndarray) -> float:
    """Decidability d' = |mu_g - mu_i| / sqrt((var_g + var_i) / 2), sample variances (ddof=1).
    NaN if either side has fewer than 2 scores or both variances are 0."""
    g, i = _arr(genuine), _arr(impostor)
    if g.size < 2 or i.size < 2:
        return NAN
    pooled = (np.var(g, ddof=1) + np.var(i, ddof=1)) / 2.0
    if pooled <= 0:
        return NAN
    return float(abs(g.mean() - i.mean()) / math.sqrt(pooled))


# --------------------------------------------------------------------------- uncertainty
def rule_of_three(n: int) -> float:
    """95% upper bound of a rate when 0 events were observed in n trials: 3/n (capped at 1). n<=0 -> 1.0."""
    if n <= 0:
        return 1.0
    return min(1.0, 3.0 / n)


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k), X ~ Bin(n, p), computed in log space (stable for n in the thousands)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    lp, lq = math.log(p), math.log1p(-p)
    lg_n1 = math.lgamma(n + 1)
    logs = [lg_n1 - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * lp + (n - j) * lq for j in range(k + 1)]
    m = max(logs)
    return min(1.0, math.exp(m) * sum(math.exp(x - m) for x in logs))


def _bisect(f: Callable[[float], float], lo: float, hi: float, iters: int = 100) -> float:
    """Root of a monotone function on [lo, hi] (f(lo) and f(hi) of opposite sign)."""
    flo = f(lo)
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact two-sided (1-alpha) binomial CI for k successes in n trials. n<=0 -> (0, 1)."""
    if n <= 0:
        return 0.0, 1.0
    if not 0 <= k <= n:
        raise ValueError("clopper_pearson: need 0 <= k <= n")
    a2 = alpha / 2.0
    lower = 0.0 if k == 0 else _bisect(lambda p: (1.0 - _binom_cdf(k - 1, n, p)) - a2, 0.0, 1.0)
    upper = 1.0 if k == n else _bisect(lambda p: _binom_cdf(k, n, p) - a2, 0.0, 1.0)
    return float(lower), float(upper)


def rate_upper95(k: int, n: int) -> float | None:
    """INTERFACES 7.1 far_upper95: 0 events -> Rule of three 3/n, otherwise Clopper-Pearson upper bound."""
    if n <= 0:
        return None
    if k == 0:
        return rule_of_three(n)
    return clopper_pearson(k, n, 0.05)[1]


def bootstrap_ci(stat: Callable[..., float], groups: Sequence[Any], iters: int, ci: float, seed: int) -> tuple[float, float]:
    """Percentile bootstrap over resampling units (e.g. persons). ``stat`` receives a list of resampled groups
    (with replacement, same count) and returns a float; NaN results are dropped. Fewer than 2 groups -> (nan, nan)."""
    if len(groups) < 2 or iters <= 0:
        return NAN, NAN
    rng = np.random.default_rng(seed)
    vals: list[float] = []
    n = len(groups)
    for _ in range(iters):
        idx = rng.integers(0, n, size=n)
        v = float(stat([groups[j] for j in idx]))
        if math.isfinite(v):
            vals.append(v)
    if not vals:
        return NAN, NAN
    lo_q, hi_q = (1.0 - ci) / 2.0, 1.0 - (1.0 - ci) / 2.0
    return float(np.quantile(vals, lo_q)), float(np.quantile(vals, hi_q))


def bootstrap_ratio_ci(num: Sequence[float], den: Sequence[float], iters: int, ci: float, seed: int) -> tuple[float, float]:
    """Fast vectorised cluster bootstrap for a pooled rate sum(num)/sum(den), resampling groups.
    ``num[j]``/``den[j]`` are the event count and trial count of group j. Fewer than 2 groups -> (nan, nan)."""
    nu, de = np.asarray(num, dtype=np.float64), np.asarray(den, dtype=np.float64)
    keep = de > 0
    nu, de = nu[keep], de[keep]
    if nu.size < 2 or iters <= 0:
        return NAN, NAN
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nu.size, size=(iters, nu.size))
    d = de[idx].sum(axis=1)
    r = nu[idx].sum(axis=1) / d
    lo_q, hi_q = (1.0 - ci) / 2.0, 1.0 - (1.0 - ci) / 2.0
    return float(np.quantile(r, lo_q)), float(np.quantile(r, hi_q))


# --------------------------------------------------------------------------- identification
def confusion_matrix(true_ids: Sequence[str], pred_ids: Sequence[str], labels: Sequence[str]) -> np.ndarray:
    """Counts with rows = true label, columns = predicted label, both indexed by ``labels``
    (which should include e.g. "_impostor" for rows and "Unknown", "HOLD" for columns).
    Raises ValueError for a value missing from ``labels`` so that silent drops cannot hide errors."""
    if len(true_ids) != len(pred_ids):
        raise ValueError("true_ids and pred_ids differ in length")
    index = {lab: j for j, lab in enumerate(labels)}
    if len(index) != len(labels):
        raise ValueError("labels contain duplicates")
    m = np.zeros((len(labels), len(labels)), dtype=np.int64)
    for t, p in zip(true_ids, pred_ids):
        if t not in index or p not in index:
            raise ValueError(f"label not in labels: true={t!r} pred={p!r}")
        m[index[t], index[p]] += 1
    return m


def system_counts(true_ids: Sequence[str], verdicts: Sequence[str], accepted_ids: Sequence[str],
                  impostor_label: str = "_impostor") -> dict[str, int]:
    """Frame-level system outcome counts (INTERFACES 8.1 definitions).

    genuine probe (true_id != impostor_label): own MATCH = true accept; MATCH as someone else = misid;
    HOLD/NO_FACE = hold; UNKNOWN = reject. impostor probe: MATCH (anyone) = false accept.
    """
    c = dict(n_genuine=0, g_match_self=0, g_misid=0, g_hold=0, g_unknown=0,
             n_impostor=0, i_fa=0, i_hold=0, i_unknown=0)
    for t, v, a in zip(true_ids, verdicts, accepted_ids):
        hold = v in ("HOLD", "NO_FACE")
        if t == impostor_label:
            c["n_impostor"] += 1
            if v == "MATCH":
                c["i_fa"] += 1
            elif hold:
                c["i_hold"] += 1
            else:
                c["i_unknown"] += 1
        else:
            c["n_genuine"] += 1
            if v == "MATCH" and a == t:
                c["g_match_self"] += 1
            elif v == "MATCH":
                c["g_misid"] += 1
            elif hold:
                c["g_hold"] += 1
            else:
                c["g_unknown"] += 1
    return c


def system_rates(counts: Mapping[str, int], count_hold_as_reject: bool = True) -> dict[str, float]:
    """FAR, FRR(system), FRR(matcher), TAR, HOLD rate, misid rate from ``system_counts``. NaN where undefined.

    FRR(system) = 1 - own-MATCH / n_genuine (HOLD counted as reject when count_hold_as_reject, else excluded).
    FRR(matcher) = same but HOLD/NO_FACE probes excluded from the denominator.
    """
    ng, ni = counts["n_genuine"], counts["n_impostor"]
    ok, hold = counts["g_match_self"], counts["g_hold"]
    nan = NAN

    def ratio(a: float, b: float) -> float:
        return a / b if b > 0 else nan

    frr_sys = ratio(ng - ok, ng) if count_hold_as_reject else ratio(ng - hold - ok, ng - hold)
    frr_matcher = ratio(ng - hold - ok, ng - hold)
    return {
        "far": ratio(counts["i_fa"], ni),
        "frr": frr_sys,
        "frr_matcher": frr_matcher,
        "tar": (1.0 - frr_sys) if math.isfinite(frr_sys) else nan,
        "hold_rate": ratio(hold + counts["i_hold"], ng + ni),
        "misid_rate": ratio(counts["g_misid"], ng),
    }


def percentiles(values: Sequence[float], qs: Sequence[float] = (50, 95)) -> dict[str, float]:
    v = _arr(values)
    out = {"mean": float(v.mean()) if v.size else NAN}
    for q in qs:
        out[f"p{int(q)}"] = float(np.percentile(v, q)) if v.size else NAN
    return out


def nan_to_none(obj: Any) -> Any:
    """Recursively replace NaN/inf floats (and numpy scalars) by JSON-safe values."""
    if isinstance(obj, dict):
        return {str(k): nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [nan_to_none(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [nan_to_none(v) for v in obj.tolist()]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj
