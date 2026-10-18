#!/usr/bin/env python3
"""Hardware self-test (owner: hw-engineer). docs/INTERFACES.md sections 9, 13.9 and 15.

    python scripts/hw_selftest.py [--config FILE] [--set hardware.k=v ...] [--mode mock|pi|auto]
                                  [--allow-solenoid] [--pulse-sec 0.5] [--step-sec 0.5]
                                  [--locker ID ...] [--all-lockers]
                                  [--hardware-log PATH] [--force-off]
                                  [--limits [--limits-sec N]] [--sim-limits]

- Red and amber LEDs blink in sequence, then the red "denied" pattern.
- Door outputs (and the green LED, which is driven together with them by design) are pulsed ONLY
  with --allow-solenoid. single mode: the solenoid output. per_locker mode: --locker ID ... (only
  those) or --all-lockers (declaration order), ONE channel at a time; before each channel every
  other channel is verified OFF. --locker/--all-lockers require --allow-solenoid.
  First tests: wire an LED in place of the relay input and disconnect the solenoid supply
  (deploy/WIRING.md).
- close_mode=limit: the pulse is ended by an explicit close() after --pulse-sec (otherwise the output
  would stay ON until the door opens/unlock_timeout_sec). The door must read "closed" (limit pressed),
  otherwise the unlock is refused (door_open / sensor_fault) and the self-test reports it.
- --limits: print the limit switch readings live (wiring check on the Pi; Ctrl-C or --limits-sec N ends).
  Press/release each switch and confirm that the right locker changes. Needs close_mode=limit.
- --sim-limits (mock only): interactive door simulation. Type 1..4 + Enter = toggle door of locker 1..4
  (open/closed), 5..8 = toggle sensor failure of locker 1..4, q = quit. Same keys as the recognition screen.
- --force-off: drive every configured output (solenoid if set, every locker channel, LEDs) to its
  OFF level and exit (systemd ExecStartPre/ExecStopPost). Reads only the hardware section, so other
  config errors cannot block it. pi mode: raw lgpio; mock mode: prints the targets only.

User-facing messages are Korean; identifiers and log lines stay English.
Exit codes: 0 OK, 2 config/usage error, 5 hardware error/unavailable or self-test failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from facedoor.errors import ConfigError, ExitCode, FaceDoorError, HardwareError  # noqa: E402
from facedoor.hardware.base import (  # noqa: E402
    DoorState,
    HardwareConfig,
    LedSnapshot,
    force_off_targets,
    format_status_line,
)
from facedoor.hardware.factory import create_hardware, is_raspberry_pi, load_hardware_config  # noqa: E402

log = logging.getLogger("hw_selftest")

# 사용자 안내 문구 (공용 facedoor/texts.py 가 생기면 그쪽으로 옮긴다)
T = {
    "desc": "facedoor GPIO 자체 점검 (LED 점멸, 선택적으로 솔밸브/사물함 출력 펄스)",
    "need_allow": "--locker / --all-lockers 는 --allow-solenoid 와 함께 써야 합니다 (실제 문이 열릴 수 있음)",
    "single_no_locker": "single 모드(hardware.locker_outputs 없음)에서는 --locker / --all-lockers 를 쓸 수 없습니다",
    "unknown_locker": "설정에 없는 사물함 ID: {ids} (설정된 사물함: {known})",
    "mock_force_off": "강제 OFF: mock 모드라 실제 GPIO는 구동하지 않습니다. 대상:",
    "force_off_failed": "강제 OFF 실패 항목이 있습니다. 배선/권한(gpio 그룹)/gpiochip 설정을 확인하세요",
    "header": "모드={mode} mock_backend={backend} 라즈베리파이={pi} close_mode={close_mode}",
    "limits_need_limit_mode": "--limits / --sim-limits 는 hardware.close_mode=limit 일 때만 쓸 수 있습니다",
    "sim_need_mock": "--sim-limits 는 시뮬레이션(mock) 모드에서만 쓸 수 있습니다 (실기에서는 스위치를 직접 누르세요)",
    "limits_help": "[리밋] 스위치를 눌렀다 떼며 해당 사물함 값이 바뀌는지 확인하세요. 종료: Ctrl-C",
    "sim_help": "[시뮬레이션] 1~4 + Enter = 문 열기/닫기, 5~8 + Enter = 센서 오류 토글, q = 종료",
    "selected": "선택된 하드웨어: {mode} ({door})  로그={log}",
    "led_on": "[LED] {name} 켜짐",
    "led_off": "[LED] {name} 꺼짐",
    "deny": "[LED] 적색 거부 점멸 패턴",
    "sol_skip": "[출력] 솔밸브/사물함 출력 시험 생략 (--allow-solenoid 를 주면 시험합니다. 녹색 LED도 함께 시험됨)",
    "sol_pulse": "[솔밸브] {sec:.2f}초 펄스 (녹색 LED가 신호를 따라 켜져야 함)",
    "locker_need_target": ("[사물함] per_locker 모드입니다. --locker ID ... 또는 --all-lockers 로 시험할 사물함을 "
                           "지정하세요. (locker_id 없는 개방 요청이 거부되는지만 확인)"),
    "locker_pulse": ("[사물함 {lid}] BCM{pin} {sec:.2f}초 펄스 — 실제로 열린 문이 사물함 {lid} 인지 눈으로 확인하고 "
                     "deploy/WIRING.md 표에 기록하세요"),
    "ok": "자체 점검 통과 (SELFTEST OK)",
    "failed": "자체 점검 실패 (SELFTEST FAILED)",
    "fail": "실패: {msg}",
    "hw_error": "하드웨어 오류: {msg}",
    "error": "오류: {msg}",
}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=T["desc"])
    p.add_argument("--config", type=Path, default=None, help="default/local 다음에 병합할 YAML")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="설정 덮어쓰기, 예: --set hardware.solenoid_active_high=false")
    p.add_argument("--mode", choices=["mock", "pi", "auto"], default=None, help="hardware.mode 덮어쓰기")
    p.add_argument("--allow-solenoid", action="store_true", help="솔밸브/사물함 출력(+녹색 LED)도 펄스로 시험")
    p.add_argument("--pulse-sec", type=float, default=0.5, help="출력 펄스 길이(초, max_open_sec 로 제한)")
    p.add_argument("--step-sec", type=float, default=0.5, help="LED 켜짐/꺼짐 유지 시간(초)")
    p.add_argument("--locker", dest="lockers", action="extend", nargs="+", default=[], metavar="ID",
                   help="이 사물함들만 한 번에 하나씩 펄스 (--allow-solenoid 필요)")
    p.add_argument("--all-lockers", action="store_true",
                   help="설정된 모든 사물함을 선언 순서로 한 번에 하나씩 펄스 (--allow-solenoid 필요)")
    p.add_argument("--hardware-log", type=Path, default=None, help="hardware.hardware_log 덮어쓰기")
    p.add_argument("--force-off", action="store_true", help="모든 출력을 OFF 로 만들고 종료")
    p.add_argument("--limits", action="store_true", help="리밋센서 읽기 값을 실시간 출력 (배선 점검)")
    p.add_argument("--limits-sec", type=float, default=0.0, help="--limits 가 이 시간(초) 후 종료 (0 = Ctrl-C까지)")
    p.add_argument("--sim-limits", action="store_true", help="mock 에서 1~4 키로 문 열기/닫기 대화형 시뮬레이션")
    return p.parse_args(argv)


def _hardware_overrides(items: list[str]) -> dict[str, Any]:
    import yaml

    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"--set expects KEY=VALUE, got {item!r}")
        key, val = item.split("=", 1)
        key = key.strip()
        if key.startswith("hardware."):
            out[key.removeprefix("hardware.")] = yaml.safe_load(val)
    return out


def load_cfg(args: argparse.Namespace, hardware_only: bool) -> HardwareConfig:
    """Prefer the app loader (facedoor.config.load_config); fall back to the hardware-only loader.

    --force-off always uses the hardware-only loader so a broken non-hardware section cannot block it.
    """
    cfg: HardwareConfig | None = None
    if not hardware_only:
        try:
            from facedoor.config import load_config  # vision-owned
        except ImportError:
            load_config = None  # type: ignore[assignment]
        if load_config is not None:
            app_cfg = load_config(args.config, overrides=args.overrides)
            cfg = app_cfg.hardware
    if cfg is None:
        extra = [args.config] if args.config else []
        cfg = load_hardware_config(extra, _hardware_overrides(args.overrides))
    if args.mode:
        cfg.mode = args.mode
    if args.hardware_log:
        cfg.hardware_log = args.hardware_log
    cfg.validate()
    return cfg


def resolve_locker_targets(cfg: HardwareConfig, args: argparse.Namespace) -> list[str]:
    """Locker ids to pulse (declaration order, de-duplicated). ConfigError on misuse."""
    wanted = list(dict.fromkeys(str(x) for x in (args.lockers or [])))
    if not wanted and not args.all_lockers:
        return []
    if not args.allow_solenoid:
        raise ConfigError(T["need_allow"])
    known = [o.locker_id for o in cfg.locker_outputs]
    if not cfg.per_locker:
        raise ConfigError(T["single_no_locker"])
    if args.all_lockers:
        return known
    unknown = [w for w in wanted if w not in known]
    if unknown:
        raise ConfigError(T["unknown_locker"].format(ids=", ".join(unknown), known=", ".join(known)))
    order = {lid: i for i, lid in enumerate(known)}
    return sorted(wanted, key=lambda x: order[x])


def force_off(cfg: HardwareConfig) -> int:
    use_pi = cfg.mode == "pi" or (cfg.mode == "auto" and is_raspberry_pi())
    if not use_pi:
        print(T["mock_force_off"])
        for name, pin, ah in force_off_targets(cfg):
            print(f"force-off: {name} BCM{pin} -> OFF (level {0 if ah else 1}) [mock: no GPIO driven]")
        return ExitCode.OK
    from facedoor.hardware.gpio_pi import force_outputs_off

    lines = force_outputs_off(cfg)
    failed = False
    for line in lines:
        print("force-off:", line)
        failed = failed or " FAILED" in line
    if failed:
        print(T["force_off_failed"], file=sys.stderr)
        return ExitCode.HARDWARE
    return ExitCode.OK


def _wait_off(door: Any, limit: float, locker_id: str | None = None) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        st = door.status()
        on = (locker_id in st.open_lockers) if locker_id is not None else st.is_open
        if not on:
            return
        time.sleep(0.01)


def _pulse_single(cfg: HardwareConfig, door: Any, leds: Any, pulse: float, failures: list[str]) -> None:
    print(T["sol_pulse"].format(sec=pulse))
    if not door.open("selftest", duration=pulse):
        failures.append(f"door.open() refused while IDLE (last_reject={getattr(door, 'last_reject', '?')})")
        return
    print("  ", format_status_line(door.status(), leds.snapshot()))
    if not leds.snapshot().green:
        failures.append("green LED not ON while the output is ON")
    if door.open("selftest-second") is not False:
        failures.append("second open() during OPEN was accepted")
    if cfg.close_mode == "limit":
        time.sleep(pulse)
        door.close("selftest-pulse-end")
    else:
        _wait_off(door, min(pulse, cfg.max_open_sec) + 2.0)
    st = door.status()
    print("  ", format_status_line(st, leds.snapshot()))
    if st.is_open:
        failures.append("output did not turn OFF")
    if leds.snapshot().green:
        failures.append("green LED still ON after close")
    if cfg.cooldown_sec > 0 and st.state is not DoorState.COOLDOWN:
        failures.append(f"expected COOLDOWN after pulse, got {st.state}")


def _pulse_lockers(cfg: HardwareConfig, door: Any, leds: Any, targets: list[str], pulse: float,
                   failures: list[str]) -> None:
    channel_map = cfg.channel_map()
    for lid in targets:
        pin = channel_map[lid][0]
        before = door.status()
        if before.open_lockers:
            failures.append(f"before locker {lid}: other outputs still ON {before.open_lockers}; aborting")
            door.close("selftest-abort")
            return
        print(T["locker_pulse"].format(lid=lid, pin=pin, sec=pulse))
        if not door.open(f"selftest:locker={lid}", duration=pulse, locker_id=lid):
            failures.append(f"locker {lid}: open refused (last_reject={door.last_reject})")
            continue
        st = door.status()
        print("  ", format_status_line(st, leds.snapshot()))
        if st.open_lockers != (lid,):
            failures.append(f"locker {lid}: expected only ({lid},) ON, got {st.open_lockers}")
            door.close("selftest-abort")
            return
        if not leds.snapshot().green:
            failures.append(f"locker {lid}: green LED not ON")
        if cfg.close_mode == "limit":
            time.sleep(pulse)
            door.close("selftest-pulse-end", locker_id=lid)
        else:
            _wait_off(door, min(pulse, cfg.max_open_sec) + 2.0, locker_id=lid)
        st = door.status()
        print("  ", format_status_line(st, leds.snapshot()))
        if st.open_lockers:
            failures.append(f"locker {lid}: outputs still ON after pulse {st.open_lockers}")
            door.close("selftest-abort")
            return
        if leds.snapshot().green:
            failures.append(f"locker {lid}: green LED still ON after close")


def _fmt_limit(v: bool | None) -> str:
    return "?" if v is None else ("닫힘(눌림)" if v else "열림(해제)")


def run_limits_watch(cfg: HardwareConfig, args: argparse.Namespace) -> int:
    """--limits: live limit readings. Prints a line whenever any reading changes."""
    if not cfg.limit_mode:
        raise ConfigError(T["limits_need_limit_mode"])
    bundle = create_hardware(cfg)
    limits = bundle.limits
    print(T["limits_help"])
    t0 = time.monotonic()
    last: dict[Any, Any] = {}
    try:
        while True:
            cur = {lid: limits.read(lid) for lid, _ in cfg.limit_pins()}
            if cur != last:
                last = cur
                print(time.strftime("%H:%M:%S"), "  ".join(f"{('도어' if k is None else k)}={_fmt_limit(v)}"
                                                        for k, v in cur.items()))
            if args.limits_sec and time.monotonic() - t0 >= args.limits_sec:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        bundle.cleanup()
    return ExitCode.OK


def run_sim_limits(cfg: HardwareConfig, args: argparse.Namespace) -> int:
    """--sim-limits: interactive door simulation on the mock limit inputs (keys as on the recognition screen)."""
    if not cfg.limit_mode:
        raise ConfigError(T["limits_need_limit_mode"])
    if cfg.mode == "pi" or (cfg.mode == "auto" and is_raspberry_pi()):
        raise ConfigError(T["sim_need_mock"])
    bundle = create_hardware(cfg)
    limits, door = bundle.limits, bundle.door
    if not getattr(limits, "is_simulated", False):
        bundle.cleanup()
        raise ConfigError(T["sim_need_mock"])
    ids = [lid for lid, _ in cfg.limit_pins()]
    print(T["sim_help"])
    try:
        while True:
            for st in door.locker_states():
                print(f"  {('도어' if st.locker_id is None else st.locker_id)}: {st.state.value:<17} "
                      f"문={_fmt_limit(st.door_closed)} 솔밸브={'ON' if st.solenoid_on else 'off'} "
                      f"센서={'정상' if st.sensor_ok else '오류'}")
            try:
                line = input("> ").strip().lower()
            except EOFError:
                break
            if line in ("q", "quit", "exit"):
                break
            if line.isdigit() and 1 <= int(line) <= 8:
                n = int(line)
                idx = (n - 1) % 4
                if idx >= len(ids):
                    print("  (해당 사물함 없음)")
                elif n <= 4:
                    print("  문 닫힘" if limits.toggle(ids[idx]) else "  문 열림")
                else:
                    f = not limits.is_fault(ids[idx]) if hasattr(limits, "is_fault") else True
                    limits.set_fault(ids[idx], f)
                    print("  센서 오류 주입" if f else "  센서 복구")
            door.poll_once()
    except KeyboardInterrupt:
        pass
    finally:
        bundle.cleanup()
    return ExitCode.OK


def run_selftest(cfg: HardwareConfig, args: argparse.Namespace) -> int:
    targets = resolve_locker_targets(cfg, args)
    print(T["header"].format(mode=cfg.mode, backend=cfg.mock_backend, pi=is_raspberry_pi(), close_mode=cfg.close_mode))
    print(f"pins(BCM)={cfg.pins()} solenoid_active_high={cfg.solenoid_active_high} "
          f"led_active_high={cfg.led_active_high} open_sec={cfg.open_sec} max_open_sec={cfg.max_open_sec} "
          f"cooldown_sec={cfg.cooldown_sec}")
    if cfg.close_mode == "limit":
        print(f"limit: pins={cfg.limit_pins()} active_low={cfg.limit_active_low} pull_up={cfg.limit_pull_up} "
              f"unlock_timeout_sec={cfg.unlock_timeout_sec} close_confirm_sec={cfg.close_confirm_sec} "
              f"max_unlock_sec={cfg.max_unlock_sec} sensor_fault_sec={cfg.sensor_fault_sec}")
    if cfg.per_locker:
        chans = ", ".join(f"{lid}=BCM{pin}({'H' if ah else 'L'})" for lid, (pin, ah) in cfg.channel_map().items())
        print(f"lockers: {chans} max_concurrent_open={cfg.max_concurrent_open}")
    bundle = create_hardware(cfg)
    print(T["selected"].format(mode=bundle.mode, door=type(bundle.door).__name__, log=cfg.hardware_log))
    door, leds = bundle.door, bundle.leds
    step = max(0.0, args.step_sec)
    pulse = max(0.01, float(args.pulse_sec))
    failures: list[str] = []
    try:
        st = door.status()
        if st.is_open or leds.snapshot() != LedSnapshot(False, False, False):
            failures.append(f"initial state not OFF: {st} {leds.snapshot()}")

        for name, label, setter in (("RED", "적색", leds.set_red), ("AMBER", "황색", leds.set_hold)):
            print(T["led_on"].format(name=f"{label}({name})"))
            setter(True)
            time.sleep(step)
            print(T["led_off"].format(name=f"{label}({name})"))
            setter(False)
            time.sleep(step)
        print(T["deny"])
        leds.show_denied()
        if hasattr(leds, "wait_blink"):
            leds.wait_blink(timeout=10.0)

        if not args.allow_solenoid:
            print(T["sol_skip"])
        elif not cfg.per_locker:
            _pulse_single(cfg, door, leds, pulse, failures)
        elif not targets:
            print(T["locker_need_target"])
            if door.open("selftest-no-locker") is not False or door.last_reject != "no_locker_id":
                failures.append("open() without locker_id was not rejected with no_locker_id")
            if door.status().open_lockers:
                failures.append("an output turned ON for an open() without locker_id")
        else:
            _pulse_lockers(cfg, door, leds, targets, pulse, failures)
    finally:
        bundle.cleanup()
    final = door.status()
    if final.is_open:
        failures.append("output still ON after cleanup")
    if failures:
        for f in failures:
            print(T["fail"].format(msg=f))
        print(T["failed"])
        return ExitCode.HARDWARE
    print(T["ok"])
    return ExitCode.OK


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_cfg(args, hardware_only=args.force_off)
        if args.force_off:
            return int(force_off(cfg))
        if args.limits:
            return int(run_limits_watch(cfg, args))
        if args.sim_limits:
            return int(run_sim_limits(cfg, args))
        return int(run_selftest(cfg, args))
    except HardwareError as exc:
        print(T["hw_error"].format(msg=exc), file=sys.stderr)
        return int(ExitCode.HARDWARE)
    except FaceDoorError as exc:
        print(T["error"].format(msg=exc), file=sys.stderr)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        return int(ExitCode.INTERRUPTED)


if __name__ == "__main__":
    sys.exit(main())
