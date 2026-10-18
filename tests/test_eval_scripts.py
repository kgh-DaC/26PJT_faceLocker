"""scripts/label_events.py, make_synthetic_probes.py and evaluate.py (owner: eval-engineer).

The end-to-end scenario builds a tiny gallery/probe tree from tests/assets in a temporary folder, makes synthetic
probes, runs evaluate.py and checks every report artifact. Nothing is written to data/, reports/ or
models/calibration.json (calibration goes to a temp file through --set app.calibration_file=...).
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tests" / "assets"
MODELS = ROOT / "models"
REPORT_FILES = ("metrics.json", "per_condition.csv", "scores.csv", "roc.png", "det.png", "score_dist.png",
                "confusion.png", "condition_bar.png", "report.html", "config_snapshot.yaml")


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


label_events = load_script("label_events")
evaluate = load_script("evaluate")
make_syn = load_script("make_synthetic_probes")

needs_models = pytest.mark.skipif(
    not ((MODELS / "face_detection_yunet_2023mar.onnx").is_file() and (MODELS / "face_recognition_sface_2021dec.onnx").is_file()),
    reason="ONNX models missing (python scripts/download_models.py)")


# =========================================================================== label_events.py
def write_events(path: Path) -> None:
    from facedoor.events import EVENT_COLUMNS
    rows = []
    for sess, n in (("S1", 6), ("S2", 3)):
        rows.append({"session_id": sess, "frame_idx": "0", "event": "start"})
        for i in range(n):
            ok = i % 2 == 0
            rows.append({"ts": "2026-10-16T09:00:00.000+09:00", "session_id": sess, "frame_idx": str(i), "event": "frame",
                         "verdict": "MATCH" if ok else "UNKNOWN", "best_id": "alice", "best_score": "0.7000" if ok else "0.4000",
                         "second_id": "bob", "second_score": "0.2000", "threshold": "0.5000", "threshold_group": "global",
                         "condition_tag": "live", "total_ms": "40.0000", "label": ""})
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in EVENT_COLUMNS})


def labels(path: Path) -> list[tuple[str, str, str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return [(r["session_id"], r["frame_idx"], r["event"], r["label"]) for r in csv.DictReader(fh)]


def test_label_events_range_conflict_and_backup(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"
    write_events(ev)
    original = ev.read_bytes()
    base = ["--events", str(ev), "--set", f"app.gallery_dir={(tmp_path / 'nogallery').as_posix()}"]

    assert label_events.main(base + ["--session", "S1", "--label", "alice", "--from-frame", "1", "--to-frame", "3"]) == 0
    got = {(s, f, e): lab for s, f, e, lab in labels(ev)}
    assert [got[("S1", str(i), "frame")] for i in range(6)] == ["", "alice", "alice", "alice", "", ""]
    assert got[("S1", "0", "start")] == ""                     # only frame rows are labelled
    assert all(lab == "" for (s, _, _), lab in got.items() if s == "S2")
    assert (tmp_path / "events.csv.bak").read_bytes() == original
    assert not ev.read_bytes().startswith(b"\xef\xbb\xbf")      # UTF-8 without BOM

    # existing different labels are kept without --overwrite, replaced with it
    assert label_events.main(base + ["--session", "S1", "--label", "_impostor"]) == 0
    got = [lab for s, f, e, lab in labels(ev) if s == "S1" and e == "frame"]
    assert got == ["_impostor", "alice", "alice", "alice", "_impostor", "_impostor"]
    assert label_events.main(base + ["--session", "S1", "--label", "_ignore", "--from-frame", "2", "--to-frame", "2",
                                     "--overwrite"]) == 0
    assert [lab for s, f, e, lab in labels(ev) if s == "S1" and e == "frame"][2] == "_ignore"

    # dry run does not touch the file
    before = ev.read_bytes()
    assert label_events.main(base + ["--session", "S2", "--label", "bob", "--dry-run"]) == 0
    assert ev.read_bytes() == before


def test_label_events_rejects_bad_input(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"
    write_events(ev)
    base = ["--events", str(ev)]
    assert label_events.main(base + ["--session", "S1", "--label", "bad name!"]) == 2          # ExitCode.CONFIG
    assert label_events.main(base + ["--session", "NOPE", "--label", "_impostor"]) == 1
    assert label_events.main(base + ["--list-sessions"]) == 0
    assert label_events.main(["--events", str(tmp_path / "missing.csv"), "--session", "S1", "--label", "a"]) == 2


def test_evaluate_events_only(tmp_path: Path) -> None:
    """Labelled events -> evaluate --events with no probe images (no models needed)."""
    ev = tmp_path / "events.csv"
    write_events(ev)
    assert label_events.main(["--events", str(ev), "--session", "S1", "--label", "alice"]) == 0
    assert label_events.main(["--events", str(ev), "--session", "S2", "--label", "_impostor"]) == 0
    out = tmp_path / "reports"
    rc = evaluate.main(["--gallery", str(tmp_path / "g"), "--probe", str(tmp_path / "p"), "--events", str(ev),
                        "--out", str(out), "--set", "evaluation.bootstrap_iters=20"])
    assert rc == 0
    (rep,) = list(out.iterdir())
    m = json.loads((rep / "metrics.json").read_text(encoding="utf-8"))
    assert m["data_origin"] == "events"
    ov = m["overall"]
    assert ov["n_genuine"] == 6 and ov["n_impostor"] == 3
    assert ov["frr"] == pytest.approx(3 / 6)          # alice MATCH on frames 0,2,4
    assert ov["far"] == pytest.approx(2 / 3)          # S2 frames 0,2 MATCH while labelled _impostor
    assert m["sample_counts"]["synthetic_fraction"] == 0.0
    assert all((rep / f).is_file() for f in REPORT_FILES)


def test_evaluate_nothing_to_do(tmp_path: Path) -> None:
    assert evaluate.main(["--gallery", str(tmp_path / "g"), "--probe", str(tmp_path / "p"),
                          "--out", str(tmp_path / "r"), "--set", "lockers.mode=single",
                          "--set", "hardware.locker_outputs=[]", "--set", "hardware.solenoid_pin=17",
                          "--set", "hardware.close_mode=timer"]) == 1


# =========================================================================== end-to-end with real models
@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """gallery: lena/astronaut enrolled from mirrored copies; probes: originals (+ a scaled copy), an upscaled
    messi face as unenrolled impostor, and one gallery crop copied into the probes (must be excluded as a leak)."""
    import cv2

    from facedoor.config import load_config
    from facedoor.database import FaceDatabase
    from facedoor.detector import create_detector
    from facedoor.embedder import create_embedder
    from facedoor.imageio import imread, imwrite

    root = tmp_path_factory.mktemp("evalws")
    cfg = load_config()
    det = create_detector(cfg.detector, cfg.app.models_dir)
    emb = create_embedder(cfg.embedder, cfg.app.models_dir)
    gallery, probe = root / "gallery", root / "probe"
    db = FaceDatabase(gallery, model_id=emb.model_id)

    def enroll(pid: str, img: np.ndarray, tag: str) -> Path:
        ds = det.detect(img)
        assert ds, f"no face in enrollment image for {pid}"
        d = max(ds, key=lambda x: x.bbox[2] * x.bbox[3])
        s = db.add(pid, tag, emb.embed(img, d), emb.align(img, d),
                   {"model_id": emb.model_id, "source": "file:test", "source_sha256": None})
        assert s.jpg_path is not None
        return s.jpg_path

    persons = {"lena": "lena.jpg", "astro": "astronaut.png"}
    crops = {}
    for pid, fn in persons.items():
        img = imread(ASSETS / fn)
        crops[pid] = enroll(pid, cv2.flip(img, 1), "normal")
        enroll(pid, np.clip(cv2.flip(img, 1).astype(np.int16) - 25, 0, 255).astype(np.uint8), "normal")
        imwrite(probe / "normal" / pid / f"{pid}_orig.jpg", img)
        imwrite(probe / "normal" / pid / f"{pid}_scaled.jpg", cv2.resize(img, None, fx=0.8, fy=0.8))
    shutil.copy(crops["lena"], probe / "normal" / "lena" / "leaked_crop.jpg")
    messi = imread(ASSETS / "messi5.jpg")
    face = cv2.resize(messi[60:200, 180:310], None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    imwrite(probe / "_impostor" / "messi" / "messi_face.jpg", face)
    imwrite(probe / "_impostor" / "messi" / "messi_full.jpg", messi)
    return {"root": root, "gallery": gallery, "probe": probe}


def make_synthetic(ws: dict[str, Path]) -> None:
    probe, gal = ws["probe"], ["--gallery", str(ws["gallery"])]
    assert make_syn.main(["--src", str(probe / "normal"), "--out", str(probe),
                          "--conditions", "syn_lowlight,syn_mask,syn_glasses", "--seed", "7"] + gal) == 0
    assert make_syn.main(["--src", str(probe / "_impostor"), "--out", str(probe), "--conditions", "syn_mask",
                          "--seed", "7"] + gal) == 0


@needs_models
def test_make_synthetic_probes(workspace: dict[str, Path], tmp_path: Path) -> None:
    probe, gal = workspace["probe"], ["--gallery", str(workspace["gallery"])]
    conds = "syn_lowlight,syn_mask,syn_glasses"
    make_synthetic(workspace)
    for c in conds.split(","):
        files = sorted((probe / c).glob("*/*.jpg"))
        assert len(files) == 4, c                     # 2 persons x 2 probes; the leaked gallery crop is skipped
        assert not any("leaked" in f.name for f in files)
        man = json.loads((probe / c / "_manifest.json").read_text(encoding="utf-8"))
        assert man["synthetic"] is True and "참고용" in man["note"]
    assert len(list((probe / "_impostor_syn_mask" / "messi").glob("*.jpg"))) == 2

    # reproducible: regenerate into another folder with the same seed -> identical bytes
    other = tmp_path / "again"
    assert make_syn.main(["--src", str(probe / "normal"), "--out", str(other), "--conditions", "syn_mask",
                          "--seed", "7"] + gal) == 0
    a = sorted((probe / "syn_mask").glob("*/*.jpg"))
    b = sorted((other / "syn_mask").glob("*/*.jpg"))
    assert [x.read_bytes() for x in a] == [y.read_bytes() for y in b]

    # existing files are not overwritten without --overwrite; gallery as source is refused (leakage)
    mtimes = [p.stat().st_mtime_ns for p in a]
    assert make_syn.main(["--src", str(probe / "normal"), "--out", str(probe), "--conditions", "syn_mask"] + gal) == 0
    assert [p.stat().st_mtime_ns for p in a] == mtimes
    assert make_syn.main(["--src", str(workspace["gallery"]), "--out", str(tmp_path / "x"),
                          "--set", f"app.gallery_dir={workspace['gallery'].as_posix()}"]) == 2
    assert make_syn.main(["--src", str(probe / "normal"), "--out", str(tmp_path / "x"), "--conditions", "syn_bogus"]) == 2


@needs_models
def test_evaluate_end_to_end(workspace: dict[str, Path], tmp_path: Path) -> None:
    probe, gallery = workspace["probe"], workspace["gallery"]
    make_synthetic(workspace)   # idempotent (existing files are kept) -> independent of test order
    real_cal = ROOT / "models" / "calibration.json"
    real_cal_before = real_cal.read_bytes() if real_cal.is_file() else None
    cal_path = tmp_path / "calibration.json"
    out = tmp_path / "reports"
    common = ["--gallery", str(gallery), "--probe", str(probe), "--out", str(out), "--target-far", "0.001",
              "--set", f"app.calibration_file={cal_path.as_posix()}", "--set", "evaluation.bootstrap_iters=50"]

    # ---- run 1: with --write-calibration
    assert evaluate.main(common + ["--write-calibration"]) == 0
    (rep1,) = list(out.iterdir())
    for f in REPORT_FILES + ("calibration_candidate.json",):
        assert (rep1 / f).is_file() and (rep1 / f).stat().st_size > 0, f
    m = json.loads((rep1 / "metrics.json").read_text(encoding="utf-8"))
    for key in ("schema_version", "created_at", "embedder", "thresholds", "sample_counts", "overall",
                "per_condition", "latency_ms", "warnings"):
        assert key in m, key
    assert m["embedder"]["model_id"].startswith("sface:")
    sc = m["sample_counts"]
    assert 0 < sc["synthetic_fraction"] < 1
    assert {"normal", "syn_mask", "syn_lowlight", "syn_glasses"} <= set(m["per_condition"])
    assert m["per_condition"]["syn_mask"]["synthetic"] is True and m["per_condition"]["normal"]["synthetic"] is False
    assert any("누수" in w for w in m["warnings"])                       # leaked crop excluded
    assert any("참고용" in w for w in m["warnings"])
    assert any("3/target_far" in w or "FAR 계산 불가" in w for w in m["warnings"])   # tiny sample flagged
    scores = (rep1 / "scores.csv").read_text(encoding="utf-8")
    assert "leaked_crop" not in scores
    assert {"cal", "test"} <= {row["split"] for row in csv.DictReader(scores.splitlines())}

    html = (rep1 / "report.html").read_text(encoding="utf-8")
    assert "합성 데이터 포함 — 참고용" in html and "합성 비중" in html
    assert html.index("참고용") < html.index("1. 요약")

    # calibration: written to the temp path only, unreliable, and the policy did not go below defaults
    cal = json.loads(cal_path.read_text(encoding="utf-8"))
    assert cal["thresholds"]["global"]["reliable"] is False
    assert cal["report_dir"] == str(rep1)
    assert m["thresholds"]["global"]["threshold"] >= 0.50
    assert m["thresholds"]["occluded"]["threshold"] >= m["thresholds"]["global"]["threshold"]
    assert (real_cal.read_bytes() if real_cal.is_file() else None) == real_cal_before

    # ---- run 2: no --write-calibration -> calibration untouched, comparison with run 1
    cal_before = cal_path.read_bytes()
    assert evaluate.main(common) == 0
    rep2 = max(out.iterdir(), key=lambda p: p.name)
    assert rep2 != rep1
    assert cal_path.read_bytes() == cal_before
    m2 = json.loads((rep2 / "metrics.json").read_text(encoding="utf-8"))
    assert m2["previous_report"] == str(rep1) and m2["comparison"]
    assert m2["calibration"]["written"] is False
    assert "이전 → 현재" in (rep2 / "report.html").read_text(encoding="utf-8")

    # ---- run 3: --no-calibrate evaluates every probe with the current (calibrated) runtime policy
    assert evaluate.main(common + ["--no-calibrate"]) == 0
    rep3 = max(out.iterdir(), key=lambda p: p.name)
    rows = list(csv.DictReader((rep3 / "scores.csv").read_text(encoding="utf-8").splitlines()))
    assert {r["split"] for r in rows} == {"test"}
    assert (real_cal.read_bytes() if real_cal.is_file() else None) == real_cal_before
