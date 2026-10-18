"""Hardware safety tests (owner: hw-engineer). No real GPIO needed.

Both backends are exercised: the pure-Python mock (MockDoorController) and the Pi code path
(PiDoorController) on gpiozero's MockFactory. Real lgpio/Pi behaviour is NOT covered here.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

from facedoor.errors import ConfigError, HardwareError, HardwareUnavailableError
from facedoor.hardware import factory as hw_factory
from facedoor.hardware.base import (
    ABSOLUTE_MAX_OPEN_SEC,
    DoorController,
    DoorState,
    HardwareBundle,
    HardwareConfig,
    LedSnapshot,
    StatusLeds,
    format_status_line,
)
from facedoor.hardware.gpio_mock import MockDoorController, MockStatusLeds

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

try:
    import gpiozero  # noqa: F401

    HAVE_GPIOZERO = True
except Exception:  # noqa: BLE001
    HAVE_GPIOZERO = False

BACKENDS = ["state", pytest.param("gpiozero", marks=pytest.mark.skipif(not HAVE_GPIOZERO, reason="gpiozero missing"))]


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_cfg(tmp_path: Path, **kw) -> HardwareConfig:
    base = dict(open_sec=3.0, max_open_sec=5.0, cooldown_sec=2.0, deny_blink_count=3, deny_blink_sec=0.02,
                hardware_log=str(tmp_path / "hardware.log"))
    base.update(kw)
    return HardwareConfig.from_dict(base)


def make_door(backend: str, cfg: HardwareConfig, clock=time.monotonic):
    if backend == "state":
        door = MockDoorController(cfg, clock=clock)
        leds = MockStatusLeds(cfg, door)
    else:
        from gpiozero.pins.mock import MockFactory

        from facedoor.hardware.gpio_pi import PiDoorController, PiStatusLeds

        door = PiDoorController(cfg, pin_factory=MockFactory(), clock=clock)
        leds = PiStatusLeds(cfg, door)
    return door, leds


def outputs(door) -> tuple[bool, bool]:
    """(solenoid signal, green LED) as actually driven."""
    return door.signal_on(), door.green_on()


@pytest.fixture
def cleanup_list():
    items: list = []
    yield items
    for it in items:
        it.cleanup()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_default_yaml_hardware_section_parses():
    """configs/default.yaml: 4 lockers (per_locker) with limit switches, close_mode=limit (INTERFACES 13.2, 15.2)."""
    d = yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))["hardware"]
    cfg = HardwareConfig.from_dict(d)
    assert cfg.per_locker and cfg.solenoid_pin is None and cfg.close_mode == "limit"
    assert [(o.locker_id, o.pin, o.limit_pin) for o in cfg.locker_outputs] == [
        ("1", 24, 5), ("2", 25, 6), ("3", 16, 12), ("4", 26, 13)]
    assert cfg.limit_switch_pin is None and cfg.max_concurrent_open == 1
    assert (cfg.limit_poll_sec, cfg.open_confirm_sec, cfg.close_confirm_sec) == (0.02, 0.1, 2.0)
    assert (cfg.unlock_timeout_sec, cfg.max_unlock_sec, cfg.sensor_fault_sec) == (10.0, 30.0, 1.0)
    assert cfg.limit_pull_up is True and cfg.limit_active_low is True
    assert cfg.max_open_sec <= ABSOLUTE_MAX_OPEN_SEC
    # every key written in default.yaml is a real field: nothing is silently ignored
    assert set(d) <= set(cfg.to_dict())
    # the app loader accepts the same file and cross-checks lockers.mode
    from facedoor.config import load_config

    app = load_config(None, use_local=False)
    assert app.lockers.mode == "per_locker" and app.hardware.close_mode == "limit"


@pytest.mark.parametrize(
    "bad",
    [
        {"bogus": 1},
        {"mode": "real"},
        {"mock_backend": "x"},
        {"open_sec": 0},
        {"open_sec": 6.0, "max_open_sec": 5.0},
        {"max_open_sec": 11.0, "open_sec": 3.0},
        {"led_red_pin": 17},  # duplicate with solenoid
        {"solenoid_pin": True},
        {"solenoid_pin": 40},
        {"solenoid_active_high": "yes"},
        {"gpio_chip": -1},
        {"cooldown_sec": -1},
        {"open_sec": float("nan")},
    ],
)
def test_config_rejects_invalid(bad):
    with pytest.raises(ConfigError):
        HardwareConfig.from_dict(bad)


def test_config_coercions():
    cfg = HardwareConfig.from_dict({"gpio_chip": "4", "open_sec": 2, "hardware_log": "logs/x.log", "mode": "AUTO"})
    assert cfg.gpio_chip == 4 and isinstance(cfg.open_sec, float) and isinstance(cfg.hardware_log, Path)
    assert cfg.mode == "auto"


def test_absolute_max_is_code_bound():
    cfg = HardwareConfig()
    cfg.max_open_sec = 30.0  # bypassing from_dict must still be caught by constructors
    with pytest.raises(ConfigError):
        MockDoorController(cfg)


# ---------------------------------------------------------------------------
# state machine (both backends)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_initial_state_off(backend, tmp_path, cleanup_list):
    door, leds = make_door(backend, make_cfg(tmp_path))
    cleanup_list += [leds, door]
    st = door.status()
    assert st.state is DoorState.IDLE and not st.is_open and st.open_count == 0
    assert outputs(door) == (False, False)
    assert leds.snapshot() == LedSnapshot(False, False, False)
    assert isinstance(door, DoorController) and isinstance(leds, StatusLeds)


@pytest.mark.parametrize("backend", BACKENDS)
def test_open_timeout_cooldown_cycle(backend, tmp_path, cleanup_list):
    clock = FakeClock()
    door, leds = make_door(backend, make_cfg(tmp_path), clock)
    cleanup_list += [leds, door]

    assert door.open("face:alice:0.6123:3/5") is True
    st = door.status()
    assert st.state is DoorState.OPEN and st.is_open and st.remaining_sec == pytest.approx(3.0)
    assert outputs(door) == (True, True)
    assert leds.snapshot().green is True

    clock.advance(1.0)
    assert door.open("again") is False  # no extension while open
    assert door.status().remaining_sec == pytest.approx(2.0)

    clock.advance(2.0)  # open_sec reached -> watchdog close
    st = door.status()
    assert st.state is DoorState.COOLDOWN and not st.is_open
    assert st.cooldown_remaining_sec == pytest.approx(2.0)
    assert outputs(door) == (False, False)

    assert door.open("during cooldown") is False
    clock.advance(1.999)
    assert door.open("still cooldown") is False
    clock.advance(0.002)
    assert door.status().state is DoorState.IDLE
    assert door.open("second") is True
    assert door.status().open_count == 2
    kinds = [k for _, k, _ in door.history]
    assert kinds.count("OPEN") == 2 and "WATCHDOG" in kinds and kinds.count("REJECT") == 3


@pytest.mark.parametrize("backend", BACKENDS)
def test_duration_clamped_to_max_open(backend, tmp_path, cleanup_list, caplog):
    clock = FakeClock()
    door, leds = make_door(backend, make_cfg(tmp_path), clock)
    cleanup_list += [leds, door]
    with caplog.at_level(logging.WARNING, logger="facedoor.hardware"):
        assert door.open("long", duration=60.0) is True
    assert "clamped" in caplog.text
    assert door.status().remaining_sec == pytest.approx(5.0)
    clock.advance(4.999)
    assert door.status().is_open is True
    clock.advance(0.001)
    assert door.status().is_open is False and outputs(door) == (False, False)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "abc"])
def test_invalid_duration_rejected(bad, tmp_path, cleanup_list):
    door, leds = make_door("state", make_cfg(tmp_path))
    cleanup_list += [leds, door]
    assert door.open("bad", duration=bad) is False
    assert outputs(door) == (False, False) and door.status().state is DoorState.IDLE


@pytest.mark.parametrize("backend", BACKENDS)
def test_realtime_watchdog_closes_without_polling(backend, tmp_path, cleanup_list):
    """The threading.Timer watchdog must turn the output OFF even if nobody calls status() (hung main loop)."""
    cfg = make_cfg(tmp_path, open_sec=0.1, max_open_sec=0.2, cooldown_sec=0.0)
    door, leds = make_door(backend, cfg)
    cleanup_list += [leds, door]
    assert door.open("rt", duration=5.0) is True  # clamped to 0.2 s
    assert door.signal_on()
    deadline = time.monotonic() + 3.0
    while door.signal_on() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert outputs(door) == (False, False)
    assert any(k == "WATCHDOG" for _, k, _ in door.history)
    assert door.status().state is DoorState.IDLE  # cooldown_sec=0


@pytest.mark.parametrize("backend", BACKENDS)
def test_manual_close_and_idempotent(backend, tmp_path, cleanup_list):
    clock = FakeClock()
    door, leds = make_door(backend, make_cfg(tmp_path), clock)
    cleanup_list += [leds, door]
    door.close("noop")  # closing while idle is harmless
    assert door.status().state is DoorState.IDLE
    door.open("x")
    door.close("camera error")
    door.close("again")
    st = door.status()
    assert st.state is DoorState.COOLDOWN and not st.is_open
    assert [k for _, k, _ in door.history].count("CLOSE") == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_cleanup_turns_off_and_blocks_reopen(backend, tmp_path):
    door, leds = make_door(backend, make_cfg(tmp_path))
    door.open("x")
    leds.set_hold(True)
    bundle = HardwareBundle(mode="mock", door=door, leds=leds, exit_button=None)
    bundle.cleanup()
    bundle.cleanup()  # idempotent
    assert door.status().is_open is False
    assert door.open("after cleanup") is False
    assert door._timer is None  # watchdog cancelled
    if backend == "state":
        assert outputs(door) == (False, False)
        assert leds.snapshot() == LedSnapshot(False, False, False)


def test_exception_in_with_block_turns_output_off(tmp_path):
    cfg = make_cfg(tmp_path)
    bundle = hw_factory.create_hardware(cfg, register_handlers=False)
    door = bundle.door
    with pytest.raises(RuntimeError):
        with bundle:
            assert door.open("x")
            assert outputs(door) == (True, True)
            raise RuntimeError("pipeline crashed")
    assert outputs(door) == (False, False)
    assert bundle.cleaned


@pytest.mark.parametrize("fail", [("solenoid", True), ("led_green", True)])
def test_drive_failure_forces_off_and_faults(fail, tmp_path, cleanup_list):
    door, leds = make_door("state", make_cfg(tmp_path))
    cleanup_list += [leds, door]
    door.pins.fail_on.add(fail)
    with pytest.raises(HardwareError):
        door.open("x")
    assert outputs(door) == (False, False)  # solenoid was forced OFF even if it had been switched ON
    st = door.status()
    assert st.state is DoorState.FAULT and not st.is_open
    door.pins.fail_on.clear()
    assert door.open("after fault") is False  # FAULT latches until restart
    assert any(k == "FAULT" for _, k, _ in door.history)


def test_pi_path_drive_failure_forces_off(tmp_path, cleanup_list):
    pytest.importorskip("gpiozero")
    door, leds = make_door("gpiozero", make_cfg(tmp_path))
    cleanup_list += [leds, door]

    def boom():
        raise OSError("lgpio write failed")

    door.green_device.on = boom
    with pytest.raises(HardwareError):
        door.open("x")
    assert door.solenoid_device.value == 0
    assert door.status().state is DoorState.FAULT


def test_green_led_always_equals_signal_under_concurrency(tmp_path, cleanup_list):
    cfg = make_cfg(tmp_path, open_sec=0.01, max_open_sec=0.02, cooldown_sec=0.0)
    door, leds = make_door("state", cfg)
    cleanup_list += [leds, door]
    stop = threading.Event()
    errors: list[str] = []

    def hammer():
        while not stop.is_set():
            door.open("t")
            door.close("t")

    threads = [threading.Thread(target=hammer) for _ in range(3)]
    for t in threads:
        t.start()
    t_end = time.monotonic() + 0.5
    while time.monotonic() < t_end:
        with door._lock:  # consistent view between transitions
            st = door.status()
            sig, green = door.signal_on(), door.green_on()
        if st.is_open != sig or sig != green:
            errors.append(f"inconsistent: is_open={st.is_open} signal={sig} green={green}")
    stop.set()
    for t in threads:
        t.join()
    door.close("end")
    assert not errors, errors[:3]
    # every electrical transition sequence keeps green <= signal
    sig = green = 0
    for _, name, _, lvl in door.pins.history:
        if name == "solenoid":
            sig = lvl
        elif name == "led_green":
            green = lvl
        assert not (green and not sig), "green LED was ON while the solenoid signal was OFF"


@pytest.mark.parametrize("backend", BACKENDS)
def test_polarity_active_low(backend, tmp_path, cleanup_list):
    cfg = make_cfg(tmp_path, solenoid_active_high=False, led_active_high=False)
    door, leds = make_door(backend, cfg, FakeClock())
    cleanup_list += [leds, door]
    if backend == "state":
        assert door.pins.level("solenoid") == 1  # OFF = HIGH on an active-low relay input
        door.open("x")
        assert door.pins.level("solenoid") == 0 and door.pins.level("led_green") == 0
    else:
        assert door.solenoid_device.pin.state == 1
        door.open("x")
        assert door.solenoid_device.pin.state == 0 and door.green_device.pin.state == 0
    assert door.status().is_open


# ---------------------------------------------------------------------------
# LEDs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_deny_blink_nonblocking_and_count(backend, tmp_path, cleanup_list):
    cfg = make_cfg(tmp_path, deny_blink_count=3, deny_blink_sec=0.05)
    door, leds = make_door(backend, cfg)
    cleanup_list += [leds, door]
    t0 = time.monotonic()
    leds.show_denied()
    assert time.monotonic() - t0 < 0.05  # non-blocking
    assert leds.wait_blink(timeout=3.0)
    assert leds.snapshot().red is False
    assert outputs(door) == (False, False)  # deny never touches the door output
    if backend == "state":
        red_on = [lvl for _, n, _, lvl in door.pins.history if n == "led_red" and lvl == 1]
        assert len(red_on) == 3


def test_hold_led_and_off(tmp_path, cleanup_list):
    door, leds = make_door("state", make_cfg(tmp_path), FakeClock())
    cleanup_list += [leds, door]
    leds.set_hold(True)
    assert leds.snapshot() == LedSnapshot(green=False, red=False, amber=True)
    door.open("x")
    assert leds.snapshot().green is True
    leds.off()
    snap = leds.snapshot()
    assert snap.amber is False and snap.red is False and snap.green is True  # green belongs to door
    line = format_status_line(door.status(), snap)
    assert line.startswith("DOOR SIGNAL: ON") and "G:ON" in line and line.isascii()


def test_hardware_log_written(tmp_path, cleanup_list):
    cfg = make_cfg(tmp_path)
    door, leds = make_door("state", cfg, FakeClock())
    cleanup_list += [leds, door]
    door.open("face:bob:0.7000:3/5")
    door.close("manual")
    text = Path(cfg.hardware_log).read_text(encoding="utf-8")
    assert "OPEN\tface:bob:0.7000:3/5 signal=ON" in text
    assert "CLOSE\tmanual signal=OFF" in text


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_mode_pi_on_pc_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(hw_factory, "is_raspberry_pi", lambda: False)
    with pytest.raises(HardwareUnavailableError):
        hw_factory.create_hardware(make_cfg(tmp_path, mode="pi"), register_handlers=False)


def test_mode_pi_without_lgpio_is_an_error(tmp_path, monkeypatch):
    try:
        import lgpio  # noqa: F401

        pytest.skip("lgpio installed here")
    except ImportError:
        pass
    monkeypatch.setattr(hw_factory, "is_raspberry_pi", lambda: True)
    with pytest.raises(HardwareUnavailableError):
        hw_factory.create_hardware(make_cfg(tmp_path, mode="pi"), register_handlers=False)


def test_mode_auto_falls_back_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(hw_factory, "is_raspberry_pi", lambda: False)
    with caplog.at_level(logging.WARNING, logger="facedoor.hardware"):
        b = hw_factory.create_hardware(make_cfg(tmp_path, mode="auto"), register_handlers=False)
    try:
        assert b.mode == "mock" and "MOCK GPIO" in caplog.text
    finally:
        b.cleanup()


@pytest.mark.parametrize("backend", BACKENDS)
def test_mock_backends(backend, tmp_path):
    b = hw_factory.create_hardware(make_cfg(tmp_path, mock_backend=backend, exit_button_pin=5), register_handlers=False)
    try:
        assert b.mode == "mock"
        expected = "MockDoorController" if backend == "state" else "PiDoorController"
        assert type(b.door).__name__ == expected
        assert b.exit_button is not None
    finally:
        b.cleanup()


def test_is_raspberry_pi_false_on_this_pc():
    if sys.platform.startswith("win"):
        assert hw_factory.is_raspberry_pi() is False


def test_signal_handler_turns_off_and_chains(tmp_path, monkeypatch):
    b = hw_factory.create_hardware(make_cfg(tmp_path), register_handlers=False)
    hw_factory.register_bundle(b, install_handlers=False)
    b.door.open("x")
    called = []
    monkeypatch.setitem(hw_factory._signals_installed, signal.SIGTERM, lambda s, f: called.append(s))
    hw_factory._signal_handler(signal.SIGTERM, None)
    assert outputs(b.door) == (False, False) and b.cleaned and called == [signal.SIGTERM]
    assert b not in hw_factory.registered_bundles()


def test_signal_handler_default_exits(tmp_path, monkeypatch):
    b = hw_factory.create_hardware(make_cfg(tmp_path), register_handlers=False)
    hw_factory.register_bundle(b, install_handlers=False)
    b.door.open("x")
    monkeypatch.setitem(hw_factory._signals_installed, signal.SIGTERM, signal.SIG_DFL)
    with pytest.raises(SystemExit):
        hw_factory._signal_handler(signal.SIGTERM, None)
    assert outputs(b.door) == (False, False)


def test_install_handlers_registers_atexit_and_signals(monkeypatch, tmp_path):
    installed: dict = {}
    registered: list = []
    monkeypatch.setattr(hw_factory, "_atexit_installed", False)
    monkeypatch.setattr(hw_factory, "_signals_installed", {})
    monkeypatch.setattr(hw_factory.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(hw_factory.signal, "getsignal", lambda s: signal.SIG_DFL)
    monkeypatch.setattr(hw_factory.signal, "signal", lambda s, h: installed.__setitem__(s, h))
    b = hw_factory.create_hardware(make_cfg(tmp_path))
    try:
        assert hw_factory.run_registered_cleanups in registered
        assert installed.get(signal.SIGINT) is hw_factory._signal_handler
        assert installed.get(signal.SIGTERM) is hw_factory._signal_handler
        assert b in hw_factory.registered_bundles()
    finally:
        b.cleanup()


CHILD = textwrap.dedent(
    """
    import sys, signal, time
    sys.path.insert(0, {src!r})
    from facedoor.hardware.base import HardwareConfig
    from facedoor.hardware.factory import create_hardware
    cfg = HardwareConfig.from_dict({{"hardware_log": {log!r}, "open_sec": 5.0}})
    b = create_hardware(cfg)
    assert b.door.open("child")
    how = {how!r}
    if how == "atexit":
        sys.exit(0)
    elif how == "exception":
        raise RuntimeError("boom")
    else:
        signal.raise_signal(getattr(signal, how))
        time.sleep(1)
    """
)


@pytest.mark.parametrize("how", ["atexit", "exception", "SIGINT", "SIGTERM"])
def test_process_exit_paths_turn_output_off(how, tmp_path):
    """Real child process: normal exit, uncaught exception, SIGINT and SIGTERM all log CLEANUP after OPEN."""
    log = tmp_path / "hw.log"
    code = CHILD.format(src=str(SRC), log=str(log), how=how)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    text = log.read_text(encoding="utf-8")
    events = [line.split("\t")[2] for line in text.splitlines() if line.strip()]
    assert "OPEN" in events, (proc.stdout, proc.stderr)
    assert "CLEANUP" in events[events.index("OPEN"):], (events, proc.stderr)
    if how == "atexit":
        assert proc.returncode == 0
    else:
        assert proc.returncode != 0


def test_hardware_package_does_not_import_gpio_libs():
    code = (
        f"import sys; sys.path.insert(0, {str(SRC)!r});"
        "import facedoor.hardware, facedoor.hardware.gpio_pi, facedoor.hardware.gpio_mock, facedoor.hardware.factory;"
        "print('gpiozero' in sys.modules, 'lgpio' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False"


def test_hw_selftest_mock_runs(tmp_path):
    script = ROOT / "scripts" / "hw_selftest.py"
    out = subprocess.run(
        [sys.executable, str(script), "--mode", "mock", "--allow-solenoid", "--pulse-sec", "0.1", "--step-sec", "0.01",
         "--hardware-log", str(tmp_path / "hw.log")],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "SELFTEST OK" in out.stdout


def test_hw_selftest_pi_on_pc_fails(tmp_path):
    if not sys.platform.startswith("win"):
        pytest.skip("only meaningful on a non-Pi PC")
    script = ROOT / "scripts" / "hw_selftest.py"
    out = subprocess.run([sys.executable, str(script), "--mode", "pi", "--hardware-log", str(tmp_path / "hw.log")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 5, out.stdout + out.stderr


# ===========================================================================
# v1.3 per-locker outputs (INTERFACES 13); limit-sensor control (INTERFACES 15) is in test_hardware_limit.py
# ===========================================================================

import importlib.util  # noqa: E402
import re  # noqa: E402

from facedoor.hardware import base as hw_base  # noqa: E402
from facedoor.hardware.base import (  # noqa: E402
    ABSOLUTE_MAX_CONCURRENT_OPEN,
    ABSOLUTE_MAX_LOCKERS,
    CloseCause,
    LockerOutput,
    force_off_targets,
)

LOCKERS4 = [{"locker_id": "1", "pin": 24}, {"locker_id": "2", "pin": 25},
            {"locker_id": "3", "pin": 16}, {"locker_id": "4", "pin": 26}]
AUDIT_RE = re.compile(r"^locker=(\S+) pin=BCM(\S+) ")


def make_locker_cfg(tmp_path: Path, lockers=None, **kw) -> HardwareConfig:
    base = dict(solenoid_pin=None, locker_outputs=lockers if lockers is not None else LOCKERS4)
    base.update(kw)
    return make_cfg(tmp_path, **base)


def make_limit_cfg(tmp_path: Path, **kw) -> HardwareConfig:
    """single output, close_mode=limit, BCM5 limit."""
    base = dict(close_mode="limit", limit_switch_pin=5)
    base.update(kw)
    return make_cfg(tmp_path, **base)


def limit_locker_cfg(tmp_path: Path, **kw) -> HardwareConfig:
    lockers = [dict(o, limit_pin=p) for o, p in zip(LOCKERS4, (5, 6, 12, 13))]
    return make_locker_cfg(tmp_path, lockers=lockers, close_mode="limit", **kw)


def levels(door) -> dict[str, bool]:
    """locker_id -> output ON, read from the backend itself (mock pin bank or gpiozero devices)."""
    if isinstance(door, MockDoorController):
        return door.channel_levels()
    return {lid: bool(dev.value) for lid, dev in door.channel_devices.items()}


def _pin_rows(door, name: str) -> tuple[list[int], bool]:
    rows = [lvl for _, n, _, lvl in door.pins.history if n == name]
    return rows[1:], door.pins._meta[name][1]  # drop the setup row


def on_transitions(door, name: str = "solenoid") -> int:
    rows, ah = _pin_rows(door, name)
    return sum(1 for lvl in rows if lvl == (1 if ah else 0))


def off_transitions(door, name: str = "solenoid") -> int:
    rows, ah = _pin_rows(door, name)
    return sum(1 for lvl in rows if lvl == (0 if ah else 1))


def log_rows(cfg: HardwareConfig) -> list[tuple[str, str, str]]:
    text = Path(cfg.hardware_log).read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            out.append((parts[1], parts[2], parts[3]))
    return out


# ---------------------------------------------------------------------------
# 13.9 scenarios 2-7: configuration
# ---------------------------------------------------------------------------


def test_per_locker_config_parses_and_maps(tmp_path):
    cfg = make_locker_cfg(tmp_path, lockers=[{"locker_id": 1, "pin": 24},
                                             {"locker_id": "2", "pin": 25, "active_high": False}])
    assert cfg.per_locker and cfg.solenoid_pin is None
    assert cfg.locker_outputs[0] == LockerOutput("1", 24, None)  # YAML int id -> "1"
    assert cfg.channel_map() == {"1": (24, True), "2": (25, False)}
    pins = cfg.pins()
    assert "solenoid" not in pins and pins["locker:1"] == 24 and pins["locker:2"] == 25
    assert cfg.to_dict()["locker_outputs"] == [{"locker_id": "1", "pin": 24},
                                               {"locker_id": "2", "pin": 25, "active_high": False}]
    assert HardwareConfig.from_dict(cfg.to_dict()) == cfg
    assert HardwareConfig().per_locker is False and HardwareConfig().channel_map() == {}


_SEVENTEEN = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 21, 24]


@pytest.mark.parametrize(
    "bad",
    [
        # 2: solenoid_pin null without locker channels (the lockers.mode cross-check is config.py's)
        {"solenoid_pin": None},
        # 3: per_locker but solenoid_pin kept
        {"locker_outputs": LOCKERS4},
        # 4: pin conflicts / reserved / out of range
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "1", "pin": 27}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "1", "pin": 24}, {"locker_id": "2", "pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "1", "pin": 2}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "1", "pin": 30}]},
        {"solenoid_pin": 14},
        # 5: ids, channel count, list-only, unknown item keys, bad id types
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "A01", "pin": 24}, {"locker_id": "A01", "pin": 25}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "A01", "pin": 24}, {"locker_id": "a01", "pin": 25}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "A 01", "pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "", "pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "사물함1", "pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": f"L{i}", "pin": p} for i, p in enumerate(_SEVENTEEN)]},
        {"solenoid_pin": None, "locker_outputs": {"1": 24, "2": 25}},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "1", "pin": 24, "led_pin": 5}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": True, "pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": 1.5, "pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": -1, "pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"pin": 24}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "1", "pin": "24"}]},
        {"solenoid_pin": None, "locker_outputs": [{"locker_id": "1", "pin": 24, "active_high": "no"}]},
        {"solenoid_pin": None, "locker_outputs": "1:24"},
        # 6: max_concurrent_open range
        {"solenoid_pin": None, "locker_outputs": LOCKERS4, "max_concurrent_open": 0},
        {"solenoid_pin": None, "locker_outputs": LOCKERS4, "max_concurrent_open": 5},
        {"solenoid_pin": None, "locker_outputs": LOCKERS4[:2], "max_concurrent_open": 3},
        {"max_concurrent_open": 2},  # single mode: only 1
        {"solenoid_pin": None, "locker_outputs": LOCKERS4, "max_concurrent_open": True},
    ],
)
def test_per_locker_config_rejects(bad):
    with pytest.raises(ConfigError):
        HardwareConfig.from_dict(bad)


def test_locker_code_limits():
    assert ABSOLUTE_MAX_LOCKERS == 16 and ABSOLUTE_MAX_CONCURRENT_OPEN == 4
    assert hw_base.RESERVED_OUTPUT_PINS == frozenset({0, 1, 2, 3, 14, 15})
    pins = _SEVENTEEN[:16]
    cfg = HardwareConfig.from_dict({"solenoid_pin": None, "max_concurrent_open": 4,
                                    "locker_outputs": [{"locker_id": f"L{i}", "pin": p} for i, p in enumerate(pins)]})
    assert len(cfg.locker_outputs) == 16


def test_active_high_boot_pullup_pin_warns_but_starts(tmp_path, caplog, cleanup_list):
    hw_base._warned_once.clear()
    with caplog.at_level(logging.WARNING, logger="facedoor.hardware"):
        cfg = make_locker_cfg(tmp_path, lockers=[{"locker_id": "1", "pin": 5},
                                                 {"locker_id": "2", "pin": 6, "active_high": False}])
        door = MockDoorController(cfg, clock=FakeClock())
    cleanup_list.append(door)
    assert "BCM5" in caplog.text and "BCM6" not in caplog.text  # active-low on a pull-up pin is fine
    assert door.open("x", locker_id="1") is True


def test_led_on_reserved_pin_only_warns(tmp_path, caplog):
    hw_base._warned_once.clear()
    with caplog.at_level(logging.WARNING, logger="facedoor.hardware"):
        make_cfg(tmp_path, led_amber_pin=14)
    assert "reserved" in caplog.text


# ---------------------------------------------------------------------------
# 13.9 scenarios 8, 13-18: runtime (both backends)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_locker_open_drives_only_that_channel(backend, tmp_path, cleanup_list):
    clock = FakeClock()
    door, leds = make_door(backend, make_locker_cfg(tmp_path), clock)
    cleanup_list += [leds, door]
    assert door.locker_ids() == ("1", "2", "3", "4")
    assert levels(door) == {"1": False, "2": False, "3": False, "4": False}
    assert door.open("face:alice:0.61:3/3:locker=2", locker_id="2") is True and door.last_reject == ""
    assert levels(door) == {"1": False, "2": True, "3": False, "4": False}
    assert door.green_on() and leds.snapshot().green
    st = door.status()
    assert st.state is DoorState.OPEN and st.is_open and st.open_lockers == ("2",) and st.locker_id == "2"
    assert st.remaining_sec == pytest.approx(3.0)
    ls = door.locker_status("2")
    assert ls.is_open and ls.locker_id == "2" and ls.open_count == 1
    assert door.locker_status("1").state is DoorState.IDLE and not door.locker_status("1").is_open
    with pytest.raises(KeyError):
        door.locker_status("Z99")
    clock.advance(3.0)  # open_sec -> off
    door.status()  # lazy transition (fake clock: no real timer fired)
    assert levels(door) == {"1": False, "2": False, "3": False, "4": False}
    assert not door.green_on() and door.status().open_lockers == ()
    assert door.locker_status("2").state is DoorState.COOLDOWN
    assert door.locker_status("2").close_cause == "timer"


def test_single_mode_has_no_locker_api(tmp_path, cleanup_list):
    door, leds = make_door("state", make_cfg(tmp_path), FakeClock())
    cleanup_list += [leds, door]
    assert door.locker_ids() == () and door.channel_levels() == {}
    with pytest.raises(KeyError):
        door.locker_status("1")
    assert door.open("x", locker_id="anything") is True  # single: id not used for routing
    st = door.status()
    assert st.locker_id is None and st.open_lockers == () and st.is_open


def test_gpiozero_per_locker_does_not_claim_bcm17(tmp_path, cleanup_list):
    pytest.importorskip("gpiozero")
    from gpiozero import DigitalOutputDevice
    from gpiozero.pins.mock import MockFactory

    from facedoor.hardware.gpio_pi import PiDoorController

    f = MockFactory()
    door = PiDoorController(make_locker_cfg(tmp_path), pin_factory=f, clock=FakeClock())
    cleanup_list.append(door)
    probe = DigitalOutputDevice(17, pin_factory=f)  # would raise GPIOPinInUse if the door had claimed 17
    probe.close()
    assert door.solenoid_device is None and set(door.channel_devices) == {"1", "2", "3", "4"}
    door.open("x", locker_id="3")
    assert f.pin(16).state == 1 and f.pin(24).state == 0 and f.pin(25).state == 0 and f.pin(26).state == 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_concurrency_limit_and_independent_timers(backend, tmp_path, cleanup_list):
    clock = FakeClock()
    door, leds = make_door(backend, make_locker_cfg(tmp_path), clock)
    cleanup_list += [leds, door]
    assert door.open("a", locker_id="2")
    assert door.open("c", locker_id="3") is False and door.last_reject == "concurrency_limit"
    assert levels(door)["2"] and not levels(door)["3"]
    door.close()

    clock2 = FakeClock()
    door2, leds2 = make_door(backend, make_locker_cfg(tmp_path / "b", max_concurrent_open=2), clock2)
    cleanup_list += [leds2, door2]
    assert door2.open("a", locker_id="2", duration=3.0)
    clock2.advance(1.0)
    assert door2.open("c", locker_id="3", duration=3.0)
    assert door2.status().open_lockers == ("2", "3")
    assert door2.open("d", locker_id="4") is False and door2.last_reject == "concurrency_limit"
    clock2.advance(2.0)  # locker 2 expires, locker 3 still has 1 s
    assert door2.status().open_lockers == ("3",) and door2.green_on()
    clock2.advance(1.0)
    assert door2.status().open_lockers == () and not door2.green_on()


@pytest.mark.parametrize("backend", BACKENDS)
def test_cooldown_is_per_channel(backend, tmp_path, cleanup_list):
    clock = FakeClock()
    door, leds = make_door(backend, make_locker_cfg(tmp_path), clock)
    cleanup_list += [leds, door]
    assert door.open("a", locker_id="2")
    assert door.open("a2", locker_id="2") is False and door.last_reject == "busy"
    door.close("manual", locker_id="2")
    assert door.open("again", locker_id="2") is False and door.last_reject == "cooldown"
    assert door.open("other", locker_id="3") is True  # another locker opens at once
    clock.advance(2.0)
    door.close(locker_id="3")
    assert door.locker_status("2").state is DoorState.IDLE


@pytest.mark.parametrize("backend", BACKENDS)
def test_locker_duration_clamped(backend, tmp_path, cleanup_list):
    clock = FakeClock()
    door, leds = make_door(backend, make_locker_cfg(tmp_path), clock)
    cleanup_list += [leds, door]
    assert door.open("long", duration=99, locker_id="2")
    assert door.locker_status("2").remaining_sec == pytest.approx(5.0)
    clock.advance(5.0)
    door.status()
    assert not levels(door)["2"]
    cfg = make_locker_cfg(tmp_path / "x")
    cfg.max_open_sec = 30.0  # bypassing validation must still be caught (10 s code limit)
    with pytest.raises(ConfigError):
        MockDoorController(cfg)


@pytest.mark.parametrize("backend", BACKENDS)
def test_locker_realtime_watchdog(backend, tmp_path, cleanup_list):
    cfg = make_locker_cfg(tmp_path, open_sec=0.1, max_open_sec=0.2, cooldown_sec=0.0)
    door, leds = make_door(backend, cfg)
    cleanup_list += [leds, door]
    assert door.open("rt", locker_id="1", duration=5.0)
    deadline = time.monotonic() + 3.0
    while door.signal_on() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(levels(door).values()) and not door.green_on()
    assert any(k == "WATCHDOG" and r.startswith("locker=1 ") for _, k, r in door.history)


@pytest.mark.parametrize("backend", BACKENDS)
def test_open_without_or_with_unknown_locker_rejected(backend, tmp_path, cleanup_list):
    door, leds = make_door(backend, make_locker_cfg(tmp_path), FakeClock())
    cleanup_list += [leds, door]
    assert door.open("manual-test") is False and door.last_reject == "no_locker_id"
    assert door.open("x", locker_id="Z99") is False and door.last_reject == "unknown_locker"
    assert door.open("x", locker_id=1) is False and door.last_reject == "unknown_locker"  # not a str
    assert not any(levels(door).values()) and not door.green_on()


def test_close_unknown_locker_closes_all(tmp_path, cleanup_list, caplog):
    door, leds = make_door("state", make_locker_cfg(tmp_path, max_concurrent_open=2), FakeClock())
    cleanup_list += [leds, door]
    door.open("a", locker_id="1")
    door.open("b", locker_id="2")
    door.close("x", locker_id="2")
    assert levels(door) == {"1": True, "2": False, "3": False, "4": False} and door.green_on()
    door.open("b2", locker_id="3")
    with caplog.at_level(logging.WARNING, logger="facedoor.hardware"):
        door.close("bogus", locker_id="Z99")
    assert not any(levels(door).values()) and not door.green_on() and "ALL" in caplog.text


@pytest.mark.parametrize("backend", BACKENDS)
def test_invariant_violation_faults_all_channels(backend, tmp_path, cleanup_list):
    door, leds = make_door(backend, make_locker_cfg(tmp_path), FakeClock())
    cleanup_list += [leds, door]
    if backend == "state":
        door.pins.set("locker:1", True)  # stray ON while IDLE
    else:
        door.channel_devices["1"].on()
    assert door.open("face:alice", locker_id="2") is False and door.last_reject == "invariant"
    assert not any(levels(door).values()) and not door.green_on()
    assert door.status().state is DoorState.FAULT
    assert door.open("later", locker_id="3") is False and door.last_reject == "fault"
    kinds = [k for _, k, _ in door.history]
    assert "FAULT" in kinds and "OPEN" not in kinds


def test_post_open_invariant_target_not_on(tmp_path, cleanup_list):
    door, leds = make_door("state", make_locker_cfg(tmp_path), FakeClock())
    cleanup_list += [leds, door]
    door._read_channel = lambda idx: False  # output never reads back ON
    assert door.open("x", locker_id="2") is False and door.last_reject == "invariant"
    assert door.status().state is DoorState.FAULT and not door.green_on()


def test_locker_drive_failure_faults_and_raises(tmp_path, cleanup_list):
    door, leds = make_door("state", make_locker_cfg(tmp_path), FakeClock())
    cleanup_list += [leds, door]
    door.pins.fail_on.add(("led_green", True))
    with pytest.raises(HardwareError):
        door.open("x", locker_id="2")
    assert not any(levels(door).values()) and door.status().state is DoorState.FAULT


def test_claim_failure_releases_already_claimed(tmp_path):
    pytest.importorskip("gpiozero")
    from gpiozero import DigitalOutputDevice
    from gpiozero.pins.mock import MockFactory

    from facedoor.hardware.gpio_pi import PiDoorController

    f = MockFactory()
    blocker = DigitalOutputDevice(25, pin_factory=f)  # 2nd channel busy
    cfg = make_locker_cfg(tmp_path, lockers=LOCKERS4[:3])
    with pytest.raises(HardwareError) as ei:
        PiDoorController(cfg, pin_factory=f)
    assert ei.value.exit_code == 5 and "BCM25" in str(ei.value)
    assert f.pin(24).state == 0
    DigitalOutputDevice(24, pin_factory=f).close()  # 1st channel released
    DigitalOutputDevice(16, pin_factory=f).close()  # 3rd never claimed
    DigitalOutputDevice(27, pin_factory=f).close()  # green never claimed
    blocker.close()


def test_factory_builds_per_locker_on_both_backends(tmp_path):
    for backend in ("state", "gpiozero") if HAVE_GPIOZERO else ("state",):
        b = hw_factory.create_hardware(make_locker_cfg(tmp_path / backend, mock_backend=backend),
                                       register_handlers=False)
        try:
            assert b.door.locker_ids() == ("1", "2", "3", "4")
            assert isinstance(b.door, DoorController)
        finally:
            b.cleanup()


# ---------------------------------------------------------------------------
# 13.9 scenario 21: exit paths turn every channel OFF
# ---------------------------------------------------------------------------


def test_per_locker_signal_handler_turns_all_off(tmp_path, monkeypatch):
    b = hw_factory.create_hardware(make_locker_cfg(tmp_path, max_concurrent_open=2), register_handlers=False)
    hw_factory.register_bundle(b, install_handlers=False)
    b.door.open("a", locker_id="2")
    b.door.open("b", locker_id="4")
    monkeypatch.setitem(hw_factory._signals_installed, signal.SIGTERM, signal.SIG_DFL)
    with pytest.raises(SystemExit) as ei:
        hw_factory._signal_handler(signal.SIGTERM, None)
    assert ei.value.code == 128 + int(signal.SIGTERM)
    assert not any(levels(b.door).values()) and not b.door.green_on()
    assert b.door.status().close_cause == "shutdown"
    closes = [r for _, k, r in b.door.history if k == "CLOSE"]
    assert len(closes) == 2 and all("shutdown" in r for r in closes)


LOCKER_CHILD = textwrap.dedent(
    """
    import sys, signal, time
    sys.path.insert(0, {src!r})
    from facedoor.hardware.base import HardwareConfig
    from facedoor.hardware.factory import create_hardware
    cfg = HardwareConfig.from_dict({{"hardware_log": {log!r}, "open_sec": 5.0, "solenoid_pin": None,
        "locker_outputs": [{{"locker_id": "1", "pin": 24}}, {{"locker_id": "2", "pin": 25}}]}})
    b = create_hardware(cfg)
    assert b.door.open("child", locker_id="2")
    how = {how!r}
    if how == "atexit":
        sys.exit(0)
    elif how == "exception":
        raise RuntimeError("boom")
    else:
        signal.raise_signal(getattr(signal, how))
        time.sleep(1)
    """
)


@pytest.mark.parametrize("how", ["atexit", "exception", "SIGINT", "SIGTERM"])
def test_per_locker_process_exit_paths(how, tmp_path):
    log = tmp_path / "hw.log"
    code = LOCKER_CHILD.format(src=str(SRC), log=str(log), how=how)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    rows = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = [r[2] for r in rows]
    assert "OPEN" in events, (proc.stdout, proc.stderr)
    after = rows[events.index("OPEN"):]
    assert any(r[2] == "CLOSE" and r[3].startswith("locker=2 pin=BCM25 ") and "cause=shutdown" in r[3]
               for r in after), (events, proc.stderr)
    assert any(r[2] == "CLEANUP" and "all channels" in r[3] for r in after)


# ---------------------------------------------------------------------------
# 13.9 scenario 22: force-off covers every channel
# ---------------------------------------------------------------------------


def test_force_off_targets_cover_all_outputs(tmp_path):
    cfg = make_locker_cfg(tmp_path, lockers=LOCKERS4[:2] + [{"locker_id": "3", "pin": 16, "active_high": False}])
    assert force_off_targets(cfg) == [("locker:1", 24, True), ("locker:2", 25, True), ("locker:3", 16, False),
                                      ("led_green", 27, True), ("led_red", 22, True), ("led_amber", 23, True)]
    assert force_off_targets(make_cfg(tmp_path))[0] == ("solenoid", 17, True)


class FakeLgpio:
    def __init__(self, fail_pins=()):
        self.fail_pins = set(fail_pins)
        self.writes: list[tuple[int, int]] = []
        self.freed: list[int] = []
        self.closed = False

    def gpiochip_open(self, chip):
        return 7

    def gpio_claim_output(self, h, pin, level):
        if pin in self.fail_pins:
            raise RuntimeError("GPIO busy")
        self.writes.append((pin, level))

    def gpio_write(self, h, pin, level):
        self.writes.append((pin, level))

    def gpio_free(self, h, pin):
        self.freed.append(pin)

    def gpiochip_close(self, h):
        self.closed = True


def test_force_outputs_off_continues_after_failure(tmp_path):
    from facedoor.hardware.gpio_pi import force_outputs_off

    cfg = make_locker_cfg(tmp_path, lockers=LOCKERS4[:3] + [{"locker_id": "4", "pin": 26, "active_high": False}])
    fake = FakeLgpio(fail_pins={25})
    lines = force_outputs_off(cfg, lgpio_module=fake)
    assert len(lines) == 7 and sum(" FAILED" in ln for ln in lines) == 1
    assert "locker:2 BCM25 FAILED" in lines[1]
    assert (26, 1) in fake.writes and (24, 0) in fake.writes and (23, 0) in fake.writes  # active-low OFF = 1
    assert 17 not in {p for p, _ in fake.writes} and fake.closed


def test_hw_selftest_force_off_per_locker_ignores_other_sections(tmp_path):
    extra = tmp_path / "extra.yaml"
    extra.write_text(yaml.safe_dump({
        "hardware": {"mode": "mock", "solenoid_pin": None, "locker_outputs": LOCKERS4},
        "decision": {"min_votes": "broken"}, "no_such_section": 1,
    }), encoding="utf-8")
    script = ROOT / "scripts" / "hw_selftest.py"
    out = subprocess.run([sys.executable, str(script), "--force-off", "--config", str(extra),
                          "--hardware-log", str(tmp_path / "hw.log")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    for name in ("locker:1 BCM24", "locker:2 BCM25", "locker:3 BCM16", "locker:4 BCM26",
                 "led_green BCM27", "led_red BCM22", "led_amber BCM23"):
        assert name in out.stdout
    assert "solenoid" not in out.stdout


# ---------------------------------------------------------------------------
# 13.9 scenario 24: hardware.log audit format
# ---------------------------------------------------------------------------


def test_per_locker_hardware_log_format(tmp_path, cleanup_list):
    clock = FakeClock()
    cfg = make_locker_cfg(tmp_path)
    door, leds = make_door("state", cfg, clock)
    cleanup_list += [leds, door]
    door.open("face:u0001:0.6123:3/3:locker=2", locker_id="2")
    clock.advance(3.0)
    door.status()  # lazy watchdog
    door.open("face:u0001:0.6050:3/3:locker=2", locker_id="2")  # cooldown
    door.open("manual-test")  # no locker
    door.open("x", locker_id="3")
    door.close("manual", locker_id="3")
    rows = log_rows(cfg)
    init = [d for s, e, d in rows if e == "INIT"]
    assert init and init[0].startswith("per_locker channels=1:BCM24:H,2:BCM25:H,3:BCM16:H,4:BCM26:H green=BCM27 "
                                       "max_concurrent_open=1")
    chan = [(e, d) for s, e, d in rows if e in {"OPEN", "CLOSE", "WATCHDOG", "REJECT", "FAULT"}]
    assert [e for e, _ in chan] == ["OPEN", "WATCHDOG", "REJECT", "REJECT", "OPEN", "CLOSE"]
    for _, d in chan:
        assert AUDIT_RE.match(d), d
    assert chan[0][1].startswith("locker=2 pin=BCM25 face:u0001:0.6123:3/3:locker=2 signal=ON duration=3.000s")
    assert re.fullmatch(r"locker=2 pin=BCM25 timeout signal=OFF cause=timer unlocked_for=3\.000s "
                        r"door_open_for=0\.000s open_for=3\.000s", chan[1][1])
    assert chan[2][1].endswith("code=cooldown") and chan[3][1] == "locker=- pin=BCM- manual-test code=no_locker_id"
    assert "cause=manual" in chan[5][1]


# ---------------------------------------------------------------------------
# hw_selftest --locker / --all-lockers (in-process, mock only)
# ---------------------------------------------------------------------------


def _load_selftest(monkeypatch):
    spec = importlib.util.spec_from_file_location("hw_selftest_mod", ROOT / "scripts" / "hw_selftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "create_hardware", lambda cfg: hw_factory.create_hardware(cfg, register_handlers=False))
    return mod


@pytest.mark.parametrize("close_mode", ["timer", "limit"])
def test_hw_selftest_all_lockers_one_at_a_time(close_mode, tmp_path, monkeypatch, capsys):
    mod = _load_selftest(monkeypatch)
    cfg = limit_locker_cfg(tmp_path) if close_mode == "limit" else make_locker_cfg(tmp_path)
    args = mod.parse_args(["--allow-solenoid", "--all-lockers", "--pulse-sec", "0.05", "--step-sec", "0"])
    assert mod.run_selftest(cfg, args) == 0, capsys.readouterr().out
    rows = [(e, d) for _, e, d in log_rows(cfg) if e in {"OPEN", "CLOSE", "WATCHDOG"}]
    seq = [(e, AUDIT_RE.match(d).group(1)) for e, d in rows]
    assert [lid for e, lid in seq if e == "OPEN"] == ["1", "2", "3", "4"]
    # strictly alternating OPEN(x) -> CLOSE/WATCHDOG(x): never two channels ON at once
    for i in range(0, 8, 2):
        assert seq[i][0] == "OPEN" and seq[i + 1][0] in {"CLOSE", "WATCHDOG"} and seq[i][1] == seq[i + 1][1]
    assert "SELFTEST OK" in capsys.readouterr().out


def test_hw_selftest_locker_subset_and_usage_errors(tmp_path, monkeypatch):
    mod = _load_selftest(monkeypatch)
    cfg = make_locker_cfg(tmp_path)
    args = mod.parse_args(["--allow-solenoid", "--locker", "3", "1", "3", "--pulse-sec", "0.05", "--step-sec", "0"])
    assert mod.resolve_locker_targets(cfg, args) == ["1", "3"]
    assert mod.run_selftest(cfg, args) == 0
    assert [AUDIT_RE.match(d).group(1) for _, e, d in log_rows(cfg) if e == "OPEN"] == ["1", "3"]
    with pytest.raises(ConfigError):
        mod.resolve_locker_targets(cfg, mod.parse_args(["--all-lockers"]))  # needs --allow-solenoid
    with pytest.raises(ConfigError):
        mod.resolve_locker_targets(cfg, mod.parse_args(["--allow-solenoid", "--locker", "Z99"]))
    with pytest.raises(ConfigError):
        mod.resolve_locker_targets(make_cfg(tmp_path), mod.parse_args(["--allow-solenoid", "--locker", "1"]))
    # per_locker + --allow-solenoid without a target: only checks that open() without id is refused
    args2 = mod.parse_args(["--allow-solenoid", "--step-sec", "0"])
    assert mod.run_selftest(make_locker_cfg(tmp_path / "b"), args2) == 0


def test_hw_selftest_locker_without_allow_exits_2(tmp_path):
    script = ROOT / "scripts" / "hw_selftest.py"
    out = subprocess.run([sys.executable, str(script), "--mode", "mock", "--locker", "1",
                          "--hardware-log", str(tmp_path / "hw.log")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "--allow-solenoid" in out.stderr


def test_hw_selftest_limit_mode_single_pulse(tmp_path, monkeypatch, capsys):
    mod = _load_selftest(monkeypatch)
    cfg = make_limit_cfg(tmp_path)
    args = mod.parse_args(["--allow-solenoid", "--pulse-sec", "0.05", "--step-sec", "0"])
    t0 = time.monotonic()
    assert mod.run_selftest(cfg, args) == 0, capsys.readouterr().out
    assert time.monotonic() - t0 < 3.0  # explicit close after the pulse, not unlock_timeout_sec


def test_hw_selftest_limits_watch_and_sim(tmp_path, monkeypatch, capsys):
    mod = _load_selftest(monkeypatch)
    cfg = limit_locker_cfg(tmp_path)
    assert mod.run_limits_watch(cfg, mod.parse_args(["--limits", "--limits-sec", "0.1"])) == 0
    out = capsys.readouterr().out
    assert "1=" in out and "4=" in out
    keys = iter(["1", "5", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(keys))
    assert mod.run_sim_limits(cfg, mod.parse_args(["--sim-limits"])) == 0
    out = capsys.readouterr().out
    assert "문 열림" in out and "센서 오류 주입" in out
    with pytest.raises(ConfigError):
        mod.run_limits_watch(make_cfg(tmp_path), mod.parse_args(["--limits"]))  # timer mode
