"""Post-hoc labelling of logs/events.csv frame rows (owner: eval-engineer). INTERFACES sections 5, 9.

    python scripts/label_events.py --events logs/events.csv --session <id> --label <person_id|_impostor|_ignore>
                                   [--from-frame a --to-frame b] [--overwrite] [--dry-run] [--list-sessions]
                                   [--config FILE] [--set k=v ...]

Sets the ``label`` column of the frame rows of one session (optionally a frame_idx range, inclusive). Frame rows are
``event=frame`` and the decision frame rows ``event=open`` / ``event=deny`` (INTERFACES 5, 13.8: open rows must be
labelled so that misid_locker_opens / impostor_locker_opens can be audited). start/stop/error/door_close rows are
never labelled. Both the 33-column (v1.2) and 35-column (v1.3) files are accepted and EVERY column of the original
header is written back unchanged (only ``label`` changes).
- <person_id>: the person really standing at the door (genuine; MATCH as that person = correct accept)
- _impostor  : an unenrolled person (any MATCH = false accept)
- _ignore    : exclude these frames (nobody / mixed / test noise)
Rows that already have a different label are kept unless --overwrite. The file is rewritten atomically and the
previous version is kept as <name>.bak. Do not run while the door app is writing to the same file.
Then: python scripts/evaluate.py --events logs/events.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facedoor.errors import ConfigError, ExitCode, FaceDoorError  # noqa: E402

log = logging.getLogger("label_events")

SPECIAL_LABELS = ("_impostor", "_ignore")
LABELLED_EVENTS = ("frame", "open", "deny")   # rows that carry a recognition result


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", default=None, help="events csv (default: logging.events_csv)")
    ap.add_argument("--session", default=None, help="session_id to label")
    ap.add_argument("--label", default=None, help="<person_id> | _impostor | _ignore")
    ap.add_argument("--from-frame", type=int, default=None, help="first frame_idx (inclusive)")
    ap.add_argument("--to-frame", type=int, default=None, help="last frame_idx (inclusive)")
    ap.add_argument("--overwrite", action="store_true", help="replace existing different labels")
    ap.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    ap.add_argument("--list-sessions", action="store_true", help="list sessions with frame counts and exit")
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    return ap.parse_args(argv)


def read_events(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read by column name (33- or 35-column layouts). A row with MORE fields than the header would lose data on
    rewrite, so it is refused (INTERFACES 5: one file never mixes column counts)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, restval="")
        header = list(reader.fieldnames or [])
        rows = []
        for r in reader:
            if None in r:
                raise ConfigError(f"{path}: line {reader.line_num} has more fields than the header "
                                  f"({len(header)} columns); refusing to rewrite (would drop data)")
            rows.append(dict(r))
    if "label" not in header or "session_id" not in header or "event" not in header:
        raise ConfigError(f"{path}: not an events.csv (missing label/session_id/event columns)")
    return header, rows


def write_events(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    """Atomic rewrite (UTF-8 without BOM, csv default dialect); previous file kept as <name>.bak."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: (r.get(k) if r.get(k) is not None else "") for k in header})
        shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _frame_idx(row: dict[str, str]) -> int | None:
    try:
        return int(str(row.get("frame_idx", "")).strip())
    except ValueError:
        return None


def apply_labels(rows: list[dict[str, str]], session: str, label: str, from_frame: int | None, to_frame: int | None,
                 overwrite: bool) -> Counter:
    """Label matching frame/open/deny rows in place. Returns counts: labelled, unchanged, conflict_kept, overwritten."""
    c: Counter = Counter()
    for r in rows:
        if r.get("session_id") != session or r.get("event") not in LABELLED_EVENTS:
            continue
        fi = _frame_idx(r)
        if from_frame is not None and (fi is None or fi < from_frame):
            continue
        if to_frame is not None and (fi is None or fi > to_frame):
            continue
        cur = (r.get("label") or "").strip()
        if cur == label:
            c["unchanged"] += 1
        elif cur and not overwrite:
            c["conflict_kept"] += 1
        else:
            c["overwritten" if cur else "labelled"] += 1
            r["label"] = label
    return c


def run(args: argparse.Namespace) -> int:
    from facedoor.config import load_config
    from facedoor.database import FaceDatabase

    cfg = load_config(args.config, args.overrides)
    path = Path(args.events).resolve() if args.events else cfg.logging.events_csv
    if not path.is_file():
        raise ConfigError(f"events file not found: {path}")
    header, rows = read_events(path)

    sessions = Counter(r.get("session_id", "") for r in rows if r.get("event") in LABELLED_EVENTS)
    if args.list_sessions or not args.session:
        if not args.list_sessions:
            log.error("--session is required (sessions listed below)")
        for s, n in sorted(sessions.items()):
            srows = [r for r in rows if r.get("session_id") == s and r.get("event") in LABELLED_EVENTS]
            labels = Counter((r.get("label") or "-") for r in srows)
            n_open = sum(1 for r in srows if r.get("event") == "open")
            print(f"{s}\t{n} frames (open {n_open})\tlabels={dict(labels)}")
        return int(ExitCode.OK if args.list_sessions else ExitCode.ERROR)
    if not args.label:
        raise ConfigError("--label is required")
    label = args.label.strip()
    if label not in SPECIAL_LABELS:
        if not re.fullmatch(cfg.enroll.person_id_pattern, label):
            raise ConfigError(f"--label {label!r}: not a valid person_id ({cfg.enroll.person_id_pattern}) "
                              f"nor one of {SPECIAL_LABELS}")
        try:
            known = FaceDatabase.load(cfg.app.gallery_dir).all_person_ids()
        except FaceDoorError:
            known = []
        if known and label not in known:
            log.warning("label %r is not an enrolled person %s: frames will be scored as a non-enrolled genuine; "
                        "use _impostor for unenrolled people", label, known)
    if args.session not in sessions:
        log.error("session %r has no frame rows in %s. Known sessions: %s", args.session, path, sorted(sessions))
        return int(ExitCode.ERROR)
    if args.from_frame is not None and args.to_frame is not None and args.from_frame > args.to_frame:
        raise ConfigError("--from-frame must be <= --to-frame")

    c = apply_labels(rows, args.session, label, args.from_frame, args.to_frame, args.overwrite)
    total = sum(c.values())
    log.info("session %s frames %s..%s -> label %r: %d matched, %d newly labelled, %d overwritten, %d unchanged, "
             "%d kept (already labelled differently; use --overwrite)", args.session,
             "start" if args.from_frame is None else args.from_frame, "end" if args.to_frame is None else args.to_frame,
             label, total, c["labelled"], c["overwritten"], c["unchanged"], c["conflict_kept"])
    if total == 0:
        log.warning("no frame rows matched the given range")
    if args.dry_run:
        log.info("--dry-run: file not modified")
        return int(ExitCode.OK)
    if c["labelled"] or c["overwritten"]:
        write_events(path, header, rows)
        log.info("written %s (backup %s.bak)", path, path.name)
    return int(ExitCode.OK)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return run(args)
    except FaceDoorError as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        return int(ExitCode.INTERRUPTED)


if __name__ == "__main__":
    raise SystemExit(main())
