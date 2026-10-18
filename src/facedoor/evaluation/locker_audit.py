"""Per-locker routing + limit-switch lock audit (owner: eval-engineer).
Contract: docs/INTERFACES.md section 13.8 + 15 (v1.5 limit-switch control; also sections 5, 13.5, 15.5).

Inputs
- ``logs/events.csv``  (33/35/36/37-column layouts; read by column NAME, missing columns = "")
  v1.5 rows used: ``door_opened``, ``door_close`` (solenoid OFF, ``close_cause``), ``door_forced``, ``door_sensor``
  and column 37 ``locker_states``.
- ``logs/hardware.log`` (``<ts>\\t<source>\\t<EVENT>\\t<detail>``; per_locker channel rows start with
  ``locker=<id|-> pin=BCM<n|-> ``, REJECT rows end with ``code=<last_reject>``; CLOSE/WATCHDOG rows end with
  ``signal=OFF cause=<c> unlocked_for=<s>s door_open_for=<s>s open_for=<s>s``; STATE rows
  ``<FROM>-><TO> limit=pressed|released|none``; SENSOR rows ``sensor_fault|sensor_ok limit_pin=BCM<n> ...``)
- optional post-hoc labels (events.csv ``label`` column on ``event=open`` rows)
- optional probe ScoreRecords + gallery person.json locker_id (``locker_misroute_rate``)

Integrity metrics (must be 0; computed without labels): ``wrong_locker_opens``, ``extra_channel_on``,
``unassigned_opens``, ``watchdog_overruns`` (limit mode: > max_unlock_sec + 0.5 s, timer mode: > max_open_sec + 0.5 s)
and, in limit mode, ``on_while_closed_overtime`` (solenoid ON while the door kept reading closed for more than
``unlock_timeout_sec`` + 0.5 s). Report-only v1.5 metrics: ``unlock_timeouts``, ``watchdog_relocks``,
``sensor_faults``, ``forced_opens``, ``open_duration``. A metric that cannot be computed from the given inputs is
``None`` (never a made-up 0) and the reason is added to ``warnings``; ``on_while_closed_overtime`` is ``None`` and
ignored for ``integrity_ok`` in timer mode (it has no meaning without a limit switch).

These metrics prove only that the *logs* show correct routing. A swapped wire (BCM24 physically on locker 3) is
invisible here; INTERFACES 13.9 scenario 30 (physical check) is still required.

Pure python + stdlib (no numpy/matplotlib) so it can run on the Pi next to the logs.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- constants
EVENT_COLUMNS_V12: tuple[str, ...] = (
    "ts", "session_id", "frame_idx", "event", "verdict", "decision_state", "best_id", "best_score", "second_id",
    "second_score", "margin", "threshold", "threshold_group", "threshold_source", "matched_tag", "condition_tag",
    "hold_reason", "n_faces", "face_w", "face_h", "det_score", "blur_var", "brightness", "yaw_ratio", "liveness",
    "votes", "door_signal", "det_ms", "emb_ms", "total_ms", "embedder_id", "message", "label")
LOCKER_COLUMNS: tuple[str, ...] = ("locker_id", "open_lockers")          # v1.3 columns 34, 35
EVENT_COLUMNS_V13: tuple[str, ...] = EVENT_COLUMNS_V12 + LOCKER_COLUMNS
EVENT_COLUMNS_V14: tuple[str, ...] = EVENT_COLUMNS_V13 + ("close_cause",)  # v1.4 column 36
EVENT_COLUMNS_V15: tuple[str, ...] = EVENT_COLUMNS_V14 + ("locker_states",)  # v1.5 column 37 (INTERFACES 5)
KNOWN_LAYOUTS: dict[int, tuple[str, ...]] = {33: EVENT_COLUMNS_V12, 35: EVENT_COLUMNS_V13, 36: EVENT_COLUMNS_V14,
                                             37: EVENT_COLUMNS_V15}

# 13.8 + 15 (on_while_closed_overtime). Any positive value -> red banner + evaluate.py exit code 1.
INTEGRITY_KEYS: tuple[str, ...] = ("wrong_locker_opens", "extra_channel_on", "unassigned_opens", "watchdog_overruns",
                                   "on_while_closed_overtime")
# CloseCause values of INTERFACES 5 / 15 (v1.5)
CLOSE_CAUSES: tuple[str, ...] = ("unlock_timeout", "close_confirmed", "watchdog", "sensor_fault", "shutdown", "fault",
                                 "timer", "manual")
LIMIT_ONLY_CAUSES: frozenset[str] = frozenset({"unlock_timeout", "close_confirmed", "sensor_fault"})
CLOSED_STATES: frozenset[str] = frozenset({"UNLOCKED_WAITING", "CLOSING_CONFIRM"})   # solenoid ON, door reads closed
CLOSED_SLACK_SEC = 0.5          # on_while_closed_overtime: unlock_timeout_sec + 0.5 s
DEFAULT_MAX_UNLOCK_SEC = 30.0
DEFAULT_UNLOCK_TIMEOUT_SEC = 10.0
SUPPRESS_KEYS: tuple[str, ...] = ("no_locker_id", "invalid_locker_id", "unknown_locker", "shared_denied")
CRITICAL_REJECT_CODES: tuple[str, ...] = ("invariant", "fault")
IMPOSTOR_LABEL, IGNORE_LABEL = "_impostor", "_ignore"

MATCH_WINDOW_SEC = 1.0          # events.csv open row <-> hardware.log OPEN (same reason, |dt| <= 1 s)
WATCHDOG_SLACK_SEC = 0.5        # OPEN -> CLOSE/WATCHDOG must be <= max_open_sec + 0.5 s
MAX_DETAILS = 50                # incident descriptions kept per metric

_CHANNEL_RE = re.compile(r"^locker=(\S+) pin=BCM(\S+) ?(.*)$")
# trailing machine tokens of a hardware.log detail: signal=ON duration=3.000s / signal=OFF cause=.. unlocked_for=..s
# door_open_for=..s open_for=..s / code=.. / limit=pressed / limit_pin=BCM6 none_for=..s.
# The reason is everything before the first such token.
_TOKEN_RE = re.compile(r"(?:^|\s)(signal|duration|cause|open_for|unlocked_for|door_open_for|code|limit|limit_pin|"
                       r"none_for|valid_for|at_start)=(\S+)")
_STATE_RE = re.compile(r"^(\w+)->(\w+)$")
_REASON_LOCKER_RE = re.compile(r":locker=([^:\s]+)")
_REJECTED_RE = re.compile(r"\(rejected by door: ([^)]*)\)")
_MAXCONC_RE = re.compile(r"max_concurrent_open=(\d+)")
_CHANNELS_RE = re.compile(r"channels=(\S+)")


# --------------------------------------------------------------------------- small helpers
def parse_ts(s: str | None) -> datetime | None:
    """ISO8601 (``2026-10-16T09:30:12.345+09:00``) -> aware datetime. Invalid/empty -> None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _dt(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    try:
        return (b - a).total_seconds()
    except TypeError:  # naive vs aware
        return None


def reason_locker(reason: str | None) -> str | None:
    """``face:<pid>:<score>:<v>/<w>:locker=<L>`` -> ``L`` (last occurrence). None when absent."""
    found = _REASON_LOCKER_RE.findall(reason or "")
    return found[-1] if found else None


def reason_person(reason: str | None) -> str | None:
    """``face:<pid>:...`` -> ``pid``. None for other reasons."""
    r = (reason or "").strip()
    if not r.startswith("face:"):
        return None
    parts = r.split(":")
    return parts[1] if len(parts) > 1 and parts[1] else None


def split_lockers(v: str | None) -> list[str]:
    """``open_lockers`` column (``;`` separated) -> list (empty column -> [])."""
    return [x.strip() for x in (v or "").split(";") if x.strip()]


def message_reason(message: str | None) -> str:
    """The open reason inside an ``event=open`` message (success: message == reason; rejection:
    ``<reason> (rejected by door: X)``). Suppression messages have no reason -> ""."""
    m = (message or "").strip()
    if not m or m.startswith("open suppressed") or m.startswith("open decision without"):
        return ""
    i = m.find(" (rejected by door:")
    return m[:i].strip() if i >= 0 else m


def classify_suppression(message: str) -> str | None:
    """``open suppressed: ...`` message -> suppression key (INTERFACES 3.13 / 13.3). None if not a suppression."""
    m = (message or "").strip()
    if not m.startswith("open suppressed"):
        return None
    if "has no locker_id" in m:
        return "no_locker_id"
    if "locker_id invalid" in m:
        return "invalid_locker_id"
    if "not configured" in m:
        return "unknown_locker"
    if "shared with" in m:
        return "shared_denied"
    return "other"


# --------------------------------------------------------------------------- events.csv
def read_events(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read events.csv by column name. Accepts the 33/35/36/37-column (v1.2-v1.5) layouts
    (and any superset); columns missing from the file are returned as "" so callers never KeyError.
    Returns (original header, rows)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, restval="")
        header = list(reader.fieldnames or [])
        rows: list[dict[str, str]] = []
        for r in reader:
            row = {k: (v if v is not None else "") for k, v in r.items() if k is not None}
            for c in EVENT_COLUMNS_V15:
                row.setdefault(c, "")
            rows.append(row)
    return header, rows


# --------------------------------------------------------------------------- hardware.log
@dataclass(slots=True)
class HwLine:
    line_no: int
    ts: datetime | None
    ts_raw: str
    source: str
    event: str
    detail: str
    locker: str | None = None      # "-" in the log -> "" ; no channel token -> None
    pin: str | None = None
    reason: str = ""
    code: str = ""
    duration: float | None = None
    cause: str = ""                # CLOSE/WATCHDOG cause=<CloseCause>
    open_for: float | None = None  # open_for=<s>s (v1.4 name; v1.5 duplicates unlocked_for)
    unlocked_for: float | None = None      # v1.5 unlocked_for=<s>s (solenoid ON time)
    door_open_for: float | None = None     # v1.5 door_open_for=<s>s (0.000 = door never opened)
    state_from: str = ""           # v1.5 STATE row <FROM>-><TO>
    state_to: str = ""
    limit: str = ""                # v1.5 STATE row limit=pressed|released|none


def _float(v: str) -> float | None:
    try:
        f = float(v.rstrip("s"))
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def parse_hw_line(line: str, line_no: int = 0) -> HwLine | None:
    parts = line.rstrip("\r\n").split("\t", 3)
    if len(parts) < 3:
        return None
    if len(parts) == 3:  # tolerate the old 3-field form <ts>\t<EVENT>\t<detail>
        ts_raw, event, detail = parts
        source = ""
    else:
        ts_raw, source, event, detail = parts
    hl = HwLine(line_no=line_no, ts=parse_ts(ts_raw), ts_raw=ts_raw, source=source, event=event.strip().upper(),
                detail=detail)
    rest = detail
    m = _CHANNEL_RE.match(detail)
    if m:
        hl.locker = "" if m.group(1) == "-" else m.group(1)
        hl.pin = None if m.group(2) == "-" else m.group(2)
        rest = m.group(3)
    first = None
    for mt in _TOKEN_RE.finditer(rest):
        first = mt.start() if first is None else first
        key, val = mt.group(1), mt.group(2)
        if key == "code":
            hl.code = val
        elif key == "duration":
            hl.duration = _float(val)
        elif key == "cause":
            hl.cause = val
        elif key == "open_for":
            hl.open_for = _float(val)
        elif key == "unlocked_for":
            hl.unlocked_for = _float(val)
        elif key == "door_open_for":
            hl.door_open_for = _float(val)
        elif key == "limit":
            hl.limit = val
    hl.reason = (rest if first is None else rest[:first]).strip()
    if hl.event == "STATE":
        ms = _STATE_RE.match(hl.reason)
        if ms:
            hl.state_from, hl.state_to = ms.group(1), ms.group(2)
    return hl


def parse_hardware_log(path: Path) -> tuple[list[HwLine], list[str]]:
    out: list[HwLine] = []
    bad = 0
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            hl = parse_hw_line(line, i)
            if hl is None or hl.ts is None:
                bad += 1
                continue
            out.append(hl)
    warns = [f"hardware.log: 형식이 맞지 않는 행 {bad}개 무시"] if bad else []
    return out, warns


def _init_info(hl: HwLine) -> tuple[bool, int | None, set[str] | None]:
    """INIT detail -> (per_locker, max_concurrent_open, configured locker ids)."""
    per = "per_locker" in hl.detail
    mc = _MAXCONC_RE.search(hl.detail)
    ch = _CHANNELS_RE.search(hl.detail)
    lockers = None
    if ch:
        lockers = {tok.split(":")[0] for tok in ch.group(1).rstrip(";,").split(",") if tok.split(":")[0]}
    return per, (int(mc.group(1)) if mc else None), lockers


# --------------------------------------------------------------------------- gallery / probes
def gallery_lockers(gallery_dir: Path) -> dict[str, str | None]:
    """person_id -> person.json locker_id (string) or None (missing, empty, non-string = would be suppressed)."""
    out: dict[str, str | None] = {}
    g = Path(gallery_dir)
    if not g.is_dir():
        return out
    for pdir in sorted(p for p in g.iterdir() if p.is_dir() and not p.name.startswith("_")):
        pj = pdir / "person.json"
        v: Any = None
        if pj.is_file():
            try:
                v = json.loads(pj.read_text(encoding="utf-8")).get("locker_id")
            except (OSError, ValueError, AttributeError):
                v = None
        out[pdir.name] = v if isinstance(v, str) and v.strip() else None
    return out


def locker_misroute(records: Sequence[Any], lockers: Mapping[str, str | None]) -> dict[str, Any]:
    """Probe evaluation: genuine probes MATCHed to someone whose assigned locker differs from the probe person's
    own locker (= another person's locker would open). A MATCH to a person without a locker opens nothing and is
    not a misroute. Denominator = genuine probes (same as misid_rate)."""
    gen = [r for r in records if not getattr(r, "is_impostor", False)]
    n_mis = 0
    examples: list[str] = []
    for r in gen:
        if r.verdict != "MATCH" or not r.accepted_id or r.accepted_id == r.true_id:
            continue
        got = lockers.get(r.accepted_id)
        if got is not None and got != lockers.get(r.true_id):
            n_mis += 1
            if len(examples) < MAX_DETAILS:
                examples.append(f"{r.probe_path}: 실제 {r.true_id}(사물함 {lockers.get(r.true_id) or '없음'}) → "
                                f"{r.accepted_id}(사물함 {got})")
    n = len(gen)
    return {"locker_misroute_rate": (n_mis / n) if n else None, "n_misroute": n_mis, "n_genuine": n,
            "examples": examples}


# --------------------------------------------------------------------------- audit
@dataclass
class _Open:
    """One successful ("output really ON") open, from either source."""
    ts: datetime | None
    reason: str
    locker: str | None          # channel actually turned on (hardware) / events locker_id column
    src: str                    # "hw" | "ev"
    ref: str                    # human readable reference (file line / session:frame)
    flags: set[str] = field(default_factory=set)


def _ev_ref(r: Mapping[str, str]) -> str:
    return f"events {r.get('session_id', '')}:{r.get('frame_idx', '')}"


def _session_per_locker(rows: Sequence[Mapping[str, str]]) -> dict[str, bool]:
    per: dict[str, bool] = {}
    for r in rows:
        s = r.get("session_id", "")
        hit = bool(split_lockers(r.get("open_lockers")))
        if any(x.split("=")[0].strip() not in ("", "-") for x in split_lockers(r.get("locker_states"))):
            hit = True          # v1.5 column 37 "<id>=<state>;..." ; single mode writes "-=<state>"
        msg = r.get("message", "") or ""
        if r.get("event") == "start" and "per_locker" in msg:
            hit = True
        if "(lockers.mode=per_locker)" in msg or "not configured in hardware.locker_outputs" in msg:
            hit = True
        per[s] = per.get(s, False) or hit
    return per


def _is_real_open_row(r: Mapping[str, str]) -> bool:
    """event=open row whose decision really turned an output on (not suppressed, not rejected)."""
    if r.get("event") != "open" or (r.get("door_signal") or "").strip() != "1":
        return False
    msg = r.get("message", "") or ""
    return not (msg.startswith("open suppressed") or msg.startswith("open decision without") or
                _REJECTED_RE.search(msg))


def audit_lockers(events: Path | Sequence[Mapping[str, str]] | None = None,
                  hardware: Path | Sequence[HwLine] | None = None, *,
                  max_open_sec: float | None = None, max_concurrent_open: int | None = None,
                  configured_lockers: Iterable[str] | None = None, mode: str | None = None,
                  match_window_sec: float = MATCH_WINDOW_SEC, watchdog_slack_sec: float = WATCHDOG_SLACK_SEC,
                  max_unlock_sec: float | None = None, unlock_timeout_sec: float | None = None,
                  close_mode: str | None = None) -> dict[str, Any]:
    """Compute the INTERFACES 13.8 + 15 audit. ``events``/``hardware`` may be paths or already-parsed rows/lines.

    ``max_open_sec`` (timer mode) / ``max_unlock_sec`` + ``unlock_timeout_sec`` (limit mode) /
    ``max_concurrent_open`` / ``configured_lockers`` / ``mode`` / ``close_mode`` ("limit" | "timer" | None = infer from
    the logs) come from the config; the INIT lines of hardware.log override max_concurrent_open and the configured
    channel list for their own process run. Returns a JSON-ready dict (see ``metrics.json`` key ``locker``)."""
    warns: list[str] = []
    ev_path = hw_path = None
    rows: list[Mapping[str, str]] = []
    hw: list[HwLine] = []
    ev_cols: int | None = None
    if events is not None:
        if isinstance(events, (str, Path)):
            ev_path = str(events)
            header, rows = read_events(Path(events))
            ev_cols = len(header)
            if not set(LOCKER_COLUMNS) <= set(header):
                warns.append(f"events.csv가 {len(header)}컬럼(v1.2 형식): locker_id/open_lockers 컬럼이 없어 "
                             "사물함 라우팅 검사는 hardware.log에만 의존함")
        else:
            rows = list(events)
    if hardware is not None:
        if isinstance(hardware, (str, Path)):
            hw_path = str(hardware)
            hw, w = parse_hardware_log(Path(hardware))
            warns.extend(w)
        else:
            hw = list(hardware)

    cfg_lockers = {str(x) for x in configured_lockers} if configured_lockers is not None else None
    cfg_lockers = cfg_lockers or None
    sess_per = _session_per_locker(rows)
    hw_per = any(h.locker is not None for h in hw) or any(h.event == "INIT" and "per_locker" in h.detail for h in hw)
    per_locker_data = any(sess_per.values()) or hw_per or (mode == "per_locker")

    init_mc = [mc for h in hw if h.event == "INIT" for mc in [_init_info(h)[1]] if mc is not None]
    mc_global = max_concurrent_open if max_concurrent_open is not None else (max(init_mc) if init_mc else None)
    if mc_global is None:
        mc_global = 1
        warns.append("max_concurrent_open 설정·INIT 기록 없음 → 기본값 1로 동시 개방 검사")

    details: dict[str, list[str]] = {k: [] for k in INTEGRITY_KEYS}

    def note(key: str, text: str) -> None:
        if len(details[key]) < MAX_DETAILS:
            details[key].append(text)

    # ------------------------------------------------ limit (v1.5) vs timer mode
    ev_door_names = ("door_opened", "door_forced", "door_sensor")
    has_state = any(h.event == "STATE" for h in hw)
    cues = (has_state or any(h.event == "SENSOR" or h.cause in LIMIT_ONLY_CAUSES for h in hw) or
            any(r.get("event") in ev_door_names or (r.get("close_cause") or "").strip() in LIMIT_ONLY_CAUSES
                or bool((r.get("locker_states") or "").strip()) for r in rows))
    is_limit = (close_mode == "limit") or (close_mode is None and cues)
    if is_limit:
        mu = float(max_unlock_sec) if max_unlock_sec is not None else DEFAULT_MAX_UNLOCK_SEC
        ut = float(unlock_timeout_sec) if unlock_timeout_sec is not None else DEFAULT_UNLOCK_TIMEOUT_SEC
        if max_unlock_sec is None:
            warns.append(f"max_unlock_sec 설정 없음 → 기본 {DEFAULT_MAX_UNLOCK_SEC:g}초로 watchdog_overruns 검사")
        if unlock_timeout_sec is None:
            warns.append(f"unlock_timeout_sec 설정 없음 → 기본 {DEFAULT_UNLOCK_TIMEOUT_SEC:g}초로 on_while_closed_overtime 검사")
        limit: float | None = mu + watchdog_slack_sec
        closed_limit: float | None = ut + CLOSED_SLACK_SEC
        if close_mode is None:
            warns.append("close_mode 설정 없음 → 로그의 STATE/SENSOR/cause 토큰으로 limit 모드로 추정")
    else:
        mu = ut = None  # type: ignore[assignment]
        limit = (float(max_open_sec) + watchdog_slack_sec) if max_open_sec is not None else None
        closed_limit = None
        if close_mode == "timer":
            warns.append("close_mode=timer: 리밋센서가 없어 on_while_closed_overtime은 계산 대상이 아님(무결성 판정에서 제외)")
    limit_kind = "max_unlock_sec" if is_limit else "max_open_sec"

    # ------------------------------------------------ hardware.log walk
    hw_opens: list[_Open] = []
    extra_hw = overruns = 0
    unclosed: list[str] = []
    rejected_hw: Counter = Counter()
    faults = 0
    seg_mc, seg_lockers = mc_global, cfg_lockers
    open_now: dict[str, tuple[HwLine, _Open]] = {}       # locker ("" for single/unknown) -> OPEN line
    hw_causes: Counter = Counter()
    hw_durations: list[float] = []
    legacy_cause = 0
    hw_close_rows: list[tuple[str, float | None, float | None, str, HwLine]] = []   # cause, unlocked_for, door_open_for
    # on_while_closed_overtime bookkeeping (hardware.log STATE rows): key -> (segment start ts, ref)
    closed_since: dict[str, tuple[datetime | None, str]] = {}
    closed_over_hw = 0
    closed_max_hw = 0.0
    n_closed_segments = 0
    sensor_faults_hw = forced_hw = 0

    def hw_cause(h: HwLine) -> str:
        nonlocal legacy_cause
        if h.cause:
            return h.cause
        legacy_cause += 1
        if h.event == "WATCHDOG":   # v1.3 log: open_sec expiry was written as WATCHDOG "timeout"
            return "watchdog" if (is_limit or h.reason == "watchdog") else "timer"
        return {"CLEANUP": "shutdown", "FAULT": "fault"}.get(h.event, "manual")

    def end_closed_segment(key: str, ts: datetime | None, why: str) -> None:
        """Solenoid ON + door reads closed ended (door opened / solenoid OFF): judge its length."""
        nonlocal closed_over_hw, closed_max_hw, n_closed_segments
        seg = closed_since.pop(key, None)
        if seg is None or closed_limit is None:
            return
        dur = _dt(seg[0], ts)
        if dur is None:
            return
        n_closed_segments += 1
        closed_max_hw = max(closed_max_hw, dur)
        if dur > closed_limit:
            closed_over_hw += 1
            note("on_while_closed_overtime", f"{seg[1]} locker={key or '-'}: 솔밸브 ON + 문 닫힘이 {dur:.3f}s 지속"
                                             f"({why}) > unlock_timeout_sec+{CLOSED_SLACK_SEC:g}s = {closed_limit:.3f}s")

    def close(key: str, h: HwLine, record: bool = True) -> None:
        nonlocal overruns
        item = open_now.pop(key, None)
        if item is None:
            return
        oline, _ = item
        dt = _dt(oline.ts, h.ts)
        if limit is not None and dt is not None and dt > limit:
            overruns += 1
            note("watchdog_overruns", f"hardware.log {oline.line_no}행 OPEN locker={key or '-'} → "
                                      f"{h.line_no}행 {h.event}: {dt:.3f}s > {limit_kind}+{watchdog_slack_sec:g}s = {limit:.3f}s")
        end_closed_segment(key, h.ts, f"{h.line_no}행 {h.event}")
        if not record:
            return
        cause = hw_cause(h)
        hw_causes[cause] += 1
        if h.unlocked_for is not None and h.event in ("CLOSE", "WATCHDOG"):
            dur = h.unlocked_for
        elif h.open_for is not None and h.event in ("CLOSE", "WATCHDOG"):
            dur = h.open_for
        else:
            dur = dt
        if dur is not None:
            hw_durations.append(dur)
        hw_close_rows.append((cause, h.unlocked_for if h.unlocked_for is not None else dur, h.door_open_for, key, h))

    for h in hw:
        if h.event == "INIT":
            for key in list(open_now):
                unclosed.append(f"hardware.log {open_now[key][0].line_no}행 OPEN locker={key or '-'}: 닫힘 기록 없이 재시작")
            open_now.clear()
            closed_since.clear()         # a process restart ends every segment (no verdict: the end is unknown)
            _, mc, lk = _init_info(h)
            seg_mc = max_concurrent_open if max_concurrent_open is not None else (mc if mc is not None else mc_global)
            seg_lockers = lk if lk else cfg_lockers
        elif h.event == "OPEN":
            key = h.locker if h.locker is not None else ""
            op = _Open(ts=h.ts, reason=h.reason, locker=h.locker, src="hw", ref=f"hardware.log {h.line_no}행")
            hw_opens.append(op)
            r_l = reason_locker(h.reason)
            if h.locker is not None and h.locker and r_l is not None and r_l != h.locker:
                op.flags.add("wrong")
                note("wrong_locker_opens", f"{op.ref}: 열린 사물함 locker={h.locker}, 사유 {h.reason} (locker={r_l})")
            if h.locker is not None and (h.locker == "" or (seg_lockers is not None and h.locker not in seg_lockers)):
                op.flags.add("unassigned")
                note("unassigned_opens", f"{op.ref}: 설정에 없는 사물함/빈 locker 개방 locker={h.locker or '-'}")
            if key in open_now:
                warns.append(f"hardware.log {h.line_no}행: 이미 열린 채널 locker={key or '-'}에 OPEN 재기록")
                close(key, h, record=False)
            open_now[key] = (h, op)
            closed_since.setdefault(key, (h.ts, f"hardware.log {h.line_no}행"))   # unlock accepted only while closed
            if len(open_now) > seg_mc:
                extra_hw += 1
                note("extra_channel_on", f"hardware.log {h.line_no}행: 동시 ON {len(open_now)}개 "
                                         f"({', '.join(sorted(k or '-' for k in open_now))}) > 상한 {seg_mc}")
        elif h.event in ("CLOSE", "WATCHDOG"):
            if h.locker:  # specific channel
                close(h.locker, h)
            else:         # single mode, or close(locker_id=None) = all channels
                for key in list(open_now):
                    close(key, h)
        elif h.event in ("CLEANUP", "FAULT"):
            if h.event == "FAULT":
                faults += 1
            for key in list(open_now):
                close(key, h)
            for key in list(closed_since):
                end_closed_segment(key, h.ts, f"{h.line_no}행 {h.event}")
        elif h.event == "REJECT":
            code = h.code or "unspecified"
            rejected_hw[code] += 1
        elif h.event == "STATE":
            key = h.locker or ""
            if h.state_to in CLOSED_STATES:
                closed_since.setdefault(key, (h.ts, f"hardware.log {h.line_no}행"))
            else:   # OPEN (door opened) / COOLDOWN / LOCKED_* / SENSOR_FAULT: the closed-while-ON stretch is over
                end_closed_segment(key, h.ts, f"{h.line_no}행 {h.state_from}->{h.state_to}")
            if h.state_from == "LOCKED_CLOSED" and h.state_to == "LOCKED_OPEN":
                forced_hw += 1
        elif h.event == "SENSOR":
            if "sensor_fault" in h.reason.split():
                sensor_faults_hw += 1
    for key, (oline, _) in open_now.items():
        unclosed.append(f"hardware.log {oline.line_no}행 OPEN locker={key or '-'}: 로그 끝까지 닫힘 기록 없음")
    if unclosed:
        warns.append(f"닫힘 기록이 없는 OPEN {len(unclosed)}건(프로그램 실행 중이거나 비정상 종료): " + "; ".join(unclosed[:5]))

    # ------------------------------------------------ events.csv walk
    ev_opens: list[tuple[Mapping[str, str], _Open]] = []
    extra_ev = 0
    suppressed: Counter = Counter({k: 0 for k in SUPPRESS_KEYS})
    rejected_ev: Counter = Counter()
    ev_causes: Counter = Counter()
    ev_durations: list[float] = []
    # v1.5 door_* event rows (fallback source for the closed-while-ON metric when hardware.log has no STATE rows)
    ev_closed: list[tuple[str, float, str]] = []     # (kind "close"|"opened", ON+closed seconds, reference)
    ev_sensor_faults = ev_forced = 0
    for r in rows:
        ev = r.get("event", "")
        msg = r.get("message", "") or ""
        ol = split_lockers(r.get("open_lockers"))
        sess = r.get("session_id", "")
        if len(ol) > mc_global:
            extra_ev += 1
            note("extra_channel_on", f"{_ev_ref(r)} ({ev}): open_lockers={';'.join(ol)} {len(ol)}개 > 상한 {mc_global}")
        if ev == "door_close":
            toks = dict(mt.groups() for mt in _TOKEN_RE.finditer(msg))
            cause = (r.get("close_cause") or "").strip() or toks.get("cause", "") or "unspecified"
            ev_causes[cause] += 1
            of = _float(toks["unlocked_for"]) if "unlocked_for" in toks else (
                _float(toks["open_for"]) if "open_for" in toks else None)
            if of is not None:
                ev_durations.append(of)
            # door never opened (door_open_for=0 or cause=unlock_timeout) -> whole unlocked time was "ON + closed"
            dof = _float(toks["door_open_for"]) if "door_open_for" in toks else None
            if of is not None and (cause == "unlock_timeout" or (dof is not None and dof == 0.0 and cause != "shutdown")):
                ev_closed.append(("close", of, f"{_ev_ref(r)} door_close cause={cause}: 문이 안 열린 채"))
            continue
        if ev == "door_opened":
            of = _float(dict(mt.groups() for mt in _TOKEN_RE.finditer(msg)).get("unlocked_for", ""))
            if of is not None:
                ev_closed.append(("opened", of, f"{_ev_ref(r)} door_opened: 해제 후 문이 열릴 때까지"))
            continue
        if ev == "door_forced":
            ev_forced += 1
            continue
        if ev == "door_sensor":
            if "sensor_fault" in msg:
                ev_sensor_faults += 1
            continue
        if ev != "open":
            continue
        sk = classify_suppression(msg)
        if sk is not None:
            suppressed[sk] += 1
            continue
        mr = _REJECTED_RE.search(msg)
        if mr:
            for code in re.split(r"[/,\s]+", mr.group(1).strip()):
                if code:
                    rejected_ev[code] += 1
            continue
        if not _is_real_open_row(r):
            continue
        per = sess_per.get(r.get("session_id", ""), False) or bool(ol)
        lk = (r.get("locker_id") or "").strip()
        reason = message_reason(msg)
        op = _Open(ts=parse_ts(r.get("ts")), reason=reason, locker=lk or None, src="ev", ref=_ev_ref(r))
        ev_opens.append((r, op))
        if not per:
            continue  # single mode: one output, routing does not apply
        r_l = reason_locker(reason)
        if lk and r_l is not None and r_l != lk:
            op.flags.add("wrong")
            note("wrong_locker_opens", f"{op.ref}: locker_id={lk}, 사유 {reason} (locker={r_l})")
        elif lk and ol and lk not in ol:
            op.flags.add("wrong")
            note("wrong_locker_opens", f"{op.ref}: door_signal=1인데 대상 locker_id={lk}가 open_lockers={';'.join(ol)}에 없음")
        if not lk or (cfg_lockers is not None and lk not in cfg_lockers):
            op.flags.add("unassigned")
            note("unassigned_opens", f"{op.ref}: 출력 ON인데 locker_id={lk or '(빈칸)'} (설정 {sorted(cfg_lockers or [])})")

    # ------------------------------------------------ correspondence events <-> hardware OPEN
    used: set[int] = set()
    matched: dict[int, int] = {}       # ev index -> hw index
    for ei, (r, eo) in enumerate(ev_opens):
        if not eo.reason:
            continue
        best, best_dt = None, None
        for hi, ho in enumerate(hw_opens):
            if hi in used or ho.reason != eo.reason:
                continue
            d = _dt(ho.ts, eo.ts)
            if d is None or abs(d) > match_window_sec:
                continue
            if best_dt is None or abs(d) < best_dt:
                best, best_dt = hi, abs(d)
        if best is not None:
            used.add(best)
            matched[ei] = best
            ho = hw_opens[best]
            if eo.locker and ho.locker and ho.locker != eo.locker and "wrong" not in ho.flags:
                ho.flags.add("wrong")
                note("wrong_locker_opens", f"{ho.ref}: 열린 사물함 locker={ho.locker} ≠ {eo.ref} locker_id={eo.locker}")
    if hw and rows:
        n_un_ev = sum(1 for ei in range(len(ev_opens)) if ei not in matched)
        n_un_hw = sum(1 for hi, ho in enumerate(hw_opens) if hi not in used and ho.reason.startswith("face:"))
        if n_un_ev:
            warns.append(f"events.csv 실제 개방 {n_un_ev}건이 hardware.log OPEN과 대응되지 않음(사유·시각 ±{match_window_sec:g}s). "
                         "다른 기간/장치의 로그일 수 있음")
        if n_un_hw:
            warns.append(f"hardware.log 얼굴 인식 OPEN {n_un_hw}건이 events.csv 행과 대응되지 않음")

    def count(flag: str) -> int:
        n = sum(1 for ho in hw_opens if flag in ho.flags)
        for ei, (_, eo) in enumerate(ev_opens):
            if flag in eo.flags and not (ei in matched and flag in hw_opens[matched[ei]].flags):
                n += 1
        return n

    # on_while_closed_overtime: STATE rows (exact) > hardware.log CLOSE rows (door never opened) + events door_opened
    # > events door_close/door_opened only. Timer mode: not applicable (None, excluded from integrity_ok).
    closed_over: int | None
    closed_src: str | None
    closed_n = 0
    closed_max: float | None = None

    def _over(items: list[tuple[str, float, str]]) -> int:
        n = 0
        for _kind, dur, ref in items:
            if closed_limit is not None and dur > closed_limit:
                n += 1
                note("on_while_closed_overtime", f"{ref} {dur:.3f}s 지속 > unlock_timeout_sec+{CLOSED_SLACK_SEC:g}s = "
                                                 f"{closed_limit:.3f}s")
        return n

    if not is_limit:
        closed_over, closed_src = None, None
    elif has_state:
        closed_over, closed_src, closed_n, closed_max = closed_over_hw, "hardware_log_state", n_closed_segments, closed_max_hw
    elif hw:
        items = [("close", float(ul), f"hardware.log {h.line_no}행 {h.event} cause={c}: 문이 안 열린 채")
                 for c, ul, dof, _k, h in hw_close_rows
                 if ul is not None and (c == "unlock_timeout" or (dof is not None and dof == 0.0 and c != "shutdown"))]
        items += [it for it in ev_closed if it[0] == "opened"]
        closed_over, closed_src, closed_n = _over(items), "hardware_log_close", len(items)
        closed_max = max((i[1] for i in items), default=None)
        warns.append("hardware.log에 STATE 행이 없어 on_while_closed_overtime을 CLOSE 행의 unlocked_for/door_open_for(+events "
                     "door_opened)로 근사함 (문이 안 열린 채 잠긴 건만 검출)")
    elif rows and any(r.get("event") in ("door_close", "door_opened") for r in rows):
        closed_over, closed_src, closed_n = _over(ev_closed), "events", len(ev_closed)
        closed_max = max((i[1] for i in ev_closed), default=None)
    else:
        closed_over, closed_src = None, None
        warns.append("on_while_closed_overtime 계산 불가: hardware.log STATE 행도 events.csv door_* 행도 없음")

    integrity: dict[str, int | None] = {
        "wrong_locker_opens": count("wrong") if (rows or hw) else None,
        "extra_channel_on": (extra_ev + extra_hw) if (rows or hw) else None,
        "unassigned_opens": count("unassigned") if (rows or hw) else None,
        "watchdog_overruns": overruns if (hw and limit is not None) else None,
        "on_while_closed_overtime": closed_over,
    }

    causes = hw_causes if hw else ev_causes
    durations = hw_durations if hw else ev_durations
    n_close = sum(causes.values())
    if hw and legacy_cause:
        warns.append(f"hardware.log 닫힘 {legacy_cause}건에 cause= 토큰 없음(v1.3 형식) → 이벤트 종류로 원인 추정")
    if not hw and rows and not any((r.get("close_cause") or "").strip() for r in rows if r.get("event") == "door_close"):
        if ev_causes:
            warns.append("events.csv door_close 행에 close_cause가 없음(36컬럼 이전 형식) → 닫힘 원인 분포는 message로만 추정")
    if not hw:
        warns.append("hardware.log 없음(--hardware-log 미지정) → watchdog_overruns 계산 불가, 실제 출력 기준 검사 생략")
    elif limit is None:
        warns.append("max_open_sec 정보 없음 → watchdog_overruns 계산 불가")
    if cfg_lockers is None and not any(h.event == "INIT" and _init_info(h)[2] for h in hw):
        warns.append("설정된 사물함 목록(hardware.locker_outputs/INIT channels)을 알 수 없어 unassigned_opens는 빈 locker_id만 검사")
    if not rows:
        warns.append("events.csv 없음 → 억제 건수·라벨 기반 지표 계산 불가")

    # timer mode: on_while_closed_overtime has no meaning (no limit switch) -> not part of the verdict
    vals = [v for k, v in integrity.items() if not (k == "on_while_closed_overtime" and not is_limit)]
    if any(v for v in vals if v is not None):
        ok: bool | None = False
    elif any(v is None for v in vals):
        ok = None
    else:
        ok = True

    # ------------------------------------------------ labels (security incidents)
    misid = impostor = n_lab = 0
    lab_examples: list[str] = []
    for r, eo in ev_opens:
        label = (r.get("label") or "").strip()
        if not label or label == IGNORE_LABEL:
            continue
        n_lab += 1
        if label == IMPOSTOR_LABEL:
            impostor += 1
            lab_examples.append(f"{eo.ref}: 미등록자(_impostor)에게 개방 ({eo.reason})")
            continue
        who = reason_person(eo.reason) or (r.get("best_id") or "").strip()
        if who and who != label:
            misid += 1
            lab_examples.append(f"{eo.ref}: 실제 {label} → {who}의 사물함 {eo.locker or '-'} 개방")

    rejected = rejected_hw if hw else rejected_ev
    per_locker_opens: Counter = Counter()
    for ho in hw_opens:
        per_locker_opens[ho.locker or "-"] += 1
    if not hw:
        for _, eo in ev_opens:
            per_locker_opens[eo.locker or "-"] += 1
    init_lockers: set[str] = set()
    for h in hw:
        if h.event == "INIT":
            init_lockers |= _init_info(h)[2] or set()
    shown_lockers = sorted(cfg_lockers) if cfg_lockers else (sorted(init_lockers) if init_lockers else None)
    crit = {c: int(rejected.get(c, 0)) for c in CRITICAL_REJECT_CODES}
    if any(crit.values()) or faults:
        warns.append(f"하드웨어 거부 invariant={crit['invariant']} fault={crit['fault']}, FAULT 기록 {faults}건: 0이어야 함 "
                     "(출력 불변식 위반/구동 실패 — 배선·드라이버 점검)")

    return {
        "per_locker_data": bool(per_locker_data),
        "sources": {"events": ev_path, "events_columns": ev_cols, "hardware_log": hw_path},
        "integrity": integrity,
        "integrity_ok": ok,
        "details": {k: v for k, v in details.items() if v},
        "suppressed": dict(suppressed),
        "rejected": dict(sorted(rejected.items())),
        "rejected_source": "hardware_log" if hw else ("events" if rows else None),
        "critical_rejects": crit,
        "faults": faults,
        "actual_opens": {"events": len(ev_opens) if rows else None, "hardware_log": len(hw_opens) if hw else None},
        "per_locker_opens": dict(sorted(per_locker_opens.items())),
        "unclosed_opens": len(unclosed),
        "close_causes": dict(sorted(causes.items())),
        "close_causes_source": "hardware_log" if hw else ("events" if rows else None),
        "n_closes": n_close,
        "watchdog_ratio": (causes.get("watchdog", 0) / n_close) if n_close else None,
        "open_duration": duration_stats(durations),
        "unlock_timeouts": causes.get("unlock_timeout", 0) if causes else (0 if (hw or rows) else None),
        "watchdog_relocks": causes.get("watchdog", 0) if causes else (0 if (hw or rows) else None),
        "sensor_faults": (sensor_faults_hw if hw else ev_sensor_faults) if (hw or rows) else None,
        "forced_opens": (forced_hw if has_state else ev_forced) if (has_state or rows) else None,
        "on_while_closed": {"source": closed_src, "n_segments": closed_n,
                            "max_sec": closed_max, "limit_sec": closed_limit},
        "misid_locker_opens": misid if n_lab else None,
        "impostor_locker_opens": impostor if n_lab else None,
        "n_labelled_opens": n_lab,
        "label_incidents": lab_examples[:MAX_DETAILS],
        "locker_misroute_rate": None,
        "locker_misroute": None,
        "params": {"max_open_sec": max_open_sec, "max_unlock_sec": mu, "unlock_timeout_sec": ut,
                   "max_concurrent_open": mc_global,
                   "configured_lockers": shown_lockers,
                   "configured_lockers_source": "config" if cfg_lockers else ("hardware.log INIT" if init_lockers else None),
                   "match_window_sec": match_window_sec, "watchdog_slack_sec": watchdog_slack_sec,
                   "close_mode": close_mode, "mode_used": "limit" if is_limit else "timer"},
        "warnings": warns,
    }


def _quantile(sorted_v: Sequence[float], q: float) -> float:
    """Linear interpolation quantile (same as numpy's default) on a sorted non-empty sequence."""
    pos = (len(sorted_v) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_v) - 1)
    return float(sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (pos - lo))


def duration_stats(values: Iterable[float]) -> dict[str, float | int | None]:
    """Open durations (s) -> {n, mean, p50, p95, max}; None values when there is no closed open."""
    v = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not v:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {"n": len(v), "mean": sum(v) / len(v), "p50": _quantile(v, 0.5), "p95": _quantile(v, 0.95), "max": v[-1]}


def integrity_failed(locker: Mapping[str, Any] | None) -> bool:
    """True when any integrity metric (INTEGRITY_KEYS) is a positive number (evaluate.py exit code 1)."""
    if not locker:
        return False
    integ = locker.get("integrity") or {}
    return any(isinstance(integ.get(k), (int, float)) and not (isinstance(integ.get(k), float) and math.isnan(integ[k]))
               and integ.get(k) > 0 for k in INTEGRITY_KEYS)
