"""FaceDoorApp integration in mock hardware mode (INTERFACES section 3.13, ARCHITECTURE section 9).

Real YuNet + SFace on tests/assets via FileCamera; GPIO = pure-Python mock. No camera/GPIO/network needed.
"""

from __future__ import annotations

import csv
import logging
import signal
from pathlib import Path

import pytest

from conftest import ASSETS, copy_assets, make_result, requires_models
from facedoor.app import FaceDoorApp, main, open_reason
from facedoor.camera import FileCamera
from facedoor.database import FaceDatabase
from facedoor.datatypes import Frame, FrameVerdict
from facedoor.decision import VoteDecider
from facedoor.display import NullDisplay
from facedoor.enroll import Enroller
from facedoor.errors import CameraError, ExitCode, ModelError
from facedoor.events import EVENT_COLUMNS, EventLogger
from facedoor.hardware.factory import create_hardware
from facedoor.imageio import imread
from facedoor.pipeline import FramePipeline


# ---------------------------------------------------------------------- helpers
def read_events(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert tuple(rows[0]) == EVENT_COLUMNS
    return [dict(zip(EVENT_COLUMNS, r)) for r in rows[1:]]


class OpenSpy:
    def __init__(self, door):
        self.door = door
        self.real_open = door.open
        self.calls: list[str] = []

    def __call__(self, reason, duration=None):
        self.calls.append(reason)
        return self.real_open(reason, duration)


def build(cfg, camera, pipeline, monkeypatch, tag: str = "test"):
    hw = create_hardware(cfg.hardware, register_handlers=False)
    spy = OpenSpy(hw.door)
    monkeypatch.setattr(hw.door, "open", spy)
    events = EventLogger(cfg.logging.events_csv, "sess-test", condition_tag=tag,
                         embedder_id=getattr(getattr(pipeline, "embedder", None), "model_id", "fake"))
    app = FaceDoorApp(cfg, camera, pipeline, VoteDecider(cfg.decision), hw, NullDisplay(), events)
    return app, hw, spy


LEGACY_SINGLE = ["lockers.mode=single", "lockers.require_locker_id=false", "hardware.close_mode=timer",
                 "hardware.locker_outputs=[]", "hardware.solenoid_pin=17", "hardware.limit_switch_pin=null"]


@pytest.fixture
def make_cfg(make_cfg):
    """v1.2/v1.3 behaviour (single output, fixed open_sec timer) for the legacy tests in this module; the
    limit-switch / per-locker app behaviour is covered in test_app_limit.py. Later overrides still win."""
    return lambda *overrides: make_cfg(*LEGACY_SINGLE, *overrides)


@pytest.fixture
def lena_cfg(make_cfg, tmp_path):
    """Config whose gallery contains only 'lena' (enrolled from tests/assets via the real models)."""
    cfg = make_cfg()
    from facedoor.detector import create_detector
    from facedoor.embedder import create_embedder

    det = create_detector(cfg.detector, cfg.app.models_dir)
    emb = create_embedder(cfg.embedder, cfg.app.models_dir)
    db = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=emb.model_id)
    rep = Enroller(det, emb, db, cfg.quality, cfg.enroll).enroll_images("lena", "normal", [ASSETS / "lena.jpg"])
    assert rep.saved == 1
    return cfg


def run_folder(cfg, tmp_path, monkeypatch, names, repeat=6):
    folder = copy_assets(tmp_path / "frames", names, repeat=repeat)
    pipeline = FramePipeline.from_config(cfg, calibration=None)
    app, hw, spy = build(cfg, FileCamera(folder), pipeline, monkeypatch)
    code = app.run()
    return app, hw, spy, code, read_events(cfg.logging.events_csv)


# ---------------------------------------------------------------------- real pipeline (models)
@pytest.mark.models
@requires_models
def test_filecamera_genuine_opens_and_logs(lena_cfg, tmp_path, monkeypatch):
    app, hw, spy, code, rows = run_folder(lena_cfg, tmp_path, monkeypatch, ["lena.jpg"], repeat=6)
    assert code == ExitCode.OK and app.stop_reason == "end of input"
    assert len(spy.calls) == 1  # 3/5 votes -> one OPEN, then REARM
    reason = spy.calls[0]
    assert reason.startswith("face:lena:") and reason.endswith(":3/3")
    events = [r["event"] for r in rows]
    assert events[0] == "start" and events[-1] == "stop"
    opens = [r for r in rows if r["event"] == "open"]
    assert len(opens) == 1
    o = opens[0]
    assert o["verdict"] == "MATCH" and o["decision_state"] == "OPEN" and o["best_id"] == "lena"
    assert o["door_signal"] == "1" and o["message"] == reason and o["condition_tag"] == "test"
    assert o["threshold_source"] == "default" and o["threshold"] == "0.5000" and o["votes"] == "3/3"
    assert float(o["best_score"]) >= 0.5 and o["label"] == ""
    frames = [r for r in rows if r["event"] in ("frame", "open")]
    assert len(frames) == 6 and all(r["verdict"] == "MATCH" for r in frames)
    assert [r["decision_state"] for r in frames[3:]] == ["REARM"] * 3
    # shutdown: outputs OFF and hardware cleaned
    assert not hw.door.status().is_open and hw.cleaned
    snap = hw.leds.snapshot()
    assert not (snap.green or snap.red or snap.amber)
    assert any(k == "OPEN" for _, k, _ in hw.door.history)


@pytest.mark.models
@requires_models
def test_unknown_person_never_opens(lena_cfg, tmp_path, monkeypatch):
    app, hw, spy, code, rows = run_folder(lena_cfg, tmp_path, monkeypatch, ["astronaut.png"], repeat=6)
    assert code == ExitCode.OK
    assert spy.calls == [] and app.open_calls == 0
    frames = [r for r in rows if r["event"] in ("frame", "deny")]
    assert frames and all(r["verdict"] == "UNKNOWN" for r in frames)
    assert all(r["best_id"] == "lena" and float(r["best_score"]) < 0.5 for r in frames)
    assert any(r["event"] == "deny" and r["decision_state"] == "DENIED" for r in rows)
    assert not any(r["event"] == "open" or r["door_signal"] == "1" for r in rows)
    assert not any(k == "OPEN" for _, k, _ in hw.door.history)


@pytest.mark.models
@requires_models
def test_hold_never_opens(lena_cfg, tmp_path, monkeypatch):
    app, hw, spy, code, rows = run_folder(lena_cfg, tmp_path, monkeypatch, ["messi5.jpg"], repeat=6)
    assert code == ExitCode.OK and spy.calls == []
    frames = [r for r in rows if r["event"] == "frame"]
    assert len(frames) == 6
    assert all(r["verdict"] == "HOLD" and r["hold_reason"] == "too_small" and r["best_score"] == ""
               for r in frames)
    assert all(r["decision_state"] == "HOLD" for r in frames)


@pytest.mark.models
@requires_models
@pytest.mark.parametrize("policy", ["deny"])
def test_occluded_deny_policy_blocks(make_cfg, tmp_path, monkeypatch, policy):
    """Gallery sample tagged 'mask' + occluded_policy=deny -> MATCH impossible -> no open."""
    cfg = make_cfg(f"recognition.occluded_policy={policy}")
    from facedoor.detector import create_detector
    from facedoor.embedder import create_embedder

    det = create_detector(cfg.detector, cfg.app.models_dir)
    emb = create_embedder(cfg.embedder, cfg.app.models_dir)
    db = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=emb.model_id)
    Enroller(det, emb, db, cfg.quality, cfg.enroll).enroll_images("lena", "mask", [ASSETS / "lena.jpg"])
    app, hw, spy, code, rows = run_folder(cfg, tmp_path, monkeypatch, ["lena.jpg"], repeat=5)
    assert spy.calls == []
    assert all(r["threshold_group"] == "occluded" for r in rows if r["event"] == "frame")


class FlakyCamera:
    """Yields `good` real frames, then raises `exc` (CameraError by default)."""

    def __init__(self, image, good: int, exc: BaseException | None = None, fail_open: bool = False):
        self.image, self.good, self.exc, self.fail_open = image, good, exc, fail_open
        self.i = 0
        self.closed = False

    @property
    def is_live(self) -> bool:
        return False

    def open(self):
        if self.fail_open:
            raise CameraError("cannot open camera 0")

    def read(self):
        if self.i >= self.good:
            raise self.exc or CameraError("5 consecutive read failures")
        f = Frame(self.image, self.i, float(self.i), "fake")
        self.i += 1
        return f

    def close(self):
        self.closed = True

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.mark.models
@requires_models
def test_camera_failure_closes_door_and_exits_3(lena_cfg, monkeypatch):
    pipeline = FramePipeline.from_config(lena_cfg, calibration=None)
    cam = FlakyCamera(imread(ASSETS / "lena.jpg"), good=3)
    app, hw, spy = build(lena_cfg, cam, pipeline, monkeypatch)
    code = app.run()
    assert code == ExitCode.CAMERA == 3
    assert len(spy.calls) == 1  # door was open when the camera died
    closes = [r for _, k, r in hw.door.history if k == "CLOSE"]
    assert closes and closes[0] == "error: CameraError"  # closed by step() before shutdown
    assert not hw.door.status().is_open and hw.cleaned and cam.closed
    rows = read_events(lena_cfg.logging.events_csv)
    err = [r for r in rows if r["event"] == "error"]
    assert len(err) == 1 and "CameraError" in err[0]["message"] and err[0]["frame_idx"] == "2"
    assert rows[-1]["event"] == "stop" and "exit 3" in rows[-1]["message"]


# ---------------------------------------------------------------------- fake pipeline (no models)
class FakePipeline:
    """Returns scripted RecognitionResults; items may be exceptions to raise."""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def process(self, frame):
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if isinstance(item, BaseException):
            raise item
        return make_result(item[0], item[1], idx=frame.index) if isinstance(item, tuple) else \
            make_result(item, idx=frame.index)


def blank_camera(n: int, exc: BaseException | None = None, fail_open: bool = False) -> FlakyCamera:
    import numpy as np

    return FlakyCamera(np.zeros((48, 64, 3), np.uint8), n, exc or StopIteration(), fail_open)


def test_camera_open_failure_exits_3_outputs_off(make_cfg, monkeypatch):
    cfg = make_cfg()
    app, hw, spy = build(cfg, blank_camera(0, fail_open=True), FakePipeline([FrameVerdict.NO_FACE]), monkeypatch)
    assert app.run() == ExitCode.CAMERA
    assert spy.calls == [] and hw.cleaned and not hw.door.status().is_open
    rows = read_events(cfg.logging.events_csv)
    assert [r["event"] for r in rows] == ["error", "stop"]


def test_unexpected_exception_closes_door_and_continues(make_cfg, monkeypatch):
    cfg = make_cfg()
    M = FrameVerdict.MATCH
    script = [(M, "a"), (M, "a"), (M, "a"), RuntimeError("boom"), (FrameVerdict.NO_FACE, None)]
    app, hw, spy = build(cfg, blank_camera(5), FakePipeline(script), monkeypatch)
    assert app.run() == ExitCode.OK
    assert len(spy.calls) == 1 and spy.calls[0] == open_reason("a", 0.8, 3, 3) == "face:a:0.8000:3/3"
    closes = [r for _, k, r in hw.door.history if k == "CLOSE"]
    assert closes and closes[0] == "error: RuntimeError"
    rows = read_events(cfg.logging.events_csv)
    assert any(r["event"] == "error" and "boom" in r["message"] for r in rows)
    assert any(r["event"] == "door_close" for r in rows)


def test_votes_do_not_carry_over_an_exception(make_cfg, monkeypatch):
    """QA A-1: MATCH, MATCH, error, MATCH must not combine into an OPEN (decider reset after error)."""
    cfg = make_cfg()
    M = FrameVerdict.MATCH
    script = [(M, "a"), (M, "a"), RuntimeError("boom"), (M, "a"), (FrameVerdict.NO_FACE, None)]
    app, hw, spy = build(cfg, blank_camera(5), FakePipeline(script), monkeypatch)
    resets = []
    real_reset = app.decider.reset
    monkeypatch.setattr(app.decider, "reset", lambda: (resets.append(1), real_reset())[1])
    assert app.run() == ExitCode.OK
    assert resets, "decider.reset() not called on the error path"
    assert spy.calls == [] and app.open_calls == 0
    assert not any(k == "OPEN" for _, k, _ in hw.door.history)
    # (control: 3 consecutive MATCH without an error opens -> test_unexpected_exception_closes_door_and_continues)


def test_fatal_model_error_exit_code(make_cfg, monkeypatch):
    cfg = make_cfg()
    app, hw, spy = build(cfg, blank_camera(5), FakePipeline([ModelError("bad model")]), monkeypatch)
    assert app.run() == ExitCode.MODEL and hw.cleaned


def test_keyboard_interrupt_turns_outputs_off(make_cfg, monkeypatch):
    cfg = make_cfg()
    M = FrameVerdict.MATCH
    app, hw, spy = build(cfg, blank_camera(3, exc=KeyboardInterrupt()), FakePipeline([(M, "a")]), monkeypatch)
    assert app.run() == ExitCode.INTERRUPTED == 130
    assert len(spy.calls) == 1 and not hw.door.status().is_open and hw.cleaned


def test_sigint_signal_turns_outputs_off(make_cfg, monkeypatch):
    """Real SIGINT through hardware.factory's handler while the door is open."""
    cfg = make_cfg()
    M = FrameVerdict.MATCH
    original = signal.getsignal(signal.SIGINT)
    try:
        hw = create_hardware(cfg.hardware, register_handlers=True)
        signals_seen = []

        class SigCamera(FlakyCamera):
            def read(self):
                if self.i >= 3:
                    signals_seen.append(hw.door.status().is_open)
                    signal.raise_signal(signal.SIGINT)
                return super().read()

        import numpy as np

        events = EventLogger(cfg.logging.events_csv, "sig", embedder_id="fake")
        app = FaceDoorApp(cfg, SigCamera(np.zeros((8, 8, 3), np.uint8), 99), FakePipeline([(M, "a")]),
                          VoteDecider(cfg.decision), hw, NullDisplay(), events)
        assert app.run() == ExitCode.INTERRUPTED
        assert signals_seen == [True]  # door was open when the signal arrived
        assert not hw.door.status().is_open and hw.cleaned
    finally:
        signal.signal(signal.SIGINT, original)


def test_process_every_n_never_repeats_open(make_cfg, monkeypatch):
    cfg = make_cfg("runtime.process_every_n=2", "decision.rearm_sec=0.0", "hardware.cooldown_sec=0.0")
    M = FrameVerdict.MATCH
    pipe = FakePipeline([(M, "a")])
    app, hw, spy = build(cfg, blank_camera(6), pipe, monkeypatch)
    app.run()
    assert pipe.i == 3 and app.frames_read == 6
    assert len(spy.calls) == 1


def test_log_every_frame_false_logs_only_decisions(make_cfg, monkeypatch):
    cfg = make_cfg("logging.log_every_frame=false")
    M = FrameVerdict.MATCH
    app, hw, spy = build(cfg, blank_camera(5), FakePipeline([(M, "a")]), monkeypatch)
    app.run()
    rows = read_events(cfg.logging.events_csv)
    assert [r["event"] for r in rows if r["event"] not in ("start", "stop", "door_close")] == ["open"]


def test_require_locker_id_suppresses_open(make_cfg, monkeypatch):
    cfg = make_cfg("lockers.require_locker_id=true")
    M = FrameVerdict.MATCH

    class P(FakePipeline):
        class recognizer:  # noqa: N801
            class db:  # noqa: N801
                @staticmethod
                def locker_id(pid):
                    return None
            policy = None

    app, hw, spy = build(cfg, blank_camera(5), P([(M, "a")]), monkeypatch)
    app.run()
    assert spy.calls == []
    rows = read_events(cfg.logging.events_csv)
    assert any("open suppressed" in r["message"] for r in rows if r["event"] == "open")


def test_shutdown_idempotent(make_cfg, monkeypatch):
    cfg = make_cfg()
    app, hw, spy = build(cfg, blank_camera(0), FakePipeline([FrameVerdict.NO_FACE]), monkeypatch)
    app.run()
    app.shutdown("again")
    rows = read_events(cfg.logging.events_csv)
    assert [r["event"] for r in rows].count("stop") == 1


# ---------------------------------------------------------------------- CLI
@pytest.mark.models
@requires_models
def test_main_cli_headless(lena_cfg, tmp_path):
    folder = copy_assets(tmp_path / "cli", ["lena.jpg"], repeat=4)
    c = lena_cfg
    sets = []
    for k, v in (("app.gallery_dir", c.app.gallery_dir), ("app.calibration_file", c.app.calibration_file),
                 ("logging.events_csv", c.logging.events_csv), ("logging.app_log", c.logging.app_log),
                 ("hardware.hardware_log", c.hardware.hardware_log)):
        sets += ["--set", f"{k}={Path(v).as_posix()}"]
    original = signal.getsignal(signal.SIGINT)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        code = main(["--source", str(folder), "--no-display", "--tag", "cli", "--max-frames", "10",
                     "--set", "hardware.mode=mock", "--set", "runtime.max_fps=0"] + sets)
    finally:
        signal.signal(signal.SIGINT, original)
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    assert code == 0
    rows = read_events(c.logging.events_csv)
    assert sum(r["event"] == "open" for r in rows) == 1 and all(r["condition_tag"] == "cli" for r in rows)


def test_main_config_error_exit_code():
    assert main(["--set", "decision.min_votes=99", "--no-display"]) == ExitCode.CONFIG
