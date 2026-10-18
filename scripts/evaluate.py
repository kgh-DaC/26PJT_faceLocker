"""Recognition reliability / robustness evaluation (owner: eval-engineer). Contract: docs/INTERFACES.md sections 8, 9.

    python scripts/evaluate.py --gallery data/gallery --probe data/probe [--target-far 0.001] [--out reports/]
                               [--write-calibration] [--events logs/events.csv] [--compare reports/<old>]
                               [--no-calibrate] [--config FILE] [--set k=v ...]

Procedure
1. Scan gallery/probes, check gallery<->probe leakage (sha256). Leaked probes are EXCLUDED and reported.
2. Split probes into calibration / test (evaluation.split_by, calibration_fraction, seed).
3. Score every probe with the runtime FramePipeline (same code as the door).
4. Calibrate thresholds on the calibration split only (target FAR) -> candidate calibration.
5. Re-score the TEST split with the ThresholdPolicy the runtime would build from that candidate
   (calibration may raise thresholds; it lowers them only when reliable) and report test metrics only.
6. Write reports/<YYYYmmdd-HHMMSS>/ (metrics.json, per_condition.csv, scores.csv, PNGs, report.html).
   models/calibration.json is written only with --write-calibration.

--no-calibrate: no split; every probe is scored once with the CURRENT runtime policy (re-evaluation of what the
door does now). --events adds labelled frame rows of logs/events.csv (field data, always "test"; frame/open/deny rows).

Locker audit (INTERFACES 13.8): with --events and/or --hardware-log logs/hardware.log, evaluation/locker_audit.py
checks per-locker routing. When per_locker data is present, metrics.json gets a ``locker`` key and report.html a
"사물함 감사" section. If any integrity metric (wrong_locker_opens, extra_channel_on, unassigned_opens,
watchdog_overruns) is non-zero the report is still written but the exit code is 1.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facedoor.errors import ConfigError, ExitCode, FaceDoorError  # noqa: E402

log = logging.getLogger("evaluate")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gallery", default=None, help="gallery dir (default: app.gallery_dir)")
    ap.add_argument("--probe", default=None, help="probe dir (default: app.probe_dir)")
    ap.add_argument("--target-far", type=float, default=None, help="target FAR (default: evaluation.target_far)")
    ap.add_argument("--out", default=None, help="reports root; a <YYYYmmdd-HHMMSS> sub-folder is created")
    ap.add_argument("--write-calibration", action="store_true",
                    help="write the calibration to app.calibration_file (otherwise it is left untouched)")
    ap.add_argument("--events", default=None, help="labelled logs/events.csv to include (frame rows with label)")
    ap.add_argument("--hardware-log", default=None,
                    help="logs/hardware.log for the per-locker integrity audit (INTERFACES 13.8)")
    ap.add_argument("--compare", default=None,
                    help="previous report dir (or metrics.json). Default: latest report under --out")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="no calibration split: evaluate all probes with the current runtime thresholds")
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    return ap.parse_args(argv)


def _report_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = root / stamp
    k = 1
    while out.exists():
        out = root / f"{stamp}-{k}"
        k += 1
    return out


def _find_previous(compare: str | None, out_root: Path) -> tuple[dict | None, str | None]:
    """--compare dir/metrics.json, else the newest <out_root>/*/metrics.json."""
    cand: Path | None = None
    if compare:
        p = Path(compare)
        cand = p / "metrics.json" if p.is_dir() else p
        if not cand.is_file():
            log.warning("--compare %s: metrics.json not found; comparison skipped", compare)
            return None, None
    elif out_root.is_dir():
        found = sorted(out_root.glob("*/metrics.json"), key=lambda q: q.parent.name)
        cand = found[-1] if found else None
    if cand is None:
        return None, None
    try:
        return json.loads(cand.read_text(encoding="utf-8")), str(cand.parent)
    except (OSError, ValueError) as exc:
        log.warning("cannot read previous metrics %s: %s", cand, exc)
        return None, None


def run(args: argparse.Namespace) -> int:
    from facedoor.calibration import save_calibration
    from facedoor.config import load_config
    from facedoor.evaluation.calibrate import calibrate
    from facedoor.evaluation.dataset import check_leakage, scan_gallery, scan_probes, split_calibration
    from facedoor.evaluation.locker_audit import integrity_failed
    from facedoor.evaluation.report import build_report
    from facedoor.evaluation.scoring import records_from_events, score_probes
    from facedoor.evaluation.summary import compute_metrics
    from facedoor.recognizer import ThresholdPolicy

    cfg = load_config(args.config, args.overrides)
    if args.gallery:
        cfg.app.gallery_dir = Path(args.gallery).resolve()
    if args.probe:
        cfg.app.probe_dir = Path(args.probe).resolve()
    ev = cfg.evaluation
    target_far = float(args.target_far if args.target_far is not None else ev.target_far)
    if not 0.0 < target_far < 1.0:
        raise ConfigError(f"--target-far must be in (0, 1), got {target_far}")
    out_root = Path(args.out).resolve() if args.out else cfg.app.reports_dir
    log.info("config: gallery=%s probe=%s target_far=%g split_by=%s cal_fraction=%s seed=%s out=%s",
             cfg.app.gallery_dir, cfg.app.probe_dir, target_far, ev.split_by, ev.calibration_fraction, ev.seed, out_root)

    warnings: list[str] = []
    records = []
    cal_file = None
    policy_desc: dict[str, Any] = {}
    embedder_info: dict[str, Any] = {}

    probes = scan_probes(cfg.app.probe_dir, ev.impostor_dir_name, ev.synthetic_prefix)
    if probes:
        from facedoor.pipeline import FramePipeline

        gallery = scan_gallery(cfg.app.gallery_dir)
        leaks = check_leakage(gallery, probes)
        if leaks:
            leaked = {p for _, p in leaks}
            for g, p in leaks:
                log.warning("LEAK: probe %s is identical to gallery %s -> excluded", p, g)
            warnings.append(f"누수: gallery와 동일한 probe {len(leaked)}개를 평가에서 제외함 "
                            f"(예: {sorted(str(p) for p in leaked)[:3]})")
            probes = [p for p in probes if p.path not in leaked]
        n_syn = sum(p.is_synthetic for p in probes)
        log.info("probes: %d (synthetic %d, impostor %d)", len(probes), n_syn, sum(p.true_id is None for p in probes))

        pipeline = FramePipeline.from_config(cfg)  # current runtime policy (existing calibration file)
        emb = pipeline.embedder
        embedder_info = {"backend": emb.backend, "model_id": emb.model_id}
        current_desc = pipeline.recognizer.policy.describe()
        log.info("embedder %s, current thresholds: %s", emb.model_id, current_desc)

        if args.no_calibrate:
            split = {p.path: "test" for p in probes}
            records = score_probes(pipeline, probes, split)
            policy_desc = dict(current_desc, policy="current runtime policy (--no-calibrate)")
            warnings.append("--no-calibrate: 보정 분할 없이 현재 런타임 임계값으로 전체 probe를 평가함")
        else:
            cal_p, test_p = split_calibration(probes, ev.calibration_fraction, ev.split_by, ev.seed)
            split = {p.path: "cal" for p in cal_p}
            split.update({p.path: "test" for p in test_p})
            log.info("split by %s: calibration %d, test %d", ev.split_by, len(cal_p), len(test_p))
            cal_records = score_probes(pipeline, cal_p, split) if cal_p else []
            if cal_records:
                cal_file = calibrate(cal_records, target_far, emb, cfg, data={
                    "gallery_dir": str(cfg.app.gallery_dir), "probe_dir": str(cfg.app.probe_dir),
                    "leaked_excluded": len(leaks)})
                for n in cal_file.notes:
                    log.info("calibration note: %s", n)
                new_policy = ThresholdPolicy.from_sources(cfg.recognition, emb.backend, emb.model_id, cal_file)
                pipeline.recognizer.policy = new_policy
                policy_desc = dict(new_policy.describe(), policy="policy built from this run's calibration (rule 7.2)",
                                   previous_runtime=current_desc,
                                   calibration_candidate={g: (t.to_dict() if t else None)
                                                          for g, t in cal_file.thresholds.items()})
            else:
                warnings.append("보정 분할이 비어 있음(사람/단위 수 부족) → 임계값 보정 불가, 현재 런타임 임계값으로 평가")
                policy_desc = dict(current_desc, policy="current runtime policy (calibration split empty)")
            test_records = score_probes(pipeline, test_p, split)
            records = cal_records + test_records
        if n_syn:
            warnings.append(f"합성 조건(syn_*) probe {n_syn}개 포함: 합성 결과는 참고용이며 실촬영 결과를 대체하지 못한다.")
    else:
        warnings.append(f"probe 이미지 없음: {cfg.app.probe_dir}")

    data_origin = "pipeline"
    if args.events:
        ev_path = Path(args.events)
        if not ev_path.is_file():
            raise ConfigError(f"--events file not found: {ev_path}")
        ev_records, ev_warns = records_from_events(ev_path)
        warnings.extend(ev_warns)
        if not ev_records:
            warnings.append(f"{ev_path}: 라벨이 붙은 frame 행이 없음 (scripts/label_events.py로 라벨 부여)")
        records = list(records) + ev_records
        data_origin = "pipeline+events" if probes else "events"
        if not policy_desc and ev_records:
            policy_desc = {"policy": "thresholds as logged in events.csv"}
        if not embedder_info:
            embedder_info = {"backend": cfg.embedder.backend, "model_id": "(events.csv embedder_id 참고)"}

    locker = _locker_audit(args, cfg, records, warnings)

    if not any(r.split == "test" for r in records):
        if locker is None:
            log.error("nothing to evaluate: no test probes and no labelled events (probe dir %s)", cfg.app.probe_dir)
            return int(ExitCode.ERROR)
        warnings.append("인식 평가 표본 없음(probe·라벨된 events 행 없음): 이 리포트는 사물함 감사만 유효하다.")
        data_origin = "audit-only"

    out_dir = _report_dir(out_root)
    previous, prev_dir = _find_previous(args.compare, out_root)
    if prev_dir:
        log.info("comparing with previous report %s", prev_dir)

    metrics = compute_metrics(records, target_far=target_far, count_hold_as_reject=ev.count_hold_as_reject,
                              bootstrap_iters=ev.bootstrap_iters, ci=ev.ci, seed=ev.seed, data_origin=data_origin,
                              embedder=embedder_info, thresholds=policy_desc, warnings=warnings, split="test")
    if prev_dir:
        metrics["previous_report"] = prev_dir
    if locker is not None:
        metrics["locker"] = locker
        if integrity_failed(locker):
            bad = {k: v for k, v in (locker.get("integrity") or {}).items() if v}
            metrics["warnings"].insert(0, f"사물함 무결성 지표가 0이 아님: {bad} — 로그상 사물함 라우팅 오류. 운영 중지 후 원인 확인")
    if cal_file is not None:
        cal_file.report_dir = str(out_dir)
        metrics["calibration"] = {"written": bool(args.write_calibration),
                                  "path": str(cfg.app.calibration_file) if args.write_calibration else None,
                                  "notes": cal_file.notes}
        if not args.write_calibration:
            metrics["warnings"].append("--write-calibration 없음: models/calibration.json은 변경하지 않음. "
                                       "보정 후보는 리포트 폴더의 calibration_candidate.json 참고.")
    html_path = build_report(out_dir, records, metrics, cfg, previous_metrics=previous)
    if cal_file is not None:
        save_calibration(out_dir / "calibration_candidate.json", cal_file)
        if args.write_calibration:
            save_calibration(cfg.app.calibration_file, cal_file)
            log.info("calibration written to %s", cfg.app.calibration_file)

    ov = metrics["overall"]

    def pct(v: Any) -> str:
        return "n/a" if v is None or v != v else f"{v:.2%}"
    log.info("overall (test): FAR=%s misID=%s FRR=%s HOLD=%s  genuine=%d impostor=%d",
             pct(ov.get("far")), pct(ov.get("misid_rate")), pct(ov.get("frr")), pct(ov.get("hold_rate")),
             ov.get("n_genuine", 0), ov.get("n_impostor", 0))
    for w in metrics["warnings"]:
        log.warning("%s", w)
    print(f"report: {html_path}")
    if locker is not None:
        integ = locker.get("integrity") or {}
        log.info("locker audit: %s  suppressed=%s rejected=%s misid_locker_opens=%s impostor_locker_opens=%s "
                 "locker_misroute_rate=%s", integ, locker.get("suppressed"), locker.get("rejected"),
                 locker.get("misid_locker_opens"), locker.get("impostor_locker_opens"), locker.get("locker_misroute_rate"))
        if integrity_failed(locker):
            log.error("LOCKER INTEGRITY FAILURE: %s (exit code 1)", {k: v for k, v in integ.items() if v})
            return int(ExitCode.ERROR)
    return int(ExitCode.OK)


def _configured_lockers(cfg: Any) -> list[str] | None:
    from facedoor.evaluation._util import cfg_get

    outs = cfg_get(cfg, "hardware.locker_outputs", None)
    if not outs:
        return None
    ids = []
    for o in outs:
        lid = o.get("locker_id") if isinstance(o, dict) else getattr(o, "locker_id", None)
        if lid is not None:
            ids.append(str(lid))
    return ids or None


def _opt_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _locker_audit(args: argparse.Namespace, cfg: Any, records: list, warnings: list[str]) -> dict | None:
    """INTERFACES 13.8 audit. Returns the metrics.json ``locker`` dict, or None when there is no per_locker data
    (single mode: the key is omitted; a single-mode watchdog overrun is still reported as a warning)."""
    from facedoor.evaluation._util import cfg_get
    from facedoor.evaluation.locker_audit import audit_lockers, gallery_lockers, locker_misroute

    mode = cfg_get(cfg, "lockers.mode", "single")
    hw_path = None
    if args.hardware_log:
        hw_path = Path(args.hardware_log)
        if not hw_path.is_file():
            raise ConfigError(f"--hardware-log file not found: {hw_path}")
    ev_path = Path(args.events) if args.events else None
    if ev_path is None and hw_path is None and mode != "per_locker":
        return None
    max_open = cfg_get(cfg, "hardware.max_open_sec", None)
    audit = audit_lockers(ev_path, hw_path, max_open_sec=float(max_open) if max_open is not None else None,
                          max_concurrent_open=cfg_get(cfg, "hardware.max_concurrent_open", None),
                          configured_lockers=_configured_lockers(cfg), mode=mode,
                          max_unlock_sec=_opt_float(cfg_get(cfg, "hardware.max_unlock_sec", None)),
                          unlock_timeout_sec=_opt_float(cfg_get(cfg, "hardware.unlock_timeout_sec", None)),
                          close_mode=cfg_get(cfg, "hardware.close_mode", None))
    if not audit["per_locker_data"]:
        wd = (audit.get("integrity") or {}).get("watchdog_overruns")
        if wd:
            warnings.append(f"hardware.log: 개방 시간 초과(watchdog_overruns) {wd}건 — 절대상한(max_unlock_sec 또는 max_open_sec)+0.5초를 넘은 개방. "
                            + "; ".join((audit.get("details") or {}).get("watchdog_overruns", [])[:3]))
        log.info("locker audit: no per_locker data (single mode) -> metrics.json 'locker' key omitted")
        return None
    test = [r for r in records if r.split == "test" and not str(r.probe_path).startswith("events:")]
    lockers = gallery_lockers(cfg.app.gallery_dir)
    if test and any(v is not None for v in lockers.values()):
        mr = locker_misroute(test, lockers)
        audit["locker_misroute"] = mr
        audit["locker_misroute_rate"] = mr["locker_misroute_rate"]
    elif test:
        audit["warnings"].append("갤러리 person.json에 사물함 배정이 없어 locker_misroute_rate 계산 불가")
    return audit


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return run(args)
    except FaceDoorError as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        return int(ExitCode.INTERRUPTED)


if __name__ == "__main__":
    raise SystemExit(main())
