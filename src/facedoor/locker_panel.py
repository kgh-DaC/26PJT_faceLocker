"""Locker (door) status panel model (owner: vision-engineer). Contract: INTERFACES section 16.

The only source is ``door.locker_states()``. Sensors are never read here (``limits`` is used only for
``is_simulated``). This module must not import ``facedoor.hardware`` (duck typing on LockerStatus).

``door.state`` is the primary display (limit sensor), the solenoid state is secondary (16.2).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from facedoor import texts as T

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

ON_STATES = frozenset({"UNLOCKED_WAITING", "OPEN", "CLOSING_CONFIRM"})   # solenoid ON (15.3)
_CLOSED_STATES = frozenset({"LOCKED_CLOSED", "UNLOCKED_WAITING", "CLOSING_CONFIRM", "COOLDOWN"})
_OPEN_STATES = frozenset({"OPEN", "LOCKED_OPEN"})


@dataclass(frozen=True, slots=True)
class LockerPanelRow:
    locker_id: str | None
    state: str                   # LockerState value, "UNKNOWN" when not known
    door: str                    # "open" | "closed" | "unknown"
    unlocked: bool
    countdown_kind: str          # "unlock_wait" | "close_confirm" | "hard" | ""
    countdown_sec: float
    cooldown_remaining_sec: float
    owner_label: str | None
    sim_toggle: bool
    sensorless: bool = False     # close_mode=timer: no door sensor, show lock state instead of open/closed
    sensor_injected: bool = False  # simulation: a read failure is currently injected (info only)


def _state_value(st: Any) -> str:
    s = getattr(st, "state", None)
    return str(getattr(s, "value", s) or "UNKNOWN")


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x else 0.0


def build_rows(door: Any, limits: Any | None, locker_map: Any | None, opener: Mapping[str, str],
               owner_policy: str, display_names: Mapping[str, str]) -> list[LockerPanelRow]:
    """One row per locker in declaration order. Returns [] when the door has no locker_states()."""
    fn = getattr(door, "locker_states", None)
    if not callable(fn):
        return []
    sim = bool(getattr(limits, "is_simulated", False))
    injected = _injected_faults(limits)
    rows: list[LockerPanelRow] = []
    for st in fn():
        lid = getattr(st, "locker_id", None)
        lid = None if lid is None else str(lid)
        state = _state_value(st)
        door_closed = getattr(st, "door_closed", None)
        sensorless = door_closed is None and state not in ("SENSOR_FAULT", "FAULT")
        if state in ("SENSOR_FAULT", "FAULT"):
            door_s = "unknown"
        elif door_closed is True:
            door_s = "closed"
        elif door_closed is False:
            door_s = "open"
        elif sensorless:
            door_s = "unknown"
        else:
            door_s = "unknown"
        unlocked = bool(getattr(st, "solenoid_on", False)) or state in ON_STATES
        uw = _f(getattr(st, "unlock_wait_remaining_sec", 0.0))
        cc = _f(getattr(st, "close_confirm_remaining_sec", 0.0))
        hard = _f(getattr(st, "hard_remaining_sec", 0.0))
        if state == "UNLOCKED_WAITING" and uw > 0:
            kind, sec = "unlock_wait", uw
        elif state == "CLOSING_CONFIRM" and cc > 0:
            kind, sec = "close_confirm", cc
        elif unlocked and hard > 0:
            kind, sec = "hard", hard
        else:
            kind, sec = "", 0.0
        owner = _owner_label(lid, state, locker_map, opener, owner_policy, display_names)
        rows.append(LockerPanelRow(lid, state, door_s, unlocked, kind, sec,
                                   _f(getattr(st, "cooldown_remaining_sec", 0.0)), owner, sim,
                                   sensorless, bool(lid in injected)))
    return rows


def _injected_faults(limits: Any | None) -> set[str | None]:
    """Mock limits with an injected read failure (snapshot() value None) - informational only."""
    if limits is None or not bool(getattr(limits, "is_simulated", False)):
        return set()
    snap = getattr(limits, "snapshot", None)
    if not callable(snap):
        return set()
    try:
        d = snap()
    except Exception:  # noqa: BLE001
        return set()
    return {(None if k in (None, "-") else str(k)) for k, v in dict(d).items() if v is None}


def _owner_label(lid: str | None, state: str, locker_map: Any | None, opener: Mapping[str, str],
                 policy: str, names: Mapping[str, str]) -> str | None:
    if policy == "none" or lid is None:
        return None
    pid: str | None = None
    if policy == "when_open":
        if state in ON_STATES:
            pid = opener.get(lid)
    elif policy == "always" and locker_map is not None:
        try:
            owners = list(locker_map.owners(lid))
        except Exception:  # noqa: BLE001
            owners = []
        pid = owners[0] if owners else None
    if pid is None:
        return None
    return names.get(pid, pid)


# ---------------------------------------------------------------------------------------------- text/colours
@dataclass(frozen=True, slots=True)
class RowView:
    title: str
    badge: str
    sub: str
    small: str               # secondary small line (owner / max time)
    bg: str                  # hex background colour
    fg: str
    badge_bg: str
    bold: bool
    progress: float | None   # 0..1 fraction remaining, None = no bar


GRAY, YELLOW, GREEN, LIGHT_GREEN, ORANGE, RED, WHITE = (
    "#d9d9d9", "#fff3b0", "#b7eb8f", "#e3f7c9", "#ffd591", "#ff9c9c", "#ffffff")
BADGE_COLOR = {"closed": "#595959", "open": "#237804", "unknown": "#d48806"}


def describe_row(row: LockerPanelRow, unlock_timeout: float = 10.0, close_confirm: float = 2.0,
                 max_unlock: float = 30.0) -> RowView:
    """Pure: text and colours of one panel (16.2 table)."""
    s = row.state
    title = T.locker_name(row.locker_id)
    if row.sensorless:
        badge = T.BADGE_UNLOCKED if row.unlocked else T.BADGE_LOCKED
        bcol = "open" if row.unlocked else "closed"
    else:
        badge = {"closed": T.BADGE_CLOSED, "open": T.BADGE_OPEN}.get(row.door, T.BADGE_UNKNOWN)
        bcol = row.door
    bg, bold, progress = GRAY, False, None
    if s == "LOCKED_CLOSED":
        sub = T.SUB_LOCKED
    elif s == "UNLOCKED_WAITING":
        sub = T.SUB_UNLOCKED_WAITING.format(sec=row.countdown_sec)
        bg = YELLOW
        progress = _frac(row.countdown_sec, unlock_timeout)
    elif s == "OPEN":
        sub = T.SUB_OPEN
        bg = GREEN
    elif s == "CLOSING_CONFIRM":
        sub = T.SUB_CLOSING.format(sec=row.countdown_sec)
        bg = LIGHT_GREEN
        progress = _frac(row.countdown_sec, close_confirm)
    elif s == "COOLDOWN":
        sub = T.SUB_COOLDOWN.format(sec=row.cooldown_remaining_sec)
    elif s == "LOCKED_OPEN":
        sub = T.SUB_LOCKED_OPEN
        bg = ORANGE
    elif s == "SENSOR_FAULT":
        sub = T.SUB_SENSOR_FAULT
        bg, bold = YELLOW, True
    elif s == "FAULT":
        sub = T.SUB_FAULT
        bg = RED
    else:
        sub = T.SUB_UNKNOWN
        bg = YELLOW
    if row.sensorless and row.unlocked and row.countdown_kind == "hard":
        sub = T.SUB_TIMER_UNLOCKED.format(sec=row.countdown_sec)
        bg = GREEN
    small_parts: list[str] = []
    if row.owner_label:
        small_parts.append(T.PANEL_OWNER.format(owner=row.owner_label))
    if row.countdown_kind == "hard" and row.countdown_sec > 0 and not row.sensorless:
        small_parts.append(T.SUB_HARD.format(sec=row.countdown_sec))
    if row.sensor_injected:
        small_parts.append(T.SUB_SENSOR_INJECTED)
    return RowView(title, badge, sub, " · ".join(small_parts), bg, "#000000", BADGE_COLOR.get(bcol, "#d48806"),
                   bold, progress)


def _frac(remaining: float, total: float) -> float:
    return max(0.0, min(1.0, remaining / total)) if total > 0 else 0.0


def ascii_line(row: LockerPanelRow) -> str:
    """OpenCV fallback line (16.2): 'L2 OPEN unlocked' / 'L2 CLOSED locked' / 'L3 SENSOR?'."""
    name = "DOOR" if row.locker_id is None else f"L{row.locker_id}"
    if row.state in ("SENSOR_FAULT",):
        return f"{name} SENSOR?"
    if row.state == "FAULT":
        return f"{name} FAULT"
    if row.state == "UNKNOWN":
        return f"{name} ?"
    lock = "unlocked" if row.unlocked else "locked"
    if row.sensorless:
        return f"{name} {lock}"
    door = {"open": "OPEN", "closed": "CLOSED"}.get(row.door, "?")
    extra = f" {row.countdown_sec:.1f}s" if row.countdown_kind in ("unlock_wait", "close_confirm") else ""
    if row.state == "LOCKED_OPEN":
        return f"{name} OPEN !forced"
    return f"{name} {door} {lock}{extra}"


# ---------------------------------------------------------------------------------------------- state file
def _row_dict(r: LockerPanelRow) -> dict[str, Any]:
    return asdict(r)


def write_state(path: Path, rows: Sequence[LockerPanelRow], session_id: str) -> None:
    """Atomic replace of the shared state file (schema_version 2)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "ts": time.time(), "session_id": session_id,
               "rows": [_row_dict(r) for r in rows]}
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
    os.replace(tmp, path)


def unknown_rows(rows: Sequence[LockerPanelRow]) -> list[LockerPanelRow]:
    """Final rows written at shutdown: nothing may be mistaken for 'closed'."""
    return [LockerPanelRow(r.locker_id, "UNKNOWN", "unknown", False, "", 0.0, 0.0, None, False,
                           r.sensorless, False) for r in rows]


def read_state(path: Path, max_age_sec: float = 3.0) -> list[LockerPanelRow] | None:
    """Rows, or None when missing/stale/unreadable/schema != 2."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    try:
        age = time.time() - float(data["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    if age > max_age_sec or age < -max_age_sec:
        return None
    rows = []
    try:
        for d in data.get("rows", []):
            rows.append(LockerPanelRow(
                d.get("locker_id"), str(d["state"]), str(d["door"]), bool(d["unlocked"]),
                str(d.get("countdown_kind", "")), float(d.get("countdown_sec", 0.0)),
                float(d.get("cooldown_remaining_sec", 0.0)), d.get("owner_label"),
                bool(d.get("sim_toggle", False)), bool(d.get("sensorless", False)),
                bool(d.get("sensor_injected", False))))
    except (KeyError, TypeError, ValueError):
        return None
    return rows
