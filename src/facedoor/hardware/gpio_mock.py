"""Pure-Python mock GPIO (owner: hw-engineer). Default on PC (``hardware.mode: mock``, ``mock_backend: state``).

How to confirm "the signal is going out" on a PC:
- console / app log: every transition is logged at INFO by logger ``facedoor.hardware``
  (``[mock-door] OPEN face:alice:0.6123:3/5 duration=3.000s``), plus electrical pin levels.
- ``logs/hardware.log``: same transitions, one line each, with ISO timestamps.
- screen overlay: ``door.status()`` (DoorStatus) and ``leds.snapshot()`` (LedSnapshot) are what
  ``display.render_overlay`` draws as 'DOOR SIGNAL: ON/OFF' and virtual G/R/A LEDs;
  ``format_status_line()`` gives the same as one ASCII line.
- tests: ``MockDoorController.history`` and ``MockPinBank.history`` (electrical level per pin,
  i.e. polarity ``*_active_high`` already applied).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from facedoor.hardware.base import BaseDoorController, BaseStatusLeds, HardwareConfig, output_channels
from facedoor.hardware.limits import MockLimitInputs

__all__ = ["MockPinBank", "MockDoorController", "MockStatusLeds", "MockExitButton"]

logger = logging.getLogger("facedoor.hardware")


class MockPinBank:
    """Virtual output pins. Stores the *logical* state and records the *electrical* level."""

    HISTORY_MAX = 5000

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._logical: dict[str, bool] = {}
        self._meta: dict[str, tuple[int, bool]] = {}  # name -> (bcm, active_high)
        self.history: list[tuple[float, str, int, int]] = []  # (ts, name, bcm, electrical level 0/1)
        self.fail_on: set[tuple[str, bool]] = set()  # fault injection for tests: {(name, logical_value)}

    def setup(self, name: str, bcm: int, active_high: bool) -> None:
        with self._lock:
            self._meta[name] = (bcm, active_high)
            self._logical[name] = False
        self._emit(name, False)

    def set(self, name: str, on: bool) -> None:
        if (name, bool(on)) in self.fail_on:
            raise OSError(f"injected mock GPIO failure on {name}={on}")
        with self._lock:
            changed = self._logical.get(name) != bool(on)
            self._logical[name] = bool(on)
        if changed:
            self._emit(name, bool(on))

    def get(self, name: str) -> bool:
        with self._lock:
            return self._logical.get(name, False)

    def level(self, name: str) -> int:
        """Electrical level (0/1) currently present on the virtual pin."""
        bcm, ah = self._meta[name]
        on = self.get(name)
        return int(on if ah else not on)

    def _emit(self, name: str, on: bool) -> None:
        bcm, ah = self._meta[name]
        lvl = int(on if ah else not on)
        with self._lock:
            self.history.append((self._clock(), name, bcm, lvl))
            if len(self.history) > self.HISTORY_MAX:
                del self.history[: len(self.history) - self.HISTORY_MAX]
        logger.debug("[mock-gpio] %s BCM%d -> %s (level %d)", name, bcm, "ON" if on else "off", lvl)


class MockDoorController(BaseDoorController):
    """DoorController with virtual door outputs + green LED.

    Virtual pin names: single mode ``"solenoid"``; per_locker ``"locker:<id>"`` per channel; ``"led_green"``.
    ``history``: (ts, OPEN|CLOSE|REJECT|WATCHDOG|FAULT|CLEANUP, reason) — per_locker reasons start
    with ``locker=<id> pin=BCM<n>``.

    close_mode=limit: ``self.limits`` is a ``MockLimitInputs`` (virtual switches, initially every door
    closed; ``limits.toggle(id)`` / ``set_closed`` / ``set_fault`` simulate the door). A daemon thread
    calls ``poll_once()`` every ``limit_poll_sec`` unless ``start_thread=False`` (tests with a fake clock).
    """

    def __init__(self, cfg: HardwareConfig, clock: Callable[[], float] = time.monotonic,
                 pins: MockPinBank | None = None, limits: Any | None = None, *,
                 start_thread: bool = True) -> None:
        cfg.validate()
        self.pins = pins if pins is not None else MockPinBank(clock)
        self._names: list[str] = []
        self._ids: list[str | None] = []
        for lid, pin, ah in output_channels(cfg):
            name = "solenoid" if lid is None else f"locker:{lid}"
            self.pins.setup(name, pin, ah)
            self._names.append(name)
            self._ids.append(lid)
        self.pins.setup("led_green", cfg.led_green_pin, cfg.led_active_high)
        if cfg.limit_mode and limits is None:
            limits = MockLimitInputs(cfg)
        super().__init__(cfg, clock=clock, source="mock-door", limits=limits)
        single = (f"mock door ready: solenoid BCM{cfg.solenoid_pin} "
                  f"(active_high={cfg.solenoid_active_high}), green BCM{cfg.led_green_pin}; outputs OFF")
        self.log.write("INIT", self._init_detail(single, "backend=mock-state"))
        self._finish_init(start_thread=start_thread)

    def _drive_channel(self, idx: int, on: bool) -> None:
        self.pins.set(self._names[idx], on)

    def _read_channel(self, idx: int) -> bool:
        return self.pins.get(self._names[idx])

    def _drive_green(self, on: bool) -> None:
        self.pins.set("led_green", on)

    def _read_green(self) -> bool:
        return self.pins.get("led_green")

    def channel_pin_name(self, locker_id: str | None = None) -> str:
        """Virtual pin name of a channel (single mode: "solenoid")."""
        for name, lid in zip(self._names, self._ids):
            if lid == locker_id:
                return name
        raise KeyError(locker_id)

    def channel_levels(self) -> dict[str, bool]:
        """per_locker: locker_id -> output logically ON (polarity-independent). single mode: {}.
        Electrical levels: ``self.pins.level("locker:<id>")``."""
        return {lid: self.pins.get(name) for name, lid in zip(self._names, self._ids) if lid is not None}


class MockStatusLeds(BaseStatusLeds):
    def __init__(self, cfg: HardwareConfig, door: MockDoorController) -> None:
        super().__init__(cfg, door, source="mock-leds")
        self.pins = door.pins
        self.pins.setup("led_red", cfg.led_red_pin, cfg.led_active_high)
        self.pins.setup("led_amber", cfg.led_amber_pin, cfg.led_active_high)

    def _set_red(self, on: bool) -> None:
        self.pins.set("led_red", on)

    def _set_amber(self, on: bool) -> None:
        self.pins.set("led_amber", on)

    def _read_red(self) -> bool:
        return self.pins.get("led_red")

    def _read_amber(self) -> bool:
        return self.pins.get("led_amber")


class MockExitButton:
    """Stand-in for gpiozero.Button when ``exit_button_pin`` is set in mock mode. Call ``press()``."""

    def __init__(self, pin: int) -> None:
        self.pin = pin
        self.when_pressed: Callable[[], None] | None = None
        self.closed = False

    def press(self) -> None:
        if not self.closed and self.when_pressed is not None:
            self.when_pressed()

    def close(self) -> None:
        self.closed = True
