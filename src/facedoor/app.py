"""Main runtime loop (owner: vision-engineer). Contract: INTERFACES section 3.13, ARCHITECTURE sections 4 and 9.

step(): camera.read -> (process_every_n) pipeline.process -> decider.update -> door.open only when
should_open -> deny/hold LEDs -> door_close transition -> events.log_frame -> display.show.

Fail-closed:
- any exception inside step() closes the door first, then an `error` event is written;
- FaceDoorError subclasses (CameraError/ModelError/HardwareError/...) end the loop with their ExitCode;
- every exit path (normal end, q/ESC, exception, SIGINT/SIGTERM, KeyboardInterrupt) runs shutdown():
  door.close() -> leds.off() -> hardware.cleanup() -> events flush/close. shutdown() is idempotent.
- door.open() is called in exactly one place: the `decision.should_open is True` branch of step()
  (via _request_open).

v1.5 additions (INTERFACES 3.13, 15, 16, 17): open goes to the winner's locker (LockerMap.resolve), door events
(`drain_events`) become events.csv rows, `locker_states()` feeds the display panel and logs/door_state.json
(schema 2), and in a simulated environment keys 1-4 / 5-8 and panel clicks drive the virtual limit switches.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from facedoor.camera import Camera, create_camera
from facedoor.config import AppConfig, load_config, project_root
from facedoor.datatypes import DecisionOutput, DecisionState, Frame, FrameVerdict, RecognitionResult
from facedoor.decision import VoteDecider
from facedoor import texts as T
from facedoor.display import Display, create_display
from facedoor.errors import CameraError, ExitCode, FaceDoorError
from facedoor.events import EventLogger, format_locker_states, new_session_id
from facedoor.locker_panel import build_rows, unknown_rows, write_state
from facedoor.lockers import LockerMap, configured_lockers
from facedoor.pipeline import FramePipeline

log = logging.getLogger("facedoor.app")

# Non-FaceDoorError exceptions in step() are logged and the loop continues (door already closed);
# this many in a row means something is persistently broken -> stop with ExitCode.ERROR.
MAX_CONSECUTIVE_ERRORS = 10


def open_reason(person_id: str, best_score: float | None, votes: int, window: int,
                locker_id: str | None = None) -> str:
    """OPEN reason string shared by events.csv and hardware.log: face:<id>:<score:.4f>:<votes>/<window>
    (+ `:locker=<L>` when a locker channel is targeted, INTERFACES 3.13)."""
    base = f"face:{person_id}:{(best_score if best_score is not None else 0.0):.4f}:{votes}/{window}"
    return f"{base}:locker={locker_id}" if locker_id else base


def _idle_decision() -> DecisionOutput:
    return DecisionOutput(DecisionState.IDLE, False, False, None, 0, 0, None, "idle")


class FaceDoorApp:
    def __init__(self, cfg: AppConfig, camera: Camera, pipeline: FramePipeline, decider: VoteDecider,
                 hardware: Any, display: Display, events: EventLogger,
                 clock: Callable[[], float] = time.monotonic, locker_map: LockerMap | None = None) -> None:
        self.cfg = cfg
        self.camera = camera
        self.pipeline = pipeline
        self.decider = decider
        self.hardware = hardware
        self.display = display
        self.events = events
        self.clock = clock
        self.exit_code: ExitCode | None = None
        self.stop_reason: str = ""
        self.frames_read = 0
        self.frames_processed = 0
        self.open_calls = 0
        self._started = False
        self._shutdown_done = False
        self._last_result: RecognitionResult | None = None
        self._last_decision: DecisionOutput = _idle_decision()
        self._prev_door_open = False
        self._hold_led: bool | None = None
        self._consecutive_errors = 0
        self._fps = 0.0
        self._last_step_t: float | None = None
        # v1.5: lockers, panel, simulation
        self.locker_map: LockerMap = locker_map if locker_map is not None else self._build_locker_map()
        self.limits: Any = getattr(hardware, "limits", None)
        self._limit_mode = str(getattr(cfg.hardware, "close_mode", "timer")) == "limit"
        self._opener: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._panel_rows: list[Any] = []
        self._state_sig: Any = None
        self._state_t = float("-inf")
        self._state_warned = False
        self._sim_faults: set[str | None] = set()
        self._sim = bool(getattr(self.limits, "is_simulated", False))
        self._configure_display()

    def _build_locker_map(self) -> LockerMap:
        db = getattr(getattr(self.pipeline, "recognizer", None), "db", None)
        return LockerMap.build(db, self.cfg.lockers, configured_lockers(self.cfg.hardware))

    def _configure_display(self) -> None:
        hw = self.cfg.hardware
        fn = getattr(self.display, "set_timings", None)
        if callable(fn):
            fn(float(getattr(hw, "unlock_timeout_sec", 10.0)), float(getattr(hw, "close_confirm_sec", 2.0)),
               float(getattr(hw, "max_unlock_sec", 30.0)))
        fn = getattr(self.display, "set_sim_handler", None)
        if callable(fn):
            fn(self._on_sim_click if self._sim else None)   # real hardware: no click/keys path to the sensors

    # ------------------------------------------------------------------ helpers
    @property
    def door(self) -> Any:
        return self.hardware.door

    @property
    def leds(self) -> Any:
        return self.hardware.leds

    def request_stop(self, reason: str, code: ExitCode = ExitCode.OK) -> None:
        if self.exit_code is None:
            self.exit_code = code
            self.stop_reason = reason

    def _safe_close_door(self, reason: str) -> None:
        try:
            self.door.close(reason)
        except Exception:  # noqa: BLE001 - closing must never raise out of the error path
            log.exception("door.close failed")

    def _set_hold_led(self, on: bool) -> None:
        if self._hold_led is not on:
            self.leds.set_hold(on)
            self._hold_led = on

    def _update_fps(self) -> None:
        now = time.perf_counter()
        if self._last_step_t is not None:
            dt = now - self._last_step_t
            if dt > 0:
                inst = 1.0 / dt
                self._fps = inst if self._fps == 0.0 else 0.9 * self._fps + 0.1 * inst
        self._last_step_t = now

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Open the camera (CameraError propagates) and write the `start` event. Called by run() if needed."""
        if self._started:
            return
        self._started = True
        self.camera.open()
        policy = getattr(getattr(self.pipeline, "recognizer", None), "policy", None)
        desc = policy.describe() if policy is not None else {}
        thr = ";".join(f"{g}={d['threshold']}({d['source']})" for g, d in desc.items()
                       if isinstance(d, dict) and "threshold" in d)
        for prob in self.locker_map.problems():
            log.warning("locker assignment problem: %s", prob.message)
        self.events.log_event("start", message=f"hw={getattr(self.hardware, 'mode', '?')} {thr} "
                                               f"lockers={self.locker_map.summary()}".strip())

    def step(self) -> bool:
        """Process one frame. Returns False when the app should stop (see exit_code / stop_reason)."""
        if self.exit_code is not None:
            return False
        frame_idx: int | None = None
        try:
            try:
                frame = self.camera.read()
            except StopIteration:
                self.request_stop("end of input")
                return False
            frame_idx = frame.index
            self.frames_read += 1
            self._update_fps()
            self._process_frame(frame)
            self._consecutive_errors = 0
        except Exception as e:  # noqa: BLE001 - fail-closed: door first, then record
            self._safe_close_door(f"error: {type(e).__name__}")
            # votes collected before an error must not combine with votes after it: restart the decision
            try:
                self.decider.reset()
            except Exception:  # noqa: BLE001
                log.exception("decider.reset failed")
            msg = f"{type(e).__name__}: {e}"
            log.error("step failed: %s", msg, exc_info=not isinstance(e, FaceDoorError))
            try:
                if frame_idx is not None:
                    self.events.log_event("error", frame_idx=frame_idx, message=msg)
                else:
                    self.events.log_event("error", message=msg)
            except Exception:  # noqa: BLE001
                log.exception("cannot write error event")
            if isinstance(e, CameraError) and not self.cfg.runtime.stop_on_camera_error:
                self._consecutive_errors += 1
            elif isinstance(e, FaceDoorError):
                self.request_stop(msg, e.exit_code)
                return False
            else:
                self._consecutive_errors += 1
            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self.request_stop(f"{self._consecutive_errors} consecutive errors, last: {msg}",
                                  e.exit_code if isinstance(e, FaceDoorError) else ExitCode.ERROR)
                return False
        return self.exit_code is None

    def _process_frame(self, frame: Frame) -> None:
        n = max(1, int(self.cfg.runtime.process_every_n))
        processed = (self.frames_read - 1) % n == 0
        result: RecognitionResult | None
        if processed:
            result = self.pipeline.process(frame)
            self.frames_processed += 1
            decision = self.decider.update(result)
        else:
            # skipped frame: display only, never re-emit one-shot open/deny flags
            result = self._last_result
            decision = dataclasses.replace(self._last_decision, should_open=False, should_deny=False)

        message = ""
        open_locker_raw: str | None = None
        open_signal: bool | None = None
        if processed:
            if decision.should_open is True:
                pid = decision.person_id
                if pid is None:
                    message = "open decision without person_id: ignored"
                    log.error(message)
                else:
                    res = self.locker_map.resolve(pid)
                    open_locker_raw = res.locker_id
                    if not res.allowed:
                        message = res.message
                        log.warning(message)
                        self._notify(T.NOTICE_NO_LOCKER if res.check.value == "no_locker_id"
                                     else T.NOTICE_LOCKER_UNUSABLE)
                    else:
                        message, open_signal = self._request_open(pid, decision, res.target)
            if decision.should_deny:
                self.leds.show_denied()
                message = message or decision.reason
            self._set_hold_led(result is not None and result.verdict is FrameVerdict.HOLD)

        status = self.door.status()
        if self._limit_mode and callable(getattr(self.door, "drain_events", None)):
            self._log_door_events(frame.index)       # no transition is missed between frames (15.3)
        else:
            if self._prev_door_open and not status.is_open:
                self.events.log_event("door_close", frame_idx=frame.index, door_signal=0,
                                      message=status.last_reason)
            self._prev_door_open = bool(status.is_open)

        if processed and result is not None and (
                self.cfg.logging.log_every_frame or decision.should_open or decision.should_deny):
            extra: dict[str, Any] = {"locker_states": self._locker_states_text()}
            if decision.should_open:
                extra["locker_id"] = open_locker_raw
                if open_signal is not None:
                    extra["door_signal"] = open_signal
            self.events.log_frame(result, decision, status, message=message, **extra)

        self._last_result = result
        self._last_decision = decision
        self._update_panel()
        key = self.display.show(frame, result, decision, status, self.leds.snapshot(), self._fps)
        if key in (ord("q"), ord("Q"), 27):
            self.request_stop("quit key")
        elif self._sim and ord("1") <= key <= ord("8"):
            self._handle_sim_key(key)

    # ------------------------------------------------------------------ open request (v1.5)
    def _request_open(self, pid: str, decision: DecisionOutput, target: str | None) -> tuple[str, bool | None]:
        """The single door.open() call site. Returns (events.csv message, target channel signal or None)."""
        reason = open_reason(pid, decision.best_score, decision.votes, decision.window, target)
        if target is None:
            accepted = bool(self.door.open(reason))
        else:
            accepted = bool(self.door.open(reason, locker_id=target))
        self.open_calls += 1
        label = target if target is not None else "-"
        if accepted:
            if target is not None:
                self._opener[target] = pid
            log.info("OPEN %s", reason)
            if self._limit_mode:
                self._notify((T.NOTICE_UNLOCKED_SINGLE if target is None else T.NOTICE_UNLOCKED).format(
                    locker=label, sec=float(getattr(self.cfg.hardware, "unlock_timeout_sec", 10.0))), 6.0)
            message = reason
        else:
            code = str(getattr(self.door, "last_reject", "") or "")
            message = f"{reason} (rejected by door: {code or 'busy/cooldown/fault'})"
            log.info("OPEN %s -> rejected by door controller (%s)", reason, code or "?")
            tmpl = {"door_open": T.NOTICE_DOOR_OPEN_REJECT, "sensor_fault": T.NOTICE_SENSOR_REJECT,
                    "busy": T.NOTICE_BUSY, "cooldown": T.NOTICE_COOLDOWN}.get(code)
            self._notify((tmpl or T.NOTICE_REJECTED).format(locker=label, code=code or "?"))
        signal: bool | None = None
        if target is not None:
            try:
                signal = bool(self.door.locker_status(target).is_open)
            except Exception:  # noqa: BLE001 - old controller / single mode
                signal = None
        return message, signal

    # ------------------------------------------------------------------ door events, panel, simulation
    def _notify(self, text: str, ttl: float = 4.0) -> None:
        fn = getattr(self.display, "set_notice", None)
        if callable(fn):
            fn(text, ttl)

    def _locker_states_text(self) -> str:
        if not self._limit_mode:
            return ""
        fn = getattr(self.door, "locker_states", None)
        return format_locker_states(fn()) if callable(fn) else ""

    def _log_door_events(self, frame_idx: int | None = None) -> None:
        """door.drain_events() -> events.csv rows (door_opened / door_close / door_forced / door_sensor)."""
        for ev in self.door.drain_events():
            kind = str(getattr(ev, "kind", ""))
            lid = getattr(ev, "locker_id", None)
            cause = str(getattr(ev, "cause", "") or "")
            detail = str(getattr(ev, "detail", "") or "")
            label = "-" if lid is None else str(lid)
            fields: dict[str, Any] = {"locker_id": lid or "", "locker_states": self._locker_states_text()}
            if frame_idx is not None:
                fields["frame_idx"] = frame_idx
            if kind == "door_opened":
                name, fields["door_signal"], msg = "door_opened", 1, f"locker={label} {detail}"
            elif kind == "relock":
                name, fields["door_signal"] = "door_close", 0
                fields["close_cause"] = cause
                msg = f"cause={cause} {detail}"
                tmpl = {"unlock_timeout": T.NOTICE_UNLOCK_TIMEOUT, "close_confirmed": T.NOTICE_CLOSED_CONFIRMED,
                        "watchdog": T.NOTICE_WATCHDOG, "sensor_fault": T.NOTICE_SENSOR_FAULT_RELOCK}.get(cause)
                if tmpl:
                    self._notify(tmpl.format(locker=label), 6.0)
                self._opener.pop(label, None)
            elif kind == "forced_open":
                name, fields["door_signal"], msg = "door_forced", 0, f"locker={label} {detail}"
                self._notify(T.NOTICE_FORCED.format(locker=label), 6.0)
            elif kind in ("sensor_fault", "sensor_ok"):
                name, msg = "door_sensor", f"{kind} locker={label} {detail}"
                if kind == "sensor_ok":
                    self._notify(T.NOTICE_SENSOR_OK.format(locker=label))
            else:       # "unlocked" (the open row exists) and "door_closing": no events.csv row
                continue
            self.events.log_event(name, message=msg.strip(), **fields)

    def _display_name(self, pid: str) -> str:
        if pid not in self._names:
            db = getattr(getattr(self.pipeline, "recognizer", None), "db", None)
            fn = getattr(db, "display_name", None)
            try:
                self._names[pid] = str(fn(pid)) if callable(fn) else pid
            except Exception:  # noqa: BLE001
                self._names[pid] = pid
        return self._names[pid]

    def _update_panel(self) -> None:
        """locker_states() -> display.set_panel() every step; door_state.json on change or every 0.5 s."""
        names = {pid: self._display_name(pid) for pid in set(self._opener.values())}
        if self.cfg.display.locker_owner == "always":
            for lid in self.locker_map.configured:
                for pid in self.locker_map.owners(lid):
                    names[pid] = self._display_name(pid)
        rows = build_rows(self.door, self.limits, self.locker_map, self._opener, self.cfg.display.locker_owner,
                          names)
        self._panel_rows = rows
        fn = getattr(self.display, "set_panel", None)
        if callable(fn):
            fn(rows)
        if not rows:
            return
        sig = tuple((r.locker_id, r.state, r.door, r.unlocked, r.owner_label, r.sensor_injected) for r in rows)
        now = self.clock()
        if sig != self._state_sig or now - self._state_t >= 0.5:
            try:
                write_state(self.cfg.logging.door_state_file, rows, self.events.session_id)
            except OSError as e:
                if not self._state_warned:
                    self._state_warned = True
                    log.warning("cannot write %s: %s", self.cfg.logging.door_state_file, e)
            self._state_sig, self._state_t = sig, now

    def _sim_ids(self) -> list[str | None]:
        fn = getattr(self.door, "locker_states", None)
        if callable(fn):
            return [getattr(st, "locker_id", None) for st in fn()]
        return [r.locker_id for r in self._panel_rows]

    def _on_sim_click(self, locker_id: str | None, action: str) -> None:
        if self._sim and action == "toggle":
            self._sim_toggle(locker_id)

    def _handle_sim_key(self, key: int) -> None:
        """Keys 1-4: toggle the door of locker 1-4; 5-8: toggle an injected read failure of locker 1-4.
        Ignored entirely unless limits.is_simulated (no keyboard path to fool real sensors)."""
        if not self._sim or self.limits is None:
            return
        ids = self._sim_ids()
        if ord("1") <= key <= ord("4"):
            idx = key - ord("1")
            if idx < len(ids):
                self._sim_toggle(ids[idx])
        elif ord("5") <= key <= ord("8"):
            idx = key - ord("5")
            if idx < len(ids):
                lid = ids[idx]
                fault = lid not in self._sim_faults
                self._sim_faults.symmetric_difference_update({lid})
                self.limits.set_fault(lid, fault)
                log.info("SIM sensor fault locker=%s -> %s", lid, "on" if fault else "off")

    def _sim_toggle(self, locker_id: str | None) -> None:
        if not self._sim or self.limits is None:
            return
        closed = self.limits.toggle(locker_id)
        log.info("SIM door locker=%s -> %s", locker_id, "closed" if closed else "open")

    def run(self, max_frames: int | None = None) -> int:
        """Loop until end of input / quit / fatal error / max_frames. Always calls shutdown(). Returns ExitCode."""
        max_fps = float(self.cfg.runtime.max_fps or 0)
        min_dt = 1.0 / max_fps if (max_fps > 0 and getattr(self.camera, "is_live", True)) else 0.0
        try:
            try:
                self.start()
            except FaceDoorError as e:
                self._safe_close_door("start failed")
                self.events.log_event("error", message=f"{type(e).__name__}: {e}")
                log.error("start failed: %s", e)
                self.request_stop(str(e), e.exit_code)
                return int(self.exit_code)
            count = 0
            while True:
                t0 = time.monotonic()
                if not self.step():
                    break
                count += 1
                if max_frames is not None and count >= max_frames:
                    self.request_stop(f"max_frames={max_frames}")
                    break
                if min_dt > 0:
                    wait = min_dt - (time.monotonic() - t0)
                    if wait > 0:
                        time.sleep(wait)
        except KeyboardInterrupt:
            self._safe_close_door("interrupted")
            self.request_stop("interrupted (SIGINT)", ExitCode.INTERRUPTED)
        except SystemExit as e:  # SIGTERM via hardware.factory signal handler
            self._safe_close_door("terminated")
            self.request_stop(f"terminated ({e.code})", ExitCode.INTERRUPTED)
        finally:
            if self.exit_code is None:
                self.request_stop("unexpected exit", ExitCode.ERROR)
            self.shutdown(self.stop_reason)
        return int(self.exit_code)

    def shutdown(self, reason: str) -> None:
        """door.close() -> leds.off() -> hardware.cleanup() -> stop event + log flush. Idempotent, never raises."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._safe_close_door(f"shutdown: {reason}")
        for name, fn in (("leds.off", lambda: self.leds.off()),
                         ("hardware.cleanup", lambda: self.hardware.cleanup())):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("%s failed", name)
        if self._limit_mode and callable(getattr(self.door, "drain_events", None)):
            try:
                self._log_door_events()
            except Exception:  # noqa: BLE001
                log.exception("final door event drain failed")
        try:
            code = self.exit_code if self.exit_code is not None else ExitCode.OK
            self.events.log_event("stop", door_signal=0,
                                  message=f"{reason} (exit {int(code)} {ExitCode(code).name}; "
                                          f"frames={self.frames_read} processed={self.frames_processed} "
                                          f"opens={self.open_calls})")
            self.events.flush()
            self.events.close()
        except Exception:  # noqa: BLE001
            log.exception("event log close failed")
        if self._panel_rows:    # never leave a stale "closed" behind (16.1)
            try:
                write_state(self.cfg.logging.door_state_file, unknown_rows(self._panel_rows),
                            self.events.session_id)
            except Exception:  # noqa: BLE001
                log.exception("final door_state write failed")
        for name, fn in (("display.close", self.display.close), ("camera.close", self.camera.close)):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("%s failed", name)
        log.info("shutdown complete: %s", reason)


# ---------------------------------------------------------------------- CLI
def build_arg_parser(prog: str = "facedoor") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Face recognition door controller (INTERFACES section 9)")
    p.add_argument("--config", type=Path, default=None, help="extra YAML merged after default/local")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="config override, e.g. --set hardware.mode=pi (repeatable)")
    p.add_argument("--source", default=None, help="camera index (0), /dev/video0, video file or image folder")
    p.add_argument("--no-display", action="store_true", help="headless mode (no window)")
    p.add_argument("--tag", default=None, help="events.csv condition_tag (default app.condition_tag = live)")
    p.add_argument("--max-frames", type=int, default=None, help="stop after N frames (testing)")
    return p


def setup_logging(level: str = "INFO", app_log: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    have_console = any(getattr(h, "_facedoor", "") == "console" for h in root.handlers)
    if not have_console:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        h._facedoor = "console"  # type: ignore[attr-defined]
        root.addHandler(h)
    if app_log is not None and not any(getattr(h, "_facedoor", "") == str(app_log) for h in root.handlers):
        try:
            Path(app_log).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(app_log, encoding="utf-8")
            fh.setFormatter(fmt)
            fh._facedoor = str(app_log)  # type: ignore[attr-defined]
            root.addHandler(fh)
        except OSError as e:
            log.warning("cannot open app log %s: %s", app_log, e)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry (scripts/run.py, python -m facedoor). Returns errors.ExitCode; hardware is always cleaned up."""
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    try:
        cfg = load_config(args.config, args.overrides)
    except FaceDoorError as e:
        setup_logging("INFO")
        log.error("config error: %s", e)
        return int(e.exit_code)
    setup_logging(cfg.app.log_level, cfg.logging.app_log)
    local = project_root() / "configs" / "local.yaml"
    log.info("config: default.yaml%s%s overrides=%s", " + local.yaml" if local.is_file() else "",
             f" + {args.config}" if args.config else "", list(args.overrides))

    # Hardware first: outputs are driven OFF as early as possible and cleanup hooks get registered.
    from facedoor.hardware.factory import create_hardware

    try:
        hardware = create_hardware(cfg.hardware)
    except FaceDoorError as e:
        log.error("hardware error: %s", e)
        return int(e.exit_code)
    except Exception as e:  # noqa: BLE001
        log.exception("hardware init failed: %s", e)
        return int(ExitCode.HARDWARE)

    try:
        try:
            pipeline = FramePipeline.from_config(cfg)
        except FaceDoorError as e:
            log.error("%s: %s", type(e).__name__, e)
            return int(e.exit_code)
        db = pipeline.recognizer.db
        log.info("embedder=%s gallery=%d samples/%d persons hardware=%s", pipeline.embedder.model_id, len(db),
                 len(db.persons()), hardware.mode)
        log.info("thresholds: %s", pipeline.recognizer.policy.describe())
        if hardware.mode == "mock":
            log.warning("MOCK hardware: no physical door signal will be output")

        camera = create_camera(cfg.camera, args.source)
        decider = VoteDecider(cfg.decision)
        display = create_display(cfg.display, enabled=not args.no_display)
        if hasattr(display, "label_fn") and hasattr(db, "locker_id"):
            korean = bool(getattr(display, "korean", False))

            def _label(pid: str) -> str:
                if korean:      # TkDisplay can draw Hangul: show the display name
                    return db.display_name(pid)
                locker = db.locker_id(pid)
                return f"{pid} [L:{locker}]" if locker else pid
            display.label_fn = _label  # type: ignore[attr-defined]
        try:
            events = EventLogger(cfg.logging.events_csv, new_session_id(),
                                 condition_tag=args.tag or cfg.app.condition_tag,
                                 embedder_id=pipeline.embedder.model_id, max_mb=cfg.logging.events_max_mb,
                                 log_no_face=cfg.logging.log_no_face)
        except OSError as e:
            log.error("cannot open events log %s: %s", cfg.logging.events_csv, e)
            return int(ExitCode.ERROR)
        log.info("session %s, events -> %s", events.session_id, cfg.logging.events_csv)
        app = FaceDoorApp(cfg, camera, pipeline, decider, hardware, display, events)
        code = app.run(args.max_frames)
        log.info("exit %d (%s): %s", code, ExitCode(code).name, app.stop_reason)
        return code
    except FaceDoorError as e:      # e.g. display.backend=tk without a usable window
        log.error("%s: %s", type(e).__name__, e)
        return int(e.exit_code)
    except KeyboardInterrupt:
        return int(ExitCode.INTERRUPTED)
    except SystemExit:
        return int(ExitCode.INTERRUPTED)
    finally:
        hardware.cleanup()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
