"""person -> locker mapping, resolution and assignment checks (owner: vision-engineer). Contract: INTERFACES 13.3.

- Mapping is stored in ``data/gallery/<pid>/person.json`` as ``"locker_id": "<str>"`` (null = none).
- ``LockerMap`` takes ONE snapshot of every person.json at build time (app start). Changes need a restart.
- This module must not import ``facedoor.hardware``: configured locker ids come from the config values only
  (``configured_lockers(cfg.hardware)`` reads ``hardware.locker_outputs[*].locker_id`` by attribute).
- Every rule here only ADDS restrictions. Nothing in this module can make an open happen that the vote/threshold
  logic did not already decide.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

MODE_SINGLE = "single"
MODE_PER_LOCKER = "per_locker"


class LockerCheck(str, Enum):
    OK = "ok"                                 # open allowed
    NOT_REQUIRED = "not_required"             # single + require_locker_id=false: allowed regardless of locker
    NO_LOCKER_ID = "no_locker_id"
    INVALID_LOCKER_ID = "invalid_locker_id"   # non-string / pattern mismatch (manual edit)
    UNKNOWN_LOCKER = "unknown_locker"         # not in hardware.locker_outputs
    SHARED_DENIED = "shared_denied"           # shared with someone else while allow_shared=false


ALLOWED_CHECKS = frozenset({LockerCheck.OK, LockerCheck.NOT_REQUIRED})


@dataclass(frozen=True, slots=True)
class LockerResolution:
    person_id: str
    locker_id: str | None      # person.json raw value as text (events.csv column 34); None = absent/null/""
    check: LockerCheck
    allowed: bool              # check in {OK, NOT_REQUIRED}
    target: str | None         # per_locker: door.open(locker_id=target). single: None
    message: str               # "" when allowed, otherwise the suppression message (INTERFACES 3.13)


@dataclass(frozen=True, slots=True)
class AssignmentIssue:
    """Structured form of one check_assignment() error (GUI maps ``code`` to Korean text)."""
    code: str                  # "pattern" | "not_configured" | "taken" | "shared_needs_flag"
    message: str               # English, used by CLI
    owners: tuple[str, ...] = ()


# ----------------------------------------------------------------------------- helpers
def configured_lockers(hw_cfg: Any) -> tuple[str, ...]:
    """Locker ids declared in hardware.locker_outputs (declaration order). () in single mode / old hw code."""
    outs = getattr(hw_cfg, "locker_outputs", None) or ()
    ids: list[str] = []
    for o in outs:
        lid = o.get("locker_id") if isinstance(o, dict) else getattr(o, "locker_id", None)
        if lid is not None:
            ids.append(str(lid))
    return tuple(ids)


def _raw_text(raw: Any) -> str | None:
    """person.json raw locker_id as text for logging. None/"" -> None; non-strings JSON-encoded (12 -> "12")."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=True)
    except (TypeError, ValueError):
        return repr(raw)


def _pattern_ok(value: str, pattern: str) -> bool:
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        return False


def _mode(cfg: Any) -> str:
    return str(getattr(cfg, "mode", MODE_SINGLE))


def _read_raw(db: Any, person_id: str) -> Any:
    """Raw person.json locker_id. Works with FaceDatabase or minimal fakes exposing only locker_id()."""
    if db is None:
        return None
    info_fn = getattr(db, "person_info", None)
    if callable(info_fn):
        try:
            info = info_fn(person_id)
        except Exception:  # noqa: BLE001 - unreadable -> treated as no locker (fail-closed in per_locker)
            log.exception("cannot read person info for %s", person_id)
            return None
        if isinstance(info, dict):
            if "locker_ids" in info:
                log.warning("person %s: 'locker_ids' (list) is reserved and ignored; only 'locker_id' is used",
                            person_id)
            return info.get("locker_id")
        return None
    lid_fn = getattr(db, "locker_id", None)
    if callable(lid_fn):
        try:
            return lid_fn(person_id)
        except Exception:  # noqa: BLE001
            log.exception("cannot read locker_id for %s", person_id)
    return None


# ----------------------------------------------------------------------------- LockerMap
class LockerMap:
    """Snapshot of person -> locker assignments plus the routing rules of INTERFACES 13.3/13.4."""

    def __init__(self, mode: str, require_locker_id: bool, pattern: str, allow_shared: bool,
                 configured: Sequence[str], raw: dict[str, Any], db: Any = None) -> None:
        self.mode = mode
        self.require_locker_id = bool(require_locker_id)
        self.pattern = pattern
        self.allow_shared = bool(allow_shared)
        self.configured: tuple[str, ...] = tuple(configured)
        self._raw: dict[str, Any] = dict(raw)
        self._db = db  # only used for persons missing from the snapshot (fake dbs in tests)
        self._owners: dict[str, list[str]] = {}
        self._rebuild_owners()

    @classmethod
    def build(cls, db: Any, cfg: Any, configured: Sequence[str]) -> "LockerMap":
        """configured = [o.locker_id for o in hw.locker_outputs] (single mode ()). Reads person.json once."""
        mode = _mode(cfg)
        raw: dict[str, Any] = {}
        ids_fn = getattr(db, "all_person_ids", None) if db is not None else None
        if callable(ids_fn):
            for pid in ids_fn():
                raw[pid] = _read_raw(db, pid)
        require = bool(getattr(cfg, "require_locker_id", False))
        if mode == MODE_PER_LOCKER and not require:
            log.info("lockers.mode=per_locker: require_locker_id is treated as true")
        return cls(mode, require, str(getattr(cfg, "locker_id_pattern", r"^[A-Za-z0-9_-]{1,16}$")),
                   bool(getattr(cfg, "allow_shared", False)), configured, raw, db)

    # ---------------------------------------------------------------- internals
    def _valid_str(self, raw: Any) -> str | None:
        """The usable locker id (string + pattern), else None."""
        if isinstance(raw, str) and raw != "" and _pattern_ok(raw, self.pattern):
            return raw
        return None

    def _rebuild_owners(self) -> None:
        owners: dict[str, list[str]] = {}
        for pid in sorted(self._raw):
            lid = self._valid_str(self._raw[pid])
            if lid is not None:
                owners.setdefault(lid, []).append(pid)
        self._owners = owners

    def _raw_for(self, person_id: str) -> Any:
        if person_id not in self._raw:
            # not in the start-up snapshot (e.g. minimal fake db): read once and keep it
            self._raw[person_id] = _read_raw(self._db, person_id)
            self._rebuild_owners()
        return self._raw[person_id]

    # ---------------------------------------------------------------- API
    def resolve(self, person_id: str) -> LockerResolution:
        raw = self._raw_for(person_id)
        text = _raw_text(raw)

        def deny(check: LockerCheck, msg: str) -> LockerResolution:
            return LockerResolution(person_id, text, check, False, None, msg)

        if self.mode != MODE_PER_LOCKER:
            if not self.require_locker_id:
                return LockerResolution(person_id, text, LockerCheck.NOT_REQUIRED, True, None, "")
            # v1.2 semantics: only presence matters (a non-string value counts as absent, fail-closed)
            if not isinstance(raw, str) or raw.strip() == "":
                return deny(LockerCheck.NO_LOCKER_ID,
                            f"open suppressed: {person_id} has no locker_id (lockers.require_locker_id)")
            return LockerResolution(person_id, text, LockerCheck.OK, True, None, "")

        # per_locker
        if raw is None or raw == "":
            return deny(LockerCheck.NO_LOCKER_ID,
                        f"open suppressed: {person_id} has no locker_id (lockers.mode=per_locker)")
        if not isinstance(raw, str):
            return deny(LockerCheck.INVALID_LOCKER_ID,
                        f"open suppressed: {person_id} locker_id invalid "
                        f"(not a string: {text} of type {type(raw).__name__})")
        if not _pattern_ok(raw, self.pattern):
            return deny(LockerCheck.INVALID_LOCKER_ID,
                        f"open suppressed: {person_id} locker_id invalid "
                        f"({raw!r} does not match lockers.locker_id_pattern)")
        if raw not in self.configured:
            return deny(LockerCheck.UNKNOWN_LOCKER,
                        f"open suppressed: {person_id} locker_id={raw} not configured in hardware.locker_outputs")
        owners = self._owners.get(raw, [])
        if len(owners) >= 2 and not self.allow_shared:
            others = ",".join(o for o in owners if o != person_id)
            return deny(LockerCheck.SHARED_DENIED,
                        f"open suppressed: {person_id} locker_id={raw} shared with {others} "
                        "(lockers.allow_shared=false)")
        return LockerResolution(person_id, text, LockerCheck.OK, True, raw, "")

    def assignments(self) -> dict[str, str | None]:
        """pid -> raw locker id text (all persons in the snapshot)."""
        return {pid: _raw_text(v) for pid, v in sorted(self._raw.items())}

    def owners(self, locker_id: str) -> list[str]:
        return list(self._owners.get(locker_id, []))

    def problems(self) -> list[LockerResolution]:
        return [r for r in (self.resolve(pid) for pid in sorted(self._raw)) if not r.allowed]

    def free_lockers(self) -> list[str]:
        return [lid for lid in self.configured if not self._owners.get(lid)]

    def summary(self) -> str:
        if self.mode == MODE_PER_LOCKER:
            assigned = sum(1 for lid in self.configured if self._owners.get(lid))
            return (f"per_locker n={len(self.configured)} assigned={assigned} free={len(self.free_lockers())} "
                    f"problems={len(self.problems())}")
        with_locker = sum(1 for v in self._raw.values() if isinstance(v, str) and v.strip())
        return (f"single require_locker_id={'true' if self.require_locker_id else 'false'} "
                f"persons={len(self._raw)} with_locker={with_locker} problems={len(self.problems())}")


# ----------------------------------------------------------------------------- assignment checks
def assignment_issues(db: Any, cfg: Any, configured: Sequence[str], person_id: str, locker_id: str, *,
                      shared_ok: bool = False) -> list[AssignmentIssue]:
    """Structured version of check_assignment() (same rules)."""
    pattern = str(getattr(cfg, "locker_id_pattern", r"^[A-Za-z0-9_-]{1,16}$"))
    issues: list[AssignmentIssue] = []
    if not isinstance(locker_id, str) or not _pattern_ok(locker_id, pattern):
        issues.append(AssignmentIssue("pattern", f"invalid locker_id {locker_id!r}: must match {pattern}"))
        return issues
    if _mode(cfg) != MODE_PER_LOCKER:
        return issues  # single output: duplicates are meaningless, only the format is checked
    configured = tuple(configured)
    if locker_id not in configured:
        issues.append(AssignmentIssue(
            "not_configured",
            f"locker_id {locker_id!r} is not configured in hardware.locker_outputs "
            f"(configured: {', '.join(configured) or 'none'})"))
        return issues
    others = [pid for pid in _owners_of(db, locker_id, pattern) if pid != person_id]
    if others:
        names = ",".join(others)
        if not bool(getattr(cfg, "allow_shared", False)):
            issues.append(AssignmentIssue(
                "taken", f"locker {locker_id} is already assigned to {names} (lockers.allow_shared=false)",
                tuple(others)))
        elif not shared_ok:
            issues.append(AssignmentIssue(
                "shared_needs_flag", f"locker {locker_id} is already assigned to {names}; "
                                     "pass --shared to share it", tuple(others)))
    return issues


def check_assignment(db: Any, cfg: Any, configured: Sequence[str], person_id: str, locker_id: str, *,
                     shared_ok: bool = False) -> list[str]:
    """Shared by enroll --locker-id, assign_locker set and the admin GUI. [] = OK, else error strings:
    pattern mismatch / per_locker and not configured / already assigned to someone else
    (allow_shared=false, or allow_shared=true without shared_ok). single mode: pattern only."""
    return [i.message for i in assignment_issues(db, cfg, configured, person_id, locker_id, shared_ok=shared_ok)]


def _owners_of(db: Any, locker_id: str, pattern: str) -> list[str]:
    ids_fn = getattr(db, "all_person_ids", None)
    pids: Iterable[str] = ids_fn() if callable(ids_fn) else ()
    out = []
    for pid in pids:
        raw = _read_raw(db, pid)
        if isinstance(raw, str) and raw == locker_id:
            out.append(pid)
    return sorted(out)
