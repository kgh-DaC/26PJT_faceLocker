"""FaceDatabase gallery layout and safety checks (INTERFACES sections 3.5 and 6)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from conftest import unit
from facedoor.database import FaceDatabase
from facedoor.errors import GalleryError

MID = "sface:abc123abc123"


def crop() -> np.ndarray:
    return np.random.default_rng(0).integers(0, 255, (60, 50, 3), dtype=np.uint8)


def test_missing_folder_is_empty(tmp_path):
    db = FaceDatabase.load(tmp_path / "nope", expected_model_id=MID)
    assert len(db) == 0 and db.persons() == [] and db.matrix.shape[0] == 0


def test_add_writes_layout_and_reloads(tmp_path):
    g = tmp_path / "gallery"
    db = FaceDatabase.load(g, expected_model_id=MID)
    s0 = db.add("alice", "normal", np.arange(1, 9, dtype=np.float32), crop(), {"model_id": MID, "pose": "front"})
    s1 = db.add("alice", "normal", unit(np.ones(8)), crop(), {"model_id": MID})
    db.add("alice", "mask", unit(np.ones(8)), None, {"model_id": MID})
    assert (s0.index, s1.index) == (0, 1)
    pdir = g / "alice"
    for name in ("normal_000.npy", "normal_000.jpg", "normal_000.json", "normal_001.npy", "mask_000.npy",
                 "person.json"):
        assert (pdir / name).is_file(), name
    assert not (pdir / "mask_000.jpg").exists()
    meta = json.loads((g / "_meta.json").read_text(encoding="utf-8"))
    assert meta["model_id"] == MID and meta["dim"] == 8
    smeta = json.loads((pdir / "normal_000.json").read_text(encoding="utf-8"))
    assert smeta["tag"] == "normal" and smeta["index"] == 0 and smeta["pose"] == "front" and "crop_sha256" in smeta

    db2 = FaceDatabase.load(g, expected_model_id=MID)
    assert len(db2) == 3 and db2.persons() == ["alice"]
    assert db2.labels == ["alice"] * 3 and sorted(db2.tags) == ["mask", "normal", "normal"]
    assert np.allclose(np.linalg.norm(db2.matrix, axis=1), 1.0, atol=1e-4)
    assert db2.matrix.dtype == np.float32


def test_model_mismatch_raises(tmp_path):
    g = tmp_path / "gallery"
    FaceDatabase.load(g, expected_model_id=MID).add("a", "normal", unit(np.ones(4)), None, {"model_id": MID})
    with pytest.raises(GalleryError, match="reembed"):
        FaceDatabase.load(g, expected_model_id="sface:different")


def test_dim_mismatch_raises(tmp_path):
    g = tmp_path / "gallery"
    db = FaceDatabase.load(g, expected_model_id=MID)
    db.add("a", "normal", unit(np.ones(4)), None, {"model_id": MID})
    np.save(g / "a" / "normal_001.npy", unit(np.ones(6)))
    with pytest.raises(GalleryError):
        FaceDatabase.load(g, expected_model_id=MID)


def test_zero_embedding_rejected(tmp_path):
    db = FaceDatabase.load(tmp_path / "g", expected_model_id=MID)
    with pytest.raises(GalleryError):
        db.add("a", "normal", np.zeros(4), None, {"model_id": MID})


def test_remove_sample_and_person(tmp_path):
    g = tmp_path / "gallery"
    db = FaceDatabase.load(g, expected_model_id=MID)
    s = db.add("a", "normal", unit(np.ones(4)), crop(), {"model_id": MID})
    db.add("a", "glasses", unit(np.arange(1, 5)), None, {"model_id": MID})
    db.add("b", "normal", unit(np.arange(4, 0, -1)), None, {"model_id": MID})
    db.remove_sample(s)
    assert not s.npy_path.exists() and not s.jpg_path.exists() and len(db) == 2
    db.remove_person("a")
    assert db.persons() == ["b"] and not (g / "a").exists()
    assert FaceDatabase.load(g, expected_model_id=MID).persons() == ["b"]


def test_find_outliers(tmp_path):
    db = FaceDatabase.load(tmp_path / "g", expected_model_id=MID)
    base = np.eye(8, dtype=np.float32)[0]
    for k in range(3):
        db.add("a", "normal", unit(base + 0.05 * k * np.eye(8)[1]), None, {"model_id": MID})
    odd = db.add("a", "normal", np.eye(8, dtype=np.float32)[5], None, {"model_id": MID})
    outs = db.find_outliers("a", 0.3)
    assert [o.npy_path for o in outs] == [odd.npy_path]
    assert db.find_outliers("nobody", 0.3) == []


def test_person_info_display_name(tmp_path):
    db = FaceDatabase.load(tmp_path / "g", expected_model_id=MID)
    db.add("hong", "normal", unit(np.ones(4)), None, {"model_id": MID})
    db.set_person_info("hong", display_name="홍길동")
    assert FaceDatabase.load(tmp_path / "g", expected_model_id=MID).display_name("hong") == "홍길동"
