"""Hardware contract types and the shared door/LED state machine (owner: hw-engineer).

Contract: docs/INTERFACES.md sections 4.1, 13 (v1.3 per-locker outputs) and 15 (v1.5 limit-sensor lock
control). This module imports only the standard library and ``facedoor.errors`` so that vision/eval
modules can import ``HardwareConfig``, ``DoorStatus`` and ``LedSnapshot`` on any platform.

Safety model (docs/ARCHITECTURE.md sections 4, 6.1 and 9):
- ``BaseDoorController`` is a *channel table*. single mode = one channel (``solenoid_pin``, no id);
  per_locker = one channel per ``hardware.locker_outputs`` entry, in declaration order. State, deadlines
  and timers are per channel; one ``RLock`` and the green LED are shared. Green ON <=> at least one
  channel output ON (ON: output then green; OFF of the last ON channel: green then output).
- close_mode=timer (v1.3, legacy): an open lasts ``min(duration, max_open_sec, 10.0)`` s.
- close_mode=limit (v1.5): face recognition only UNLOCKS (solenoid ON). A limit switch per door says
  whether the door is closed. A hardware thread calls ``poll_once()`` every ``limit_poll_sec``:
  UNLOCKED_WAITING --door opens--> OPEN --door closes for close_confirm_sec--> relock, or
  --unlock_timeout_sec without the door opening--> relock. ``max_unlock_sec`` after the unlock the
  solenoid goes OFF no matter what the sensor says (independent ``threading.Timer`` + thread + lazy
  check). Unreadable sensor for ``sensor_fault_sec`` -> solenoid OFF (fail-closed); unlocking is refused
  while the sensor is unreadable or the door is already open.
- Every output is verified: an output observed ON on a channel that is not in an ON state -> every
  channel OFF + FAULT (latched until restart). If driving an output raises, same.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from facedoor.errors import ConfigError, HardwareError

__all__ = [
    "ABSOLUTE_MAX_OPEN_SEC",
    "ABSOLUTE_MAX_UNLOCK_SEC",
    "ABSOLUTE_MAX_LOCKERS",
    "ABSOLUTE_MAX_CONCURRENT_OPEN",
    "RESERVED_OUTPUT_PINS",
    "BOOT_PULL_UP_PINS",
    "VALID_MODES",
    "VALID_MOCK_BACKENDS",
    "VALID_PIN_FACTORIES",
    "VALID_CLOSE_MODES",
    "REJECT_CODES",
    "ON_STATES",
    "CloseCause",
    "LockerOutput",
    "HardwareConfig",
    "DoorState",
    "LockerState",
    "DoorStatus",
    "LockerStatus",
    "DoorEvent",
    "LedSnapshot",
    "LimitInputs",
    "DoorController",
    "StatusLeds",
    "HardwareBundle",
    "HardwareEventLog",
    "BaseDoorController",
    "BaseStatusLeds",
    "output_channels",
    "limit_channels",
    "force_off_targets",
    "format_status_line",
    "resolve_project_path",
]

logger = logging.getLogger("facedoor.hardware")

ABSOLUTE_MAX_OPEN_SEC: float = 10.0
"""Code-level upper bound on a timer-mode open duration. No configuration can exceed it."""

ABSOLUTE_MAX_UNLOCK_SEC: float = 120.0
"""Code-level upper bound on the limit-mode solenoid ON time (``max_unlock_sec``)."""

ABSOLUTE_MAX_LOCKERS: int = 16
"""Code-level upper bound on the number of locker output channels."""

ABSOLUTE_MAX_CONCURRENT_OPEN: int = 4
"""Code-level upper bound on simultaneously open locker channels."""

RESERVED_OUTPUT_PINS: frozenset[int] = frozenset({0, 1, 2, 3, 14, 15})
"""0/1 = HAT ID EEPROM (I2C0), 2/3 = I2C1 (1.8k pull-ups on the board -> HIGH while booting),
14/15 = UART (console TX toggles during boot). Never used for solenoid/locker outputs or limit inputs."""

BOOT_PULL_UP_PINS: frozenset[int] = frozenset(range(0, 9))
"""BCM 0-8 default to pull-UP at power-on (BCM 9-27 pull-down). Not verified on Pi 5 RP1."""

VALID_MODES = frozenset({"mock", "pi", "auto"})
VALID_MOCK_BACKENDS = frozenset({"state", "gpiozero"})
VALID_PIN_FACTORIES = frozenset({"lgpio", "mock"})
VALID_CLOSE_MODES = frozenset({"timer", "limit"})
BCM_MIN, BCM_MAX = 0, 27  # 40-pin header user GPIOs (Pi 5: GPIO0-27)
EPS = 1e-9  # tolerance for "elapsed >= threshold" comparisons on float clocks
MAX_PENDING_EVENTS = 1000

REJECT_CODES = frozenset({
    "shutdown", "fault", "no_locker_id", "unknown_locker", "busy", "cooldown",
    "concurrency_limit", "invalid_duration", "invariant", "sensor_fault", "door_open",
})
"""Values of ``DoorController.last_reject`` (INTERFACES section 13.4 / 15.3). "" after a successful open."""

_LOCKER_OUTPUT_KEYS = frozenset({"locker_id", "pin", "active_high", "limit_pin"})
_warned_once: set[str] = set()


def _warn_once(msg: str) -> None:
    """Validation runs several times per process (from_dict, constructors, factory): warn once."""
    if msg in _warned_once:
        return
    _warned_once.add(msg)
    logger.warning(msg)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LockerOutput:
    """One entry of ``hardware.locker_outputs``: locker id -> BCM output pin (+ polarity, + limit input)."""

    locker_id: str
    pin: int  # BCM
    active_high: bool | None = None  # None -> HardwareConfig.solenoid_active_high
    limit_pin: int | None = None  # BCM input of this locker's limit switch (limit mode on a real Pi: required)

    def effective_active_high(self, cfg: "HardwareConfig") -> bool:
        return bool(cfg.solenoid_active_high) if self.active_high is None else bool(self.active_high)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"locker_id": self.locker_id, "pin": self.pin}
        if self.active_high is not None:
            d["active_high"] = self.active_high
        if self.limit_pin is not None:
            d["limit_pin"] = self.limit_pin
        return d


@dataclass(slots=True)
class HardwareConfig:
    """Mirror of the ``hardware`` section of configs/default.yaml (same names; code defaults are the
    legacy-compatible ones: single output, close_mode=timer)."""

    mode: str = "mock"
    mock_backend: str = "state"
    pin_factory: str = "lgpio"
    gpio_chip: str | int = "auto"
    solenoid_pin: int | None = 17  # single mode only; must be None in per_locker
    led_green_pin: int = 27
    led_red_pin: int = 22
    led_amber_pin: int = 23
    exit_button_pin: int | None = None
    solenoid_active_high: bool = True
    led_active_high: bool = True
    open_sec: float = 3.0  # timer mode only
    max_open_sec: float = 5.0  # timer mode only
    cooldown_sec: float = 2.0
    deny_blink_count: int = 3
    deny_blink_sec: float = 0.15
    hardware_log: Path = field(default_factory=lambda: Path("logs/hardware.log"))
    locker_outputs: tuple[LockerOutput, ...] = ()
    max_concurrent_open: int = 1
    # --- v1.5 limit-sensor lock control (INTERFACES 15.2) ---
    close_mode: str = "timer"  # "timer" (legacy fixed open_sec) | "limit" (limit-switch state machine)
    limit_switch_pin: int | None = None  # single mode limit input (per_locker: None, use locker_outputs[].limit_pin)
    limit_pull_up: bool = True
    limit_active_low: bool = True  # True: pressed (door closed) = pin LOW
    limit_poll_sec: float = 0.02
    open_confirm_sec: float = 0.1
    close_confirm_sec: float = 2.0
    unlock_timeout_sec: float = 10.0
    max_unlock_sec: float = 30.0
    sensor_fault_sec: float = 1.0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "HardwareConfig":
        """Build and validate from a YAML mapping. Unknown keys / bad types / bad ranges -> ConfigError."""
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ConfigError(f"hardware: unknown key(s) {unknown}; allowed: {sorted(known)}")
        kwargs: dict[str, Any] = {}
        for name, value in d.items():
            kwargs[name] = _coerce_field(name, value)
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Path):
                v = str(v)
            elif f.name == "locker_outputs":
                v = [o.to_dict() for o in v]
            out[f.name] = v
        return out

    @property
    def per_locker(self) -> bool:
        return len(self.locker_outputs) > 0

    @property
    def limit_mode(self) -> bool:
        return self.close_mode == "limit"

    def channel_map(self) -> dict[str, tuple[int, bool]]:
        """locker_id -> (BCM pin, effective active_high), declaration order. Empty in single mode."""
        return {o.locker_id: (o.pin, o.effective_active_high(self)) for o in self.locker_outputs}

    def pins(self) -> dict[str, int]:
        """Name -> BCM pin for every configured output/input ("locker:<id>" / "limit:<id>" for lockers)."""
        p: dict[str, int] = {}
        if self.solenoid_pin is not None:
            p["solenoid"] = self.solenoid_pin
        p["led_green"] = self.led_green_pin
        p["led_red"] = self.led_red_pin
        p["led_amber"] = self.led_amber_pin
        if self.exit_button_pin is not None:
            p["exit_button"] = self.exit_button_pin
        if self.limit_switch_pin is not None:
            p["limit"] = self.limit_switch_pin
        for o in self.locker_outputs:
            p[f"locker:{o.locker_id}"] = o.pin
            if o.limit_pin is not None:
                p[f"limit:{o.locker_id}"] = o.limit_pin
        return p

    def limit_pins(self) -> list[tuple[str | None, int | None]]:
        """(locker_id | None, limit BCM pin | None) per door channel, channel order."""
        return limit_channels(self)

    def missing_limit_pins(self) -> list[str]:
        return [("limit_switch_pin" if lid is None else f"locker_outputs[{lid}].limit_pin")
                for lid, pin in limit_channels(self) if pin is None]

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ConfigError(f"hardware.mode must be one of {sorted(VALID_MODES)}, got {self.mode!r}")
        if self.mock_backend not in VALID_MOCK_BACKENDS:
            raise ConfigError(
                f"hardware.mock_backend must be one of {sorted(VALID_MOCK_BACKENDS)}, got {self.mock_backend!r}"
            )
        if self.pin_factory not in VALID_PIN_FACTORIES:
            raise ConfigError(
                f"hardware.pin_factory must be one of {sorted(VALID_PIN_FACTORIES)}, got {self.pin_factory!r}"
            )
        if not (self.gpio_chip == "auto" or (isinstance(self.gpio_chip, int) and self.gpio_chip >= 0)):
            raise ConfigError(f"hardware.gpio_chip must be 'auto' or a non-negative int, got {self.gpio_chip!r}")
        if self.close_mode == "hold":
            raise ConfigError("hardware.close_mode=hold was replaced by close_mode=limit (INTERFACES section 15)")
        if self.close_mode not in VALID_CLOSE_MODES:
            raise ConfigError(
                f"hardware.close_mode must be one of {sorted(VALID_CLOSE_MODES)}, got {self.close_mode!r}"
            )
        self._validate_lockers()
        if self.per_locker and self.limit_switch_pin is not None:
            raise ConfigError("hardware: limit_switch_pin must be null in per_locker mode "
                              "(use locker_outputs[].limit_pin per locker)")
        pins = self.pins()
        for name, pin in pins.items():
            if isinstance(pin, bool) or not isinstance(pin, int):
                raise ConfigError(f"hardware: pin for {name} must be an integer, got {pin!r}")
            if not (BCM_MIN <= pin <= BCM_MAX):
                raise ConfigError(f"hardware: {name} pin {pin} out of BCM range {BCM_MIN}..{BCM_MAX}")
        seen: dict[int, str] = {}
        for name, pin in pins.items():
            if pin in seen:
                raise ConfigError(f"hardware: pin {pin} used by both {seen[pin]} and {name}")
            seen[pin] = name
        for name, pin in pins.items():
            if pin not in RESERVED_OUTPUT_PINS:
                continue
            if name == "solenoid" or name.startswith(("locker:", "limit")):
                raise ConfigError(
                    f"hardware: {name} pin BCM{pin} is reserved (0/1 HAT EEPROM, 2/3 I2C, 14/15 UART); "
                    "it can toggle during boot. Use another pin (deploy/WIRING.md)"
                )
            if name.startswith("led_"):
                _warn_once(f"hardware: {name} uses reserved pin BCM{pin}; it may light up during boot")
        self._validate_timing()
        if not (0.0 <= self.cooldown_sec <= 600.0):
            raise ConfigError(f"hardware.cooldown_sec must be in [0, 600], got {self.cooldown_sec}")
        if not (0 <= self.deny_blink_count <= 50):
            raise ConfigError(f"hardware.deny_blink_count must be in [0, 50], got {self.deny_blink_count}")
        if not (0.0 < self.deny_blink_sec <= 5.0):
            raise ConfigError(f"hardware.deny_blink_sec must be in (0, 5], got {self.deny_blink_sec}")
        if self.limit_mode and self.mode == "pi":
            missing = self.missing_limit_pins()
            if missing:
                raise ConfigError(
                    f"hardware: close_mode=limit with mode=pi requires a limit pin for every door; missing {missing}"
                )

    def _validate_timing(self) -> None:
        def rng(name: str, lo: float, hi: float) -> float:
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ConfigError(f"hardware.{name} must be a finite number, got {v!r}")
            if not (lo <= v <= hi):
                raise ConfigError(f"hardware.{name} must be in [{lo:g}, {hi:g}], got {v}")
            return float(v)

        rng("limit_poll_sec", 0.005, 0.1)
        rng("open_confirm_sec", 0.0, 1.0)
        ccs = rng("close_confirm_sec", 0.5, 10.0)
        uts = rng("unlock_timeout_sec", 1.0, 60.0)
        rng("sensor_fault_sec", 0.2, 5.0)
        if self.limit_mode:
            lo = uts + ccs + 1.0
            if not (lo - EPS <= self.max_unlock_sec <= ABSOLUTE_MAX_UNLOCK_SEC):
                raise ConfigError(
                    "hardware.max_unlock_sec must satisfy unlock_timeout_sec + close_confirm_sec + 1.0 <= v <= "
                    f"{ABSOLUTE_MAX_UNLOCK_SEC:g} (got {self.max_unlock_sec}, minimum {lo:g})"
                )
            if self.max_unlock_sec > 60.0:
                _warn_once(f"hardware.max_unlock_sec={self.max_unlock_sec:g} > 60: continuous solenoid energizing "
                           "may overheat the coil; check the solenoid duty rating")
            if not self.limit_pull_up:
                _warn_once("hardware.limit_pull_up=false: an external pull resistor is required on the limit input")
            if self.limit_active_low:
                _warn_once("hardware.limit_active_low=true (NO contact + GND): a disconnected sensor wire reads "
                           "'door open'. Recommended fail-safe wiring: NC contact + limit_active_low: false "
                           "(deploy/WIRING.md)")
        else:
            rng("max_unlock_sec", 0.0, ABSOLUTE_MAX_UNLOCK_SEC)
            if not (0.0 < self.open_sec <= self.max_open_sec <= ABSOLUTE_MAX_OPEN_SEC):
                raise ConfigError(
                    "hardware: require 0 < open_sec <= max_open_sec <= "
                    f"{ABSOLUTE_MAX_OPEN_SEC} (got open_sec={self.open_sec}, max_open_sec={self.max_open_sec})"
                )
            return
        # limit mode: open_sec / max_open_sec are ignored but must still be sane numbers
        for name in ("open_sec", "max_open_sec"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise ConfigError(f"hardware.{name} must be a positive number, got {v!r}")

    def _validate_lockers(self) -> None:
        outs = self.locker_outputs
        if not isinstance(outs, (tuple, list)):
            raise ConfigError("hardware.locker_outputs must be a list")
        for o in outs:
            if not isinstance(o, LockerOutput):
                raise ConfigError(f"hardware.locker_outputs items must be LockerOutput, got {o!r}")
        n = len(outs)
        if (self.solenoid_pin is None) != (n > 0):
            if n > 0:
                raise ConfigError(
                    "hardware: locker_outputs is set (per_locker) but solenoid_pin is not null. "
                    "In per_locker mode solenoid_pin must be null; to reuse BCM17 declare it as a locker "
                    "channel (INTERFACES section 13.2)"
                )
            raise ConfigError("hardware: solenoid_pin is null but locker_outputs is empty "
                              "(single mode needs solenoid_pin, per_locker needs locker_outputs)")
        if n > ABSOLUTE_MAX_LOCKERS:
            raise ConfigError(f"hardware.locker_outputs has {n} channels; code limit is {ABSOLUTE_MAX_LOCKERS}")
        folded: dict[str, str] = {}
        for o in outs:
            lid = o.locker_id
            if not isinstance(lid, str) or lid == "":
                raise ConfigError(f"hardware.locker_outputs: locker_id must be a non-empty string, got {lid!r}")
            if not lid.isascii() or not lid.isprintable() or any(c.isspace() for c in lid):
                raise ConfigError(f"hardware.locker_outputs: locker_id {lid!r} must be printable ASCII without spaces")
            key = lid.casefold()
            if key in folded:
                raise ConfigError(
                    f"hardware.locker_outputs: duplicate locker_id {lid!r} (conflicts with {folded[key]!r}; "
                    "ids differing only by case are duplicates)"
                )
            folded[key] = lid
            if o.active_high is not None and not isinstance(o.active_high, bool):
                raise ConfigError(f"hardware.locker_outputs[{lid}].active_high must be a boolean or null")
            if o.limit_pin is not None and (isinstance(o.limit_pin, bool) or not isinstance(o.limit_pin, int)):
                raise ConfigError(f"hardware.locker_outputs[{lid}].limit_pin must be an integer or null")
        m = self.max_concurrent_open
        if isinstance(m, bool) or not isinstance(m, int):
            raise ConfigError(f"hardware.max_concurrent_open must be an integer, got {m!r}")
        upper = min(max(n, 1), ABSOLUTE_MAX_CONCURRENT_OPEN)
        if not (1 <= m <= upper):
            raise ConfigError(
                f"hardware.max_concurrent_open must be in 1..{upper} "
                f"(channels={n}, code limit {ABSOLUTE_MAX_CONCURRENT_OPEN}), got {m}"
            )
        for o in outs:
            if (isinstance(o.pin, int) and o.pin in BOOT_PULL_UP_PINS and o.pin not in RESERVED_OUTPUT_PINS
                    and o.effective_active_high(self)):
                _warn_once(
                    f"hardware: locker {o.locker_id} is active-high on BCM{o.pin} (boot default pull-UP, BCM 0-8): "
                    "the relay may switch ON while the Pi boots. Prefer BCM >= 9 (unverified on Pi 5 RP1)"
                )


_BOOL_FIELDS = {"solenoid_active_high", "led_active_high", "limit_pull_up", "limit_active_low"}
_INT_FIELDS = {"led_green_pin", "led_red_pin", "led_amber_pin", "deny_blink_count", "max_concurrent_open"}
_OPT_INT_FIELDS = {"solenoid_pin", "exit_button_pin", "limit_switch_pin"}
_FLOAT_FIELDS = {"open_sec", "max_open_sec", "cooldown_sec", "deny_blink_sec", "limit_poll_sec", "open_confirm_sec",
                 "close_confirm_sec", "unlock_timeout_sec", "max_unlock_sec", "sensor_fault_sec"}
_STR_FIELDS = {"mode", "mock_backend", "pin_factory", "close_mode"}


def _parse_locker_id(value: Any, where: str) -> str:
    if isinstance(value, bool):
        raise ConfigError(f"{where}.locker_id must be a string, got bool {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"{where}.locker_id must be a string or a non-negative integer, got {value!r}")
        return str(value)
    if isinstance(value, str):
        return value
    raise ConfigError(f"{where}.locker_id must be a string, got {value!r} ({type(value).__name__})")


def _parse_locker_outputs(value: Any) -> tuple[LockerOutput, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        raise ConfigError(
            "hardware.locker_outputs must be a LIST of {locker_id, pin[, active_high, limit_pin]} items, not a "
            "mapping (a YAML mapping silently drops duplicate keys)"
        )
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigError(f"hardware.locker_outputs must be a list, got {type(value).__name__}")
    out: list[LockerOutput] = []
    for i, item in enumerate(value):
        where = f"hardware.locker_outputs[{i}]"
        if isinstance(item, LockerOutput):
            out.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ConfigError(f"{where} must be a mapping {{locker_id, pin[, active_high, limit_pin]}}, got {item!r}")
        unknown = sorted(set(item) - _LOCKER_OUTPUT_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(_LOCKER_OUTPUT_KEYS)}")
        for req in ("locker_id", "pin"):
            if req not in item:
                raise ConfigError(f"{where}: missing required key {req!r}")
        lid = _parse_locker_id(item["locker_id"], where)
        pin = item["pin"]
        if isinstance(pin, bool) or not isinstance(pin, int):
            raise ConfigError(f"{where}.pin must be an integer (BCM), got {pin!r}")
        ah = item.get("active_high")
        if ah is not None and not isinstance(ah, bool):
            raise ConfigError(f"{where}.active_high must be a boolean or null, got {ah!r}")
        lp = item.get("limit_pin")
        if lp is not None and (isinstance(lp, bool) or not isinstance(lp, int)):
            raise ConfigError(f"{where}.limit_pin must be an integer (BCM) or null, got {lp!r}")
        out.append(LockerOutput(locker_id=lid, pin=pin, active_high=ah, limit_pin=lp))
    return tuple(out)


def _coerce_field(name: str, value: Any) -> Any:
    def bad(expected: str) -> ConfigError:
        return ConfigError(f"hardware.{name} must be {expected}, got {value!r} ({type(value).__name__})")

    if name in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise bad("a boolean")
        return value
    if name in _INT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad("an integer")
        return value
    if name in _OPT_INT_FIELDS:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad("an integer or null")
        return value
    if name in _FLOAT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad("a number")
        f = float(value)
        if not math.isfinite(f):
            raise bad("a finite number")
        return f
    if name in _STR_FIELDS:
        if not isinstance(value, str):
            raise bad("a string")
        return value.strip().lower()
    if name == "gpio_chip":
        if isinstance(value, bool):
            raise bad("'auto' or an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v == "auto":
                return v
            if v.isdigit():
                return int(v)
        raise bad("'auto' or an integer")
    if name == "hardware_log":
        if isinstance(value, (str, os.PathLike)):
            return Path(value)
        raise bad("a path string")
    if name == "locker_outputs":
        return _parse_locker_outputs(value)
    raise ConfigError(f"hardware: unknown key {name!r}")  # pragma: no cover - guarded by from_dict


def output_channels(cfg: HardwareConfig) -> list[tuple[str | None, int, bool]]:
    """Door output channels as (locker_id, BCM pin, active_high), in channel order.

    single mode: ``[(None, solenoid_pin, solenoid_active_high)]``; per_locker: one per locker_outputs entry.
    """
    if cfg.per_locker:
        return [(o.locker_id, o.pin, o.effective_active_high(cfg)) for o in cfg.locker_outputs]
    if cfg.solenoid_pin is None:  # pragma: no cover - rejected by validate()
        raise ConfigError("hardware: no door output configured")
    return [(None, cfg.solenoid_pin, cfg.solenoid_active_high)]


def limit_channels(cfg: HardwareConfig) -> list[tuple[str | None, int | None]]:
    """Limit input per door channel as (locker_id, BCM pin | None), same order as ``output_channels``."""
    if cfg.per_locker:
        return [(o.locker_id, o.limit_pin) for o in cfg.locker_outputs]
    return [(None, cfg.limit_switch_pin)]


def force_off_targets(cfg: HardwareConfig) -> list[tuple[str, int, bool]]:
    """Every output a force-off must drive OFF: (name, BCM pin, active_high).

    solenoid (if configured) + every locker channel + the three LEDs. Limit switches and the exit button
    are inputs and are never driven.
    """
    t: list[tuple[str, int, bool]] = []
    if cfg.solenoid_pin is not None:
        t.append(("solenoid", cfg.solenoid_pin, cfg.solenoid_active_high))
    for o in cfg.locker_outputs:
        t.append((f"locker:{o.locker_id}", o.pin, o.effective_active_high(cfg)))
    t += [
        ("led_green", cfg.led_green_pin, cfg.led_active_high),
        ("led_red", cfg.led_red_pin, cfg.led_active_high),
        ("led_amber", cfg.led_amber_pin, cfg.led_active_high),
    ]
    return t


def resolve_project_path(p: Path) -> Path:
    """Absolute paths unchanged; relative paths resolve against FACEDOOR_HOME or the project root.

    (config.py normally hands us absolute paths already; this keeps the hardware package usable
    stand-alone, e.g. from scripts/hw_selftest.py --force-off under systemd.)
    """
    p = Path(p)
    if p.is_absolute():
        return p
    home = os.environ.get("FACEDOOR_HOME")
    root = Path(home) if home else Path(__file__).resolve().parents[3]
    return root / p


# ---------------------------------------------------------------------------
# Status types
# ---------------------------------------------------------------------------


class DoorState(str, Enum):
    """Legacy aggregate state (kept for ``DoorStatus``): FAULT > OPEN > COOLDOWN > IDLE."""

    IDLE = "IDLE"
    OPEN = "OPEN"
    COOLDOWN = "COOLDOWN"
    FAULT = "FAULT"


class LockerState(str, Enum):
    """Lock + door state of one locker channel (INTERFACES 15.3)."""

    LOCKED_CLOSED = "LOCKED_CLOSED"          # solenoid OFF, limit pressed (door closed)
    UNLOCKED_WAITING = "UNLOCKED_WAITING"    # solenoid ON, pressed: waiting for the door to open (unlock_timeout_sec)
    OPEN = "OPEN"                            # solenoid ON, released (door open)
    CLOSING_CONFIRM = "CLOSING_CONFIRM"      # solenoid ON, pressed again, confirming for close_confirm_sec
    COOLDOWN = "COOLDOWN"                    # just relocked, re-unlock refused for cooldown_sec
    LOCKED_OPEN = "LOCKED_OPEN"              # solenoid OFF but limit released (forced open / sensor wire off)
    SENSOR_FAULT = "SENSOR_FAULT"            # limit unreadable: solenoid forced OFF, unlock refused
    FAULT = "FAULT"                          # output invariant violated / drive failure: restart required


ON_STATES = frozenset({LockerState.UNLOCKED_WAITING, LockerState.OPEN, LockerState.CLOSING_CONFIRM})
"""The only states in which the solenoid output may be ON (invariant)."""


class CloseCause(str, Enum):
    """Why an output channel went OFF (INTERFACES 15)."""

    UNLOCK_TIMEOUT = "unlock_timeout"    # door did not open within unlock_timeout_sec
    CLOSE_CONFIRMED = "close_confirmed"  # door opened, then limit pressed for close_confirm_sec
    WATCHDOG = "watchdog"                # unlocked_at + max_unlock_sec reached (whatever the sensor says)
    SENSOR_FAULT = "sensor_fault"        # limit unreadable for sensor_fault_sec
    SHUTDOWN = "shutdown"                # cleanup / signal / atexit / app exception / camera failure
    FAULT = "fault"
    TIMER = "timer"                      # close_mode=timer: fixed open_sec expired
    MANUAL = "manual"                    # close() called directly (default)


@dataclass(slots=True, frozen=True)
class DoorStatus:
    state: DoorState  # aggregate: FAULT > OPEN > COOLDOWN > IDLE
    is_open: bool  # an output is active (signal ON) — always equal to the green LED
    opened_at: float | None  # monotonic
    remaining_sec: float  # timer: remaining open time; limit: unlock-wait remaining (UNLOCKED_WAITING) else hard
    cooldown_remaining_sec: float
    last_reason: str
    open_count: int  # unlocks since process start
    locker_id: str | None = None  # locker of the last OPEN/CLOSE transition (single mode: None)
    open_lockers: tuple[str, ...] = ()  # lockers whose output is ON now, sorted (single mode: ())
    hard_remaining_sec: float = 0.0  # ON: hard deadline (max_unlock_sec / timer deadline) - now
    close_cause: str = ""  # CloseCause value of the last close


@dataclass(slots=True, frozen=True)
class LockerStatus:
    """Read-only snapshot of one locker channel (panel, logs, tests). INTERFACES 4.1."""

    locker_id: str | None  # single mode: None
    state: LockerState
    solenoid_on: bool  # True only in UNLOCKED_WAITING / OPEN / CLOSING_CONFIRM
    door_closed: bool | None  # confirmed judgement: True closed, False open, None unknown (no sensor / fault)
    sensor_ok: bool
    unlock_wait_remaining_sec: float  # UNLOCKED_WAITING: until unlock_timeout relock, else 0.0
    close_confirm_remaining_sec: float  # CLOSING_CONFIRM: until relock, else 0.0
    hard_remaining_sec: float  # solenoid_on: until the max_unlock_sec watchdog, else 0.0
    cooldown_remaining_sec: float
    unlocked_at: float | None  # monotonic, last unlock
    last_close_cause: str  # CloseCause value, "" if none
    last_reject: str


@dataclass(slots=True, frozen=True)
class DoorEvent:
    """Transition record made by the hardware thread; the app drains it every step (nothing is missed)."""

    ts: float  # monotonic
    locker_id: str | None
    kind: str  # unlocked | door_opened | door_closing | relock | forced_open | sensor_fault | sensor_ok
    cause: str  # CloseCause value for relock, else ""
    detail: str  # e.g. "unlocked_for=3.2s door_open_for=2.1s"


@dataclass(slots=True, frozen=True)
class LedSnapshot:
    green: bool
    red: bool
    amber: bool


@runtime_checkable
class LimitInputs(Protocol):
    """Limit switch inputs (hardware/limits.py). ``read`` never raises."""

    is_simulated: bool

    def read(self, locker_id: str | None) -> bool | None: ...  # True pressed(closed) / False released / None fail
    def close(self) -> None: ...


@runtime_checkable
class DoorController(Protocol):
    def open(self, reason: str, duration: float | None = None, *, locker_id: str | None = None) -> bool: ...
    def close(self, reason: str = "", *, locker_id: str | None = None, cause: str = "manual") -> None: ...
    def status(self) -> DoorStatus: ...
    def locker_status(self, locker_id: str) -> DoorStatus: ...
    def locker_ids(self) -> tuple[str, ...]: ...
    def locker_state(self, locker_id: str | None = None) -> LockerStatus: ...
    def locker_states(self) -> tuple[LockerStatus, ...]: ...
    def drain_events(self) -> list[DoorEvent]: ...
    def poll_once(self) -> None: ...
    @property
    def last_reject(self) -> str: ...
    def cleanup(self) -> None: ...


@runtime_checkable
class StatusLeds(Protocol):
    def show_denied(self) -> None: ...
    def set_hold(self, on: bool) -> None: ...
    def snapshot(self) -> LedSnapshot: ...
    def off(self) -> None: ...
    def cleanup(self) -> None: ...


@dataclass(slots=True)
class HardwareBundle:
    mode: str  # actually selected mode: "pi" | "mock"
    door: DoorController
    leds: StatusLeds
    exit_button: Any | None = None
    limits: Any | None = None  # LimitInputs (MockLimitInputs / PiLimitInputs); None in close_mode=timer
    _cleaned: bool = field(default=False, repr=False)
    _cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def cleanup(self) -> None:
        """door.cleanup() -> leds.cleanup() -> exit button / limits close. Idempotent, never raises."""
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
        for name, fn in (
            ("door", getattr(self.door, "cleanup", None)),
            ("leds", getattr(self.leds, "cleanup", None)),
            ("exit_button", getattr(self.exit_button, "close", None)),
            ("limits", getattr(self.limits, "close", None)),
        ):
            if fn is None:
                continue
            try:
                fn()
            except Exception:  # noqa: BLE001 - cleanup must never raise
                logger.exception("hardware cleanup of %s failed", name)
        try:
            from facedoor.hardware import factory as _factory  # local import: avoid cycle

            _factory.unregister_bundle(self)
        except Exception:  # noqa: BLE001
            pass

    @property
    def cleaned(self) -> bool:
        return self._cleaned

    def __enter__(self) -> "HardwareBundle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def format_status_line(status: DoorStatus, leds: LedSnapshot) -> str:
    """ASCII one-liner for console/overlay, e.g. 'DOOR SIGNAL: ON (2.4s) | G:ON R:off A:off | OPEN'."""
    if status.is_open:
        sig = f"DOOR SIGNAL: ON ({status.remaining_sec:.1f}s)"
        if status.open_lockers:
            sig += " lockers=" + ",".join(status.open_lockers)
    elif status.state is DoorState.COOLDOWN:
        sig = f"DOOR SIGNAL: OFF (cooldown {status.cooldown_remaining_sec:.1f}s)"
    else:
        sig = "DOOR SIGNAL: OFF"

    def onoff(b: bool) -> str:
        return "ON" if b else "off"

    line = f"{sig} | G:{onoff(leds.green)} R:{onoff(leds.red)} A:{onoff(leds.amber)} | {status.state.value}"
    return line.encode("ascii", "replace").decode("ascii")


# ---------------------------------------------------------------------------
# hardware.log writer
# ---------------------------------------------------------------------------


class HardwareEventLog:
    """Append-only, line-flushed log of output transitions (logs/hardware.log) + python logging mirror.

    Line format: ``<ISO8601 ms+TZ>\t<source>\t<EVENT>\t<detail>``. Write failures are logged, never raised
    (a full disk must not prevent the door from closing).
    """

    def __init__(self, path: Path | None, source: str = "hw") -> None:
        self._path = resolve_project_path(path) if path is not None else None
        self._source = source
        self._lock = threading.Lock()
        self._warned = False

    @property
    def path(self) -> Path | None:
        return self._path

    def write(self, event: str, detail: str = "", level: int = logging.INFO) -> None:
        logger.log(level, "[%s] %s %s", self._source, event, detail)
        if self._path is None:
            return
        ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
        detail = detail.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        line = f"{ts}\t{self._source}\t{event}\t{detail}\n"
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
            except OSError:
                if not self._warned:
                    self._warned = True
                    logger.exception("cannot write hardware log %s", self._path)


# ---------------------------------------------------------------------------
# Shared state machine (channel table)
# ---------------------------------------------------------------------------

_CHANNEL_EVENTS = frozenset({"OPEN", "CLOSE", "WATCHDOG", "REJECT", "FAULT"})
_UNSET: Any = object()
_MIN_TIMER_SEC = 0.005


class _Channel:
    """Per-output state. Mutated only under BaseDoorController._lock."""

    __slots__ = ("idx", "locker_id", "pin", "active_high", "limit_pin", "state", "opened_at", "unlocked_at",
                 "unlock_deadline", "close_deadline", "hard_deadline", "cooldown_until", "timer", "timer_token",
                 "last_reason", "open_count", "close_cause", "last_reject", "last_sample", "rel_since",
                 "prs_since", "none_since", "good_since", "door_open_at", "need_close_confirm")

    def __init__(self, idx: int, locker_id: str | None, pin: int, active_high: bool,
                 limit_pin: int | None = None) -> None:
        self.idx = idx
        self.locker_id = locker_id
        self.pin = pin
        self.active_high = active_high
        self.limit_pin = limit_pin
        self.state = LockerState.LOCKED_CLOSED
        self.opened_at: float | None = None
        self.unlocked_at: float | None = None  # last unlock (kept after relock)
        self.unlock_deadline: float | None = None  # limit: relock if the door has not opened by then
        self.close_deadline: float | None = None  # timer: scheduled OFF time
        self.hard_deadline: float | None = None  # limit: unlocked_at + max_unlock_sec; timer: opened_at + max_open
        self.cooldown_until: float | None = None
        self.timer: threading.Timer | None = None
        self.timer_token = 0
        self.last_reason = ""
        self.open_count = 0
        self.close_cause = ""
        self.last_reject = ""
        self.last_sample: Any = _UNSET  # True pressed / False released / None unreadable / _UNSET never read
        self.rel_since: float | None = None  # start of the current continuous "released" run
        self.prs_since: float | None = None  # start of the current continuous "pressed" run
        self.none_since: float | None = None  # start of the current continuous "unreadable" run
        self.good_since: float | None = None  # start of the current continuous run of valid samples
        self.door_open_at: float | None = None  # first OPEN confirmation of the current unlock
        # H-1: set when the solenoid went OFF while the door may have been open (watchdog/sensor_fault/manual
        # with door open): leaving the locked-open/cooldown state needs close_confirm_sec of continuous "pressed".
        self.need_close_confirm = False

    def clear_open(self) -> None:
        self.opened_at = None
        self.unlock_deadline = None
        self.close_deadline = None
        self.hard_deadline = None
        self.door_open_at = None


def _token(value: Any) -> str:
    """Log token without whitespace (the audit regex is ``^locker=(\\S+) pin=BCM(\\S+) ``)."""
    if value is None:
        return "-"
    s = str(value)
    if s == "" or any(c.isspace() for c in s):
        return "-"
    return s


def _normalize_cause(cause: Any) -> str:
    try:
        return CloseCause(cause).value
    except ValueError:
        logger.warning("close(): unknown cause %r -> treated as 'manual'", cause)
        return CloseCause.MANUAL.value


def _door_state_of(state: LockerState) -> DoorState:
    if state in ON_STATES:
        return DoorState.OPEN
    if state is LockerState.COOLDOWN:
        return DoorState.COOLDOWN
    if state is LockerState.FAULT:
        return DoorState.FAULT
    return DoorState.IDLE


class BaseDoorController(ABC):
    """Channel-table door controller. Subclasses only implement output drivers.

    close_mode=timer (legacy): a channel stays ON for ``min(duration or open_sec, max_open_sec)``; states
    LOCKED_CLOSED -> OPEN -> COOLDOWN -> LOCKED_CLOSED.
    close_mode=limit (v1.5): see the module docstring and INTERFACES 15.3. ``poll_once()`` is the single
    entry that samples the limit inputs and runs one tick of the state machine; a daemon thread calls it
    every ``limit_poll_sec``. Tests pass ``start_thread=False`` plus an injected clock and call it directly.

    Subclasses implement:
      ``_drive_channel(idx, on)``  set door output channel ``idx`` (logical ON/OFF, polarity in the driver)
      ``_read_channel(idx)``       actual logical state of channel ``idx``
      ``_drive_green(on)``         set the shared green LED
      ``_read_green()``            actual green LED state
      ``_release()``               release pins/resources (called once from cleanup, after outputs OFF)
    Subclasses must call ``cfg.validate()`` and claim their outputs (all OFF) before ``super().__init__`` and
    call ``self._finish_init(...)`` after their INIT log line.
    """

    HISTORY_MAX = 1000

    def __init__(self, cfg: HardwareConfig, clock: Callable[[], float] = time.monotonic,
                 source: str = "door", limits: Any | None = None) -> None:
        cfg.validate()
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.RLock()
        self._per_locker = cfg.per_locker
        self._limit_mode = cfg.limit_mode
        self.limits = limits
        if self._limit_mode and limits is None:
            raise HardwareError("close_mode=limit needs a LimitInputs object (limits=...)")
        lim_pins = dict(limit_channels(cfg))
        self._channels: list[_Channel] = [
            _Channel(i, lid, pin, ah, lim_pins.get(lid)) for i, (lid, pin, ah) in enumerate(output_channels(cfg))
        ]
        self._by_id: dict[str, _Channel] = {
            ch.locker_id: ch for ch in self._channels if ch.locker_id is not None
        }
        self._fault = False
        self._last_reason = ""
        self._last_locker: str | None = None
        self._last_reject = ""
        self._last_cause = ""
        self._open_count = 0
        self._shutdown = False
        self._events: list[DoorEvent] = []
        self._monitor: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self.history: list[tuple[float, str, str]] = []
        self.log = HardwareEventLog(cfg.hardware_log, source=source)

    # -- hooks -------------------------------------------------------------------------
    @abstractmethod
    def _drive_channel(self, idx: int, on: bool) -> None: ...

    @abstractmethod
    def _read_channel(self, idx: int) -> bool: ...

    @abstractmethod
    def _drive_green(self, on: bool) -> None: ...

    @abstractmethod
    def _read_green(self) -> bool: ...

    def _release(self) -> None:  # pragma: no cover - default no-op
        return None

    # -- start-up ------------------------------------------------------------------------
    def _finish_init(self, *, start_thread: bool = True, require_any_sensor: bool = False) -> None:
        """After the subclass INIT line: limit-mode start-up check (R7) and the polling thread.

        ``require_any_sensor``: every channel unreadable -> HardwareError (real/pi path; caller releases)."""
        if not self._limit_mode:
            return
        with self._lock:
            self._startup_check_locked()
            faulted = [ch for ch in self._channels if ch.state is LockerState.SENSOR_FAULT]
            if require_any_sensor and len(faulted) == len(self._channels):
                raise HardwareError(
                    "no limit switch is readable on any door (all channels SENSOR_FAULT): check the limit wiring "
                    "and hardware.limit_* pins (deploy/WIRING.md)"
                )
        if start_thread:
            self.start_monitor()

    def _startup_check_locked(self) -> None:
        now = self._clock()
        for ch in self._channels:
            s = self._read_sample(ch)
            ch.last_sample = s
            if s is None:
                ch.state = LockerState.SENSOR_FAULT
                ch.none_since = now
                self._log_channel("SENSOR", ch, f"sensor_fault limit_pin=BCM{_token(ch.limit_pin)} at_start=1",
                                  logging.WARNING)
                self._emit(ch, "sensor_fault", "", "at_start")
            elif s is False:
                ch.state = LockerState.LOCKED_OPEN
                ch.rel_since = now
                ch.good_since = now
                logger.warning("locker %s: limit reads 'door open' at start -> LOCKED_OPEN (unlock refused)",
                               _token(ch.locker_id))
            else:
                ch.state = LockerState.LOCKED_CLOSED
                ch.prs_since = now
                ch.good_since = now

    def start_monitor(self) -> None:
        """Start the limit polling thread (idempotent). Daemon: it must never block interpreter exit."""
        with self._lock:
            if not self._limit_mode or self._shutdown or (self._monitor is not None and self._monitor.is_alive()):
                return
            self._monitor_stop.clear()
            t = threading.Thread(target=self._monitor_loop, name="facedoor-hw-poll", daemon=True)
            self._monitor = t
            t.start()

    def monitor_alive(self) -> bool:
        t = self._monitor
        return bool(t is not None and t.is_alive())

    def _monitor_loop(self) -> None:
        period = float(self.cfg.limit_poll_sec)
        try:
            while not self._monitor_stop.wait(period):
                self.poll_once()
        except BaseException as exc:  # noqa: BLE001 - R8: the guard must not die silently
            logger.exception("limit polling thread crashed")
            with self._lock:
                if not self._shutdown:
                    self._fault_locked(f"polling thread crashed: {exc!r}")

    # -- descriptive helpers -------------------------------------------------------------
    @property
    def per_locker(self) -> bool:
        return self._per_locker

    @property
    def close_mode(self) -> str:
        return self.cfg.close_mode

    def _init_detail(self, single_detail: str, backend: str = "") -> str:
        """INIT line: single mode keeps the v1.2 text, per_locker uses the section 13.5 format."""
        mode = f" close_mode={self.cfg.close_mode}"
        if self._limit_mode:
            c = self.cfg
            level = "L" if c.limit_active_low else "H"
            lims = ",".join(f"{_token(lid)}:BCM{_token(p)}:{level}" for lid, p in limit_channels(c))
            mode += (f" limit={lims} unlock_timeout={c.unlock_timeout_sec:g}s close_confirm={c.close_confirm_sec:g}s "
                     f"max_unlock={c.max_unlock_sec:g}s sensor_fault={c.sensor_fault_sec:g}s")
        if not self._per_locker:
            if single_detail.endswith("; outputs OFF"):
                return single_detail[: -len("; outputs OFF")] + mode + "; outputs OFF"
            return single_detail + mode
        chans = ",".join(f"{ch.locker_id}:BCM{ch.pin}:{'H' if ch.active_high else 'L'}" for ch in self._channels)
        extra = f" {backend}" if backend else ""
        return (f"per_locker channels={chans} green=BCM{self.cfg.led_green_pin} "
                f"max_concurrent_open={self.cfg.max_concurrent_open}{mode}{extra}; outputs OFF")

    def _prefix(self, ch: _Channel | None, lid: Any = _UNSET) -> str:
        if ch is not None:
            return f"locker={_token(ch.locker_id)} pin=BCM{ch.pin} "
        return f"locker={_token(None if lid is _UNSET else lid)} pin=BCM- "

    def _record(self, kind: str, reason: str, level: int = logging.INFO, extra: str = "", *,
                ch: _Channel | None = None, lid: Any = _UNSET) -> None:
        if self._per_locker and kind in _CHANNEL_EVENTS:
            head = f"{self._prefix(ch, lid)}{reason}"
            hist = head.rstrip()
            detail = f"{head} {extra}" if extra else head
        else:
            hist = reason
            detail = reason if not extra else f"{reason} {extra}".strip()
        self.history.append((self._clock(), kind, hist))
        if len(self.history) > self.HISTORY_MAX:
            del self.history[: len(self.history) - self.HISTORY_MAX]
        self.log.write(kind, detail, level)

    def _log_channel(self, kind: str, ch: _Channel, text: str, level: int = logging.INFO) -> None:
        """STATE / SENSOR rows: always ``locker=<id|-> pin=BCM<n> `` first; not part of ``history``."""
        self.log.write(kind, f"{self._prefix(ch)}{text}", level)

    def _reject(self, code: str, reason: str, level: int, single_extra: str, *,
                ch: _Channel | None = None, lid: Any = _UNSET) -> bool:
        self._last_reject = code
        if ch is not None:
            ch.last_reject = code
        if self._per_locker:
            self._record("REJECT", reason, level, f"code={code}", ch=ch, lid=lid)
        else:
            self._record("REJECT", reason, level, single_extra)
        return False

    def _emit(self, ch: _Channel, kind: str, cause: str = "", detail: str = "") -> None:
        self._events.append(DoorEvent(ts=self._clock(), locker_id=ch.locker_id, kind=kind, cause=cause,
                                      detail=detail))
        if len(self._events) > MAX_PENDING_EVENTS:
            del self._events[: len(self._events) - MAX_PENDING_EVENTS]

    def _limit_text(self, ch: _Channel) -> str:
        s = ch.last_sample
        if s is _UNSET or s is None:
            return "none"
        return "pressed" if s else "released"

    def _set_state(self, ch: _Channel, new: LockerState) -> None:
        old = ch.state
        if old is new:
            return
        ch.state = new
        if new is LockerState.LOCKED_CLOSED:
            ch.need_close_confirm = False
        if self._limit_mode:
            self._log_channel("STATE", ch, f"{old.value}->{new.value} limit={self._limit_text(ch)}")

    # -- low-level output helpers (lock held) ---------------------------------------------
    def _read_or(self, idx: int, default: bool) -> bool:
        try:
            return bool(self._read_channel(idx))
        except Exception:  # noqa: BLE001
            return default

    def _read_sample(self, ch: _Channel) -> bool | None:
        if self.limits is None:
            return None
        try:
            v = self.limits.read(ch.locker_id)
        except Exception:  # noqa: BLE001 - LimitInputs must not raise; if it does, unreadable
            logger.exception("limit read failed for locker %s", _token(ch.locker_id))
            return None
        return None if v is None else bool(v)

    def _all_off_locked(self) -> Exception | None:
        """Green first, then every channel. Every write is attempted; returns the first error."""
        err: Exception | None = None
        try:
            self._drive_green(False)
        except Exception as exc:  # noqa: BLE001
            err = exc
        for ch in self._channels:
            try:
                self._drive_channel(ch.idx, False)
            except Exception as exc:  # noqa: BLE001
                err = err or exc
        return err

    def _channel_off_locked(self, ch: _Channel) -> Exception | None:
        """Turn one channel OFF; if no other channel is ON, green goes OFF first."""
        err: Exception | None = None
        if not any(o is not ch and o.state in ON_STATES for o in self._channels):
            try:
                self._drive_green(False)
            except Exception as exc:  # noqa: BLE001
                err = exc
        try:
            self._drive_channel(ch.idx, False)
        except Exception as exc:  # noqa: BLE001
            err = err or exc
        return err

    def _cancel_timer(self, ch: _Channel) -> None:
        ch.timer_token += 1
        t, ch.timer = ch.timer, None
        if t is not None:
            t.cancel()

    def _cancel_all_timers(self) -> None:
        for ch in self._channels:
            self._cancel_timer(ch)

    def _timer_deadline(self, ch: _Channel) -> float | None:
        return ch.hard_deadline if self._limit_mode else ch.close_deadline

    def _arm_timer(self, ch: _Channel) -> None:
        """Real-time timer at the channel's absolute deadline (runs even if the main loop and the polling
        thread hang): limit mode = max_unlock_sec watchdog, timer mode = the open duration."""
        self._cancel_timer(ch)
        deadline = self._timer_deadline(ch)
        if deadline is None:
            return
        delay = max(_MIN_TIMER_SEC, deadline - self._clock())
        token = ch.timer_token
        t = threading.Timer(delay, self._on_timer, args=(ch.idx, token))
        t.daemon = True
        t.name = f"facedoor-door-watchdog-{_token(ch.locker_id) if ch.locker_id is not None else 'main'}"
        ch.timer = t
        t.start()

    def _due_locked(self, ch: _Channel, now: float, from_timer: bool) -> tuple[str, str, str] | None:
        """(kind, reason, cause) if the ON channel must be switched OFF now by a deadline, else None."""
        if ch.state not in ON_STATES:
            return None
        if self._limit_mode:
            if ch.hard_deadline is not None and now >= ch.hard_deadline - EPS:
                return "WATCHDOG", "watchdog", CloseCause.WATCHDOG.value
            return None
        if ch.close_deadline is not None and now >= ch.close_deadline - EPS:
            return "WATCHDOG", ("watchdog" if from_timer else "timeout"), CloseCause.TIMER.value
        return None

    def _on_timer(self, idx: int, token: int) -> None:
        with self._lock:
            ch = self._channels[idx]
            if token != ch.timer_token or ch.state not in ON_STATES or self._shutdown:
                return
            due = self._due_locked(ch, self._clock(), from_timer=True)
            if due is None:
                self._arm_timer(ch)  # clock not there yet (e.g. injected clock): re-arm
                return
            kind, reason, cause = due
            self._relock_channel_locked(ch, reason, kind=kind, cause=cause)

    # v1.3 name kept for callers
    _on_watchdog = _on_timer

    def _fault_locked(self, detail: str, *, ch: _Channel | None = None, lid: Any = _UNSET,
                      report_off: bool = True) -> Exception | None:
        """Every output OFF, every channel FAULT (latched), FAULT row. Returns the OFF error, if any."""
        self._cancel_all_timers()
        err = self._all_off_locked()
        self._fault = True
        any_open = False
        for c in self._channels:
            if c.state in ON_STATES:
                any_open = True
                c.close_cause = CloseCause.FAULT.value
                self._emit(c, "relock", CloseCause.FAULT.value, self._relock_detail(c))
            c.state = LockerState.FAULT
            c.clear_open()
            c.cooldown_until = None
        if any_open:
            self._last_cause = CloseCause.FAULT.value
        if report_off:
            detail += "; forced OFF " + ("ok" if err is None else f"FAILED {err!r}")
        if any_open:
            detail += " cause=fault"
        self._record("FAULT", detail, logging.ERROR, ch=ch, lid=lid)
        return err

    def _times_for(self, ch: _Channel, now: float) -> tuple[float, float]:
        """(unlocked_for, door_open_for) seconds for an ON channel."""
        unl = max(0.0, now - ch.opened_at) if ch.opened_at is not None else 0.0
        dop = max(0.0, now - ch.door_open_at) if ch.door_open_at is not None else 0.0
        return unl, dop

    def _relock_detail(self, ch: _Channel) -> str:
        unl, dop = self._times_for(ch, self._clock())
        return f"unlocked_for={unl:.3f}s door_open_for={dop:.3f}s"

    def _close_extra(self, ch: _Channel, cause: str) -> str:
        unl, dop = self._times_for(ch, self._clock())
        # open_for duplicates unlocked_for for log parsers written against the v1.4 format
        return f"signal=OFF cause={cause} unlocked_for={unl:.3f}s door_open_for={dop:.3f}s open_for={unl:.3f}s"

    def _settle_locked(self, ch: _Channel) -> None:
        """After a relock/cooldown: LOCKED_CLOSED only if the limit is known to be closed (and, when the door
        may have been open, pressed for close_confirm_sec); unreadable (None) never counts as closed (H-3)."""
        if not self._limit_mode:
            self._set_state(ch, LockerState.LOCKED_CLOSED)
            return
        s = ch.last_sample
        if s is None:
            # unknown: keep the channel out of LOCKED_CLOSED; the next valid sample / sensor_fault timer decides
            ch.cooldown_until = None
            self._set_state(ch, LockerState.COOLDOWN)
        else:
            ch.cooldown_until = None
            self._set_state(ch, self._locked_target(ch, s, self._clock()))

    def _locked_target(self, ch: _Channel, s: Any, now: float) -> LockerState:
        """LOCKED_CLOSED / LOCKED_OPEN for a valid sample ``s`` (H-1: honour need_close_confirm)."""
        if s is False:
            return LockerState.LOCKED_OPEN
        if ch.need_close_confirm and not (
                s is True and ch.prs_since is not None and now - ch.prs_since >= self.cfg.close_confirm_sec - EPS):
            return LockerState.LOCKED_OPEN
        return LockerState.LOCKED_CLOSED

    def _after_off_locked(self, ch: _Channel, reason: str, kind: str, cause: str,
                          next_state: LockerState | None = None) -> None:
        """Bookkeeping once an ON channel's output is OFF: state, cooldown, hardware.log, event."""
        now = self._clock()
        extra = self._close_extra(ch, cause)
        ev_detail = self._relock_detail(ch)
        ch.close_cause = cause
        self._last_cause = cause
        self._last_locker = ch.locker_id
        ch.need_close_confirm = bool(self._limit_mode and cause != CloseCause.CLOSE_CONFIRMED.value and (
            ch.door_open_at is not None or cause in (CloseCause.WATCHDOG.value, CloseCause.SENSOR_FAULT.value)))
        ch.clear_open()
        ch.cooldown_until = now + self.cfg.cooldown_sec if self.cfg.cooldown_sec > 0 else None
        if next_state is not None:
            self._set_state(ch, next_state)
        elif ch.cooldown_until is not None:
            self._set_state(ch, LockerState.COOLDOWN)
        else:
            self._settle_locked(ch)
        self._record(kind, reason or "close", logging.INFO, extra, ch=ch)
        self._emit(ch, "relock", cause, ev_detail)

    def _relock_channel_locked(self, ch: _Channel, reason: str, kind: str = "CLOSE",
                               cause: str = CloseCause.MANUAL.value, next_state: LockerState | None = None) -> None:
        was_on = ch.state in ON_STATES
        self._cancel_timer(ch)
        err = self._channel_off_locked(ch)
        if err is not None:
            self._fault_locked(f"output OFF failed during close: {err!r}", ch=ch, report_off=self._per_locker)
            return
        if self._fault:
            return
        if was_on:
            self._after_off_locked(ch, reason, kind, cause, next_state)

    # legacy name used by tests/other modules
    _close_channel_locked = _relock_channel_locked

    def _close_all_locked(self, reason: str, kind: str = "CLOSE", cause: str = CloseCause.MANUAL.value) -> None:
        was_on = [ch for ch in self._channels if ch.state in ON_STATES]
        self._cancel_all_timers()
        err = self._all_off_locked()
        if err is not None:
            self._fault_locked(f"output OFF failed during close: {err!r}", report_off=self._per_locker)
            return
        if self._fault:
            return
        for ch in was_on:
            self._after_off_locked(ch, reason, kind, cause)

    def _refresh_locked(self) -> None:
        """Lazy, clock-driven transitions per channel (deterministic with injected clocks): deadline
        watchdogs and cooldown expiry. Door-dependent transitions need a sensor sample -> poll_once()."""
        for ch in self._channels:
            now = self._clock()
            due = self._due_locked(ch, now, from_timer=False)
            if due is not None:
                kind, reason, cause = due
                self._relock_channel_locked(ch, reason, kind=kind, cause=cause)
                now = self._clock()
            if ch.state is LockerState.COOLDOWN and ch.cooldown_until is not None and now >= ch.cooldown_until - EPS:
                self._settle_locked(ch)

    def _stray_on_locked(self) -> list[_Channel]:
        """Channels whose output reads ON although they are not in an ON state (read errors count as ON)."""
        return [c for c in self._channels if c.state not in ON_STATES and self._read_or(c.idx, True)]

    # -- limit state machine ---------------------------------------------------------------
    def poll_once(self) -> None:
        """One limit sampling + one state-machine tick for every channel (INTERFACES 15.3).

        The polling thread calls this every ``limit_poll_sec``; tests call it with an injected clock.
        timer mode: only the lazy deadline checks run. Any internal error -> every channel OFF + FAULT (R8)."""
        with self._lock:
            if self._shutdown:
                return
            try:
                if not self._limit_mode:
                    self._refresh_locked()
                    return
                self._poll_locked()
            except Exception as exc:  # noqa: BLE001 - R8
                logger.exception("poll_once failed")
                if not self._fault:
                    self._fault_locked(f"poll_once failed: {exc!r}")

    def _poll_locked(self) -> None:
        if self._fault:
            return
        stray = self._stray_on_locked()
        if stray:
            ids = ",".join(_token(c.locker_id) for c in stray)
            self._fault_locked(f"invariant violated during poll: output ON while not unlocked ({ids})")
            return
        for ch in self._channels:
            if self._fault:
                return
            sample = self._read_sample(ch)
            self._tick_locked(ch, sample, self._clock())

    def _tick_locked(self, ch: _Channel, s: bool | None, now: float) -> None:
        cfg = self.cfg
        ch.last_sample = s
        # R5: the absolute watchdog is checked first, with whatever the sensor says
        due = self._due_locked(ch, now, from_timer=False)
        if due is not None:
            kind, reason, cause = due
            self._relock_channel_locked(ch, reason, kind=kind, cause=cause)
            if self._fault:
                return
        # R6: unreadable sensor
        if s is None:
            ch.rel_since = ch.prs_since = ch.good_since = None
            if ch.none_since is None:
                ch.none_since = now
            if ch.state not in (LockerState.SENSOR_FAULT, LockerState.FAULT) \
                    and now - ch.none_since >= cfg.sensor_fault_sec - EPS:
                self._sensor_fault_locked(ch, now)
            return
        ch.none_since = None
        if ch.good_since is None:
            ch.good_since = now
        if s:
            ch.rel_since = None
            if ch.prs_since is None:
                ch.prs_since = now
        else:
            ch.prs_since = None
            if ch.rel_since is None:
                ch.rel_since = now
        st = ch.state
        if st is LockerState.UNLOCKED_WAITING:
            if ch.rel_since is not None and now - ch.rel_since >= cfg.open_confirm_sec - EPS:
                ch.door_open_at = now
                self._set_state(ch, LockerState.OPEN)
                self._emit(ch, "door_opened", "", f"unlocked_for={self._times_for(ch, now)[0]:.3f}s")
            elif ch.unlock_deadline is not None and now >= ch.unlock_deadline - EPS:
                self._relock_channel_locked(ch, "unlock_timeout", kind="CLOSE", cause=CloseCause.UNLOCK_TIMEOUT.value)
        elif st is LockerState.OPEN:
            if s:
                self._set_state(ch, LockerState.CLOSING_CONFIRM)
                self._emit(ch, "door_closing", "", "")
        elif st is LockerState.CLOSING_CONFIRM:
            if not s:
                self._set_state(ch, LockerState.OPEN)  # R4: chatter -> back to OPEN, counter restarts, output untouched
            elif ch.prs_since is not None and now - ch.prs_since >= cfg.close_confirm_sec - EPS:
                self._relock_channel_locked(ch, "close_confirmed", kind="CLOSE",
                                            cause=CloseCause.CLOSE_CONFIRMED.value)
        elif st is LockerState.LOCKED_CLOSED:
            if ch.rel_since is not None and now - ch.rel_since >= cfg.open_confirm_sec - EPS:
                self._set_state(ch, LockerState.LOCKED_OPEN)
                self._emit(ch, "forced_open", "", "")
                logger.warning("locker %s: door opened while locked (limit released)", _token(ch.locker_id))
        elif st is LockerState.LOCKED_OPEN:
            if ch.prs_since is not None and now - ch.prs_since >= cfg.close_confirm_sec - EPS:
                self._set_state(ch, LockerState.LOCKED_CLOSED)
        elif st is LockerState.SENSOR_FAULT:
            if ch.good_since is not None and now - ch.good_since >= cfg.close_confirm_sec - EPS:
                self._emit(ch, "sensor_ok", "", "")
                self._log_channel("SENSOR", ch, f"sensor_ok limit_pin=BCM{_token(ch.limit_pin)} "
                                  f"valid_for={now - ch.good_since:.3f}s")
                if ch.cooldown_until is not None and now < ch.cooldown_until - EPS:
                    self._set_state(ch, LockerState.COOLDOWN)
                else:
                    ch.cooldown_until = None
                    self._set_state(ch, LockerState.LOCKED_CLOSED if s else LockerState.LOCKED_OPEN)
        elif st is LockerState.COOLDOWN:
            if ch.cooldown_until is None or now >= ch.cooldown_until - EPS:
                ch.cooldown_until = None
                self._set_state(ch, self._locked_target(ch, s, now))

    def _sensor_fault_locked(self, ch: _Channel, now: float) -> None:
        none_for = now - ch.none_since if ch.none_since is not None else 0.0
        if ch.state in ON_STATES:
            self._relock_channel_locked(ch, "sensor_fault", kind="CLOSE", cause=CloseCause.SENSOR_FAULT.value,
                                        next_state=LockerState.SENSOR_FAULT)
            if self._fault:
                return
        else:
            self._set_state(ch, LockerState.SENSOR_FAULT)
        self._log_channel("SENSOR", ch, f"sensor_fault limit_pin=BCM{_token(ch.limit_pin)} none_for={none_for:.3f}s",
                          logging.WARNING)
        self._emit(ch, "sensor_fault", "", f"none_for={none_for:.3f}s")

    # -- public API (DoorController) ----------------------------------------------------
    @property
    def last_reject(self) -> str:
        """Code of the last refused open() (INTERFACES 13.4), "" after a successful open."""
        return self._last_reject

    def locker_ids(self) -> tuple[str, ...]:
        """Configured locker ids in declaration order; () in single mode."""
        return tuple(ch.locker_id for ch in self._channels if ch.locker_id is not None)

    def drain_events(self) -> list[DoorEvent]:
        """Transitions made since the last call (oldest first); empties the queue. Thread safe."""
        with self._lock:
            out, self._events = self._events, []
        return out

    def open(self, reason: str, duration: float | None = None, *, locker_id: str | None = None) -> bool:
        """Unlock one channel. limit mode: -> UNLOCKED_WAITING (``duration`` ignored); timer mode: ON for
        min(duration or open_sec, max_open_sec). Refusal codes: INTERFACES 13.4 / 15.3 (``last_reject``)."""
        with self._lock:
            if self._shutdown:
                return self._reject("shutdown", reason, logging.WARNING, "(controller cleaned up)", lid=locker_id)
            self._refresh_locked()
            if self._per_locker:
                if self._fault:
                    return self._reject("fault", reason, logging.WARNING, "", lid=locker_id,
                                        ch=self._by_id.get(locker_id) if isinstance(locker_id, str) else None)
                if locker_id is None:
                    return self._reject("no_locker_id", reason, logging.WARNING, "", lid=None)
                ch = self._by_id.get(locker_id) if isinstance(locker_id, str) else None
                if ch is None:
                    return self._reject("unknown_locker", reason, logging.WARNING, "", lid=locker_id)
            else:
                if locker_id is not None:
                    logger.debug("single mode: locker_id=%r is not used for output routing", locker_id)
                ch = self._channels[0]
                if self._fault:
                    return self._reject("fault", reason, logging.INFO, "(state=FAULT)", ch=ch)
            if ch.state in ON_STATES:
                return self._reject("busy", reason, logging.INFO, f"(state={ch.state.value})", ch=ch)
            if self._limit_mode:
                if ch.state is LockerState.SENSOR_FAULT:
                    return self._reject("sensor_fault", reason, logging.WARNING, "(limit unreadable)", ch=ch)
                s = self._read_sample(ch)  # R: synchronous re-read at request time
                if s is None:
                    return self._reject("sensor_fault", reason, logging.WARNING, "(limit read failed)", ch=ch)
                if ch.state is LockerState.LOCKED_OPEN or s is False:
                    return self._reject("door_open", reason, logging.WARNING, "(door already open)", ch=ch)
            if ch.state is LockerState.COOLDOWN:
                return self._reject("cooldown", reason, logging.INFO, "(state=COOLDOWN)", ch=ch)
            if self._per_locker:
                n_on = sum(1 for c in self._channels if c.state in ON_STATES)
                if n_on >= self.cfg.max_concurrent_open:
                    return self._reject("concurrency_limit", reason, logging.INFO, "", ch=ch)
            d = 0.0
            if self._limit_mode:
                if duration is not None:
                    logger.debug("close_mode=limit: open(duration=%r) ignored (unlock_timeout_sec/max_unlock_sec "
                                 "apply)", duration)
            else:
                dd: Any = self.cfg.open_sec if duration is None else duration
                try:
                    d = float(dd)
                except (TypeError, ValueError):
                    d = float("nan")
                if not math.isfinite(d) or d <= 0:
                    return self._reject("invalid_duration", reason, logging.WARNING,
                                        f"(invalid duration {duration!r})", ch=ch)
                cap = min(self.cfg.max_open_sec, ABSOLUTE_MAX_OPEN_SEC)
                if d > cap:
                    logger.warning("open duration %.3fs clamped to max_open_sec %.3fs", d, cap)
                    d = cap

            check_inv = self._per_locker or self._limit_mode
            if check_inv:
                stray = self._stray_on_locked()
                if stray:
                    ids = ",".join(_token(c.locker_id) for c in stray)
                    self._fault_locked(f"invariant violated before open: output ON while not unlocked ({ids})", ch=ch)
                    return self._reject("invariant", reason, logging.ERROR, "", ch=ch)

            try:
                self._drive_channel(ch.idx, True)
                self._drive_green(True)
            except Exception as exc:
                self._last_reject = "fault"
                self._fault_locked(f"output ON failed: {exc!r}", ch=ch)
                raise HardwareError(f"failed to drive door output: {exc}") from exc

            if check_inv:
                target_on = self._read_or(ch.idx, False)
                stray = [c for c in self._stray_on_locked() if c is not ch]
                if not target_on or stray:
                    what = "target output did not read ON" if not target_on else \
                        "other output ON while not unlocked (" + ",".join(_token(c.locker_id) for c in stray) + ")"
                    self._fault_locked(f"invariant violated after open: {what}", ch=ch)
                    return self._reject("invariant", reason, logging.ERROR, "", ch=ch)

            now = self._clock()
            ch.opened_at = now
            ch.unlocked_at = now
            ch.cooldown_until = None
            ch.door_open_at = None
            if self._limit_mode:
                ch.unlock_deadline = now + self.cfg.unlock_timeout_sec
                ch.hard_deadline = now + min(self.cfg.max_unlock_sec, ABSOLUTE_MAX_UNLOCK_SEC)
                ch.close_deadline = None
                ch.rel_since = None
                self._set_state(ch, LockerState.UNLOCKED_WAITING)
                extra = (f"signal=ON unlock_timeout={self.cfg.unlock_timeout_sec:.3f}s "
                         f"max_unlock={self.cfg.max_unlock_sec:.3f}s")
            else:
                ch.hard_deadline = now + min(self.cfg.max_open_sec, ABSOLUTE_MAX_OPEN_SEC)
                ch.close_deadline = now + d
                ch.state = LockerState.OPEN
                extra = f"signal=ON duration={d:.3f}s"
            ch.last_reason = reason
            ch.last_reject = ""
            ch.open_count += 1
            self._last_reason = reason
            self._last_locker = ch.locker_id
            self._open_count += 1
            self._last_reject = ""
            self._record("OPEN", reason, logging.INFO, extra, ch=ch)
            self._emit(ch, "unlocked", "", extra.replace("signal=ON ", ""))
            self._arm_timer(ch)
            return True

    def close(self, reason: str = "", *, locker_id: str | None = None, cause: str = "manual") -> None:
        """locker_id None -> every channel OFF (idempotent). Known id -> that channel only.
        Unknown id -> every channel OFF + WARNING (fail-closed). Immediate. No-op after cleanup()."""
        cause = _normalize_cause(cause)
        with self._lock:
            if self._shutdown:
                return
            self._refresh_locked()
            if locker_id is None or not self._per_locker:
                self._close_all_locked(reason or "close", cause=cause)
                return
            ch = self._by_id.get(locker_id) if isinstance(locker_id, str) else None
            if ch is None:
                logger.warning("close(locker_id=%r): not a configured locker -> closing ALL channels", locker_id)
                self._close_all_locked(reason or "close", cause=cause)
                return
            self._relock_channel_locked(ch, reason or "close", cause=cause)

    # -- status ---------------------------------------------------------------------------
    def _lookup(self, locker_id: str | None) -> _Channel:
        if self._per_locker:
            ch = self._by_id.get(locker_id) if isinstance(locker_id, str) else None
            if ch is None:
                raise KeyError(locker_id)
            return ch
        if locker_id is not None:
            raise KeyError(locker_id)
        return self._channels[0]

    def _locker_status_locked(self, ch: _Channel, now: float) -> LockerStatus:
        state = LockerState.FAULT if self._fault else ch.state
        on = state in ON_STATES
        if self._limit_mode:
            s = ch.last_sample
            sensor_ok = state is not LockerState.SENSOR_FAULT and s is not None and s is not _UNSET
            if self._shutdown or not sensor_ok or state is LockerState.FAULT:
                door_closed: bool | None = None
            elif state in (LockerState.OPEN, LockerState.LOCKED_OPEN):
                door_closed = False
            elif state is LockerState.COOLDOWN:
                door_closed = bool(s)
            else:
                door_closed = True
        else:
            sensor_ok, door_closed = True, None
        unlock_wait = (max(0.0, ch.unlock_deadline - now)
                       if (state is LockerState.UNLOCKED_WAITING and ch.unlock_deadline is not None) else 0.0)
        close_rem = 0.0
        if state is LockerState.CLOSING_CONFIRM:
            close_rem = (max(0.0, ch.prs_since + self.cfg.close_confirm_sec - now) if ch.prs_since is not None
                         else float(self.cfg.close_confirm_sec))
        dl = self._timer_deadline(ch)
        hard = max(0.0, dl - now) if (on and dl is not None) else 0.0
        cd = (max(0.0, ch.cooldown_until - now)
              if (state is LockerState.COOLDOWN and ch.cooldown_until is not None) else 0.0)
        return LockerStatus(
            locker_id=ch.locker_id, state=state, solenoid_on=on, door_closed=door_closed, sensor_ok=bool(sensor_ok),
            unlock_wait_remaining_sec=unlock_wait, close_confirm_remaining_sec=close_rem, hard_remaining_sec=hard,
            cooldown_remaining_sec=cd, unlocked_at=ch.unlocked_at, last_close_cause=ch.close_cause,
            last_reject=ch.last_reject,
        )

    def locker_state(self, locker_id: str | None = None) -> LockerStatus:
        """Snapshot of one channel (single mode: ``locker_id=None``). KeyError for an unknown id."""
        with self._lock:
            ch = self._lookup(locker_id)
            self._refresh_locked()
            return self._locker_status_locked(ch, self._clock())

    def locker_states(self) -> tuple[LockerStatus, ...]:
        """Snapshots of every channel in declaration order (single mode: exactly one). The only source for
        the locker panel (INTERFACES 16)."""
        with self._lock:
            self._refresh_locked()
            now = self._clock()
            return tuple(self._locker_status_locked(ch, now) for ch in self._channels)

    def poll(self) -> DoorStatus:
        """Alias of status(); convenient name for loops that only want lazy transitions applied."""
        return self.status()

    def _open_lockers_locked(self) -> tuple[str, ...]:
        if not self._per_locker:
            return ()
        return tuple(sorted(ch.locker_id for ch in self._channels
                            if ch.locker_id is not None and self._read_or(ch.idx, False)))

    def _remaining_locked(self, ch: _Channel, now: float) -> tuple[float, float]:
        """(remaining_sec, hard_remaining_sec) for an ON channel."""
        dl = self._timer_deadline(ch)
        hard = max(0.0, dl - now) if dl is not None else 0.0
        if self._limit_mode:
            if ch.state is LockerState.UNLOCKED_WAITING and ch.unlock_deadline is not None:
                return max(0.0, ch.unlock_deadline - now), hard
            return hard, hard
        rem = max(0.0, ch.close_deadline - now) if ch.close_deadline is not None else 0.0
        hard_t = max(0.0, ch.hard_deadline - now) if ch.hard_deadline is not None else 0.0
        return rem, hard_t

    def status(self) -> DoorStatus:
        """Aggregate status. single + timer mode: identical to v1.2 for the first 7 fields."""
        with self._lock:
            self._refresh_locked()
            now = self._clock()
            states = [_door_state_of(ch.state) for ch in self._channels]
            if self._fault or DoorState.FAULT in states:
                agg = DoorState.FAULT
            elif DoorState.OPEN in states:
                agg = DoorState.OPEN
            elif DoorState.COOLDOWN in states:
                agg = DoorState.COOLDOWN
            else:
                agg = DoorState.IDLE
            open_lockers = self._open_lockers_locked()
            on = [ch for ch in self._channels if ch.state in ON_STATES and self._read_or(ch.idx, False)]
            is_open = bool(open_lockers) if self._per_locker else bool(on)
            remaining = hard_rem = 0.0
            opened_at: float | None = None
            if on:
                lead = max(on, key=lambda c: self._timer_deadline(c) or 0.0)
                opened_at = lead.opened_at
                remaining, hard_rem = self._remaining_locked(lead, now)
            elif not self._per_locker:
                opened_at = self._channels[0].opened_at
            cd = 0.0
            if agg is DoorState.COOLDOWN:
                cd = max((max(0.0, ch.cooldown_until - now) for ch in self._channels
                          if ch.state is LockerState.COOLDOWN and ch.cooldown_until is not None), default=0.0)
            return DoorStatus(
                state=agg,
                is_open=is_open,
                opened_at=opened_at,
                remaining_sec=remaining,
                cooldown_remaining_sec=cd,
                last_reason=self._last_reason,
                open_count=self._open_count,
                locker_id=self._last_locker if self._per_locker else None,
                open_lockers=open_lockers,
                hard_remaining_sec=hard_rem,
                close_cause=self._last_cause,
            )

    def locker_status(self, locker_id: str) -> DoorStatus:
        """DoorStatus of one locker channel. KeyError for an unknown id or in single mode."""
        with self._lock:
            if not self._per_locker:
                raise KeyError(f"single mode has no locker channels (got {locker_id!r})")
            ch = self._by_id.get(locker_id) if isinstance(locker_id, str) else None
            if ch is None:
                raise KeyError(locker_id)
            self._refresh_locked()
            now = self._clock()
            state = DoorState.FAULT if self._fault else _door_state_of(ch.state)
            is_open = self._read_or(ch.idx, False)
            remaining, hard_rem = self._remaining_locked(ch, now) if (is_open and ch.state in ON_STATES) else (0.0, 0.0)
            cd = (max(0.0, ch.cooldown_until - now)
                  if (ch.state is LockerState.COOLDOWN and ch.cooldown_until is not None) else 0.0)
            return DoorStatus(
                state=state,
                is_open=is_open,
                opened_at=ch.opened_at,
                remaining_sec=remaining,
                cooldown_remaining_sec=cd,
                last_reason=ch.last_reason,
                open_count=ch.open_count,
                locker_id=ch.locker_id,
                open_lockers=self._open_lockers_locked(),
                hard_remaining_sec=hard_rem,
                close_cause=ch.close_cause,
            )

    def green_on(self) -> bool:
        """Actual green LED output (used by StatusLeds.snapshot)."""
        try:
            return bool(self._read_green())
        except Exception:  # noqa: BLE001
            return False

    def signal_on(self) -> bool:
        """True if any door output channel is actually ON."""
        return any(self._read_or(ch.idx, False) for ch in self._channels)

    def _read_signal(self) -> bool:  # v1.2 hook name, kept for callers/tests
        return self.signal_on()

    @property
    def _timer(self) -> threading.Timer | None:
        """First pending watchdog timer (v1.2 attribute name, single mode = the only one)."""
        for ch in self._channels:
            if ch.timer is not None:
                return ch.timer
        return None

    def _stop_monitor(self) -> None:
        self._monitor_stop.set()
        t = self._monitor
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)

    def cleanup(self) -> None:
        """Every output OFF immediately (cause shutdown, no waiting), cancel timers, stop the polling thread,
        release pins. Idempotent; swallows (and logs) errors."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            was_on = [ch for ch in self._channels if ch.state in ON_STATES]
            self._cancel_all_timers()
            err = self._all_off_locked()
            if err is not None:
                logger.error("cleanup: output OFF failed: %r", err)
                self._fault = True
            for ch in was_on:
                cause = CloseCause.FAULT.value if err is not None else CloseCause.SHUTDOWN.value
                extra = self._close_extra(ch, cause)
                ev_detail = self._relock_detail(ch)
                ch.close_cause = cause
                self._last_cause = cause
                if err is None:
                    self._record("CLOSE", "shutdown", logging.INFO, extra, ch=ch)
                self._emit(ch, "relock", cause, ev_detail)
            for ch in self._channels:
                ch.state = LockerState.FAULT if self._fault else LockerState.LOCKED_CLOSED
                ch.clear_open()
                ch.cooldown_until = None
            ok_text = "outputs OFF (all channels)" if self._per_locker else "outputs OFF"
            self._record("CLEANUP", ok_text if err is None else f"OFF failed {err!r}")
            try:
                self._release()
            except Exception:  # noqa: BLE001
                logger.exception("cleanup: releasing pins failed")
        self._stop_monitor()
        lim = self.limits
        if lim is not None:
            try:
                lim.close()
            except Exception:  # noqa: BLE001
                logger.exception("cleanup: closing limit inputs failed")

    def _abort_init(self) -> None:
        """Constructor failed after outputs were claimed: outputs OFF, stop thread, release everything."""
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001
            logger.exception("abort-init cleanup failed")


class BaseStatusLeds(ABC):
    """Red (deny blink, non-blocking) and amber (hold). Green is read from the door controller."""

    def __init__(self, cfg: HardwareConfig, door: BaseDoorController, source: str = "leds") -> None:
        self.cfg = cfg
        self.door = door
        self._lock = threading.RLock()
        self._blink_stop = threading.Event()
        self._blink_thread: threading.Thread | None = None
        self._hold = False
        self._shutdown = False
        self.log = HardwareEventLog(cfg.hardware_log, source=source)

    @abstractmethod
    def _set_red(self, on: bool) -> None: ...

    @abstractmethod
    def _set_amber(self, on: bool) -> None: ...

    @abstractmethod
    def _read_red(self) -> bool: ...

    @abstractmethod
    def _read_amber(self) -> bool: ...

    def _release(self) -> None:  # pragma: no cover - default no-op
        return None

    def _stop_blink(self) -> None:
        t = self._blink_thread
        self._blink_stop.set()
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._blink_thread = None

    def _blink_worker(self, stop: threading.Event, count: int, period: float) -> None:
        try:
            for _ in range(count):
                if stop.is_set():
                    break
                self._set_red(True)
                if stop.wait(period):
                    break
                self._set_red(False)
                if stop.wait(period):
                    break
        except Exception:  # noqa: BLE001
            logger.exception("red LED blink failed")
        finally:
            try:
                self._set_red(False)
            except Exception:  # noqa: BLE001
                logger.exception("red LED off failed")

    def show_denied(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._stop_blink()
            self._blink_stop = threading.Event()
            self.log.write("DENY_BLINK", f"count={self.cfg.deny_blink_count} period={self.cfg.deny_blink_sec}s")
            if self.cfg.deny_blink_count <= 0:
                return
            t = threading.Thread(
                target=self._blink_worker,
                args=(self._blink_stop, self.cfg.deny_blink_count, self.cfg.deny_blink_sec),
                name="facedoor-red-blink",
                daemon=True,
            )
            self._blink_thread = t
            t.start()

    def is_blinking(self) -> bool:
        t = self._blink_thread
        return bool(t is not None and t.is_alive())

    def wait_blink(self, timeout: float | None = None) -> bool:
        """Test/selftest helper: wait for the current deny blink to finish. True if finished."""
        t = self._blink_thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    def set_red(self, on: bool) -> None:
        """Direct red LED control (self-test/manual use; normal runtime uses show_denied)."""
        with self._lock:
            if self._shutdown:
                return
            self._stop_blink()
            self._set_red(bool(on))

    def set_hold(self, on: bool) -> None:
        with self._lock:
            if self._shutdown:
                return
            on = bool(on)
            if on != self._hold:
                self._hold = on
                self.log.write("HOLD_LED", "ON" if on else "OFF", logging.DEBUG)
            self._set_amber(on)

    def snapshot(self) -> LedSnapshot:
        return LedSnapshot(green=self.door.green_on(), red=bool(self._safe(self._read_red)),
                           amber=bool(self._safe(self._read_amber)))

    @staticmethod
    def _safe(fn: Callable[[], bool]) -> bool:
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return False

    def off(self) -> None:
        """Red and amber OFF (green belongs to the door controller)."""
        with self._lock:
            self._stop_blink()
            self._hold = False
            for fn in (self._set_red, self._set_amber):
                try:
                    fn(False)
                except Exception:  # noqa: BLE001
                    logger.exception("LED off failed")

    def cleanup(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self.off()
            self._shutdown = True
            try:
                self._release()
            except Exception:  # noqa: BLE001
                logger.exception("LED release failed")
