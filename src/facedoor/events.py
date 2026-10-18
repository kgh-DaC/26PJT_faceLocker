"""Recognition event log logs/events.csv (owner: vision-engineer). Contract: INTERFACES sections 3.11 and 5.

UTF-8 (no BOM), header row, csv default dialect, flushed per row. Floats 4 decimals, n/a = empty string.
The eval agent recomputes metrics from this file after post-hoc labelling (label column is always written empty).
"""

from __future__ import annotations

import csv
import logging
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Sequence

from facedoor.datatypes import DecisionOutput, FrameVerdict, RecognitionResult
from facedoor.liveness import liveness_label
from facedoor.timeutil import now_iso

log = logging.getLogger(__name__)

EVENT_COLUMNS: tuple[str, ...] = (
    "ts", "session_id", "frame_idx", "event", "verdict", "decision_state", "best_id", "best_score",
    "second_id", "second_score", "margin", "threshold", "threshold_group", "threshold_source", "matched_tag",
    "condition_tag", "hold_reason", "n_faces", "face_w", "face_h", "det_score", "blur_var", "brightness",
    "yaw_ratio", "liveness", "votes", "door_signal", "det_ms", "emb_ms", "total_ms", "embedder_id", "message",
    "label",
    # v1.3 / v1.4 (appended only; INTERFACES section 5)
    "locker_id", "open_lockers", "close_cause",
    # v1.5
    "locker_states",
)
assert len(EVENT_COLUMNS) == 37

EVENT_NAMES = frozenset({"start", "frame", "open", "deny", "door_close", "error", "stop",
                         "door_opened", "door_forced", "door_sensor"})


def new_session_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def format_lockers(lockers: Sequence[str] | None) -> str:
    """open_lockers column: sorted, ';'-separated, '' when none."""
    return ";".join(sorted(str(x) for x in (lockers or ())))


def format_locker_states(states: Any) -> str:
    """locker_states column (INTERFACES 5, col 37): '<id>=<LockerState>' joined by ';' in declaration order,
    single mode '-=<state>'. '' when there is nothing."""
    parts = []
    for st in states or ():
        lid = getattr(st, "locker_id", None)
        state = getattr(st, "state", "")
        parts.append(f"{'-' if lid is None else lid}={getattr(state, 'value', state)}")
    return ";".join(parts)


def _f(v: Any) -> str:
    if v is None:
        return ""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if x != x or x in (float("inf"), float("-inf")):
        return ""
    return f"{x:.4f}"


def _s(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "value"):
        return str(v.value)
    return str(v)


class EventLogger:
    def __init__(self, path: Path, session_id: str, condition_tag: str = "live", embedder_id: str = "",
                 max_mb: float = 50.0, log_no_face: bool = False) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.condition_tag = condition_tag
        self.embedder_id = embedder_id
        self.max_bytes = int(max_mb * 1024 * 1024) if max_mb and max_mb > 0 else 0
        self.log_no_face = log_no_face
        self._lock = threading.Lock()
        self._fh: IO[str] | None = None
        self._writer: Any = None
        self._last_frame_idx = -1
        self._open()

    # ------------------------------------------------------------------ file handling
    def _rotate_name(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = self.path.with_name(f"{self.path.stem}_{stamp}{self.path.suffix}")
        i = 1
        while p.exists():
            p = self.path.with_name(f"{self.path.stem}_{stamp}_{i}{self.path.suffix}")
            i += 1
        return p

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        need_header = True
        if self.path.is_file() and self.path.stat().st_size > 0:
            with open(self.path, "r", encoding="utf-8", newline="") as f:
                first = f.readline().strip("\r\n")
            if first == ",".join(EVENT_COLUMNS):
                need_header = False
            else:
                dst = self._rotate_name()
                log.warning("events file %s has a different header; rotated to %s", self.path, dst.name)
                os.replace(self.path, dst)
        self._fh = open(self.path, "a", encoding="utf-8", newline="")
        self._writer = csv.writer(self._fh)
        if need_header:
            self._writer.writerow(EVENT_COLUMNS)
            self._fh.flush()

    def _maybe_rotate(self) -> None:
        if not self.max_bytes or self._fh is None:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size >= self.max_bytes:
            self._fh.close()
            os.replace(self.path, self._rotate_name())
            self._open()

    def _write(self, row: dict[str, str]) -> None:
        with self._lock:
            if self._fh is None:
                return
            self._maybe_rotate()
            self._writer.writerow([row.get(c, "") for c in EVENT_COLUMNS])
            self._fh.flush()

    # ------------------------------------------------------------------ public API
    def log_frame(self, result: RecognitionResult, decision: DecisionOutput, door: Any,
                  event: str = "frame", message: str = "", *, locker_id: str | None = None,
                  door_signal: bool | None = None, open_lockers: Sequence[str] | None = None,
                  locker_states: str = "") -> None:
        """door_signal overrides door.is_open (per_locker open rows: target channel state).
        open_lockers None -> taken from door.open_lockers (empty in single mode)."""
        if decision.should_open:
            event = "open"
        elif decision.should_deny:
            event = "deny"
        if event == "frame" and result.verdict is FrameVerdict.NO_FACE and not self.log_no_face:
            return
        m = result.match
        det = result.detection
        q = result.quality
        best = m.best if m is not None else None
        second = m.second if m is not None else None
        tm = result.timings_ms or {}
        row = {
            "ts": now_iso(), "session_id": self.session_id, "frame_idx": str(result.frame_index),
            "event": event, "verdict": _s(result.verdict), "decision_state": _s(decision.state),
            "best_id": best.person_id if best else "", "best_score": _f(best.score if best else None),
            "second_id": second.person_id if second else "", "second_score": _f(second.score if second else None),
            "margin": _f(m.margin if (m is not None and best is not None) else None),
            "threshold": _f(m.threshold if m is not None else None),
            "threshold_group": m.threshold_group if m is not None else "",
            "threshold_source": m.threshold_source if m is not None else "",
            "matched_tag": best.tag if best else "", "condition_tag": self.condition_tag,
            "hold_reason": _s(result.hold_reason), "n_faces": str(len(result.all_detections)),
            "face_w": str(det.bbox[2]) if det else "", "face_h": str(det.bbox[3]) if det else "",
            "det_score": _f(det.score if det else None),
            "blur_var": _f(q.blur_var if q else None), "brightness": _f(q.brightness if q else None),
            "yaw_ratio": _f(q.yaw_ratio if q else None), "liveness": liveness_label(result.liveness),
            "votes": f"{decision.votes}/{decision.window}",
            "door_signal": "1" if (bool(getattr(door, "is_open", False)) if door_signal is None
                                   else bool(door_signal)) else "0",
            "det_ms": _f(tm.get("det")), "emb_ms": _f(tm.get("emb")), "total_ms": _f(tm.get("total")),
            "embedder_id": self.embedder_id, "message": message, "label": "",
            "locker_id": locker_id or "",
            "open_lockers": format_lockers(getattr(door, "open_lockers", ()) if open_lockers is None
                                           else open_lockers),
            "close_cause": "", "locker_states": locker_states,
        }
        self._last_frame_idx = result.frame_index
        self._write(row)

    def log_event(self, event: str, **fields: object) -> None:
        row = {"ts": now_iso(), "session_id": self.session_id, "event": event,
               "condition_tag": self.condition_tag, "embedder_id": self.embedder_id, "label": ""}
        for k, v in fields.items():
            if k not in EVENT_COLUMNS:
                raise KeyError(f"unknown events.csv column {k!r}")
            if k == "open_lockers" and isinstance(v, (list, tuple, set, frozenset)):
                row[k] = format_lockers(v)  # type: ignore[arg-type]
            else:
                row[k] = _f(v) if isinstance(v, float) else _s(v)
        if "frame_idx" not in fields and self._last_frame_idx >= 0:
            row["frame_idx"] = str(self._last_frame_idx)
        self._write(row)

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
