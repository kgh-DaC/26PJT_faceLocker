"""Shared exceptions and process exit codes (contract, owner: face-architect). See docs/INTERFACES.md section 10."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    CONFIG = 2
    CAMERA = 3
    MODEL = 4
    HARDWARE = 5
    GALLERY = 6
    INTERRUPTED = 130


class FaceDoorError(Exception):
    """Base class. exit_code is used by app.main() after hardware cleanup."""

    exit_code: ExitCode = ExitCode.ERROR


class ConfigError(FaceDoorError):
    exit_code = ExitCode.CONFIG


class CameraError(FaceDoorError):
    exit_code = ExitCode.CAMERA


class ModelError(FaceDoorError):
    exit_code = ExitCode.MODEL


class GalleryError(FaceDoorError):
    exit_code = ExitCode.GALLERY


class CalibrationError(FaceDoorError):
    """Raised only by explicit calibration tooling; runtime falls back to defaults instead."""

    exit_code = ExitCode.CONFIG


class HardwareError(FaceDoorError):
    exit_code = ExitCode.HARDWARE


class HardwareUnavailableError(HardwareError):
    """mode=pi requested but not running on a Raspberry Pi or gpiozero/lgpio unavailable."""
