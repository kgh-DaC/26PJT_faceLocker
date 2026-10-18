"""Limit-sensor lock control tests (owner: hw-engineer). INTERFACES 15 scenarios L1-L17.

Deterministic: a fake clock + MockLimitInputs/PiLimitInputs + ``door.poll_once()`` (no polling thread, no
sleeps) on both backends: the pure-Python mock ("state") and the Pi code path on gpiozero's MockFactory.
Only the thread tests (L10) use real time, with a few milliseconds of sleeping.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import pytest

from facedoor.errors import ConfigError, HardwareError
from facedoor.hardware import factory as hw_factory
from facedoor.hardware.base import (
    ABSOLUTE_MAX_UNLOCK_SEC,
    ON_STATES,
    CloseCause,
    DoorState,
    HardwareConfig,
    LockerState,
)
from facedoor.hardware.gpio_mock import MockDoorController, MockStatusLeds
from facedoor.hardware.limits import MockLimitInputs, PiLimitInputs

try:
    import gpiozero  # noqa: F401
    from gpiozero.pins.mock import MockFactory

    HAVE_GPIOZERO = True
except Exception:  # noqa: BLE001
    HAVE_GPIOZERO = False

BACKENDS = ["state", pytest.param("gpiozero", marks=pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing"))]

L = LockerState
DT = 0.01
LOCKERS4 = [{"locker_id": "1", "pin": 24, "limit_pin": 5}, {"locker_id": "2", "pin": 25, "limit_pin": 6},
            {"locker_id": "3", "pin": 16, "limit_pin": 12}, {"locker_id": "4", "pin": 26, "limit_pin": 13}]


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_cfg(tmp_path: Path, **kw) -> HardwareConfig:
    base = dict(close_mode="limit", cooldown_sec=2.0, deny_blink_sec=0.02, hardware_log=str(tmp_path / "hw.log"))
    base.update(kw)
    return HardwareConfig.from_dict(base)


def single_cfg(tmp_path: Path, **kw) -> HardwareConfig:
    return make_cfg(tmp_path, limit_switch_pin=5, **kw)


def locker_cfg(tmp_path: Path, n: int = 4, **kw) -> HardwareConfig:
    return make_cfg(tmp_path, solenoid_pin=None, locker_outputs=LOCKERS4[:n], **kw)


class Rig:
    """Door + leds + fake clock + driver helpers."""

    def __init__(self, backend: str, cfg: HardwareConfig, limits=None) -> None:
        self.clock = FakeClock()
        self.cfg = cfg
        self.backend = backend
        if backend == "state":
            self.door = MockDoorController(cfg, clock=self.clock, limits=limits, start_thread=False)
            self.leds = MockStatusLeds(cfg, self.door)
        else:
            from facedoor.hardware.gpio_pi import PiDoorController, PiStatusLeds

            self.factory = MockFactory()
            self.door = PiDoorController(cfg, pin_factory=self.factory, clock=self.clock, limits=limits,
                                         start_thread=False)
            self.leds = PiStatusLeds(cfg, self.door)
        self.limits = self.door.limits

    # -- time -----------------------------------------------------------------------------
    def run(self, secs: float) -> None:
        """Poll every DT seconds for ``secs`` (polls at t0, t0+DT, ..., t0+secs-DT; clock ends at t0+secs)."""
        t0 = self.clock.t
        n = round(secs / DT)
        for i in range(n):
            self.clock.t = t0 + i * DT
            self.door.poll_once()
        self.clock.t = t0 + n * DT

    def poll(self) -> None:
        self.door.poll_once()

    # -- sensors ----------------------------------------------------------------------------
    def open_door(self, lid=None) -> None:
        self.limits.set_closed(lid, False)

    def close_door(self, lid=None) -> None:
        self.limits.set_closed(lid, True)

    # -- observers ----------------------------------------------------------------------------
    def st(self, lid=None) -> LockerState:
        return self.door.locker_state(lid).state

    def on(self, lid=None) -> bool:
        if self.cfg.per_locker:
            return self.door.channel_levels()[lid] if self.backend == "state" else bool(
                self.door.channel_devices[lid].value)
        return self.door.signal_on()

    def solenoid_on_count(self, name="solenoid") -> int:
        assert self.backend == "state"
        return sum(1 for _, n, _, lvl in self.door.pins.history if n == name and lvl == 1)

    def cleanup(self) -> None:
        self.leds.cleanup()
        self.door.cleanup()


@pytest.fixture
def rigs():
    made: list[Rig] = []

    def make(backend, cfg, limits=None) -> Rig:
        r = Rig(backend, cfg, limits)
        made.append(r)
        return r

    yield make
    for r in made:
        r.cleanup()


def kinds(events) -> list[str]:
    return [e.kind for e in events]


# ===========================================================================
# L1 unlock timeout
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l1_unlock_timeout_exactly_and_cooldown(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    assert r.st() is L.LOCKED_CLOSED
    assert r.door.open("face:a:0.7:3/3") is True
    assert r.st() is L.UNLOCKED_WAITING and r.on() and r.leds.snapshot().green
    r.run(9.9)
    assert r.on() and r.st() is L.UNLOCKED_WAITING  # 9.9 s: still unlocked
    s = r.door.locker_state()
    assert 0.0 < s.unlock_wait_remaining_sec <= 0.11 and s.hard_remaining_sec > 19.0
    r.poll()  # t = +9.9 -> not yet
    r.clock.t += 0.1
    r.poll()  # t = +10.0
    assert not r.on() and not r.leds.snapshot().green
    s = r.door.locker_state()
    assert s.state is L.COOLDOWN and s.last_close_cause == "unlock_timeout" and not s.solenoid_on
    assert r.door.open("again") is False and r.door.last_reject == "cooldown"
    r.run(1.9)
    assert r.st() is L.COOLDOWN
    r.run(0.2)
    assert r.st() is L.LOCKED_CLOSED
    assert kinds(r.door.drain_events()) == ["unlocked", "relock"]


# ===========================================================================
# L2 door opens -> stays unlocked for as long as it is open
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l2_open_door_keeps_unlock_beyond_timeout(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.run(5.0)
    r.open_door()
    r.run(0.05)
    assert r.st() is L.UNLOCKED_WAITING  # 50 ms < open_confirm_sec
    r.run(0.1)
    assert r.st() is L.OPEN and r.door.locker_state().door_closed is False
    r.run(20.0)  # t = +25.15 s, way past unlock_timeout_sec (10 s) but before max_unlock_sec (30 s)
    assert r.st() is L.OPEN and r.on()
    ev = kinds(r.door.drain_events())
    assert ev == ["unlocked", "door_opened"]


# ===========================================================================
# L3 close confirmation
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l3_close_confirm_exact_and_no_chatter(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.open_door()
    r.run(0.5)
    assert r.st() is L.OPEN
    r.close_door()
    r.run(1.9)
    s = r.door.locker_state()
    assert s.state is L.CLOSING_CONFIRM and s.solenoid_on and 0.0 < s.close_confirm_remaining_sec <= 0.11
    r.open_door()  # 1.9 s of "closed" then open again: nothing locks
    r.run(0.5)
    assert r.st() is L.OPEN and r.on()
    r.close_door()
    r.run(1.99)
    assert r.on() and r.st() is L.CLOSING_CONFIRM  # 1.99 s: still ON
    r.poll()  # 1.99
    assert r.on()
    r.clock.t += DT
    r.poll()  # 2.00
    assert not r.on() and r.st() is L.COOLDOWN
    assert r.door.locker_state().last_close_cause == "close_confirmed"
    if r.backend == "state":
        assert r.solenoid_on_count() == 1  # exactly one ON transition: no output chatter at all


@pytest.mark.parametrize("backend", BACKENDS)
def test_l3_output_never_toggles_during_close_chatter(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.open_door()
    r.run(0.5)
    for _ in range(10):  # door rattles: 0.5 s closed / 0.02 s open
        r.close_door()
        r.run(0.5)
        r.open_door()
        r.run(0.02)
        assert r.on()
    if r.backend == "state":
        rows = [lvl for _, n, _, lvl in r.door.pins.history if n == "solenoid"]
        assert rows == [0, 1]  # setup row + a single ON


# ===========================================================================
# L4 absolute watchdog
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l4_watchdog_from_unlock_time_then_locked_open(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.open_door()
    r.run(10.0)
    r.close_door()
    r.run(1.0)
    r.open_door()  # open/close cycles do not reset the watchdog
    r.run(10.0)
    r.close_door()
    r.run(1.0)
    r.open_door()
    r.run(7.9)  # t = +29.9
    assert r.on()
    assert 0.0 < r.door.locker_state().hard_remaining_sec <= 0.11
    r.run(0.1)
    r.poll()  # t = +30.0
    assert not r.on()
    s = r.door.locker_state()
    assert s.state is L.COOLDOWN and s.last_close_cause == "watchdog"
    r.run(2.1)
    assert r.st() is L.LOCKED_OPEN  # the door is still open
    assert r.door.open("again") is False and r.door.last_reject == "door_open"
    r.close_door()
    r.run(1.9)
    assert r.st() is L.LOCKED_OPEN
    r.run(0.2)
    assert r.st() is L.LOCKED_CLOSED
    assert "relock" in kinds(r.door.drain_events())


@pytest.mark.parametrize("backend", BACKENDS)
def test_l4_cooldown_expiry_after_open_door_needs_close_confirm(backend, tmp_path, rigs):
    """H-1: a single pressed sample at cooldown expiry must not skip close_confirm after a watchdog relock."""
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.open_door()
    r.run(30.0)
    r.poll()
    assert r.st() is L.COOLDOWN and r.door.locker_state().last_close_cause == "watchdog"
    r.run(1.9)
    r.close_door()
    r.run(0.1)  # a short touch right at the end of the cooldown
    r.poll()
    assert r.st() is L.LOCKED_OPEN and not r.on()
    assert r.door.open("again") is False
    r.open_door()
    r.run(0.5)
    r.close_door()
    r.run(1.9)
    assert r.st() is L.LOCKED_OPEN  # pressed for < close_confirm_sec
    r.run(0.2)
    assert r.st() is L.LOCKED_CLOSED and not r.on()


@pytest.mark.parametrize("backend", BACKENDS)
def test_h3_unreadable_sample_is_never_locked_closed(backend, tmp_path, rigs):
    """H-3: lazy cooldown expiry with an unreadable last sample must not show LOCKED_CLOSED."""
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.run(10.1)  # unlock_timeout (door never opened) -> COOLDOWN
    assert r.st() is L.COOLDOWN
    r.limits.set_fault(None, True)
    r.poll()
    r.clock.t += 2.5  # past the cooldown, no further polling
    st = r.door.locker_state().state
    assert st is not L.LOCKED_CLOSED
    assert r.door.open("again") is False


@pytest.mark.parametrize("backend", BACKENDS)
def test_l4_watchdog_via_lazy_status_without_polling(backend, tmp_path, rigs):
    """main loop + polling thread both dead: status() alone still enforces max_unlock_sec."""
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.clock.t += 29.9
    assert r.door.status().is_open
    r.clock.t += 0.1
    st = r.door.status()
    assert not st.is_open and st.close_cause == "watchdog" and not r.on()


# ===========================================================================
# L5 contact bounce
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l5_bounce_does_not_open_or_close(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.open_door()
    r.run(0.05)  # 50 ms release < open_confirm_sec
    r.close_door()
    r.run(0.5)
    assert r.st() is L.UNLOCKED_WAITING
    r.open_door()
    r.run(0.3)
    assert r.st() is L.OPEN
    # pressed 1.5 s, one 20 ms release sample, pressed 1.5 s -> the 2 s counter restarted: still ON
    r.close_door()
    r.run(1.5)
    r.open_door()
    r.run(0.02)
    assert r.st() is L.OPEN
    r.close_door()
    r.run(1.5)
    assert r.on() and r.st() is L.CLOSING_CONFIRM
    r.run(0.6)
    assert not r.on() and r.door.locker_state().last_close_cause == "close_confirmed"


# ===========================================================================
# L6 sensor failure
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l6_sensor_fault_while_open_locks_at_sensor_fault_sec(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.open_door()
    r.run(0.5)
    r.limits.set_fault(None, True)
    r.run(0.9)
    assert r.on() and r.door.locker_state().door_closed is None  # unknown is not "closed"
    r.limits.set_fault(None, False)  # recovered after 0.9 s: nothing happens
    r.run(0.1)
    assert r.on() and r.st() is L.OPEN
    r.limits.set_fault(None, True)
    r.run(0.99)
    assert r.on()
    r.poll()
    r.clock.t += DT
    r.poll()  # 1.00 s of None
    assert not r.on()
    s = r.door.locker_state()
    assert s.state is L.SENSOR_FAULT and not s.sensor_ok and s.last_close_cause == "sensor_fault"
    assert r.door.open("again") is False and r.door.last_reject == "sensor_fault"
    # recovery: valid samples for close_confirm_sec
    r.limits.set_fault(None, False)
    r.run(1.9)
    assert r.st() is L.SENSOR_FAULT
    r.run(0.2)
    assert r.st() in (L.COOLDOWN, L.LOCKED_OPEN)  # the door was left open
    r.run(2.5)
    assert r.st() is L.LOCKED_OPEN
    ev = kinds(r.door.drain_events())
    assert ev.count("sensor_fault") == 1 and ev.count("sensor_ok") == 1


# ===========================================================================
# L7 NO wiring: unplugged sensor reads "open"
# ===========================================================================


def test_l7_unplugged_from_start_never_unlocks(tmp_path, rigs):
    cfg = single_cfg(tmp_path)
    lim = MockLimitInputs(cfg)
    lim.set_closed(None, False)  # reads "released" from the very beginning
    r = rigs("state", cfg, lim)
    assert r.st() is L.LOCKED_OPEN
    for _ in range(3):
        assert r.door.open("face") is False and r.door.last_reject == "door_open"
    assert r.solenoid_on_count() == 0 and not r.on()


@pytest.mark.parametrize("backend", BACKENDS)
def test_l7_request_time_reread_catches_open_door(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    assert r.st() is L.LOCKED_CLOSED
    r.open_door()  # no poll happened yet: the synchronous re-read must see it
    assert r.door.open("face") is False and r.door.last_reject == "door_open"
    assert not r.on()


@pytest.mark.parametrize("backend", BACKENDS)
def test_l7_wire_pulled_after_unlock_still_ends_at_watchdog(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("face")
    r.open_door()  # wire falls off right after the unlock: reads "released" forever
    r.run(0.3)
    assert r.st() is L.OPEN
    r.run(29.0)
    assert r.on()
    r.run(0.8)  # crosses unlocked_at + 30 s
    assert not r.on() and r.door.locker_state().last_close_cause == "watchdog"


# ===========================================================================
# L8 start-up check
# ===========================================================================


def test_l8_unreadable_channel_at_start_is_sensor_fault_others_work(tmp_path, rigs, caplog):
    cfg = locker_cfg(tmp_path, n=4)
    lim = MockLimitInputs(cfg)
    lim.set_fault("2", True)
    with caplog.at_level(logging.WARNING, logger="facedoor.hardware"):
        r = rigs("state", cfg, lim)
    states = {s.locker_id: s.state for s in r.door.locker_states()}
    assert states == {"1": L.LOCKED_CLOSED, "2": L.SENSOR_FAULT, "3": L.LOCKED_CLOSED, "4": L.LOCKED_CLOSED}
    assert r.door.open("a", locker_id="2") is False and r.door.last_reject == "sensor_fault"
    assert r.door.open("a", locker_id="1") is True
    assert [e.kind for e in r.door.drain_events()][0] == "sensor_fault"
    assert any("SENSOR" == e for _, e, _ in log_rows(cfg))


class _DeadLimits:
    is_simulated = False

    def read(self, locker_id):
        return None

    def close(self):
        pass


@pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing")
def test_l8_all_sensors_unreadable_on_pi_path_is_hardware_error(tmp_path):
    from facedoor.hardware.gpio_pi import PiDoorController

    cfg = locker_cfg(tmp_path, n=2)
    factory = MockFactory()
    with pytest.raises(HardwareError, match="limit"):
        PiDoorController(cfg, pin_factory=factory, limits=_DeadLimits(), start_thread=False)
    # outputs OFF and released
    for pin in (24, 25, 27):
        assert factory.pin(pin).state == 0
    # the mock (PC) path starts anyway
    door = MockDoorController(cfg, limits=_DeadLimits(), start_thread=False)
    try:
        assert all(s.state is L.SENSOR_FAULT for s in door.locker_states())
    finally:
        door.cleanup()


@pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing")
def test_l8_pi_path_requires_limit_pins(tmp_path):
    from facedoor.hardware.gpio_pi import PiDoorController

    cfg = make_cfg(tmp_path, solenoid_pin=None, locker_outputs=[{"locker_id": "1", "pin": 24}])
    with pytest.raises(ConfigError, match="limit pin"):
        PiDoorController(cfg, pin_factory=MockFactory(), start_thread=False)


# ===========================================================================
# L9 request-time re-read
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l9_reread_failure_refuses_without_output_change(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.limits.set_fault(None, True)  # first poll has not noticed yet
    assert r.st() is L.LOCKED_CLOSED
    assert r.door.open("face") is False and r.door.last_reject == "sensor_fault"
    assert not r.on() and not r.leds.snapshot().green
    assert r.st() is L.LOCKED_CLOSED  # state untouched (the polling thread judges persistence)


# ===========================================================================
# L10 independence from the main loop
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l10_poll_once_alone_drives_all_transitions(backend, tmp_path, rigs):
    """No app.step(), no status() calls: only poll_once() (what the hardware thread does)."""
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.open_door()
    r.run(0.5)
    assert r.door._channels[0].state is L.OPEN
    r.close_door()
    r.run(2.1)
    assert r.door._channels[0].state is L.COOLDOWN and not r.on()


def test_l10_independent_timer_enforces_max_unlock_when_everything_else_is_dead(tmp_path, rigs):
    r = rigs("state", single_cfg(tmp_path))
    r.door.open("x")
    ch = r.door._channels[0]
    t = ch.timer
    assert t is not None and t.daemon and abs(t.interval - 30.0) < 0.5  # armed at max_unlock_sec
    assert r.door.monitor_alive() is False  # no thread in this test; nobody calls poll_once or status
    r.clock.t += 30.0
    r.door._on_timer(ch.idx, ch.timer_token)  # what threading.Timer calls
    assert not r.on()
    assert r.door._channels[0].close_cause == "watchdog"


def _wait(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_l10_polling_thread_runs_the_state_machine_in_real_time(tmp_path):
    cfg = single_cfg(tmp_path, limit_poll_sec=0.005, open_confirm_sec=0.02, close_confirm_sec=0.5,
                     unlock_timeout_sec=1.0, max_unlock_sec=3.0, cooldown_sec=0.0, sensor_fault_sec=0.2)
    door = MockDoorController(cfg)
    try:
        assert door.monitor_alive()
        assert door.open("x")
        door.limits.set_closed(None, False)
        assert _wait(lambda: door.locker_state().state is L.OPEN)
        door.limits.set_closed(None, True)
        assert _wait(lambda: door.locker_state().state is L.LOCKED_CLOSED, 3.0)  # close_confirmed, cooldown 0
        assert door.locker_state().last_close_cause == "close_confirmed"
        assert not door.signal_on()
    finally:
        door.cleanup()
    assert not door.monitor_alive()


def test_l10_exception_in_the_polling_path_forces_everything_off_and_faults(tmp_path, monkeypatch):
    cfg = single_cfg(tmp_path, limit_poll_sec=0.005)
    door = MockDoorController(cfg)
    try:
        assert door.open("x")

        def boom():
            raise RuntimeError("injected")

        monkeypatch.setattr(door, "_poll_locked", boom)
        assert _wait(lambda: door.status().state is DoorState.FAULT)
        assert not door.signal_on() and not door.green_on()
        assert door.open("y") is False and door.last_reject == "fault"
    finally:
        door.cleanup()


# ===========================================================================
# L11 no extension; busy / cooldown
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l11_open_again_is_busy_and_never_extends(backend, tmp_path, rigs):
    r = rigs(backend, single_cfg(tmp_path))
    r.door.open("x")
    r.run(3.0)
    before = r.door.locker_state()
    assert r.door.open("again") is False and r.door.last_reject == "busy"  # UNLOCKED_WAITING
    r.open_door()
    r.run(0.2)
    assert r.st() is L.OPEN
    assert r.door.open("again") is False and r.door.last_reject == "busy"  # OPEN
    r.close_door()
    r.run(0.5)
    assert r.st() is L.CLOSING_CONFIRM
    assert r.door.open("again") is False and r.door.last_reject == "busy"  # CLOSING_CONFIRM
    after = r.door.locker_state()
    # the hard deadline kept running from the first unlock (3.0 + 0.2 + 0.5 s ago), not from "again"
    assert after.hard_remaining_sec == pytest.approx(before.hard_remaining_sec - 0.7, abs=0.02)
    assert after.unlocked_at == before.unlocked_at
    r.run(1.6)
    assert r.st() is L.COOLDOWN
    assert r.door.open("again") is False and r.door.last_reject == "cooldown"


# ===========================================================================
# L12 independent channels
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l12_channels_are_independent(backend, tmp_path, rigs):
    r = rigs(backend, locker_cfg(tmp_path, n=3, max_concurrent_open=2))
    assert r.door.open("a", locker_id="1") and r.door.open("b", locker_id="2")
    assert r.door.open("c", locker_id="3") is False and r.door.last_reject == "concurrency_limit"
    r.open_door("2")
    r.run(0.5)
    assert r.st("2") is L.OPEN and r.st("1") is L.UNLOCKED_WAITING
    r.run(9.6)  # t = +10.1: locker 1 times out, locker 2 (door open) stays unlocked
    assert not r.on("1") and r.on("2")
    assert r.door.locker_state("1").last_close_cause == "unlock_timeout"
    assert r.leds.snapshot().green  # still one ON channel
    r.limits.set_fault("1", True)  # sensor error on 1 does not switch 2 off
    r.run(1.5)
    assert r.st("1") is L.SENSOR_FAULT and r.st("2") is L.OPEN and r.on("2")
    r.run(19.0)  # t = ~+30.6 -> watchdog of 2 only
    assert not r.on("2") and r.door.locker_state("2").last_close_cause == "watchdog"
    assert not r.leds.snapshot().green
    s = r.door.status()
    assert s.open_lockers == () and not s.is_open


# ===========================================================================
# L13 shutdown paths are immediate
# ===========================================================================


def _drive_to(r: Rig, state: LockerState) -> None:
    r.door.open("x", locker_id="1") if r.cfg.per_locker else r.door.open("x")
    lid = "1" if r.cfg.per_locker else None
    if state is L.UNLOCKED_WAITING:
        return
    r.open_door(lid)
    r.run(0.3)
    if state is L.OPEN:
        return
    r.close_door(lid)
    r.run(0.5)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("state", [L.UNLOCKED_WAITING, L.OPEN, L.CLOSING_CONFIRM])
@pytest.mark.parametrize("how", ["close_shutdown", "cleanup", "signal"])
def test_l13_shutdown_turns_off_immediately(how, state, backend, tmp_path, rigs, monkeypatch):
    r = rigs(backend, locker_cfg(tmp_path, n=2))
    _drive_to(r, state)
    assert r.st("1") is state and r.on("1")
    if how == "close_shutdown":
        r.door.close("camera failure", cause="shutdown")
        assert not r.on("1") and r.door.locker_state("1").last_close_cause == "shutdown"
    elif how == "cleanup":
        r.door.cleanup()
        assert not r.on("1") if r.backend == "state" else True
        assert r.door.status().close_cause == "shutdown"
        assert r.door.open("x", locker_id="1") is False and r.door.last_reject == "shutdown"
    else:
        from facedoor.hardware.base import HardwareBundle

        bundle = HardwareBundle(mode="mock", door=r.door, leds=r.leds, limits=r.limits)
        hw_factory.register_bundle(bundle, install_handlers=False)
        try:
            with pytest.raises(SystemExit):
                monkeypatch.setattr(hw_factory, "_signals_installed", {15: None})
                hw_factory._signal_handler(15, None)
        finally:
            hw_factory.unregister_bundle(bundle)
        assert r.door.status().close_cause == "shutdown" and not r.door.status().is_open
    ev = r.door.drain_events()
    assert ev[-1].kind == "relock" and ev[-1].cause == "shutdown"


# ===========================================================================
# L14 invariant
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_l14_stray_output_faults_on_next_open_and_on_poll(backend, tmp_path, rigs):
    def force_on(r: Rig, lid: str) -> None:
        if r.backend == "state":
            r.door.pins.set(f"locker:{lid}", True)
        else:
            r.door.channel_devices[lid].on()

    r = rigs(backend, locker_cfg(tmp_path, n=2))
    force_on(r, "1")  # channel 1 is LOCKED_CLOSED but its output reads ON
    assert r.door.open("x", locker_id="2") is False and r.door.last_reject == "invariant"
    assert r.door.status().state is DoorState.FAULT
    assert not any(r.door.channel_levels().values() if r.backend == "state" else
                   (bool(d.value) for d in r.door.channel_devices.values()))
    assert r.st("2") is L.FAULT
    assert r.door.open("y", locker_id="2") is False and r.door.last_reject == "fault"

    r2 = rigs(backend, single_cfg(tmp_path / "b"))
    if r2.backend == "state":
        r2.door.pins.set("solenoid", True)
    else:
        r2.door.solenoid_device.on()
    r2.poll()  # poll_once performs the same check
    assert r2.door.status().state is DoorState.FAULT and not r2.on()
    assert r2.st() is L.FAULT


# ===========================================================================
# L15 simulation inputs
# ===========================================================================


def test_l15_mock_limit_inputs_api():
    cfg = HardwareConfig.from_dict({"close_mode": "limit", "solenoid_pin": None, "locker_outputs": LOCKERS4})
    lim = MockLimitInputs(cfg)
    assert lim.is_simulated is True
    assert lim.snapshot() == {"1": True, "2": True, "3": True, "4": True}  # initial: all doors closed
    assert lim.toggle("1") is False and lim.read("1") is False
    assert lim.toggle("1") is True and lim.read("1") is True
    lim.set_fault("2", True)
    assert lim.read("2") is None and lim.snapshot()["2"] is None and lim.is_fault("2")
    lim.set_fault("2", False)
    assert lim.read("2") is True
    assert lim.read("9") is None
    with pytest.raises(KeyError):
        lim.toggle("9")
    lim.close()
    assert lim.read("1") is None
    single = MockLimitInputs(HardwareConfig.from_dict({"close_mode": "limit", "limit_switch_pin": 5}))
    assert single.toggle(None) is False and single.snapshot() == {"-": False}


@pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing")
@pytest.mark.parametrize("active_low", [True, False])
def test_l15_pi_limit_inputs_polarity_on_mockfactory(active_low, tmp_path):
    cfg = locker_cfg(tmp_path, n=2, limit_active_low=active_low)
    f = MockFactory()
    lim = PiLimitInputs(cfg, f)
    try:
        assert lim.is_simulated
        level = lambda pin: int(f.pin(pin).state)  # noqa: E731
        lim.set_closed("1", True)
        assert lim.read("1") is True and level(5) == (0 if active_low else 1)
        lim.set_closed("1", False)
        assert lim.read("1") is False and level(5) == (1 if active_low else 0)
        f.pin(6).drive_low() if active_low else f.pin(6).drive_high()  # a "real" edge on the pin
        assert lim.read("2") is True
        lim.set_fault("2", True)
        assert lim.read("2") is None
        assert lim.snapshot() == {"1": False, "2": None}
        assert lim.toggle("1") is True
    finally:
        lim.close()
    assert lim.read("1") is None  # closed: never raises


@pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing")
def test_l15_real_factory_forbids_simulation_helpers(tmp_path):
    class RealishFactory(MockFactory):  # not named MockFactory -> is_simulated False
        pass

    lim = PiLimitInputs(single_cfg(tmp_path), RealishFactory())
    try:
        assert lim.is_simulated is False
        for call in (lambda: lim.set_closed(None, False), lambda: lim.toggle(None),
                     lambda: lim.set_fault(None, True)):
            with pytest.raises(RuntimeError):
                call()
        assert lim.read(None) in (True, False)  # reading works
    finally:
        lim.close()


@pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing")
def test_l15_pi_limit_claim_failure_releases_inputs(tmp_path):
    cfg = locker_cfg(tmp_path, n=3)
    f = MockFactory()
    import gpiozero as gz

    held = gz.DigitalOutputDevice(6, pin_factory=f)  # somebody else owns BCM6 (limit of locker 2)
    try:
        with pytest.raises(HardwareError, match="limit input"):
            PiLimitInputs(cfg, f)
        gz.DigitalInputDevice(5, pin_factory=f).close()  # BCM5 was released again
    finally:
        held.close()


def test_l15_bundle_exposes_limits(tmp_path):
    bundle = hw_factory.create_hardware(locker_cfg(tmp_path, n=2, mode="mock", limit_poll_sec=0.05),
                                        register_handlers=False)
    try:
        assert isinstance(bundle.limits, MockLimitInputs) and bundle.limits is bundle.door.limits
        bundle.limits.toggle("1")
        assert _wait(lambda: bundle.door.locker_state("1").door_closed is False)  # polled by the hw thread
    finally:
        bundle.cleanup()
    tb = hw_factory.create_hardware(HardwareConfig.from_dict({"hardware_log": str(tmp_path / "t.log")}),
                                    register_handlers=False)
    try:
        assert tb.limits is None  # timer mode needs no limits
    finally:
        tb.cleanup()


@pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing")
def test_l15_gpiozero_backend_bundle_limits_simulated(tmp_path):
    bundle = hw_factory.create_hardware(locker_cfg(tmp_path, n=2, mode="mock", mock_backend="gpiozero",
                                                   limit_poll_sec=0.05), register_handlers=False)
    try:
        assert isinstance(bundle.limits, PiLimitInputs) and bundle.limits.is_simulated
        assert bundle.door.pin_factory.pin(5) is not None
    finally:
        bundle.cleanup()


# ===========================================================================
# L16 configuration
# ===========================================================================


@pytest.mark.parametrize(
    "bad",
    [
        {"close_mode": "hold"},
        {"close_mode": "fast"},
        {"max_unlock_sec": 12.9},  # < 10 + 2 + 1
        {"max_unlock_sec": 121.0},
        {"unlock_timeout_sec": 0.5},
        {"unlock_timeout_sec": 61.0},
        {"close_confirm_sec": 0.4},
        {"close_confirm_sec": 10.5},
        {"open_confirm_sec": -0.1},
        {"open_confirm_sec": 1.1},
        {"limit_poll_sec": 0.004},
        {"limit_poll_sec": 0.11},
        {"sensor_fault_sec": 0.1},
        {"sensor_fault_sec": 5.1},
        {"limit_switch_pin": 17},  # duplicate with the solenoid
        {"limit_switch_pin": 22},  # duplicate with a LED
        {"limit_switch_pin": 2},  # reserved
        {"limit_switch_pin": 28},  # out of range
        {"limit_switch_pin": True},
        {"limit_pull_up": "yes"},
        {"close_hold_sec": 5.0},  # removed key
    ],
)
def test_l16_limit_config_rejects(bad, tmp_path):
    base = dict(close_mode="limit", limit_switch_pin=5)
    base.update(bad)
    with pytest.raises(ConfigError):
        HardwareConfig.from_dict(base)


def test_l16_per_locker_limit_pin_rules():
    def lockers(*pairs):
        return [{"locker_id": str(i + 1), "pin": p, "limit_pin": lp} for i, (p, lp) in enumerate(pairs)]

    ok = dict(close_mode="limit", solenoid_pin=None)
    HardwareConfig.from_dict(dict(ok, locker_outputs=lockers((24, 5), (25, 6))))
    for bad in (
        dict(locker_outputs=lockers((24, 5)), limit_switch_pin=7),  # per_locker: limit_switch_pin must be null
        dict(locker_outputs=lockers((24, 24))),  # limit pin == its own output
        dict(locker_outputs=lockers((24, 5), (25, 5))),  # shared limit pin
        dict(locker_outputs=lockers((24, 27))),  # LED pin
        dict(locker_outputs=lockers((24, 3))),  # reserved
        dict(locker_outputs=lockers((24, 40))),  # out of range
        dict(locker_outputs=[{"locker_id": "1", "pin": 24, "limit_pin": "5"}]),
        dict(locker_outputs=[{"locker_id": "1", "pin": 24, "limit_pin": True}]),
    ):
        with pytest.raises(ConfigError):
            HardwareConfig.from_dict(dict(ok, **bad))


def test_l16_pi_mode_requires_every_limit_pin_but_mock_does_not():
    two = [{"locker_id": "1", "pin": 24, "limit_pin": 5}, {"locker_id": "2", "pin": 25}]
    base = dict(close_mode="limit", solenoid_pin=None, locker_outputs=two)
    HardwareConfig.from_dict(dict(base, mode="mock"))  # simulation: limit pins optional
    HardwareConfig.from_dict(dict(base, mode="auto"))  # decided at construction time on a Pi
    with pytest.raises(ConfigError, match="limit pin"):
        HardwareConfig.from_dict(dict(base, mode="pi"))
    with pytest.raises(ConfigError, match="limit pin"):
        HardwareConfig.from_dict(dict(close_mode="limit", mode="pi"))  # single: limit_switch_pin missing
    cfg = HardwareConfig.from_dict(dict(base, mode="mock"))
    assert cfg.missing_limit_pins() == ["locker_outputs[2].limit_pin"]


def test_l16_timer_mode_old_config_is_unchanged(tmp_path):
    cfg = HardwareConfig.from_dict({"solenoid_pin": 17, "open_sec": 3.0, "max_open_sec": 5.0})
    assert cfg.close_mode == "timer" and cfg.limit_switch_pin is None and not cfg.limit_mode
    with pytest.raises(ConfigError):
        HardwareConfig.from_dict({"open_sec": 6.0, "max_open_sec": 5.0})  # timer still validates these
    # limit mode ignores open_sec / max_open_sec ordering (they are timer-only)
    HardwareConfig.from_dict({"close_mode": "limit", "limit_switch_pin": 5, "open_sec": 6.0, "max_open_sec": 5.0})
    assert ABSOLUTE_MAX_UNLOCK_SEC == 120.0


def test_l16_warnings(caplog):
    from facedoor.hardware import base as hw_base

    hw_base._warned_once.clear()
    with caplog.at_level(logging.WARNING, logger="facedoor.hardware"):
        HardwareConfig.from_dict({"close_mode": "limit", "limit_switch_pin": 5, "max_unlock_sec": 90.0,
                                  "limit_pull_up": False, "limit_active_low": True})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "max_unlock_sec=90" in text and "external pull" in text and "NC contact" in text


def test_l16_app_config_cross_checks(tmp_path):
    from facedoor.config import load_config

    base = [f"hardware.hardware_log={(tmp_path / 'hw.log').as_posix()}", "app.logs_dir=" + tmp_path.as_posix()]
    cfg = load_config(None, base, use_local=False)  # default.yaml: per_locker + limit
    assert cfg.lockers.mode == "per_locker" and cfg.hardware.limit_mode and cfg.hardware.per_locker
    for bad in (["hardware.close_mode=hold"], ["hardware.close_hold_sec=5"], ["hardware.max_unlock_sec=5"],
                ["lockers.mode=single"]):  # single + locker_outputs
        with pytest.raises(ConfigError):
            load_config(None, base + bad, use_local=False)
    single = load_config(None, base + ["lockers.mode=single", "hardware.locker_outputs=[]",
                                       "hardware.solenoid_pin=17", "hardware.limit_switch_pin=5"], use_local=False)
    assert single.hardware.limit_switch_pin == 5 and not single.hardware.per_locker
    old = load_config(None, base + ["lockers.mode=single", "hardware.locker_outputs=[]", "hardware.solenoid_pin=17",
                                    "hardware.close_mode=timer"], use_local=False)
    assert old.hardware.close_mode == "timer"


# ===========================================================================
# L17 logs and events
# ===========================================================================


def log_rows(cfg: HardwareConfig) -> list[tuple[str, str, str]]:
    out = []
    for line in Path(cfg.hardware_log).read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            out.append((parts[1], parts[2], parts[3]))
    return out


def test_l17_hardware_log_state_sensor_cause_and_events(tmp_path, rigs):
    cfg = locker_cfg(tmp_path, n=2)
    r = rigs("state", cfg)
    r.door.open("face:u1:0.7:3/3:locker=2", locker_id="2")
    r.open_door("2")
    r.run(0.5)
    r.close_door("2")
    r.run(2.1)  # close_confirmed
    r.door.open("face:u1:0.7:3/3:locker=1", locker_id="1")
    r.limits.set_fault("1", True)
    r.run(1.2)  # sensor_fault relock
    r.limits.set_fault("1", False)
    r.run(2.2)  # sensor_ok
    rows = log_rows(cfg)
    init = [d for _, e, d in rows if e == "INIT"][0]
    assert re.search(r"limit=1:BCM5:L,2:BCM6:L close_mode=|close_mode=limit limit=1:BCM5:L,2:BCM6:L", init), init
    assert "unlock_timeout=10s" in init and "max_unlock=30s" in init
    states = [d for _, e, d in rows if e == "STATE"]
    for d in states:
        assert re.match(r"^locker=(\S+) pin=BCM(\S+) [A-Z_]+->[A-Z_]+ limit=(pressed|released|none)$", d), d
    assert "locker=2 pin=BCM25 LOCKED_CLOSED->UNLOCKED_WAITING limit=pressed" in states
    assert "locker=2 pin=BCM25 UNLOCKED_WAITING->OPEN limit=released" in states
    assert "locker=2 pin=BCM25 OPEN->CLOSING_CONFIRM limit=pressed" in states
    assert any("CLOSING_CONFIRM->COOLDOWN" in d for d in states)
    sensor = [d for _, e, d in rows if e == "SENSOR"]
    assert any(re.match(r"^locker=1 pin=BCM24 sensor_fault limit_pin=BCM5 none_for=1\.0\d\ds$", d) for d in sensor), sensor
    assert any(d.startswith("locker=1 pin=BCM24 sensor_ok limit_pin=BCM5") for d in sensor)
    closes = [d for _, e, d in rows if e in ("CLOSE", "WATCHDOG")]
    pat = re.compile(r"^locker=(\S+) pin=BCM(\S+) \S+ signal=OFF cause=(\w+) unlocked_for=(\d+\.\d{3})s "
                     r"door_open_for=(\d+\.\d{3})s")
    parsed = [pat.match(d) for d in closes]
    assert all(parsed), closes
    assert [m.group(3) for m in parsed] == ["close_confirmed", "sensor_fault"]
    assert 2.0 < float(parsed[0].group(5)) < 3.0
    assert float(parsed[1].group(5)) == 0.0  # the door never opened

    ev = r.door.drain_events()
    assert [e.kind for e in ev if e.locker_id == "2"] == ["unlocked", "door_opened", "door_closing", "relock"]
    relock = [e for e in ev if e.kind == "relock"]
    assert [e.cause for e in relock] == ["close_confirmed", "sensor_fault"]
    assert re.fullmatch(r"unlocked_for=\d+\.\d{3}s door_open_for=\d+\.\d{3}s", relock[0].detail)
    assert [e.kind for e in ev if e.locker_id == "1"] == ["unlocked", "relock", "sensor_fault", "sensor_ok"]
    assert r.door.drain_events() == []  # drained once


def test_l17_forced_open_event_and_locker_open(tmp_path, rigs):
    r = rigs("state", single_cfg(tmp_path))
    r.open_door()
    r.run(0.05)
    assert r.st() is L.LOCKED_CLOSED
    r.run(0.1)
    assert r.st() is L.LOCKED_OPEN
    assert kinds(r.door.drain_events()) == ["forced_open"]
    assert r.door.open("x") is False and r.door.last_reject == "door_open"
    r.close_door()
    r.run(2.1)
    assert r.st() is L.LOCKED_CLOSED
    assert r.door.open("x") is True


# ===========================================================================
# structure / API
# ===========================================================================


@pytest.mark.parametrize("backend", BACKENDS)
def test_locker_states_shape_and_invariant(backend, tmp_path, rigs):
    r = rigs(backend, locker_cfg(tmp_path, n=4))
    sts = r.door.locker_states()
    assert [s.locker_id for s in sts] == ["1", "2", "3", "4"]
    assert all(s.state is L.LOCKED_CLOSED and s.door_closed is True and s.sensor_ok and not s.solenoid_on
               for s in sts)
    r.door.open("x", locker_id="3")
    for s in r.door.locker_states():
        assert s.solenoid_on == (s.state in ON_STATES) == (s.locker_id == "3")
    with pytest.raises(KeyError):
        r.door.locker_state("9")
    with pytest.raises(KeyError):
        r.door.locker_state(None)
    r1 = rigs(backend, single_cfg(tmp_path / "s"))
    (only,) = r1.door.locker_states()
    assert only.locker_id is None and r1.door.locker_state() == only
    with pytest.raises(KeyError):
        r1.door.locker_state("1")


@pytest.mark.parametrize("backend", BACKENDS)
def test_door_status_aggregate_in_limit_mode(backend, tmp_path, rigs):
    r = rigs(backend, locker_cfg(tmp_path, n=2, max_concurrent_open=2))
    s = r.door.status()
    assert s.state is DoorState.IDLE and not s.is_open
    r.door.open("x", locker_id="2")
    s = r.door.status()
    assert s.state is DoorState.OPEN and s.is_open and s.open_lockers == ("2",) and s.locker_id == "2"
    assert 9.0 < s.remaining_sec <= 10.0 and 29.0 < s.hard_remaining_sec <= 30.0  # unlock wait / hard
    r.run(10.0)
    r.poll()  # the poll at exactly +10.0 s
    s = r.door.status()
    assert s.state is DoorState.COOLDOWN and s.close_cause == "unlock_timeout" and not s.is_open


def test_duration_ignored_in_limit_mode(tmp_path, rigs):
    r = rigs("state", single_cfg(tmp_path))
    assert r.door.open("x", duration=0.5) is True  # not an invalid/short pulse: limit mode ignores it
    r.run(1.0)
    assert r.on()
    assert r.door.open("y", duration=float("nan")) is False and r.door.last_reject == "busy"


def test_timer_mode_still_has_locker_state_and_events(tmp_path):
    cfg = HardwareConfig.from_dict({"hardware_log": str(tmp_path / "t.log"), "cooldown_sec": 1.0})
    clock = FakeClock()
    door = MockDoorController(cfg, clock=clock)
    try:
        s = door.locker_state()
        assert s.state is L.LOCKED_CLOSED and s.door_closed is None and s.sensor_ok
        assert door.open("x", 2.0) is True
        assert door.locker_state().state is L.OPEN and door.locker_state().hard_remaining_sec > 0
        clock.t += 2.0
        s = door.locker_state()
        assert s.state is L.COOLDOWN and s.last_close_cause == "timer"
        clock.t += 1.0
        assert door.locker_state().state is L.LOCKED_CLOSED
        assert kinds(door.drain_events()) == ["unlocked", "relock"]
        door.poll_once()  # harmless in timer mode
    finally:
        door.cleanup()


def test_concurrent_polling_and_requests_keep_output_consistent(tmp_path):
    """Real thread + hammering open()/close()/status(): green LED always equals the signal when quiescent,
    and nothing raises."""
    cfg = single_cfg(tmp_path, limit_poll_sec=0.005, cooldown_sec=0.0, unlock_timeout_sec=1.0)
    door = MockDoorController(cfg)
    errors: list[BaseException] = []

    def hammer():
        try:
            for i in range(300):
                door.open(f"t{i}")
                door.status()
                door.locker_states()
                if i % 3 == 0:
                    door.close("c")
                door.limits.toggle(None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=hammer) for _ in range(3)]
    try:
        for t in ts:
            t.start()
        for t in ts:
            t.join(20)
        assert not errors
        door.close("end")
        assert not door.signal_on() and not door.green_on()
        assert door.status().state is not DoorState.FAULT
    finally:
        door.cleanup()


def test_cause_enum_has_v15_members_only():
    assert {c.value for c in CloseCause} == {"unlock_timeout", "close_confirmed", "watchdog", "sensor_fault",
                                              "shutdown", "fault", "timer", "manual"}
