"""App integration for limit-switch lockers (INTERFACES 3.13, 15.4, 16): routing, door events -> events.csv,
door_state.json schema 2, simulation keys. Real mock hardware (close_mode=limit), scripted recognition, no models.

Real-time waits are short (unlock_timeout_sec has a 1.0 s minimum in the config).
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import numpy as np
import pytest

from conftest import make_result
from facedoor.app import FaceDoorApp
from facedoor.datatypes import Frame, FrameVerdict
from facedoor.decision import VoteDecider
from facedoor.display import NullDisplay
from facedoor.events import EVENT_COLUMNS, EventLogger
from facedoor.hardware.factory import create_hardware

LIMIT_SETS = [
    "hardware.close_mode=limit", "lockers.mode=per_locker", "hardware.solenoid_pin=null",
    "hardware.locker_outputs=[{locker_id: '1', pin: 24, limit_pin: 5}, {locker_id: '2', pin: 25, limit_pin: 6}]",
    "hardware.max_concurrent_open=2",
    "hardware.limit_poll_sec=0.01", "hardware.open_confirm_sec=0.0", "hardware.close_confirm_sec=0.5",
    "hardware.unlock_timeout_sec=1.0", "hardware.max_unlock_sec=3.0", "hardware.sensor_fault_sec=0.2",
    "hardware.cooldown_sec=0.0", "decision.rearm_sec=0.0",
]


class FakeDB:
    def __init__(self, mapping: dict[str, str | None]):
        self.mapping = mapping

    def all_person_ids(self):
        return sorted(self.mapping)

    def person_info(self, pid):
        return {"locker_id": self.mapping.get(pid)}

    def display_name(self, pid):
        return {"alice": "앨리스"}.get(pid, pid)


class ScriptedPipeline:
    """MATCH `who` for frames while `self.active`, NO_FACE otherwise."""

    class recognizer:  # noqa: N801
        policy = None
        db = None

    def __init__(self, db: FakeDB, who: str = "alice"):
        self.recognizer = type("R", (), {"db": db, "policy": None})()
        self.who = who
        self.active = True

    def process(self, frame):
        if self.active:
            return make_result(FrameVerdict.MATCH, self.who, 0.8, idx=frame.index)
        return make_result(FrameVerdict.NO_FACE, idx=frame.index)


class EndlessCamera:
    is_live = False

    def __init__(self):
        self.i = 0

    def open(self):
        pass

    def read(self):
        self.i += 1
        return Frame(np.zeros((48, 64, 3), np.uint8), self.i, float(self.i), "fake")

    def close(self):
        pass


class SpyDisplay(NullDisplay):
    def __init__(self):
        self.keys: list[int] = []
        self.rows = []
        self.notices: list[str] = []
        self.sim_handler = "unset"

    def show(self, *a, **k):
        return self.keys.pop(0) if self.keys else -1

    def set_panel(self, rows):
        self.rows = list(rows)

    def set_notice(self, text, ttl=4.0):
        self.notices.append(text)

    def set_sim_handler(self, cb):
        self.sim_handler = cb


def read_rows(path: Path):
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert tuple(rows[0]) == EVENT_COLUMNS and len(EVENT_COLUMNS) == 37
    return [dict(zip(EVENT_COLUMNS, r)) for r in rows[1:]]


@pytest.fixture
def env(make_cfg, tmp_path):
    cfg = make_cfg(*LIMIT_SETS, f"logging.door_state_file={(tmp_path / 'logs' / 'door_state.json').as_posix()}")
    hw = create_hardware(cfg.hardware, register_handlers=False)
    db = FakeDB({"alice": "2", "bob": None})
    pipe = ScriptedPipeline(db)
    disp = SpyDisplay()
    events = EventLogger(cfg.logging.events_csv, "sess", embedder_id="fake")
    app = FaceDoorApp(cfg, EndlessCamera(), pipe, VoteDecider(cfg.decision), hw, disp, events)
    app.start()

    class E:
        pass

    e = E()
    e.cfg, e.hw, e.db, e.pipe, e.disp, e.app = cfg, hw, db, pipe, disp, app
    e.csv = cfg.logging.events_csv
    e.state = cfg.logging.door_state_file

    def pump(cond, timeout=4.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            app.step()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def state(lid):
        return next(s for s in hw.door.locker_states() if s.locker_id == lid).state.value

    e.pump, e.state_of = pump, state
    yield e
    app.shutdown("test end")


def open_alice(e):
    assert e.pump(lambda: e.state_of("2") == "UNLOCKED_WAITING")
    e.pipe.active = False


def test_open_goes_to_the_winners_locker(env):
    open_alice(env)
    assert env.state_of("1") == "LOCKED_CLOSED"
    env.app.step()
    env.app.shutdown("done")
    rows = read_rows(env.csv)
    o = [r for r in rows if r["event"] == "open"]
    assert len(o) == 1
    assert o[0]["locker_id"] == "2" and o[0]["door_signal"] == "1"
    assert o[0]["message"].endswith(":locker=2") and o[0]["message"].startswith("face:alice:0.8000:")
    assert o[0]["locker_states"] == "1=LOCKED_CLOSED;2=UNLOCKED_WAITING"
    assert rows[0]["event"] == "start" and "lockers=per_locker" in rows[0]["message"]
    assert any("사물함 2" in n and "잠금 해제" in n for n in env.disp.notices)


def test_person_without_locker_never_opens(env):
    env.pipe.who = "bob"
    env.app.step(), env.app.step(), env.app.step(), env.app.step()
    assert all(s.state.value == "LOCKED_CLOSED" for s in env.hw.door.locker_states())
    o = [r for r in read_rows(env.csv) if r["event"] == "open"]
    assert o and "has no locker_id (lockers.mode=per_locker)" in o[0]["message"] and o[0]["door_signal"] == "0"
    assert any("배정되지 않았습니다" in n for n in env.disp.notices)


def test_open_rejected_when_door_is_already_open(env):
    env.hw.limits.set_closed("2", False)
    assert env.pump(lambda: env.state_of("2") == "LOCKED_OPEN")
    env.pump(lambda: any(r["event"] == "open" for r in read_rows(env.csv)), timeout=1.0)
    o = [r for r in read_rows(env.csv) if r["event"] == "open"]
    assert o and o[0]["message"].endswith("(rejected by door: door_open)") and o[0]["door_signal"] == "0"
    assert env.state_of("2") == "LOCKED_OPEN"
    assert any("문이 열려 있어 해제할 수 없습니다" in n for n in env.disp.notices)


def test_open_rejected_on_sensor_fault(env):
    env.hw.limits.set_fault("2", True)
    env.app.step(), env.app.step(), env.app.step()
    o = [r for r in read_rows(env.csv) if r["event"] == "open"]
    assert o and o[0]["message"].endswith("(rejected by door: sensor_fault)") and o[0]["door_signal"] == "0"
    assert any("센서 오류" in n for n in env.disp.notices)


def test_door_events_become_events_csv_rows(env):
    open_alice(env)
    env.hw.limits.set_closed("2", False)                       # open the door
    assert env.pump(lambda: env.state_of("2") == "OPEN")
    env.app.step()
    env.hw.limits.set_closed("2", True)                        # close it again, 0.5 s confirm
    assert env.pump(lambda: env.state_of("2") in ("COOLDOWN", "LOCKED_CLOSED"))
    env.app.step()
    rows = read_rows(env.csv)
    opened = [r for r in rows if r["event"] == "door_opened"]
    closed = [r for r in rows if r["event"] == "door_close"]
    assert len(opened) == 1 and opened[0]["locker_id"] == "2" and opened[0]["door_signal"] == "1"
    assert opened[0]["locker_states"].startswith("1=LOCKED_CLOSED;2=")
    assert len(closed) == 1 and closed[0]["close_cause"] == "close_confirmed"
    assert closed[0]["message"].startswith("cause=close_confirmed ") and closed[0]["door_signal"] == "0"
    assert closed[0]["locker_id"] == "2" and closed[0]["verdict"] == ""      # face columns blank
    assert any("닫혀 잠갔습니다" in n for n in env.disp.notices)


def test_unlock_timeout_relock_and_forced_open_and_sensor_rows(env):
    open_alice(env)
    assert env.pump(lambda: any(r["event"] == "door_close" for r in read_rows(env.csv)), timeout=4.0)
    closed = [r for r in read_rows(env.csv) if r["event"] == "door_close"]
    assert closed[0]["close_cause"] == "unlock_timeout"
    assert any("문이 열리지 않아 사물함 2를 다시 잠갔습니다" in n for n in env.disp.notices)
    # forced open of locker 1 (no unlock)
    env.hw.limits.set_closed("1", False)
    assert env.pump(lambda: any(r["event"] == "door_forced" for r in read_rows(env.csv)))
    env.hw.limits.set_closed("1", True)
    # sensor fault on locker 1 -> door_sensor row
    env.hw.limits.set_fault("1", True)
    assert env.pump(lambda: any(r["event"] == "door_sensor" for r in read_rows(env.csv)))
    rows = read_rows(env.csv)
    forced = [r for r in rows if r["event"] == "door_forced"][0]
    assert forced["locker_id"] == "1" and forced["door_signal"] == "0"
    sensor = [r for r in rows if r["event"] == "door_sensor"][0]
    assert "sensor_fault" in sensor["message"] and sensor["locker_id"] == "1"
    assert "1=SENSOR_FAULT" in sensor["locker_states"]


def test_door_state_json_schema2_and_final_unknown(env):
    open_alice(env)
    env.app.step()
    data = json.loads(Path(env.state).read_text(encoding="utf-8"))
    assert data["schema_version"] == 2 and data["session_id"] == "sess"
    assert [r["locker_id"] for r in data["rows"]] == ["1", "2"]
    r2 = data["rows"][1]
    assert r2["state"] == "UNLOCKED_WAITING" and r2["door"] == "closed" and r2["unlocked"] is True
    assert r2["countdown_kind"] == "unlock_wait" and r2["owner_label"] == "앨리스" and r2["sim_toggle"] is True
    from facedoor.locker_panel import read_state

    assert read_state(env.state) is not None
    env.app.shutdown("bye")
    rows = read_state(env.state)
    assert rows is not None and all(r.state == "UNKNOWN" and r.door == "unknown" for r in rows)


def test_panel_rows_reach_display(env):
    env.app.step()
    assert [r.locker_id for r in env.disp.rows] == ["1", "2"]
    assert all(r.door == "closed" and r.sim_toggle for r in env.disp.rows)
    assert callable(env.disp.sim_handler)


def test_sim_keys_toggle_doors_and_inject_faults(env):
    env.disp.keys = [ord("1")]
    env.app.step()
    assert env.hw.limits.read("1") is False and env.hw.limits.read("2") is True
    env.disp.keys = [ord("1")]
    env.app.step()
    assert env.hw.limits.read("1") is True
    env.disp.keys = [ord("6")]                       # sensor fault of locker 2
    env.app.step()
    assert env.hw.limits.read("2") is None
    env.disp.keys = [ord("6")]
    env.app.step()
    assert env.hw.limits.read("2") is True
    env.disp.keys = [ord("4")]                       # no locker 4 configured: ignored
    env.app.step()
    assert env.hw.limits.read("1") is True and env.hw.limits.read("2") is True


def test_sim_click_handler_toggles(env):
    env.disp.sim_handler("2", "toggle")
    assert env.hw.limits.read("2") is False


def test_keys_ignored_when_not_simulated(env):
    env.hw.limits.is_simulated = False
    env.app._sim = bool(getattr(env.hw.limits, "is_simulated", False))
    env.app._configure_display()
    assert env.disp.sim_handler is None
    env.disp.keys = [ord("1"), ord("5")]
    env.app.step(), env.app.step()
    assert env.hw.limits.read("1") is True and not env.hw.limits.is_fault("1")


def test_quit_key_still_stops(env):
    env.disp.keys = [ord("q")]
    assert env.app.step() is False
