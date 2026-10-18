"""Report artifacts: CSV/JSON, PNG charts and report.html (owner: eval-engineer).

This is the ONLY module in facedoor that imports matplotlib (INTERFACES 0). Charts use English labels so they
render on any machine (Raspberry Pi images often lack CJK fonts); the HTML carries the Korean explanations.

Chart conventions (data-visualisation principles):
- Okabe-Ito colour-blind-safe palette; genuine = blue, impostor = vermillion, consistently in every chart.
- Every axis states the quantity and unit/scale; FAR axes are logarithmic because the operating region is
  0.01%-1%. Zero rates cannot be drawn on a log axis and are clipped to 0.5/N (stated in the caption).
- Operating points (actual thresholds) and the target FAR are marked; sample sizes appear in legends.
- Synthetic conditions are drawn dashed/hatched and suffixed "(syn)" so they are never mistaken for real captures.
"""

from __future__ import annotations

import csv
import html
import json
import math
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from facedoor.evaluation import metrics as M  # noqa: E402
from facedoor.evaluation._util import cfg_raw  # noqa: E402
from facedoor.evaluation.locker_audit import INTEGRITY_KEYS  # noqa: E402
from facedoor.evaluation.scoring import ScoreRecord, openset_scores, verification_scores, write_scores_csv  # noqa: E402
from facedoor.evaluation.summary import compare_metrics, condition_order  # noqa: E402

OKABE = {"black": "#000000", "orange": "#E69F00", "sky": "#56B4E9", "green": "#009E73", "yellow": "#F0E442",
         "blue": "#0072B2", "vermillion": "#D55E00", "purple": "#CC79A7", "grey": "#7F7F7F"}
COND_COLORS = [OKABE[k] for k in ("blue", "orange", "green", "purple", "sky", "vermillion", "yellow", "grey")]
GENUINE_C, IMPOSTOR_C = OKABE["blue"], OKABE["vermillion"]
DPI = 120

METRIC_HELP: list[tuple[str, str, str]] = [
    ("FAR", "미등록자(impostor) 프레임이 누군가로 MATCH 된 비율", "가장 중요. 문이 타인에게 열릴 위험. 목표 ≤ target_far"),
    ("misID", "등록자 프레임이 다른 등록자로 MATCH 된 비율", "보안 사고(엉뚱한 사람으로 개방). FAR과 함께 본다"),
    ("FRR(system)", "등록자 프레임이 본인으로 MATCH 되지 않은 비율 (HOLD 포함)", "편의성. 사용자가 체감하는 거부율"),
    ("FRR(matcher)", "HOLD/NO_FACE를 뺀 FRR", "매칭 자체의 거부율. 품질 게이트 영향과 분리"),
    ("TAR", "1 - FRR(system)", "올바른 개방률"),
    ("HOLD율", "품질 미달/다중얼굴 등으로 판정 보류된 프레임 비율", "저조도·역광에서 높으면 의도된 동작(오개방보다 낫다)"),
    ("EER", "1:1 검증에서 FAR = FRR 이 되는 지점의 오류율", "모델 비교용. 운영점이 아님"),
    ("d′", "|μ_genuine − μ_impostor| / √((σ²_g + σ²_i)/2)", "점수 분리도. 클수록 좋음"),
    ("FAR 95% 상한", "FA 0건이면 3/N (Rule of three), 아니면 Clopper-Pearson 상한", "FA 0건이 'FAR 0'을 뜻하지 않음"),
    ("95% CI", "사람(단위) 부트스트랩 백분위 신뢰구간", "사람 수가 적으면 넓고 불안정"),
    ("ROC / DET", "임계값을 바꿔 가며 TAR-FAR, FRR-FAR 곡선", "임계값 선택 근거"),
    ("혼동행렬", "실제 사람(행) × 판정(열) 개수", "어떤 사람끼리 혼동되는지"),
]


# --------------------------------------------------------------------------- formatting
def _isnum(v: Any) -> bool:
    return isinstance(v, (int, float, np.floating)) and not isinstance(v, bool) and math.isfinite(float(v))


def pct(v: Any, nd: int = 2) -> str:
    return f"{float(v) * 100:.{nd}f}%" if _isnum(v) else "계산 불가"


def num(v: Any, nd: int = 4) -> str:
    return f"{float(v):.{nd}f}" if _isnum(v) else "계산 불가"


def ci_str(ci: Any) -> str:
    if isinstance(ci, (list, tuple)) and len(ci) == 2 and _isnum(ci[0]) and _isnum(ci[1]):
        return f"[{float(ci[0]) * 100:.1f}, {float(ci[1]) * 100:.1f}]%"
    return "—"


def _cond_label(cond: str) -> str:
    return f"{cond} (syn)" if cond.startswith("syn_") else cond


def _esc(s: Any) -> str:
    return html.escape(str(s))


# --------------------------------------------------------------------------- charts
def _clip_log(a: np.ndarray, n: int) -> np.ndarray:
    floor = 0.5 / max(n, 1)
    return np.maximum(a, floor)


def _main_threshold(metrics: dict) -> float | None:
    thr = metrics.get("thresholds", {}) or {}
    g = thr.get("global")
    if isinstance(g, dict) and _isnum(g.get("threshold")):
        return float(g["threshold"])
    if _isnum(g):
        return float(g)
    obs = (thr.get("observed") or {}).get("global") or []
    return float(obs[0]) if obs else None


def _occluded_threshold(metrics: dict) -> float | None:
    thr = metrics.get("thresholds", {}) or {}
    o = thr.get("occluded")
    if isinstance(o, dict) and _isnum(o.get("threshold")):
        return float(o["threshold"])
    if _isnum(o):
        return float(o)
    obs = (thr.get("observed") or {}).get("occluded") or []
    return float(obs[0]) if obs else None


def _conditions(records: Sequence[ScoreRecord]) -> list[str]:
    return condition_order({r.condition for r in records})


def plot_roc_det(records: Sequence[ScoreRecord], metrics: dict, out_dir: Path) -> tuple[Path, Path]:
    target = float(metrics.get("target_far") or 0.001)
    thr = _main_threshold(metrics)
    roc_path, det_path = out_dir / "roc.png", out_dir / "det.png"
    figs = {"roc": plt.subplots(figsize=(7.5, 5.5)), "det": plt.subplots(figsize=(7.5, 5.5))}
    any_curve = False
    series: list[tuple[str, Sequence[ScoreRecord], str, str, float]] = [("overall (all test)", records, OKABE["black"], "-", 2.4)]
    for j, c in enumerate(_conditions(records)):
        series.append((_cond_label(c), [r for r in records if r.condition == c], COND_COLORS[j % len(COND_COLORS)],
                       "--" if c.startswith("syn_") else "-", 1.4))
    n_imp_max = 1
    for label, recs, color, ls, lw in series:
        g, i = verification_scores(recs)
        if g.size == 0 or i.size == 0:
            continue
        any_curve = True
        n_imp_max = max(n_imp_max, i.size)
        _, far, tar = M.roc_curve(g, i)
        far_c = _clip_log(far, i.size)
        lab = f"{label}  (gen n={g.size}, imp n={i.size})"
        figs["roc"][1].plot(far_c, tar, color=color, ls=ls, lw=lw, label=lab, drawstyle="steps-post")
        figs["det"][1].plot(far_c, _clip_log(1.0 - tar, g.size), color=color, ls=ls, lw=lw, label=lab, drawstyle="steps-post")
        if thr is not None and recs is records:
            fa, fr = M.far_frr_at(g, i, thr)
            figs["roc"][1].plot([max(fa, 0.5 / i.size)], [1 - fr], "o", ms=9, mfc="none", mec=OKABE["vermillion"], mew=2,
                                label=f"operating point thr={thr:.3f}: FAR={fa:.2%}, TAR={1 - fr:.2%}", zorder=5)
            figs["det"][1].plot([max(fa, 0.5 / i.size)], [max(fr, 0.5 / g.size)], "o", ms=9, mfc="none",
                                mec=OKABE["vermillion"], mew=2, label=f"operating point thr={thr:.3f}", zorder=5)
    for key, (fig, ax) in figs.items():
        ax.set_xscale("log")
        ax.axvline(target, color=OKABE["grey"], ls=":", lw=1.2, label=f"target FAR = {target:.2%}")
        ax.set_xlabel("FAR - impostor pairs accepted (log scale)")
        ax.set_xlim(min(0.5 / n_imp_max, target / 2), 1.0)
        ax.grid(True, which="both", alpha=0.25)
        if key == "roc":
            ax.set_ylabel("TAR - genuine accepted (= 1 - FRR)")
            ax.set_ylim(0, 1.01)
            ax.set_title("ROC - 1:1 verification scores (frame level, HOLD excluded)")
        else:
            ax.set_yscale("log")
            ax.set_ylabel("FRR - genuine rejected (log scale)")
            ax.set_title("DET - FRR vs FAR (frame level, HOLD excluded)")
        if not any_curve:
            ax.text(0.5, 0.5, "not computable: need both genuine and impostor scores", transform=ax.transAxes,
                    ha="center", va="center", color=OKABE["vermillion"])
        ax.legend(fontsize=7, loc="lower right" if key == "roc" else "upper right", framealpha=0.9)
        fig.text(0.01, 0.005, "Zero rates are drawn at 0.5/N (log axes cannot show 0). Matcher-level curve; "
                 "system FAR/FRR (with margin & quality gate) are in the tables.", fontsize=6.5, color=OKABE["grey"])
        fig.tight_layout(rect=(0, 0.03, 1, 1))
    figs["roc"][0].savefig(roc_path, dpi=DPI)
    figs["det"][0].savefig(det_path, dpi=DPI)
    for fig, _ in figs.values():
        plt.close(fig)
    return roc_path, det_path


def plot_score_dist(records: Sequence[ScoreRecord], metrics: dict, out_dir: Path) -> Path:
    path = out_dir / "score_dist.png"
    g, i = openset_scores(records)
    _, vi = verification_scores(records)
    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    bins = np.linspace(-0.3, 1.0, 66)
    if vi.size:
        ax.hist(vi, bins=bins, density=True, histtype="stepfilled", alpha=0.15, color=OKABE["grey"],
                label=f"impostor, all 1:1 pairs (n={vi.size})")
    if i.size:
        ax.hist(i, bins=bins, density=True, histtype="stepfilled", alpha=0.35, color=IMPOSTOR_C, edgecolor=IMPOSTOR_C,
                label=f"impostor, best wrong identity per probe (n={i.size})")
    if g.size:
        ax.hist(g, bins=bins, density=True, histtype="stepfilled", alpha=0.35, color=GENUINE_C, edgecolor=GENUINE_C,
                label=f"genuine, own identity (n={g.size})")
    thr, othr = _main_threshold(metrics), _occluded_threshold(metrics)
    if thr is not None:
        ax.axvline(thr, color=OKABE["black"], lw=1.6, label=f"threshold global = {thr:.3f}")
    if othr is not None and (thr is None or abs(othr - thr) > 1e-9):
        ax.axvline(othr, color=OKABE["black"], lw=1.4, ls="--", label=f"threshold occluded = {othr:.3f}")
    dp = M.d_prime(g, i)
    ax.set_title(f"Score distribution (cosine, HOLD excluded)  d' (open-set) = {dp:.2f}" if _isnum(dp)
                 else "Score distribution (cosine, HOLD excluded)  d' not computable")
    ax.set_xlabel("cosine similarity (accept if score >= threshold)")
    ax.set_ylabel("density (each histogram integrates to 1)")
    if not (g.size or i.size):
        ax.text(0.5, 0.5, "no scored probes", transform=ax.transAxes, ha="center", color=OKABE["vermillion"])
    ax.legend(fontsize=7.5, loc="upper left")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def confusion_labels(records: Sequence[ScoreRecord]) -> tuple[list[str], list[str], list[str], list[str]]:
    persons = sorted({r.true_id for r in records if not r.is_impostor} | {r.accepted_id for r in records if r.accepted_id})
    rows = persons + (["_impostor"] if any(r.is_impostor for r in records) else [])
    cols = persons + ["Unknown", "HOLD"]
    true_ids = [r.true_id for r in records]
    preds = [r.accepted_id if r.verdict == "MATCH" else ("Unknown" if r.verdict == "UNKNOWN" else "HOLD") for r in records]
    return rows, cols, true_ids, preds


def plot_confusion(records: Sequence[ScoreRecord], out_dir: Path) -> tuple[Path, dict]:
    path = out_dir / "confusion.png"
    rows, cols, t, p = confusion_labels(records)
    labels = list(dict.fromkeys(rows + cols))
    full = M.confusion_matrix(t, p, labels) if records else np.zeros((len(labels), len(labels)), int)
    ri = [labels.index(x) for x in rows]
    ci_ = [labels.index(x) for x in cols]
    mat = full[np.ix_(ri, ci_)] if rows else np.zeros((0, len(cols)), int)
    fig_h = max(3.0, 0.45 * len(rows) + 1.8)
    fig_w = max(5.5, 0.6 * len(cols) + 2.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    if mat.size:
        row_sum = mat.sum(axis=1, keepdims=True)
        norm = np.divide(mat, row_sum, out=np.zeros_like(mat, dtype=float), where=row_sum > 0)
        ax.imshow(norm, cmap="Blues", vmin=0, vmax=1, aspect="auto")
        for a in range(mat.shape[0]):
            for b in range(mat.shape[1]):
                if mat[a, b]:
                    bad = (rows[a] == "_impostor" and b < len(cols) - 2) or (rows[a] != "_impostor" and b < len(cols) - 2 and cols[b] != rows[a])
                    ax.text(b, a, str(mat[a, b]), ha="center", va="center", fontsize=8,
                            color=OKABE["vermillion"] if bad else ("white" if norm[a, b] > 0.6 else "black"),
                            fontweight="bold" if bad else "normal")
    ax.set_xticks(range(len(cols)), cols, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(rows)), rows, fontsize=8)
    ax.set_xlabel("system decision\n(MATCH as person / Unknown / HOLD incl. no-face)", fontsize=8.5)
    ax.set_ylabel("true identity")
    ax.set_title("Confusion matrix (frame counts, colour = row share)\nred bold = wrong person opened (misID / FA)", fontsize=9.5)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path, {"rows": rows, "cols": cols, "matrix": mat.tolist()}


def plot_condition_bar(metrics: dict, out_dir: Path) -> Path:
    path = out_dir / "condition_bar.png"
    pc = metrics.get("per_condition", {}) or {}
    conds = list(pc.keys())
    keys = [("frr", "FRR(system)", OKABE["orange"]), ("hold_rate", "HOLD rate", OKABE["sky"]),
            ("misid_rate", "misID", OKABE["purple"]), ("far", "FAR", OKABE["vermillion"])]
    fig, ax = plt.subplots(figsize=(8.0, max(3.2, 0.8 * len(conds) + 1.4)))
    h = 0.8 / len(keys)
    y = np.arange(len(conds))
    for k, (key, lab, color) in enumerate(keys):
        vals, lo, hi = [], [], []
        for c in conds:
            v = pc[c].get(key)
            civ = (pc[c].get("ci") or {}).get(key) or [None, None]
            vv = float(v) if _isnum(v) else np.nan
            vals.append(vv)
            lo.append(vv - float(civ[0]) if _isnum(civ[0]) and _isnum(vv) else 0.0)
            hi.append(float(civ[1]) - vv if _isnum(civ[1]) and _isnum(vv) else 0.0)
        yy = y - 0.4 + h * (k + 0.5)
        v_arr = np.array(vals)
        bars = ax.barh(yy, np.nan_to_num(v_arr), height=h * 0.92, color=color, label=lab,
                       xerr=[np.maximum(lo, 0), np.maximum(hi, 0)], error_kw=dict(ecolor="#333333", lw=0.8, capsize=2))
        for b_, c, vv in zip(bars, conds, v_arr):
            if c.startswith("syn_"):
                b_.set_hatch("///")
                b_.set_edgecolor("white")
            if np.isnan(vv):
                ax.text(0.003, b_.get_y() + b_.get_height() / 2, f"{lab}: n/a", va="center", fontsize=6.5, color=OKABE["grey"])
            elif vv == 0:
                ax.plot([0], [b_.get_y() + b_.get_height() / 2], marker="|", color=color, ms=8, mew=2)
                ax.text(0.006, b_.get_y() + b_.get_height() / 2, f"{lab}: 0", va="center", fontsize=6.5, color=color)
    ylabels = [f"{_cond_label(c)}\n(gen {pc[c].get('n_genuine', 0)}, imp {pc[c].get('n_impostor', 0)})" for c in conds]
    ax.set_yticks(y, ylabels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, 1.0)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_xlabel("rate per frame (bars = 95% person-bootstrap CI; n/a = not computable)")
    ax.set_title("Per-condition error rates (test split; hatched = synthetic condition)")
    ax.grid(True, axis="x", alpha=0.25)
    ax.legend(fontsize=7.5, loc="lower right", ncols=2)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- tables / files
PC_COLUMNS = ("condition", "synthetic", "n_genuine", "n_impostor", "far", "far_ci_lo", "far_ci_hi", "far_upper95",
              "misid_rate", "misid_ci_lo", "misid_ci_hi", "frr", "frr_ci_lo", "frr_ci_hi", "frr_matcher", "tar",
              "hold_rate", "hold_ci_lo", "hold_ci_hi", "eer", "eer_threshold", "d_prime")


def per_condition_rows(metrics: dict) -> list[dict[str, Any]]:
    rows = []
    blocks = [("overall", metrics.get("overall") or {})] + list((metrics.get("per_condition") or {}).items())
    for cond, b in blocks:
        ci = b.get("ci") or {}

        def c(k: str, j: int) -> Any:
            v = (ci.get(k) or [None, None])[j]
            return v if _isnum(v) else None
        rows.append({
            "condition": cond, "synthetic": bool(b.get("synthetic")), "n_genuine": b.get("n_genuine"),
            "n_impostor": b.get("n_impostor"), "far": b.get("far"), "far_ci_lo": c("far", 0), "far_ci_hi": c("far", 1),
            "far_upper95": b.get("far_upper95"), "misid_rate": b.get("misid_rate"), "misid_ci_lo": c("misid_rate", 0),
            "misid_ci_hi": c("misid_rate", 1), "frr": b.get("frr"), "frr_ci_lo": c("frr", 0), "frr_ci_hi": c("frr", 1),
            "frr_matcher": b.get("frr_matcher"), "tar": b.get("tar"), "hold_rate": b.get("hold_rate"),
            "hold_ci_lo": c("hold_rate", 0), "hold_ci_hi": c("hold_rate", 1), "eer": b.get("eer"),
            "eer_threshold": b.get("eer_threshold"), "d_prime": b.get("d_prime")})
    return rows


def write_per_condition_csv(path: Path, metrics: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PC_COLUMNS)
        w.writeheader()
        for r in per_condition_rows(metrics):
            w.writerow({k: ("" if v is None or (isinstance(v, float) and not math.isfinite(v)) else
                            (f"{v:.6f}" if isinstance(v, float) else v)) for k, v in r.items()})


def _far_cell(b: dict) -> str:
    cnt = b.get("counts") or {}
    ni, fa = cnt.get("n_impostor", 0), cnt.get("i_fa", 0)
    if not ni:
        return "<span class='na'>계산 불가 (impostor 0)</span>"
    up = b.get("far_upper95")
    s = f"{pct(b.get('far'))} ({fa}/{ni})"
    if fa == 0:
        s += f"<br><small>0건, 95% 상한 {pct(up)}</small>"
    else:
        s += f"<br><small>95% 상한 {pct(up)}</small>"
    return s


def _row_html(name: str, b: dict) -> str:
    cnt = b.get("counts") or {}
    ci = b.get("ci") or {}
    syn = " <span class='tag'>합성</span>" if b.get("synthetic") or name.startswith("syn_") else ""
    ng = cnt.get("n_genuine", 0)
    low = " class='low'" if 0 < ng < 30 else ""
    return ("<tr>" f"<td>{_esc(name)}{syn}</td><td{low}>{ng}</td><td>{cnt.get('n_impostor', 0)}</td>"
            f"<td>{_far_cell(b)}</td>"
            f"<td>{pct(b.get('misid_rate'))} ({cnt.get('g_misid', 0)})<br><small>{ci_str(ci.get('misid_rate'))}</small></td>"
            f"<td>{pct(b.get('frr'))}<br><small>{ci_str(ci.get('frr'))}</small></td>"
            f"<td>{pct(b.get('frr_matcher'))}</td><td>{pct(b.get('hold_rate'))}<br><small>{ci_str(ci.get('hold_rate'))}</small></td>"
            f"<td>{pct(b.get('eer'))}<br><small>thr {num(b.get('eer_threshold'), 3)}</small></td>"
            f"<td>{num(b.get('d_prime'), 2)}</td></tr>")


TABLE_HEAD = ("<tr><th>조건</th><th>genuine n</th><th>impostor n</th><th>FAR (FA/N)</th><th>misID</th>"
              "<th>FRR(system)<br><small>95% CI</small></th><th>FRR(matcher)</th><th>HOLD율</th><th>EER (1:1)</th><th>d′</th></tr>")


def _thresholds_html(metrics: dict) -> str:
    thr = metrics.get("thresholds") or {}
    rows = []
    for g in ("global", "occluded"):
        v = thr.get(g)
        if isinstance(v, dict):
            rows.append(f"<tr><td>{g}</td><td>{num(v.get('threshold'))}</td><td>{_esc(v.get('source', ''))}</td></tr>")
        elif _isnum(v):
            rows.append(f"<tr><td>{g}</td><td>{num(v)}</td><td></td></tr>")
    obs = thr.get("observed") or {}
    extra = "".join(f"<li>{_esc(g)}: 실제 기록된 값 {', '.join(f'{x:.4f}' for x in vals)}</li>" for g, vals in obs.items())
    other = {k: v for k, v in thr.items() if k not in ("global", "occluded", "observed")}
    other_html = f"<pre>{_esc(json.dumps(M.nan_to_none(other), ensure_ascii=False, indent=1))}</pre>" if other else ""
    if not rows and not extra:
        return "<p>임계값 정보 없음</p>"
    return (f"<table><tr><th>그룹</th><th>임계값</th><th>source</th></tr>{''.join(rows)}</table>"
            f"<ul>{extra}</ul>{other_html}")


def _compare_html(metrics: dict, previous: dict | None) -> str:
    if not previous:
        return "<p>이전 리포트 없음 (비교 생략)</p>"
    rows = compare_metrics(metrics, previous)
    head = "<tr><th>조건</th>" + "".join(f"<th>{k}<br><small>이전 → 현재 (Δ)</small></th>"
                                       for k in ("far", "misid_rate", "frr", "frr_matcher", "hold_rate", "eer")) + "</tr>"
    body = []
    for r in rows:
        cells = []
        for k in ("far", "misid_rate", "frr", "frr_matcher", "hold_rate", "eer"):
            d = r[k + "_delta"]
            cls = "" if d is None or abs(d) < 1e-12 else (" class='worse'" if d > 0 else " class='better'")
            dtxt = "—" if d is None else f"{d * 100:+.2f}%p"
            cells.append(f"<td{cls}>{pct(r[k + '_prev'])} → {pct(r[k])} ({dtxt})</td>")
        body.append(f"<tr><td>{_esc(r['condition'])}</td>{''.join(cells)}</tr>")
    note = (f"<p>이전: {_esc(previous.get('created_at', '?'))}, data_origin={_esc(previous.get('data_origin', '?'))}. "
            "모든 지표는 낮을수록 좋다(빨강 = 악화, 초록 = 개선). 데이터·임계값이 다르면 직접 비교에 주의.</p>")
    return note + f"<table>{head}{''.join(body)}</table>"


LIMITATIONS = [
    "위조(스푸핑) 방어 없음: 2D RGB 웹캠 + 임베딩만으로는 사진·휴대폰 화면·영상으로 열릴 수 있다. 이 리포트의 FAR은 '다른 사람의 실제 얼굴'에 대한 값이며 위조 공격 성공률이 아니다.",
    "모든 수치는 프레임 단위(N-of-M 투표 전)다. 투표를 적용한 실제 오개방률은 더 낮지만, 연속 프레임은 상관이 커서 p³처럼 낮아지지 않는다.",
    "합성 조건(syn_*)은 실제 조명·마스크·안경을 단순 흉내 낸 것으로 실촬영 결과를 대체하지 못한다. 합성만으로 만든 보정은 reliable=false.",
    "미등록자(_impostor) probe가 없으면 등록자 간 교차 비교만으로 impostor 점수를 만들며, 이는 실제 미등록자보다 낙관적이다.",
    "표본이 3/target_far 보다 적으면 목표 FAR을 검증할 수 없다(FA 0건이어도 상한은 3/N).",
    "보정 데이터와 평가 데이터는 사람 단위로 분리했으며, 보고 수치는 평가(test) 분할에서만 계산했다.",
    "HOLD(품질 미달)는 시스템 FRR에 포함된다. 저조도/역광에서 HOLD가 높은 것은 오개방을 막기 위한 의도된 동작이다.",
]


def synthetic_banner(metrics: dict) -> str:
    """Top-of-report notice whenever synthetic (syn_*) probes are part of the evaluated data."""
    sc = metrics.get("sample_counts") or {}
    frac = sc.get("synthetic_fraction")
    if not _isnum(frac) or float(frac) <= 0:
        return ""
    real = [c for c, v in (sc.get("per_condition") or {}).items() if not (v or {}).get("synthetic")]
    if float(frac) >= 1.0 or not real:
        what = "평가 표본 전부가 합성 조건이다(실촬영 없음)."
    else:
        what = f"평가 표본의 {pct(frac, 1)}가 합성 조건이다."
    return ("<div class='synb'>합성 데이터 포함 — 참고용. " + what +
            " 합성 조건(syn_*)은 조명·마스크·안경을 단순 흉내 낸 것으로 실촬영 결과를 대체하지 못하며, "
            "합성 결과만으로 만든 임계값 보정은 reliable=false로 기록된다.</div>")


LOCKER_HELP: list[tuple[str, str, str]] = [
    ("wrong_locker_opens", "열린 사물함(hardware.log OPEN의 locker=)이 개방 사유의 locker= 또는 events.csv locker_id와 다름",
     "0이어야 함 (중대 오류: 올바른 사람인데 다른 사물함이 열림)"),
    ("extra_channel_on", "동시에 ON인 사물함 수가 max_concurrent_open을 넘은 행/구간 수", "0이어야 함"),
    ("unassigned_opens", "출력이 실제 ON 됐는데 locker_id가 빈칸이거나 설정에 없는 사물함", "0이어야 함"),
    ("watchdog_overruns", "OPEN→CLOSE/WATCHDOG 간격 > 절대상한 + 0.5초 (limit 모드 max_unlock_sec, timer 모드 max_open_sec; "
                          "hardware.log 필요)", "0이어야 함"),
    ("on_while_closed_overtime", "솔밸브 ON인데 문이 닫힘으로 계속 읽힌 시간 > unlock_timeout_sec + 0.5초 "
                                 "(limit 모드, hardware.log STATE 행 기준)", "0이어야 함 (timer 모드는 계산 대상 아님)"),
]
LOCKER_REPORT_HELP: list[tuple[str, str]] = [
    ("unlock_timeouts", "해제 후 문이 안 열려 재잠금된 횟수 (오인식/문 안 열림 지표)"),
    ("watchdog_relocks", "절대상한 워치독으로 잠긴 횟수 (비율이 높으면 max_unlock_sec 재검토)"),
    ("sensor_faults", "센서 읽기 실패로 SENSOR_FAULT가 된 횟수"),
    ("forced_opens", "해제 없이 문이 열린 횟수 (LOCKED_CLOSED→LOCKED_OPEN)"),
]
SUPPRESS_KO = {"no_locker_id": "배정 없음", "invalid_locker_id": "잘못된 locker_id", "unknown_locker": "설정에 없는 사물함",
               "shared_denied": "공유 금지", "other": "기타"}


def _fmt_s(v: Any) -> str:
    return "계산 불가" if v is None else f"{float(v):.2f}"


def _cnt(v: Any) -> str:
    return "계산 불가" if v is None else _esc(v)


def locker_banner(metrics: dict) -> str:
    """Red top-of-report banner when any integrity metric is non-zero (INTERFACES 13.8)."""
    lk = metrics.get("locker")
    if not isinstance(lk, dict):
        return ""
    integ = lk.get("integrity") or {}
    bad = [f"{k}={integ[k]}" for k in INTEGRITY_KEYS
           if isinstance(integ.get(k), (int, float)) and integ.get(k) > 0]
    if not bad:
        return ""
    return ("<div class='lockbad'>사물함 무결성 오류 — " + _esc(", ".join(bad)) +
            ". 로그상 사물함 라우팅/동시 개방/개방 시간 제한이 지켜지지 않았다. 원인을 찾기 전까지 운영을 중지할 것 "
            "(아래 '사물함 감사' 절의 사례 목록 참고). evaluate.py 종료 코드 1.</div>")


def locker_html(metrics: dict) -> str:
    lk = metrics.get("locker")
    if not isinstance(lk, dict):
        return ""
    integ = lk.get("integrity") or {}
    rows = []
    for k, desc, crit in LOCKER_HELP:
        if k == "on_while_closed_overtime" and integ.get(k) is None and                 (lk.get("params") or {}).get("mode_used") == "timer":
            continue          # timer mode has no limit switch: row would only confuse
        v = integ.get(k)
        cls = " class='lockcell'" if isinstance(v, (int, float)) and v > 0 else (" class='na'" if v is None else "")
        rows.append(f"<tr><td>{_esc(k)}</td><td{cls}>{_cnt(v)}</td><td>{_esc(desc)}</td><td>{_esc(crit)}</td></tr>")
    ok = lk.get("integrity_ok")
    n_checked = sum(1 for k, _d, _c in LOCKER_HELP if isinstance(integ.get(k), (int, float)))
    status = (f"<p class='lockok'>무결성 지표 {n_checked}개 모두 0 (로그상 라우팅·잠금 정상)</p>" if ok is True else
              "<p class='lockbad'>무결성 지표 중 0이 아닌 값이 있음</p>" if ok is False else
              "<p class='warnline'>일부 무결성 지표를 계산할 수 없음(입력 로그 부족) — 아래 주의 참고</p>")
    sup = lk.get("suppressed") or {}
    sup_txt = ", ".join(f"{SUPPRESS_KO.get(k, k)}({k}) {v}" for k, v in sup.items()) or "없음"
    rej = lk.get("rejected") or {}
    rej_txt = ", ".join(f"{k} {v}" for k, v in rej.items()) or "없음"
    crit = lk.get("critical_rejects") or {}
    crit_bad = any(crit.values()) or (lk.get("faults") or 0) > 0
    acts = lk.get("actual_opens") or {}
    per_open = ", ".join(f"{k}: {v}" for k, v in (lk.get("per_locker_opens") or {}).items()) or "없음"
    mr = lk.get("locker_misroute") or {}
    lab = (f"misid_locker_opens(다른 사람 사물함 개방) <b>{_cnt(lk.get('misid_locker_opens'))}</b> · "
           f"impostor_locker_opens(미등록자에게 개방) <b>{_cnt(lk.get('impostor_locker_opens'))}</b> "
           f"(라벨된 실제 개방 {lk.get('n_labelled_opens', 0)}건)")
    if not lk.get("n_labelled_opens"):
        lab += " — 라벨된 event=open 행이 없어 계산 불가 (scripts/label_events.py로 open 행 라벨 부여)"
    misroute = (f"locker_misroute_rate(probe 평가) {pct(lk.get('locker_misroute_rate'))} "
                f"({mr.get('n_misroute', 0)}/{mr.get('n_genuine', 0)}) — genuine probe가 다른 사물함 배정자로 MATCH 된 비율"
                if isinstance(mr, dict) and mr else "locker_misroute_rate: probe 평가 없음 또는 갤러리에 사물함 배정 없음 → 계산 불가")
    det = lk.get("details") or {}
    det_html = "".join(f"<li><b>{_esc(k)}</b><ul>{''.join(f'<li>{_esc(x)}</li>' for x in v)}</ul></li>"
                       for k, v in det.items() if v)
    det_html += "".join(f"<li>{_esc(x)}</li>" for x in (lk.get("label_incidents") or []))
    ex = "".join(f"<li>{_esc(x)}</li>" for x in (mr.get("examples") or [])) if isinstance(mr, dict) else ""
    warns = "".join(f"<li>{_esc(w)}</li>" for w in lk.get("warnings") or [])
    src = lk.get("sources") or {}
    params = lk.get("params") or {}
    rep_rows = "".join(f"<tr><td>{_esc(k)}</td><td>{_cnt(lk.get(k))}</td><td>{_esc(d)}</td></tr>"
                       for k, d in LOCKER_REPORT_HELP)
    od = lk.get("open_duration") or {}
    od_txt = ("해제→재잠금 시간(s): n={n} · p50 {p50} · p95 {p95} · max {mx}".format(
        n=od.get("n", 0), p50=_fmt_s(od.get("p50")), p95=_fmt_s(od.get("p95")), mx=_fmt_s(od.get("max")))
        if od.get("n") else "해제→재잠금 시간: 닫힘 기록 없음 → 계산 불가")
    causes = ", ".join(f"{k} {v}" for k, v in (lk.get("close_causes") or {}).items()) or "없음"
    v15_html = (f"<table><tr><th>보고 지표 (v1.5)</th><th>값</th><th>설명</th></tr>{rep_rows}</table>"
                f"<p>{_esc(od_txt)}<br>재잠금 원인(cause): {_esc(causes)}</p>")
    return f"""
<h2>1-2. 사물함 감사 (per_locker)</h2>
<p>입력: events.csv {_esc(src.get('events') or '없음')}{f" ({src.get('events_columns')}컬럼)" if src.get('events_columns') else ''} ·
hardware.log {_esc(src.get('hardware_log') or '없음')} · 모드 {_esc(params.get('mode_used') or '?')} ·
max_open_sec {_esc(params.get('max_open_sec'))} · max_unlock_sec {_esc(params.get('max_unlock_sec'))} ·
unlock_timeout_sec {_esc(params.get('unlock_timeout_sec'))} ·
max_concurrent_open {_esc(params.get('max_concurrent_open'))} · 설정 사물함 {_esc(params.get('configured_lockers') or '알 수 없음')}</p>
{status}
<table><tr><th>무결성 지표</th><th>값</th><th>정의</th><th>합격 기준</th></tr>{''.join(rows)}</table>
{v15_html}
<p>실제 개방: events.csv {_cnt(acts.get('events'))}건 · hardware.log {_cnt(acts.get('hardware_log'))}건 · 사물함별 {_esc(per_open)}</p>
<p>억제된 개방 결정(문 안 열림): {_esc(sup_txt)}<br>도어 거부({_esc(lk.get('rejected_source') or '-')}): {_esc(rej_txt)}
{"<br><span class='lockcell'>invariant/fault 거부 또는 FAULT 기록 있음 — 0이어야 함</span>" if crit_bad else ""}</p>
<p>{lab}</p>
<p>{misroute}</p>
{f"<details open><summary>사례 목록</summary><ul>{det_html}{ex}</ul></details>" if (det_html or ex) else ""}
{f"<div class='warn'><b>사물함 감사 주의</b><ul>{warns}</ul></div>" if warns else ""}
<p><small>이 지표는 "로그상 라우팅이 맞았는가"만 증명한다. 배선이 바뀌어 다른 문이 연결된 경우는 로그로 알 수 없으므로
사물함마다 실물 개방 확인(INTERFACES §13.9 시나리오 30)이 필요하다. 실제 개방 수는 event=open 행 수가 아니라
door_signal=1(억제·거부 제외) 또는 hardware.log OPEN 수로 센다.</small></p>
"""


def per_person_html(metrics: dict) -> str:
    pp = metrics.get("per_person") or {}
    if not pp:
        return ""
    body = "".join(
        f"<tr><td>{_esc(pid)}</td><td{' class=low' if b.get('low_sample') else ''}>{b.get('n_genuine', 0)}</td>"
        f"<td>{pct(b.get('frr'))}</td><td>{pct(b.get('frr_matcher'))}</td><td>{pct(b.get('hold_rate'))}</td>"
        f"<td>{pct(b.get('misid_rate'))}</td><td>{_esc(', '.join(b.get('conditions') or []))}</td></tr>"
        for pid, b in pp.items())
    return ("<h3>사람별 (test 분할, 실촬영 우선)</h3><p>관리 화면의 '보강 필요' 판정에 쓰인다(frr &gt; gui.weak_frr). "
            "사람 단위 분할이면 보정 분할에 들어간 사람은 이 표에 없다. 노란 칸 = genuine 30개 미만.</p>"
            "<table><tr><th>person</th><th>genuine n</th><th>FRR(system)</th><th>FRR(matcher)</th><th>HOLD율</th>"
            f"<th>misID</th><th>조건</th></tr>{body}</table>")


def render_html(metrics: dict, records: Sequence[ScoreRecord], previous: dict | None, images: dict[str, str], cfg: Any) -> str:
    sc = metrics.get("sample_counts") or {}
    origin = metrics.get("data_origin", "pipeline")
    syn = sc.get("synthetic_fraction")
    banner = ""
    if origin == "simulation":
        banner = ("<div class='sim'>합성/시뮬레이션 데이터 — 이 리포트의 점수는 실제 얼굴 이미지가 아니라 난수로 생성된 "
                  "시뮬레이션 점수다. 리포트 생성 경로 시연용이며, 아래 어떤 수치도 시스템 성능으로 인용하면 안 된다.</div>")
    banner = locker_banner(metrics) + banner + synthetic_banner(metrics)
    sample_line = (f"<p class='counts'><b>표본</b>: test 분할 genuine {sc.get('genuine', 0)}개 · 실제 미등록자 probe "
                   f"{sc.get('real_impostor', 0)}개 · open-set impostor 점수 {sc.get('impostor', 0)}개 "
                   f"(출처: {_esc(sc.get('impostor_source', '?'))}) · 등록자 {sc.get('persons', 0)}명 · "
                   f"보정 분할 {sc.get('records_cal', 0)}개 · <b>합성 비중 {pct(syn, 1)}</b> · 데이터 출처 <b>{_esc(origin)}</b></p>")
    warn_items = "".join(f"<li>{_esc(w)}</li>" for w in metrics.get("warnings", []))
    overall = metrics.get("overall") or {}
    pc = metrics.get("per_condition") or {}
    rows_real = "".join(_row_html(c, b) for c, b in pc.items() if not c.startswith("syn_"))
    rows_syn = "".join(_row_html(c, b) for c, b in pc.items() if c.startswith("syn_"))
    syn_table = (f"<h3>합성 조건 (syn_*) — 참고용, 실촬영 아님</h3><table>{TABLE_HEAD}{rows_syn}</table>" if rows_syn else "")
    ov_all = metrics.get("overall_including_synthetic")
    ov_all_row = _row_html("overall (합성 포함)", ov_all) if isinstance(ov_all, dict) else ""
    lat = metrics.get("latency_ms") or {}
    imgs = "".join(
        f"<figure><img src='{_esc(fn)}' alt='{_esc(cap)}'><figcaption>{_esc(cap)}</figcaption></figure>"
        for fn, cap in images.items())
    helps = "".join(f"<tr><td>{_esc(a)}</td><td>{_esc(b)}</td><td>{_esc(c)}</td></tr>" for a, b, c in METRIC_HELP)
    lims = "".join(f"<li>{_esc(x)}</li>" for x in LIMITATIONS)
    emb = metrics.get("embedder") or {}
    raw = cfg_raw(cfg)
    try:
        import yaml
        cfg_txt = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False) if raw else "(설정 정보 없음)"
    except Exception:  # pragma: no cover
        cfg_txt = json.dumps(raw, ensure_ascii=False, indent=1)
    title = "facedoor 인식 신뢰도·강건성 평가" + (" [합성/시뮬레이션]" if origin == "simulation" else "")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body{{font-family:"Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",sans-serif;max-width:1180px;margin:24px auto;padding:0 16px;color:#222;line-height:1.5}}
h1{{font-size:1.5em}} h2{{border-bottom:2px solid #0072B2;padding-bottom:4px;margin-top:2em}}
table{{border-collapse:collapse;margin:8px 0;font-size:.88em}} th,td{{border:1px solid #ccc;padding:4px 7px;text-align:right;vertical-align:top}}
th{{background:#eef3f8}} td:first-child,th:first-child{{text-align:left}}
.sim{{background:#D55E00;color:#fff;font-weight:bold;padding:12px 16px;border-radius:4px;font-size:1.05em}}
.synb{{background:#CC79A7;color:#fff;font-weight:bold;padding:10px 16px;border-radius:4px;margin:6px 0}}
.warn{{background:#fff4e5;border-left:5px solid #E69F00;padding:8px 16px}}
.counts{{background:#eef3f8;padding:8px 12px}}
.na{{color:#888}} .tag{{background:#CC79A7;color:#fff;border-radius:3px;padding:0 4px;font-size:.8em}}
.low{{background:#fff4e5}} .worse{{background:#fde0d6}} .better{{background:#d9f2e6}}
figure{{display:inline-block;margin:8px;vertical-align:top;max-width:560px}} figure img{{max-width:100%;border:1px solid #ddd}}
figcaption{{font-size:.85em;color:#555}} small{{color:#666}} pre{{background:#f6f6f6;padding:8px;overflow:auto;font-size:.8em}}
.lockbad{{background:#C00000;color:#fff;font-weight:bold;padding:12px 16px;border-radius:4px;font-size:1.1em;margin:6px 0}}
.lockok{{background:#d9f2e6;padding:6px 12px}} .lockcell{{background:#C00000;color:#fff;font-weight:bold}} .warnline{{background:#fff4e5;padding:6px 12px}}
</style></head><body>
{banner}
<h1>{_esc(title)}</h1>
<p>생성: {_esc(metrics.get('created_at', ''))} · 임베더: {_esc(emb.get('backend', '?'))} / {_esc(emb.get('model_id', '?'))} ·
목표 FAR: {pct(metrics.get('target_far'), 3)}</p>
{sample_line}
<div class="warn"><b>경고·주의 (먼저 읽을 것)</b><ul>{warn_items}</ul></div>

<h2>1. 요약 (test 분할, 프레임 단위)</h2>
<table>{TABLE_HEAD}{_row_html('overall', overall)}{ov_all_row}</table>
<p>지연시간(ms): 평균 {num(lat.get('mean'), 1)} · p50 {num(lat.get('p50'), 1)} · p95 {num(lat.get('p95'), 1)}</p>
{locker_html(metrics)}

<h2>2. 적용 임계값</h2>
{_thresholds_html(metrics)}

<h2>3. 조건별 표</h2>
<p>전체 평균은 약한 조건을 숨기므로 조건별로 따로 본다. 노란 칸 = genuine 30개 미만(신뢰구간 넓음). FAR은 해당 조건의 미등록자 probe가 있을 때만 계산된다.</p>
<table>{TABLE_HEAD}{rows_real}</table>
{syn_table}
{per_person_html(metrics)}

<h2>4. 그래프</h2>
{imgs}

<h2>5. 지표 설명</h2>
<table><tr><th>지표</th><th>정의</th><th>보안/운영 관점</th></tr>{helps}</table>

<h2>6. 이전 리포트와 비교</h2>
{_compare_html(metrics, previous)}

<h2>7. 한계·주의</h2>
<ul>{lims}</ul>

<h2>8. 설정 스냅샷</h2>
<details><summary>펼치기</summary><pre>{_esc(cfg_txt)}</pre></details>
</body></html>
"""


def build_report(out_dir: Path, records: Sequence[ScoreRecord], metrics: dict, cfg: Any,
                 previous_metrics: dict | None = None) -> Path:
    """Write every artifact of INTERFACES 8.1 into ``out_dir`` and return the report.html path.
    ``records`` are ALL scored records (cal + test) and go to scores.csv; charts/tables use the test split."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    test = [r for r in records if r.split == "test"] or list(records)
    write_scores_csv(out_dir / "scores.csv", records)
    plot_roc_det(test, metrics, out_dir)
    plot_score_dist(test, metrics, out_dir)
    _, conf = plot_confusion(test, out_dir)
    plot_condition_bar(metrics, out_dir)
    metrics = dict(metrics)
    metrics["confusion"] = conf
    if previous_metrics:
        metrics["comparison"] = compare_metrics(metrics, previous_metrics)
    with open(out_dir / "metrics.json", "w", encoding="utf-8") as fh:
        json.dump(M.nan_to_none(metrics), fh, ensure_ascii=False, indent=2)
    write_per_condition_csv(out_dir / "per_condition.csv", metrics)
    raw = cfg_raw(cfg)
    try:
        import yaml
        (out_dir / "config_snapshot.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception:  # pragma: no cover
        (out_dir / "config_snapshot.yaml").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    images = {
        "condition_bar.png": "조건별 FRR·HOLD·misID·FAR (95% CI). 빗금 = 합성 조건",
        "roc.png": "ROC: 1:1 검증 TAR vs FAR (로그). 원 = 실제 임계값 운영점",
        "det.png": "DET: FRR vs FAR (로그-로그)",
        "score_dist.png": "점수 분포: genuine(파랑) vs impostor(주황). 세로선 = 임계값",
        "confusion.png": "혼동행렬: 행 = 실제, 열 = 판정. 빨강 굵게 = 잘못 열림",
    }
    html_path = out_dir / "report.html"
    html_path.write_text(render_html(metrics, test, previous_metrics, images, cfg), encoding="utf-8")
    return html_path
