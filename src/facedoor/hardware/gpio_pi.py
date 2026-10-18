"""Raspberry Pi 5 GPIO via gpiozero + lgpio (owner: hw-engineer).

gpiozero / lgpio are imported lazily inside functions so this module imports cleanly on Windows.
On a PC the same code path can be exercised by injecting ``gpiozero.pins.mock.MockFactory()``
(``hardware.mock_backend: gpiozero``), which is what tests/test_hardware.py does.

UNVERIFIED ON REAL HARDWARE (needs a Pi 5): LGPIOFactory chip selection, ``force_outputs_off``
via raw lgpio, and the electrical behaviour of released lines.
"""

from __future__ import annotations

import glob
import logging
import time
from typing import Any, Callable

from facedoor.errors import HardwareError, HardwareUnavailableError
from facedoor.errors import ConfigError
from facedoor.hardware.base import (
    BaseDoorController,
    BaseStatusLeds,
    HardwareConfig,
    force_off_targets,
    output_channels,
)

__all__ = [
    "PiDoorController",
    "PiStatusLeds",
    "make_pin_factory",
    "detect_rp1_chip",
    "create_exit_button",
    "force_outputs_off",
]

logger = logging.getLogger("facedoor.hardware")

RP1_LABEL = "pinctrl-rp1"


def _import_gpiozero() -> Any:
    try:
        import gpiozero  # noqa: PLC0415 - deliberate lazy import
    except Exception as exc:  # noqa: BLE001
        raise HardwareUnavailableError(
            "gpiozero is not installed/importable (pip install 'gpiozero>=2.0.1'): " f"{exc}"
        ) from exc
    return gpiozero


def detect_rp1_chip() -> int | None:
    """Return the gpiochip number whose label is 'pinctrl-rp1' (Pi 5 header GPIO), or None.

    Kernel-dependent: older Pi 5 kernels expose RP1 as gpiochip4, newer ones as gpiochip0.
    UNVERIFIED on Ubuntu 24.04 Pi 5 kernel.
    """
    try:
        import lgpio  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    for dev in sorted(glob.glob("/dev/gpiochip*")):
        try:
            num = int(dev.removeprefix("/dev/gpiochip"))
        except ValueError:
            continue
        h = None
        try:
            h = lgpio.gpiochip_open(num)
            info = lgpio.gpio_get_chip_info(h)  # (lines, name, label)
            if RP1_LABEL in str(info[2]):
                return num
        except Exception:  # noqa: BLE001
            continue
        finally:
            if h is not None:
                try:
                    lgpio.gpiochip_close(h)
                except Exception:  # noqa: BLE001
                    pass
    return None


def _resolve_chip(cfg: HardwareConfig) -> int | None:
    if isinstance(cfg.gpio_chip, int):
        return cfg.gpio_chip
    return detect_rp1_chip()  # None -> let gpiozero apply its own heuristic


def make_pin_factory(cfg: HardwareConfig) -> Any:
    """Create the gpiozero pin factory named by ``cfg.pin_factory`` ('lgpio' | 'mock')."""
    _import_gpiozero()
    if cfg.pin_factory == "mock":
        from gpiozero.pins.mock import MockFactory  # noqa: PLC0415

        return MockFactory()
    try:
        import lgpio  # noqa: F401, PLC0415
        from gpiozero.pins.lgpio import LGPIOFactory  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise HardwareUnavailableError(
            "lgpio is not available. On Ubuntu 24.04: sudo apt install python3-lgpio and create the venv "
            f"with --system-site-packages (deploy/install_rpi.sh): {exc}"
        ) from exc
    chip = _resolve_chip(cfg)
    try:
        factory = LGPIOFactory(chip=chip)
    except Exception as exc:  # noqa: BLE001
        raise HardwareUnavailableError(
            f"cannot open gpiochip {chip if chip is not None else '(auto)'} via lgpio: {exc}. "
            "Check /dev/gpiochip* permissions (user in 'gpio' group?) and hardware.gpio_chip."
        ) from exc
    logger.info("lgpio pin factory on gpiochip%s", getattr(factory, "chip", chip))
    return factory


def _close_quietly(dev: Any) -> None:
    if dev is None:
        return
    try:
        dev.close()
    except Exception:  # noqa: BLE001
        logger.exception("closing gpiozero device failed")


def _off_and_close_quietly(dev: Any) -> None:
    if dev is None:
        return
    try:
        dev.off()
    except Exception:  # noqa: BLE001
        logger.exception("turning gpiozero device OFF failed")
    _close_quietly(dev)


class PiDoorController(BaseDoorController):
    """Door output channels (DigitalOutputDevice each) + green LED (LED), all ``initial_value=False``.

    single mode: one channel on ``solenoid_pin``. per_locker: one channel per ``locker_outputs`` entry.
    If ANY claim fails, every already-claimed output is turned OFF and released and HardwareError is
    raised (never run with a subset of channels).
    """

    def __init__(self, cfg: HardwareConfig, pin_factory: Any | None = None,
                 clock: Callable[[], float] = time.monotonic, limits: Any | None = None, *,
                 start_thread: bool = True) -> None:
        cfg.validate()
        if cfg.limit_mode and limits is None:
            missing = cfg.missing_limit_pins()
            if missing:
                raise ConfigError(f"hardware: close_mode=limit on the Pi path requires a limit pin for every "
                                  f"door; missing {missing} (deploy/WIRING.md)")
        gz = _import_gpiozero()
        self.pin_factory = pin_factory if pin_factory is not None else make_pin_factory(cfg)
        specs = output_channels(cfg)
        self._devs: list[Any] = []
        self._ids: list[str | None] = []
        self._green = None
        what = ""
        try:
            for lid, pin, ah in specs:
                what = f"{'solenoid' if lid is None else 'locker ' + lid} BCM{pin}"
                dev = gz.DigitalOutputDevice(pin, active_high=ah, initial_value=False, pin_factory=self.pin_factory)
                self._devs.append(dev)
                self._ids.append(lid)
            what = f"green LED BCM{cfg.led_green_pin}"
            self._green = gz.LED(
                cfg.led_green_pin, active_high=cfg.led_active_high, initial_value=False,
                pin_factory=self.pin_factory,
            )
        except Exception as exc:
            claimed = len(self._devs)
            self._release_devices()
            raise HardwareError(
                f"cannot claim door output GPIO ({what}): {exc}; {claimed} already-claimed output(s) "
                "turned OFF and released"
            ) from exc
        self.limits = None
        if cfg.limit_mode:
            if limits is None:
                from facedoor.hardware.limits import PiLimitInputs  # noqa: PLC0415

                try:
                    limits = PiLimitInputs(cfg, self.pin_factory)
                except Exception:
                    self._release_devices()
                    raise
        try:
            super().__init__(cfg, clock=clock, source="pi-door", limits=limits)
        except Exception:
            self._release_devices()
            if limits is not None:
                limits.close()
            raise
        single = (f"pi door ready: solenoid BCM{cfg.solenoid_pin} (active_high={cfg.solenoid_active_high}), "
                  f"green BCM{cfg.led_green_pin}, factory={type(self.pin_factory).__name__}; outputs OFF")
        self.log.write("INIT", self._init_detail(single, f"factory={type(self.pin_factory).__name__}"))
        try:
            self._finish_init(start_thread=start_thread, require_any_sensor=True)
        except Exception:
            self._abort_init()
            raise

    @property
    def solenoid_device(self) -> Any:
        """single mode: the solenoid DigitalOutputDevice; per_locker: None."""
        if self._devs and self._ids and self._ids[0] is None:
            return self._devs[0]
        return None

    @property
    def channel_devices(self) -> dict[str, Any]:
        """per_locker: locker_id -> DigitalOutputDevice ({} in single mode or after release)."""
        return {lid: dev for lid, dev in zip(self._ids, self._devs) if lid is not None and dev is not None}

    @property
    def green_device(self) -> Any:
        return self._green

    def _dev(self, idx: int) -> Any:
        if idx >= len(self._devs) or self._devs[idx] is None:
            raise HardwareError("GPIO devices already released")
        return self._devs[idx]

    def _drive_channel(self, idx: int, on: bool) -> None:
        dev = self._dev(idx)
        if on:
            dev.on()
        else:
            dev.off()

    def _read_channel(self, idx: int) -> bool:
        if idx >= len(self._devs) or self._devs[idx] is None:
            return False
        return bool(self._devs[idx].value)

    def _drive_green(self, on: bool) -> None:
        if self._green is None:
            raise HardwareError("GPIO devices already released")
        if on:
            self._green.on()
        else:
            self._green.off()

    def _read_green(self) -> bool:
        return bool(self._green is not None and self._green.value)

    def _release_devices(self) -> None:
        g, devs = self._green, list(self._devs)
        self._green = None
        self._devs = [None] * len(devs)
        _off_and_close_quietly(g)
        for d in devs:
            _off_and_close_quietly(d)

    def _release(self) -> None:
        self._release_devices()


class PiStatusLeds(BaseStatusLeds):
    def __init__(self, cfg: HardwareConfig, door: PiDoorController, pin_factory: Any | None = None) -> None:
        gz = _import_gpiozero()
        factory = pin_factory if pin_factory is not None else door.pin_factory
        self._red = None
        self._amber = None
        try:
            self._red = gz.LED(cfg.led_red_pin, active_high=cfg.led_active_high, initial_value=False,
                               pin_factory=factory)
            self._amber = gz.LED(cfg.led_amber_pin, active_high=cfg.led_active_high, initial_value=False,
                                 pin_factory=factory)
        except Exception as exc:
            _close_quietly(self._amber)
            _close_quietly(self._red)
            raise HardwareError(f"cannot claim red/amber LED GPIO (BCM {cfg.led_red_pin}/{cfg.led_amber_pin}): {exc}") from exc
        super().__init__(cfg, door, source="pi-leds")

    def _set_red(self, on: bool) -> None:
        if self._red is not None:
            self._red.value = bool(on)

    def _set_amber(self, on: bool) -> None:
        if self._amber is not None:
            self._amber.value = bool(on)

    def _read_red(self) -> bool:
        return bool(self._red is not None and self._red.value)

    def _read_amber(self) -> bool:
        return bool(self._amber is not None and self._amber.value)

    def _release(self) -> None:
        r, a = self._red, self._amber
        self._red = self._amber = None
        _close_quietly(r)
        _close_quietly(a)


def create_exit_button(cfg: HardwareConfig, pin_factory: Any) -> Any | None:
    """gpiozero Button on ``exit_button_pin`` (pull-up, button to GND), or None if unset.
    The app assigns ``button.when_pressed``. UNVERIFIED on real hardware."""
    if cfg.exit_button_pin is None:
        return None
    gz = _import_gpiozero()
    try:
        return gz.Button(cfg.exit_button_pin, pull_up=True, bounce_time=0.05, pin_factory=pin_factory)
    except Exception as exc:
        raise HardwareError(f"cannot claim exit button GPIO BCM{cfg.exit_button_pin}: {exc}") from exc


def force_outputs_off(cfg: HardwareConfig, lgpio_module: Any | None = None) -> list[str]:
    """Drive solenoid (if configured), EVERY locker channel and all LED pins to their OFF level with raw
    lgpio, then free them. One line per output; a failing line contains " FAILED:" and the remaining
    outputs are still processed.

    Used by systemd ``ExecStartPre``/``ExecStopPost`` (scripts/hw_selftest.py --force-off) while the app
    is not running, as an extra line of defence. Raises HardwareUnavailableError when lgpio is missing
    and HardwareError when the gpiochip cannot be opened. ``lgpio_module`` is for tests.
    UNVERIFIED on real hardware: whether a freed line keeps its level depends on the kernel gpio driver;
    we claim -> write OFF -> free (external pull resistors must hold OFF, deploy/WIRING.md).
    """
    if lgpio_module is not None:
        lgpio = lgpio_module
        chip = cfg.gpio_chip if isinstance(cfg.gpio_chip, int) else 0
    else:
        try:
            import lgpio  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise HardwareUnavailableError(f"lgpio not available: {exc}") from exc
        chip = cfg.gpio_chip if isinstance(cfg.gpio_chip, int) else detect_rp1_chip()
        if chip is None:
            chip = 0
    out: list[str] = []
    try:
        h = lgpio.gpiochip_open(chip)
    except Exception as exc:  # noqa: BLE001
        raise HardwareError(f"cannot open gpiochip{chip} for force-off: {exc}") from exc
    try:
        for name, pin, active_high in force_off_targets(cfg):
            off_level = 0 if active_high else 1
            try:
                lgpio.gpio_claim_output(h, pin, off_level)
                lgpio.gpio_write(h, pin, off_level)
                out.append(f"{name} BCM{pin} -> OFF (level {off_level}) on gpiochip{chip}")
            except Exception as exc:  # noqa: BLE001
                out.append(f"{name} BCM{pin} FAILED: {exc}")
            finally:
                try:
                    lgpio.gpio_free(h, pin)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        lgpio.gpiochip_close(h)
    return out
