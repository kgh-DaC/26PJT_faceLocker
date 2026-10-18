"""Enrollment with the real YuNet + SFace models on tests/assets images (INTERFACES section 3.12)."""

from __future__ import annotations

import importlib.util
import json

import cv2
import numpy as np
import pytest

from conftest import ASSETS, ROOT, copy_assets, requires_models
from facedoor.camera import FileCamera
from facedoor.database import FaceDatabase
from facedoor.detector import create_detector
from facedoor.embedder import create_embedder
from facedoor.enroll import Enroller, validate_person_id
from facedoor.errors import GalleryError
from facedoor.imageio import imread, imwrite, sha256_file

pytestmark = [requires_models, pytest.mark.models]


@pytest.fixture(scope="module")
def models():
    from facedoor.config import load_config

    cfg = load_config(None, [], use_local=False)
    return create_detector(cfg.detector, ROOT / "models"), create_embedder(cfg.embedder, ROOT / "models")


@pytest.fixture
def setup(make_cfg, models):
    cfg = make_cfg()
    det, emb = models
    db = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=emb.model_id)
    return cfg, db, Enroller(det, emb, db, cfg.quality, cfg.enroll)


def variants(tmp_path):
    img = imread(ASSETS / "lena.jpg")
    out = []
    for name, im in (("orig.jpg", img), ("flip.jpg", cv2.flip(img, 1)),
                     ("bright.jpg", cv2.convertScaleAbs(img, alpha=1.0, beta=25))):
        p = tmp_path / "src" / name
        imwrite(p, im)
        out.append(p)
    return out


def test_enroll_images_saves_samples_with_meta(setup, tmp_path):
    cfg, db, enr = setup
    paths = variants(tmp_path)
    rep = enr.enroll_images("lena", "normal", paths)
    assert rep.saved == 3 and rep.rejected == {}
    assert rep.self_score_mean is not None and rep.self_score_mean > 0.5
    assert rep.outliers == []
    g = cfg.app.gallery_dir
    meta = json.loads((g / "lena" / "normal_000.json").read_text(encoding="utf-8"))
    assert meta["source_sha256"] == sha256_file(paths[0])
    assert meta["model_id"] == enr.embedder.model_id and meta["quality"]["face_px"] >= cfg.quality.min_face_px
    assert (g / "lena" / "normal_000.jpg").is_file()
    db2 = FaceDatabase.load(g, expected_model_id=enr.embedder.model_id)
    assert len(db2) == 3 and np.allclose(np.linalg.norm(db2.matrix, axis=1), 1.0, atol=1e-4)


def test_enroll_rejections(setup, tmp_path):
    _, db, enr = setup
    lena = imread(ASSETS / "lena.jpg")
    astro = imread(ASSETS / "astronaut.png")
    imwrite(tmp_path / "blank.png", np.full((300, 300, 3), 127, np.uint8))
    imwrite(tmp_path / "two.png", np.hstack([lena, astro]))
    paths = [ASSETS / "lena.jpg", ASSETS / "lena.jpg", ASSETS / "messi5.jpg", tmp_path / "blank.png",
             tmp_path / "two.png", tmp_path / "missing.jpg"]
    rep = enr.enroll_images("lena", "normal", paths)
    assert rep.saved == 1
    assert rep.rejected == {"duplicate": 1, "too_small": 1, "no_face": 1, "multi_face": 1, "unreadable": 1}


def test_invalid_person_and_tag(setup):
    _, _, enr = setup
    with pytest.raises(ValueError):
        enr.enroll_images("홍길동", "normal", [ASSETS / "lena.jpg"])
    with pytest.raises(ValueError):
        enr.enroll_images("lena", "sunglasses", [ASSETS / "lena.jpg"])
    with pytest.raises(ValueError):
        validate_person_id("_impostor", "^[A-Za-z0-9_-]{1,32}$")


def test_enroll_live_from_file_camera(setup, tmp_path):
    _, db, enr = setup
    folder = copy_assets(tmp_path / "cam", ["messi5.jpg", "lena.jpg", "astronaut.png"])
    cam = FileCamera(folder)
    cam.open()
    rep = enr.enroll_live(cam, "mix", "glasses", poses=["front"], samples_per_pose=2)
    cam.close()
    assert rep.saved == 2 and rep.rejected == {"too_small": 1}
    assert [s.tag for s in db.samples("mix")] == ["glasses", "glasses"]
    assert db.samples("mix")[0].meta["pose"] == "front"


def test_reembed_after_model_change(setup, tmp_path, models):
    cfg, db, enr = setup
    enr.enroll_images("lena", "normal", variants(tmp_path)[:2])
    meta_path = cfg.app.gallery_dir / "_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["model_id"] = "sface:oldmodel0000"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(GalleryError):
        FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=enr.embedder.model_id)
    old = FaceDatabase.load(cfg.app.gallery_dir)
    det, emb = models
    n = Enroller(det, emb, old, cfg.quality, cfg.enroll).reembed_all()
    assert n == 2
    db2 = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=emb.model_id)
    assert len(db2) == 2


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_enroll_script_images_and_prune(tmp_path, capsys):
    script = _load_script("enroll")
    g = tmp_path / "gallery"
    src = tmp_path / "src"
    variants(tmp_path)
    sets = ["--set", f"app.gallery_dir={g.as_posix()}"]
    code = script.main(["--name", "lena", "--tag", "normal", "--images", str(src), "--no-display",
                        "--display-name", "레나"] + sets)
    assert code == 0
    assert "lena: normal=3" in capsys.readouterr().out
    assert json.loads((g / "lena" / "person.json").read_text(encoding="utf-8"))["display_name"] == "레나"
    # bad id -> CONFIG exit code, nothing written
    assert script.main(["--name", "bad id", "--tag", "normal", "--images", str(src)] + sets) == 2
    assert script.main(["--name", "lena", "--prune-outliers"] + sets) == 0
    assert script.main(["--reembed"] + sets) == 0


SINGLE_SETS = ["--set", "lockers.mode=single", "--set", "hardware.locker_outputs=[]", "--set", "hardware.solenoid_pin=17",
          "--set", "hardware.close_mode=timer"]


def test_enroll_script_locker_id(tmp_path, capsys):
    """QA D-1: --locker-id validated by lockers.locker_id_pattern and stored in person.json."""
    script = _load_script("enroll")
    g = tmp_path / "gallery"
    src = tmp_path / "src"
    variants(tmp_path)
    # default.yaml is per_locker (lockers "1".."4"); this test is about the single-mode pattern check
    sets = ["--set", f"app.gallery_dir={g.as_posix()}"] + SINGLE_SETS
    pj = g / "lena" / "person.json"
    # invalid locker id -> CONFIG, nothing enrolled
    assert script.main(["--name", "lena", "--tag", "normal", "--images", str(src), "--no-display",
                        "--locker-id", "bad id!"] + sets) == 2
    assert not (g / "lena").exists()
    # info-only update of a person that is not enrolled -> GALLERY, no person.json
    assert script.main(["--name", "lena", "--locker-id", "L01"] + sets) == 6
    assert not pj.exists()
    # enroll with locker id
    assert script.main(["--name", "lena", "--tag", "normal", "--images", str(src), "--no-display",
                        "--locker-id", "L01", "--display-name", "레나"] + sets) == 0
    info = json.loads(pj.read_text(encoding="utf-8"))
    assert info["locker_id"] == "L01" and info["display_name"] == "레나"
    # info-only update keeps samples and display_name
    assert script.main(["--name", "lena", "--locker-id", "B-12"] + sets) == 0
    db = FaceDatabase.load(g)
    assert db.locker_id("lena") == "B-12" and db.display_name("lena") == "레나" and len(db.samples("lena")) == 3
    # custom pattern from config
    assert script.main(["--name", "lena", "--locker-id", "B-12",
                        "--set", "lockers.locker_id_pattern=^[0-9]{1,3}$"] + sets) == 2
    assert script.main(["--name", "lena", "--locker-id", "007",
                        "--set", "lockers.locker_id_pattern=^[0-9]{1,3}$"] + sets) == 0
    assert FaceDatabase.load(g).locker_id("lena") == "007"
