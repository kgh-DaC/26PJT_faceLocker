"""Hardware abstraction (owner: hw-engineer). The only place allowed to touch GPIO. See docs/INTERFACES.md
sections 4, 13 (per-locker outputs) and 15 (limit-sensor lock control).

Do not import gpiozero/lgpio at module level anywhere in this package.
"""

from __future__ import annotations

from facedoor.hardware.base import (
    ABSOLUTE_MAX_CONCURRENT_OPEN,
    ABSOLUTE_MAX_LOCKERS,
    ABSOLUTE_MAX_OPEN_SEC,
    ABSOLUTE_MAX_UNLOCK_SEC,
    RESERVED_OUTPUT_PINS,
    CloseCause,
    DoorController,
    DoorEvent,
    DoorState,
    DoorStatus,
    HardwareBundle,
    HardwareConfig,
    LedSnapshot,
    LimitInputs,
    LockerOutput,
    LockerState,
    LockerStatus,
    StatusLeds,
    format_status_line,
)
from facedoor.hardware.factory import create_hardware, is_raspberry_pi

__all__ = [
    "ABSOLUTE_MAX_CONCURRENT_OPEN",
    "ABSOLUTE_MAX_LOCKERS",
    "ABSOLUTE_MAX_OPEN_SEC",
    "ABSOLUTE_MAX_UNLOCK_SEC",
    "RESERVED_OUTPUT_PINS",
    "CloseCause",
    "DoorController",
    "DoorEvent",
    "DoorState",
    "DoorStatus",
    "HardwareBundle",
    "HardwareConfig",
    "LedSnapshot",
    "LimitInputs",
    "LockerOutput",
    "LockerState",
    "LockerStatus",
    "StatusLeds",
    "format_status_line",
    "create_hardware",
    "is_raspberry_pi",
]
