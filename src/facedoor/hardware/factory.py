"""Hardware selection + process-exit safety hooks (owner: hw-engineer). Contract: docs/INTERFACES.md 4.2.

mode=pi   : not a Raspberry Pi, or gpiozero/lgpio unusable -> HardwareUnavailableError (never falls back).
mode=auto : Raspberry Pi -> pi (errors propagate, no silent mock on a real Pi); otherwise WARNING + mock.
mode=mock : mock_backend=state -> pure-Python mock; mock_backend=gpiozero -> Pi code path on MockFactory.

Every bundle created here is registered for cleanup (outputs OFF) on atexit, SIGINT and SIGTERM.
Previously installed signal handlers are chained (called after our cleanup).
"""

from __future__ import annotations

import atexit
import logging
import signal
import threading
from pathlib import Path
from typing import Any, Mapping

from facedoor.errors import HardwareUnavailableError
from facedoor.hardware.base import HardwareBundle, HardwareConfig

__all__ = [
    "is_raspberry_pi",
    "create_hardware",
    "register_bundle",
    "unregister_bundle",
    "run_registered_cleanups",
    "load_hardware_config",
]

logger = logging.getLogger("facedoor.hardware")

_MODEL_FILES = ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model")


def is_raspberry_pi() -> bool:
    """True if the device-tree model string contains 'Raspberry Pi'."""
    for p in _MODEL_FILES:
        try:
            with open(p, "rb") as fh:
                model = fh.read().decode("utf-8", "replace").replace("\x00", "").strip()
        except OSError:
            continue
        if "Raspberry Pi" in model:
            return True
    return False


# ---------------------------------------------------------------------------
# cleanup registry (atexit + signals)
# ---------------------------------------------------------------------------

_registry_lock = threading.RLock()
_bundles: list[HardwareBundle] = []
_atexit_installed = False
_signals_installed: dict[int, Any] = {}  # signum -> previous handler


def run_registered_cleanups() -> None:
    """Clean up every registered bundle (outputs OFF). Safe to call many times."""
    with _registry_lock:
        bundles = list(_bundles)
    for b in bundles:
        try:
            b.cleanup()
        except Exception:  # noqa: BLE001
            logger.exception("hardware cleanup failed")


def _signal_handler(signum: int, frame: Any) -> None:
    logger.warning("signal %s received: forcing hardware outputs OFF", signum)
    run_registered_cleanups()
    prev = _signals_installed.get(signum)
    if callable(prev) and prev is not _signal_handler:
        prev(signum, frame)
        return
    if prev == signal.SIG_IGN:
        return
    # SIG_DFL / None: emulate default termination but let Python unwind (atexit, finally blocks).
    if signum == getattr(signal, "SIGINT", None):
        raise KeyboardInterrupt
    raise SystemExit(128 + int(signum))


def _install_handlers() -> None:
    global _atexit_installed
    with _registry_lock:
        if not _atexit_installed:
            atexit.register(run_registered_cleanups)
            _atexit_installed = True
        if threading.current_thread() is not threading.main_thread():
            logger.warning("create_hardware called off the main thread: signal handlers not installed "
                           "(atexit cleanup still active)")
            return
        for name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            current = signal.getsignal(signum)
            if current is _signal_handler:
                continue
            try:
                signal.signal(signum, _signal_handler)
            except (ValueError, OSError) as exc:
                logger.warning("cannot install %s handler: %s", name, exc)
                continue
            _signals_installed[signum] = current


def register_bundle(bundle: HardwareBundle, install_handlers: bool = True) -> None:
    with _registry_lock:
        if bundle not in _bundles:
            _bundles.append(bundle)
    if install_handlers:
        _install_handlers()


def unregister_bundle(bundle: HardwareBundle) -> None:
    with _registry_lock:
        for i, b in enumerate(_bundles):
            if b is bundle:
                del _bundles[i]
                break


def registered_bundles() -> list[HardwareBundle]:
    with _registry_lock:
        return list(_bundles)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def _build_mock_state(cfg: HardwareConfig) -> HardwareBundle:
    from facedoor.hardware.gpio_mock import MockDoorController, MockExitButton, MockStatusLeds

    door = MockDoorController(cfg)
    leds = MockStatusLeds(cfg, door)
    btn = MockExitButton(cfg.exit_button_pin) if cfg.exit_button_pin is not None else None
    return HardwareBundle(mode="mock", door=door, leds=leds, exit_button=btn, limits=getattr(door, "limits", None))


def _build_pi(cfg: HardwareConfig, pin_factory: Any | None, mode_label: str) -> HardwareBundle:
    from facedoor.hardware.gpio_pi import PiDoorController, PiStatusLeds, create_exit_button

    door = PiDoorController(cfg, pin_factory=pin_factory)
    try:
        leds = PiStatusLeds(cfg, door)
        btn = create_exit_button(cfg, door.pin_factory)
    except Exception:
        door.cleanup()
        raise
    return HardwareBundle(mode=mode_label, door=door, leds=leds, exit_button=btn, limits=getattr(door, "limits", None))


def _require_pi(cfg: HardwareConfig) -> None:
    if not is_raspberry_pi():
        raise HardwareUnavailableError(
            "hardware.mode=pi but this machine is not a Raspberry Pi (/proc/device-tree/model). "
            "Use hardware.mode=mock on a PC (or auto to fall back with a warning)."
        )


def create_hardware(cfg: HardwareConfig, *, register_handlers: bool = True) -> HardwareBundle:
    """Build the HardwareBundle for ``cfg.mode``. See module docstring for selection rules.

    ``register_handlers=False`` skips atexit/signal registration (tests only).
    """
    cfg.validate()
    mode = cfg.mode
    if mode == "pi":
        _require_pi(cfg)
        bundle = _build_pi(cfg, None, "pi")
    elif mode == "auto":
        if is_raspberry_pi():
            logger.info("hardware.mode=auto: Raspberry Pi detected -> pi")
            bundle = _build_pi(cfg, None, "pi")
        else:
            logger.warning("hardware.mode=auto: not a Raspberry Pi -> MOCK GPIO (no physical signal will be output)")
            bundle = _build_mock(cfg)
    else:
        bundle = _build_mock(cfg)
    if register_handlers:
        register_bundle(bundle)
    logger.info("hardware ready: mode=%s door=%s leds=%s", bundle.mode, type(bundle.door).__name__,
                type(bundle.leds).__name__)
    return bundle


def _build_mock(cfg: HardwareConfig) -> HardwareBundle:
    if cfg.mock_backend == "gpiozero":
        try:
            from gpiozero.pins.mock import MockFactory  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise HardwareUnavailableError(
                f"hardware.mock_backend=gpiozero requires gpiozero (pip install gpiozero): {exc}"
            ) from exc
        return _build_pi(cfg, MockFactory(), "mock")
    return _build_mock_state(cfg)


def load_hardware_config(paths: list[Path] | None = None,
                         overrides: Mapping[str, Any] | None = None) -> HardwareConfig:
    """Stand-alone loader for the ``hardware`` section only (default.yaml < local.yaml < extra paths).

    Used by scripts/hw_selftest.py (incl. systemd ExecStopPost) so that turning outputs OFF never
    depends on the rest of the application config being valid.
    """
    import yaml  # noqa: PLC0415

    from facedoor.hardware.base import resolve_project_path

    root = resolve_project_path(Path("."))
    files = [root / "configs" / "default.yaml", root / "configs" / "local.yaml"] + list(paths or [])
    merged: dict[str, Any] = {}
    for f in files:
        f = Path(f)
        if not f.is_file():
            continue
        with open(f, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        hw = data.get("hardware") if isinstance(data, dict) else None
        if isinstance(hw, dict):
            merged.update(hw)
    if overrides:
        merged.update(overrides)
    cfg = HardwareConfig.from_dict(merged)
    cfg.hardware_log = resolve_project_path(cfg.hardware_log)
    return cfg
