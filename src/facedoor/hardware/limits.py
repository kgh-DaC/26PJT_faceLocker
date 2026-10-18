"""Limit switch inputs (owner: hw-engineer). Contract: docs/INTERFACES.md 4.1 and 15.1/15.4.

``read(locker_id)`` -> True = pressed (door closed), False = released (door open), None = unreadable /
not configured. The polarity (``limit_active_low``) is interpreted HERE; the state machine in base.py only
sees pressed / released / unknown. ``read`` never raises.

- ``MockLimitInputs``: pure-Python virtual switches for the PC. Initial value: every door closed (pressed).
  ``set_closed`` / ``toggle`` / ``set_fault`` are what the recognition screen wires to keys and clicks.
- ``PiLimitInputs``: gpiozero ``DigitalInputDevice`` per door (``pull_up=limit_pull_up``). With a
  gpiozero ``MockFactory`` (``hardware.mock_backend: gpiozero``) it is flagged ``is_simulated``, starts with
  every door closed, and the same ``set_closed`` / ``toggle`` / ``set_fault`` helpers drive the mock pins (the real code path:
  pin level -> polarity -> state machine). On real hardware these helpers raise.

Nothing outside ``facedoor.hardware`` may call ``read`` (the app only looks at ``locker_states()``).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from facedoor.errors import HardwareError, HardwareUnavailableError
from facedoor.hardware.base import HardwareConfig, limit_channels

__all__ = ["MockLimitInputs", "PiLimitInputs"]

logger = logging.getLogger("facedoor.hardware")


def _key(locker_id: str | None) -> str:
    return "-" if locker_id is None else str(locker_id)


class MockLimitInputs:
    """Virtual limit switches (``is_simulated`` is True). Thread safe."""

    is_simulated: bool = True

    def __init__(self, cfg: HardwareConfig) -> None:
        self._lock = threading.Lock()
        self._ids: list[str | None] = [lid for lid, _ in limit_channels(cfg)]
        self._closed: dict[str | None, bool] = {lid: True for lid in self._ids}
        self._fault: dict[str | None, bool] = {lid: False for lid in self._ids}
        self._opened = True

    def locker_ids(self) -> tuple[str | None, ...]:
        return tuple(self._ids)

    def _check(self, locker_id: str | None) -> None:
        if locker_id not in self._closed:
            raise KeyError(locker_id)

    def read(self, locker_id: str | None) -> bool | None:
        with self._lock:
            if not self._opened or locker_id not in self._closed or self._fault[locker_id]:
                return None
            return self._closed[locker_id]

    def set_closed(self, locker_id: str | None, closed: bool) -> None:
        """Press (closed=True) or release (closed=False) the virtual switch."""
        with self._lock:
            self._check(locker_id)
            self._closed[locker_id] = bool(closed)

    def toggle(self, locker_id: str | None) -> bool:
        """Open a closed door / close an open one. Returns the new ``closed`` value."""
        with self._lock:
            self._check(locker_id)
            self._closed[locker_id] = not self._closed[locker_id]
            return self._closed[locker_id]

    def set_fault(self, locker_id: str | None, fault: bool) -> None:
        """True: ``read`` returns None (broken wire / unreadable sensor)."""
        with self._lock:
            self._check(locker_id)
            self._fault[locker_id] = bool(fault)

    def is_fault(self, locker_id: str | None) -> bool:
        with self._lock:
            self._check(locker_id)
            return self._fault[locker_id]

    def snapshot(self) -> dict[str, bool | None]:
        """{locker_id ("-" in single mode): closed | None when faulted}"""
        with self._lock:
            return {_key(lid): (None if self._fault[lid] else self._closed[lid]) for lid in self._ids}

    def close(self) -> None:
        with self._lock:
            self._opened = False


class PiLimitInputs:
    """gpiozero inputs, one per door. A pin claim failure releases the pins already claimed and raises
    HardwareError (exit code 5)."""

    def __init__(self, cfg: HardwareConfig, pin_factory: Any) -> None:
        try:
            import gpiozero  # noqa: PLC0415 - lazy
        except Exception as exc:  # noqa: BLE001
            raise HardwareUnavailableError(f"gpiozero is not installed/importable: {exc}") from exc
        missing = cfg.missing_limit_pins()
        if missing:
            raise HardwareError(f"limit pins missing for {missing}; set hardware.limit_switch_pin / "
                                "locker_outputs[].limit_pin (deploy/WIRING.md)")
        self._lock = threading.Lock()
        self._active_low = bool(cfg.limit_active_low)
        self._pull_up = bool(cfg.limit_pull_up)
        self._pin_factory = pin_factory
        self.is_simulated = type(pin_factory).__name__ == "MockFactory"
        self._devs: dict[str | None, Any] = {}
        self._pins: dict[str | None, int] = {}
        self._fault: dict[str | None, bool] = {}
        what = ""
        try:
            for lid, pin in limit_channels(cfg):
                assert pin is not None
                what = f"limit {_key(lid)} BCM{pin}"
                dev = gpiozero.DigitalInputDevice(pin, pull_up=self._pull_up, pin_factory=pin_factory)
                self._devs[lid] = dev
                self._pins[lid] = pin
                self._fault[lid] = False
        except Exception as exc:
            claimed = len(self._devs)
            self.close()
            raise HardwareError(f"cannot claim limit input GPIO ({what}): {exc}; {claimed} already-claimed "
                                "input(s) released") from exc
        if self.is_simulated:  # same initial state as MockLimitInputs: every door closed (pressed)
            for lid in list(self._pins):
                self._drive(lid, True)

    def locker_ids(self) -> tuple[str | None, ...]:
        return tuple(self._pins)

    def _pressed_level(self) -> int:
        return 0 if self._active_low else 1

    def read(self, locker_id: str | None) -> bool | None:
        with self._lock:
            dev = self._devs.get(locker_id)
            if dev is None or self._fault.get(locker_id, False):
                return None
            try:
                level = int(dev.pin.state)
            except Exception:  # noqa: BLE001 - never raise
                logger.debug("limit read failed for %s", _key(locker_id), exc_info=True)
                return None
        return level == self._pressed_level()

    # -- simulation helpers (MockFactory only) ----------------------------------------------
    def _require_sim(self) -> None:
        if not self.is_simulated:
            raise RuntimeError("limit simulation helpers are only available with a simulated pin factory "
                               "(hardware.mock_backend: gpiozero)")

    def _drive(self, locker_id: str | None, closed: bool) -> None:
        pin = self._pins[locker_id]
        want = self._pressed_level() if closed else (1 - self._pressed_level())
        mp = self._pin_factory.pin(pin)
        if want:
            mp.drive_high()
        else:
            mp.drive_low()

    def set_closed(self, locker_id: str | None, closed: bool) -> None:
        self._require_sim()
        if locker_id not in self._devs:
            raise KeyError(locker_id)
        self._drive(locker_id, bool(closed))

    def toggle(self, locker_id: str | None) -> bool:
        self._require_sim()
        cur = self.read(locker_id)
        new_closed = not bool(cur) if cur is not None else True
        self.set_closed(locker_id, new_closed)
        return new_closed

    def set_fault(self, locker_id: str | None, fault: bool) -> None:
        self._require_sim()
        if locker_id not in self._devs:
            raise KeyError(locker_id)
        with self._lock:
            self._fault[locker_id] = bool(fault)

    def snapshot(self) -> dict[str, bool | None]:
        return {_key(lid): self.read(lid) for lid in list(self._pins)}

    def close(self) -> None:
        with self._lock:
            devs, self._devs = list(self._devs.values()), {}
        for d in devs:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                logger.exception("closing limit input failed")
