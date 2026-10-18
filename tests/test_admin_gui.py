"""Admin GUI service layer + admin PIN (INTERFACES 14.10 G1-G10, 12.1, 16). No camera, no display, no models:
fake detector / embedder / camera are injected into AdminService."""

from __future__ import annotations

import importlib.util
import json
import re
import threading
from pathlib import Path

import numpy as np
import pytest

from conftest import ROOT, fake_detection, unit
from facedoor import adminpin
from facedoor.database import FaceDatabase
from facedoor.datatypes import Frame
from facedoor.enroll import EnrollProgress
from facedoor.gui import texts as T
from facedoor.gui.service import AdminError, AdminService

runlock = pytest.importorskip("facedoor.runlock")

HANGUL = re.compile(r"[가-힣]")
DIM = 16
MODEL_ID = "fake:000000000000"


# ------------------------------------------------------------------------------------------- fakes
class FakeDetector:
    """One face at a fixed box when the frame is not black; nothing on black frames."""

    def detect(self, image):
        if float(image.mean()) < 5:
            return []
        return [fake_detection(100, 100, 120, 140, 0.95)]


class FakeEmbedder:
    backend = "fake"
    dim = DIM
    model_id = MODEL_ID

    def __init__(self, seed: int = 1) -> None:
        self.base = unit(np.random.default_rng(seed).normal(size=DIM))
        self.rng = np.random.default_rng(seed + 100)

    def embed(self, image, det):
        return unit(self.base + 0.25 * self.rng.normal(size=DIM)).astype(np.float32)

    def align(self, image, det):
        return image[:112, :112].copy()


class FakeCamera:
    """Non-live camera yielding noise frames (good quality) or black frames (no face)."""

    instances: list["FakeCamera"] = []

    def __init__(self, n_good: int = 50, n_black: int = 0, on_read=None) -> None:
        self.frames = ["good"] * n_good + ["black"] * n_black
        self.on_read = on_read
        self.opened = False
        self.closed = False
        self.reads = 0
        self.rng = np.random.default_rng(7)
        FakeCamera.instances.append(self)

    @property
    def is_live(self) -> bool:
        return False

    def open(self):
        self.opened = True

    def read(self):
        if self.reads >= len(self.frames):
            raise StopIteration
        kind = self.frames[self.reads]
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        img = (self.rng.integers(40, 215, size=(480, 640, 3), dtype=np.uint8) if kind == "good"
               else np.zeros((480, 640, 3), np.uint8))
        return Frame(img, self.reads - 1, 0.0, "fake:0")

    def close(self):
        self.closed = True

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


def factory(**kw):
    def make(cam_cfg, source):
        return FakeCamera(**kw)
    return make


@pytest.fixture
def svc_factory(make_cfg, tmp_path):
    FakeCamera.instances.clear()

    def _make(*overrides: str, camera_factory=None, **kw):
        cfg = make_cfg(f"app.reports_dir={(tmp_path / 'reports').as_posix()}", *overrides)
        return AdminService(cfg, camera_factory=camera_factory or factory(),
                            detector=FakeDetector(), embedder=FakeEmbedder(**kw))
    return _make


def add_person(db: FaceDatabase, pid: str, n: int, tag: str = "normal", seed: int = 3, noise: float = 0.2,
               name: str | None = None, locker: str | None = None) -> None:
    rng = np.random.default_rng(seed)
    base = rng.normal(size=DIM)
    for _ in range(n):
        db.add(pid, tag, unit(base + noise * rng.normal(size=DIM)), None, {"model_id": MODEL_ID})
    if name is not None or locker is not None:
        db.set_person_info(pid, **({"display_name": name} if name else {}), **({"locker_id": locker} if locker else {}))


def read_lines(p: Path) -> list[list[str]]:
    return [ln.split("\t") for ln in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []


# ------------------------------------------------------------------------------------------- G1 / G2 capture
def test_g1_new_user_too_few_samples_rolls_back(svc_factory):
    svc = svc_factory(camera_factory=factory(n_good=3))           # 3 < min_register_samples (5)
    rep = svc.capture("u0001", "normal", new_user=True, display_name="홍길동")
    assert rep.saved == 3 and rep.rolled_back and not rep.cancelled
    assert not (Path(svc.cfg.app.gallery_dir) / "u0001").exists()
    assert FakeCamera.instances[0].opened and FakeCamera.instances[0].closed


def test_new_user_success_records_name_and_locker(svc_factory):
    svc = svc_factory()
    seen: list[EnrollProgress] = []
    rep = svc.capture("u0001", "normal", new_user=True, display_name=" 홍길동 ", locker_id="2", progress=seen.append)
    assert rep.saved == 10 and not rep.rolled_back and rep.warnings == []
    db = FaceDatabase.load(svc.cfg.app.gallery_dir)
    assert db.display_name("u0001") == "홍길동" and db.locker_id("u0001") == "2"
    phases = {p.phase for p in seen}
    assert {"pose_change", "capturing", "done"} <= phases
    assert any(p.just_saved and p.frame is not None and p.bbox == (100, 100, 120, 140) for p in seen)
    assert seen[-1].phase == "done" and seen[-1].saved == 10 and seen[-1].target == 10
    # camera/gallery locks are released afterwards
    assert runlock.lock_holder(svc.camera_lock_path) is None and runlock.lock_holder(svc.gallery_lock_path) is None
    assert any(r[2] == "set" and r[3] == "u0001" and r[5] == "2" for r in read_lines(svc.locker_log))


def test_new_user_locker_conflict_keeps_registration_with_warning(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 10, name="김철수", locker="2")
    rep = svc.capture("u0002", "normal", new_user=True, display_name="홍길동", locker_id="2")
    assert not rep.rolled_back and rep.saved == 10
    assert rep.warnings and "김철수" in rep.warnings[0] and HANGUL.search(rep.warnings[0])
    db = FaceDatabase.load(svc.cfg.app.gallery_dir)
    assert db.locker_id("u0002") is None and db.locker_id("u0001") == "2"


def test_g2_cancel_new_user_rolls_back(svc_factory):
    cancel = threading.Event()
    svc = svc_factory(camera_factory=factory(on_read=lambda n: cancel.set() if n == 7 else None))
    rep = svc.capture("u0001", "normal", new_user=True, display_name="홍길동", cancel=cancel)
    assert rep.cancelled and rep.rolled_back and rep.saved >= 5
    assert not (Path(svc.cfg.app.gallery_dir) / "u0001").exists()


def test_g2_cancel_augment_keeps_samples(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 10, name="홍길동")
    cancel = threading.Event()
    svc._camera_factory = factory(on_read=lambda n: cancel.set() if n == 3 else None)
    rep = svc.capture("u0001", "mask", new_user=False, cancel=cancel)
    assert rep.cancelled and not rep.rolled_back and rep.saved == 3
    db = FaceDatabase.load(svc.cfg.app.gallery_dir)
    assert len(db.samples("u0001")) == 13 and sum(s.tag == "mask" for s in db.samples("u0001")) == 3


def test_augment_other_tag_stores_note_and_no_rollback_on_few(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 10)
    svc._camera_factory = factory(n_good=2)
    rep = svc.capture("u0001", "other", new_user=False, note="두꺼운 안경테")
    assert rep.saved == 2 and not rep.rolled_back
    db = FaceDatabase.load(svc.cfg.app.gallery_dir)
    other = [s for s in db.samples("u0001") if s.tag == "other"]
    assert len(other) == 2 and all(s.meta.get("note") == "두꺼운 안경테" for s in other)


def test_capture_rejects_reported_in_progress(svc_factory):
    svc = svc_factory(camera_factory=factory(n_good=0, n_black=3))
    seen: list[EnrollProgress] = []
    rep = svc.capture("u0001", "normal", new_user=True, display_name="홍길동", progress=seen.append)
    assert rep.rolled_back and rep.rejected == {"no_face": 3}
    assert [p.last_reject for p in seen if p.phase == "capturing"] == ["no_face"] * 3
    assert T.reject_text("no_face") == T.REJ_no_face and HANGUL.search(T.reject_text("too_dark"))


def test_capture_validation_errors(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 3)
    with pytest.raises(AdminError, match="이미 사용 중"):
        svc.capture("u0001", "normal", new_user=True, display_name="홍길동")
    with pytest.raises(AdminError, match="등록되지 않은"):
        svc.capture("u0009", "mask", new_user=False)
    with pytest.raises(AdminError, match="촬영 조건"):
        svc.capture("u0001", "sunglasses", new_user=False)
    assert FakeCamera.instances == []          # camera never created


# ------------------------------------------------------------------------------------------- G3 remove
def test_g3_remove_user(svc_factory):
    from facedoor.lockers import LockerMap

    svc = svc_factory()
    add_person(svc.db, "u0001", 4, name="홍길동", locker="2")
    add_person(svc.db, "u0002", 4, seed=9, name="김철수", locker="3")
    svc.remove_user("u0001")
    assert not (Path(svc.cfg.app.gallery_dir) / "u0001").exists()
    db = FaceDatabase.load(svc.cfg.app.gallery_dir)
    lm = LockerMap.build(db, svc.cfg.lockers, ())
    assert "u0001" not in lm.assignments() and lm.assignments()["u0002"] == "3"
    rows = read_lines(svc.locker_log)
    assert rows[-1][2:6] == ["clear", "u0001", "2", "-"] and "reason=remove" in rows[-1]
    assert any(r[3] == "remove" and r[4] == "u0001" for r in read_lines(svc.admin_log))
    with pytest.raises(AdminError):
        svc.remove_user("u0001")


# ------------------------------------------------------------------------------------------- G4 lockers
def test_g4_assign_locker(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 4, name="홍길동")
    add_person(svc.db, "u0002", 4, seed=9, name="김철수")
    assert svc.assign_locker("u0001", "2") == []
    errs = svc.assign_locker("u0002", "2")
    assert errs == [T.LOCKER_TAKEN.format(locker="2", owner="홍길동")]
    assert svc.assign_locker("u0002", "2", take_over=True) == []
    db = FaceDatabase.load(svc.cfg.app.gallery_dir)
    assert db.locker_id("u0001") is None and db.locker_id("u0002") == "2"
    rows = read_lines(svc.locker_log)
    assert [r[2:6] for r in rows[-2:]] == [["clear", "u0001", "2", "-"], ["set", "u0002", "-", "2"]]
    assert HANGUL.search(svc.assign_locker("nobody", "1")[0])
    assert HANGUL.search(svc.assign_locker("u0001", "9")[0])           # not in choices
    assert HANGUL.search(svc.assign_locker("u0001", "1", shared=True)[0])   # allow_shared=false
    svc.clear_locker("u0002")
    assert FaceDatabase.load(svc.cfg.app.gallery_dir).locker_id("u0002") is None
    assert read_lines(svc.locker_log)[-1][2:6] == ["clear", "u0002", "2", "-"]


def test_locker_rows_and_choices(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 4, locker="1")
    add_person(svc.db, "u0002", 4, seed=9, locker="Z9")
    svc.reload()
    assert svc.locker_choices() == ["1", "2", "3", "4"]
    rows = {r.locker_id: r for r in svc.locker_rows()}
    assert rows["1"].status == "assigned" and rows["1"].owners == ["u0001"] and rows["1"].pin is None
    assert rows["2"].status == "free" and rows["Z9"].status == "unknown"
    # hand-edited duplicate -> shared_denied
    svc.db.set_person_info("u0002", locker_id="1")
    assert {r.locker_id: r.status for r in svc.locker_rows()}["1"] == "shared_denied"


def test_gallery_lock_busy(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 4)
    other = runlock.RunLock(svc.gallery_lock_path, "assign_locker.py")
    assert other.acquire()
    try:
        assert svc.assign_locker("u0001", "1") == [T.GALLERY_BUSY]
        with pytest.raises(AdminError, match="다른 관리 작업"):
            svc.remove_user("u0001")
    finally:
        other.release()


# ------------------------------------------------------------------------------------------- G5 validation
def test_g5_validate_new_user_and_suggest_id(svc_factory):
    svc = svc_factory()
    assert svc.validate_new_user("", "u0001")[0] == [T.NAME_EMPTY]
    assert svc.validate_new_user("홍/길동", "u0001")[0] == [T.NAME_INVALID]
    assert svc.validate_new_user("가" * 21, "u0001")[0] == [T.NAME_TOO_LONG.format(max=20)]
    assert svc.validate_new_user("가" * 20, "u0001") == ([], [])
    assert svc.validate_new_user("Kim Lee-2_a.b", "u0001") == ([], [])
    assert svc.validate_new_user("홍길동", "홍길동")[0] == [T.ID_INVALID.format(pid="홍길동")]
    assert svc.validate_new_user("홍길동", "_x")[0] == [T.ID_INVALID.format(pid="_x")]
    add_person(svc.db, "u0007", 2, name="홍길동")
    errs, warns = svc.validate_new_user("홍길동", "u0008")
    assert errs == [] and warns == [T.NAME_DUPLICATE.format(name="홍길동", pid="u0007")]
    assert svc.validate_new_user("이몽룡", "u0007")[0] == [T.ID_EXISTS.format(pid="u0007")]
    assert svc.suggest_person_id() == "u0008"
    add_person(svc.db, "alice", 2, seed=5)
    assert svc.suggest_person_id() == "u0008"


def test_suggest_first_id(svc_factory):
    assert svc_factory().suggest_person_id() == "u0001"


# ------------------------------------------------------------------------------------------- G6 weak reasons
def test_g6_weak_reasons(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0001", 6, tag="mask", noise=3.0, name="홍길동")    # few, no normal, low self-score
    add_person(svc.db, "u0002", 12, seed=9, noise=0.1, name="김철수")       # fine
    us = {u.person_id: u for u in svc.users()}
    r = us["u0001"].weak_reasons
    assert us["u0001"].n_samples == 6 and us["u0001"].tag_counts == {"mask": 6}
    assert T.WEAK_FEW_SAMPLES.format(n=6, need=10) in r
    assert T.WEAK_MISSING_TAG.format(tag="일반") in r
    assert any(x.startswith("촬영 사진끼리 일관성이 낮습니다") for x in r)
    assert us["u0002"].weak_reasons == [] and us["u0002"].display_name == "김철수"


def test_weak_frr_from_latest_report(svc_factory):
    svc = svc_factory()
    add_person(svc.db, "u0002", 12, seed=9, noise=0.1)
    rep = Path(svc.cfg.app.reports_dir) / "20261016-120000"
    rep.mkdir(parents=True)
    (rep / "metrics.json").write_text(json.dumps({"per_person": {"u0002": {"frr": 0.25}}}), encoding="utf-8")
    assert svc.users()[0].weak_reasons == [T.WEAK_FRR.format(pct=25.0)]


# ------------------------------------------------------------------------------------------- G7 camera lock
def test_g7_camera_busy_does_not_open_camera(svc_factory):
    svc = svc_factory()
    held = runlock.RunLock(svc.camera_lock_path, "run.py")
    assert held.acquire()
    try:
        ok, why = svc.camera_status()
        assert not ok and "run.py" in why and HANGUL.search(why)
        with pytest.raises(AdminError, match="얼굴인식 프로그램이 실행 중"):
            svc.capture("u0001", "normal", new_user=True, display_name="홍길동")
        assert FakeCamera.instances == []
    finally:
        held.release()
    assert svc.camera_status() == (True, T.CAMERA_READY)


def test_g7_run_py_refused_while_admin_captures(make_cfg, tmp_path):
    app_src = (ROOT / "src" / "facedoor" / "app.py").read_text(encoding="utf-8")
    if "runlock" not in app_src:
        pytest.skip("app.py does not take camera.lock yet (vision core, INTERFACES 14.6) - integration pending")
    from facedoor import app as app_mod

    cfg = make_cfg()
    lock = runlock.RunLock(runlock.camera_lock_path(cfg.app.logs_dir), "admin_gui.py")
    assert lock.acquire()
    try:
        code = app_mod.main(["--no-display", "--max-frames", "1",
                             "--set", f"app.logs_dir={Path(cfg.app.logs_dir).as_posix()}",
                             "--set", f"app.gallery_dir={Path(cfg.app.gallery_dir).as_posix()}",
                             "--set", "app.allow_empty_gallery=true", "--set", "hardware.mode=mock",
                             "--set", f"hardware.hardware_log={(tmp_path / 'hw.log').as_posix()}",
                             "--set", f"logging.events_csv={(tmp_path / 'ev.csv').as_posix()}",
                             "--set", f"logging.app_log={(tmp_path / 'app.log').as_posix()}",
                             "--source", str(ROOT / "tests" / "assets")])
    finally:
        lock.release()
    assert code == 3


# ------------------------------------------------------------------------------------------- G8 models missing
def test_g8_models_missing_list_still_works(make_cfg, tmp_path):
    cfg = make_cfg(f"app.models_dir={(tmp_path / 'nomodels').as_posix()}")
    db = FaceDatabase.load(cfg.app.gallery_dir)
    add_person(db, "u0001", 3, name="홍길동")
    svc = AdminService(cfg, camera_factory=factory())
    assert svc.camera_status() == (False, T.MODEL_MISSING)
    assert [u.display_name for u in svc.users()] == ["홍길동"]
    with pytest.raises(AdminError) as ei:
        svc.capture("u0002", "normal", new_user=True, display_name="이몽룡")
    assert str(ei.value) == T.MODEL_MISSING and FakeCamera.instances == []


def test_gallery_model_mismatch_blocks_capture(make_cfg):
    cfg = make_cfg()
    db = FaceDatabase.load(cfg.app.gallery_dir)
    add_person(db, "u0001", 3)
    svc = AdminService(cfg, camera_factory=factory(), detector=FakeDetector(), embedder=type(
        "E2", (FakeEmbedder,), {"model_id": "fake:999999999999"})())
    assert svc.camera_status() == (False, T.GALLERY_MISMATCH)
    assert len(svc.users()) == 1
    with pytest.raises(AdminError, match="reembed"):
        svc.capture("u0001", "mask", new_user=False)


# ------------------------------------------------------------------------------------------- G9 texts
def test_g9_all_texts_contain_hangul():
    texts = T.all_texts()
    assert len(texts) > 100
    bad = {k: v for k, v in texts.items() if not isinstance(v, str) or not HANGUL.search(v)}
    assert bad == {}
    for key in ("PIN_SETUP", "PIN_PROMPT", "PIN_WRONG", "PIN_LOCKED", "CAMERA_BUSY", "CAMERA_FAIL", "MODEL_MISSING",
                "GALLERY_MISMATCH", "NAME_EMPTY", "NAME_INVALID", "ID_EXISTS", "REGISTER_OK", "REGISTER_FAIL",
                "REMOVE_CONFIRM", "LOCKER_TAKEN", "LOCKER_SINGLE_MODE", "RESTART_NOTICE", "ERROR_GENERIC"):
        assert key in texts
    for pose in ("front", "left", "right", "up", "down"):
        assert HANGUL.search(T.pose_text(pose))
    for rej in ("too_small", "too_dark", "too_bright", "blur", "pose", "border", "low_det_score", "multi_face",
                "no_face", "duplicate"):
        assert f"REJ_{rej}" in texts
    for tag in ("normal", "mask", "hat", "glasses", "noglasses", "scarf", "lowlight", "backlight", "other"):
        assert HANGUL.search(T.tag_label(tag))


def test_texts_formats():
    assert T.CAMERA_BUSY.format(holder="run.py pid=1") and T.PIN_WRONG.format(n=2) == "PIN이 올바르지 않습니다. (남은 횟수 2회)"
    assert T.REMOVE_CONFIRM.format(name="홍길동", pid="u0001", n=10, locker="2").startswith("'홍길동'(u0001)")
    assert T.panel_text("2", "open", 3.44, owner="홍길동") == "사물함 2  열림 · 잠김까지 3.4초 · 사용자 홍길동"
    assert T.panel_text(None, "unknown") == "도어  상태 알 수 없음"
    assert T.panel_text("1", "cooldown", cooldown_sec=1.2) == "사물함 1  잠김 · 재개방 대기 1.2초"


# ------------------------------------------------------------------------------------------- G10 admin PIN
@pytest.fixture
def acfg(make_cfg, tmp_path):
    cfg = make_cfg(f"admin.pin_file={(tmp_path / 'pin' / 'admin_pin.json').as_posix()}",
                   "admin.pbkdf2_iterations=2000", "admin.lockout_sec=60")
    return cfg.admin


def test_g10_set_and_verify(acfg):
    assert not adminpin.pin_is_set(acfg)
    assert adminpin.verify_pin(acfg, "1234") is adminpin.PinResult.NOT_SET
    adminpin.set_pin(acfg, "135790")
    assert adminpin.pin_is_set(acfg)
    rec = json.loads(Path(acfg.pin_file).read_text(encoding="utf-8"))
    assert rec["algo"] == "pbkdf2_sha256" and rec["iterations"] == 2000 and "135790" not in json.dumps(rec)
    assert adminpin.verify_pin(acfg, "135790") is adminpin.PinResult.OK
    with pytest.raises(ValueError, match="이미"):
        adminpin.set_pin(acfg, "999999")


@pytest.mark.parametrize("pin", ["12a4", "123", "", "１２３４", "12 34"])
def test_g10_rejects_bad_pins(acfg, pin):
    with pytest.raises(ValueError) as ei:
        adminpin.set_pin(acfg, pin)
    assert HANGUL.search(str(ei.value))
    assert not Path(acfg.pin_file).exists()


def test_g10_lockout_persists_across_restart(acfg):
    adminpin.set_pin(acfg, "2468")
    t = 1_000_000.0
    assert adminpin.verify_pin(acfg, "0000", now=t) is adminpin.PinResult.WRONG
    assert adminpin.attempts_left(acfg, now=t) == 2
    assert adminpin.verify_pin(acfg, "0000", now=t + 1) is adminpin.PinResult.WRONG
    assert adminpin.verify_pin(acfg, "0000", now=t + 2) is adminpin.PinResult.LOCKED
    # correct PIN refused while locked, also after a "restart" (state comes from the file)
    importlib.reload(adminpin)
    assert adminpin.verify_pin(acfg, "2468", now=t + 30) is adminpin.PinResult.LOCKED
    assert adminpin.lock_remaining_sec(acfg, now=t + 30) == pytest.approx(32.0)
    assert adminpin.verify_pin(acfg, "2468", now=t + 63) is adminpin.PinResult.OK
    assert adminpin.attempts_left(acfg, now=t + 63) == 3


def test_g10_corrupt_files_fail_closed(acfg):
    p = Path(acfg.pin_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert not adminpin.pin_is_set(acfg)
    msg = adminpin.pin_file_error(acfg)
    assert msg and "손상" in msg
    with pytest.raises(adminpin.PinFileError):
        adminpin.verify_pin(acfg, "1234")
    with pytest.raises(adminpin.PinFileError):
        adminpin.set_pin(acfg, "1234")          # corrupt file is not "not set"
    p.write_text(json.dumps({"schema_version": 1, "algo": "md5", "iterations": 1, "salt": "00", "hash": "00"}),
                 encoding="utf-8")
    assert adminpin.pin_file_error(acfg)
    # corrupt state file -> treated as locked, not as zero failures
    p.unlink()
    adminpin.set_pin(acfg, "2468")
    Path(str(p) + ".state").write_text("garbage", encoding="utf-8")
    assert adminpin.verify_pin(acfg, "2468", now=5000.0) is adminpin.PinResult.LOCKED


# ------------------------------------------------------------------------------------------- door panel (16)
def test_door_panel_unknown_when_program_not_running(svc_factory):
    svc = svc_factory()
    rows, note = svc.door_panel()
    assert [r.state for r in rows] == ["unknown"] and rows[0].locker_id is None
    assert note == T.PANEL_PROGRAM_OFF


def test_door_panel_reads_state_when_running(svc_factory, monkeypatch):
    import sys
    import types

    svc = svc_factory()
    path = Path(svc.cfg.logging.door_state_file) if getattr(svc.cfg.logging, "door_state_file", None) else None
    row = types.SimpleNamespace(locker_id=None, state="open", hold_remaining_sec=3.2, cooldown_remaining_sec=0.0,
                                owner_label="홍길동")
    calls = []
    fake = types.ModuleType("facedoor.locker_panel")
    fake.read_state = lambda p, max_age_sec=3.0: calls.append((p, max_age_sec)) or [row]
    try:
        import facedoor.locker_panel as real
    except ImportError:
        real = fake
        monkeypatch.setitem(sys.modules, "facedoor.locker_panel", fake)
    monkeypatch.setattr(real, "read_state", fake.read_state)
    held = runlock.RunLock(svc.camera_lock_path, "run.py")
    assert held.acquire()
    try:
        rows, note = svc.door_panel()
    finally:
        held.release()
    assert note is None and rows[0].state == "open" and rows[0].owner_label == "홍길동"
    assert calls and calls[0][1] == 3.0 and (path is None or calls[0][0] == path)
    # stale / missing state -> unknown (never "closed")
    monkeypatch.setattr(real, "read_state", lambda p, max_age_sec=3.0: None)
    assert held.acquire()
    try:
        rows, note = svc.door_panel()
    finally:
        held.release()
    assert [r.state for r in rows] == ["unknown"] and note == T.PANEL_PROGRAM_OFF


# ------------------------------------------------------------------------------------------- preview / tk
def test_preview_encode_ppm_and_queue():
    from facedoor.gui import preview as pv

    img = np.full((480, 640, 3), 128, np.uint8)
    out = pv.encode_preview(img, 320, (100, 100, 120, 140), pv.COLOR_SAVED)
    assert out.fmt == "ppm" and (out.width, out.height) == (320, 240) and out.data.startswith(b"P6")
    assert img[100, 100].tolist() == [128, 128, 128]           # original untouched
    q = pv.LatestQueue()
    q.put(1)
    q.put(2)
    assert q.get() == 2 and q.get() is None


def test_font_pick_aliases():
    from facedoor.gui.admin_app import pick_font_family

    assert pick_font_family(["Malgun Gothic", "NanumGothic"], ["Arial", "맑은 고딕"]) == "맑은 고딕"
    assert pick_font_family(["Malgun Gothic", "NanumGothic"], ["nanumgothic"]) == "nanumgothic"
    assert pick_font_family(["Malgun Gothic"], ["Arial"]) is None


def _tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"Tk not available: {e}")
    root.withdraw()
    return root


def test_tk_app_builds_without_camera(svc_factory):
    """Builds the GUI hidden (no mainloop): PIN screen first, then main menu; the camera is never opened."""
    from facedoor.gui import admin_app

    svc = svc_factory("gui.require_pin=false")
    add_person(svc.db, "u0001", 4, name="홍길동", locker="2")
    root = _tk_root()
    try:
        app = admin_app.AdminApp(root, svc)
        app._enter_main()
        for key in ("register", "list", "augment", "remove", "home"):
            app.show(key)
            root.update_idletasks()
        app.panel.refresh()
        assert "단일 출력" in app.status.cget("text")
        assert FakeCamera.instances == []
        from facedoor.gui import preview as pv
        photo = pv.photo_from_preview(pv.encode_preview(np.zeros((48, 64, 3), np.uint8), 32), root)
        assert photo is None or photo.width() == 32
    finally:
        root.destroy()


def test_tk_pin_screen_builds(svc_factory, tmp_path):
    from facedoor.gui import admin_app

    svc = svc_factory(f"admin.pin_file={(tmp_path / 'p.json').as_posix()}", "admin.pbkdf2_iterations=2000")
    root = _tk_root()
    try:
        app = admin_app.AdminApp(root, svc)
        app.start()
        assert app._pin_frame is not None and app._pin_frame.setup
        app._pin_frame.pin1.insert(0, "123")
        app._pin_frame.pin2.insert(0, "124")
        app._pin_frame._submit()
        assert app._pin_frame.msg.cget("text") == T.PIN_SETUP_MISMATCH
    finally:
        root.destroy()
