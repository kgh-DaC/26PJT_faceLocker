#!/usr/bin/env python3
"""Enroll faces into data/gallery (INTERFACES sections 3.12, 6, 9). Exit code = facedoor.errors.ExitCode.

    python scripts/enroll.py --name <person_id> --tag <tag> [--images DIR|GLOB] [--source 0] [--count N]
                             [--no-display] [--reembed] [--prune-outliers] [--display-name NAME]
                             [--locker-id ID] [--config FILE] [--set k=v ...]

- --locker-id ID: store locker_id in person.json (validated by lockers.locker_id_pattern; needed when
  lockers.require_locker_id=true). With only --name --locker-id/--display-name, updates an enrolled person.

- Live (default): guided capture from --source (default camera.source) over enroll.poses.
  --count N = total samples for this tag (spread over the poses; default poses x samples_per_pose).
- --images DIR|GLOB: enroll from files (one face per image; multi-face images are rejected).
  --count N limits the number of image files used.
- --reembed: re-detect + re-embed every stored crop with the current embedder (after changing embedder/model).
- --prune-outliers: delete samples of --name whose mean similarity to the person's other samples
  is below enroll.outlier_min_self_score (without it, outliers are only reported).

Register several tags per person (normal, glasses, noglasses, mask, scarf, lowlight, backlight):
condition diversity is the first line of robustness against lighting/mask/glasses/scarf.
"""

from __future__ import annotations

import argparse
import glob
import logging
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from facedoor.camera import IMAGE_EXTS, create_camera  # noqa: E402
from facedoor.config import load_config  # noqa: E402
from facedoor.database import FaceDatabase  # noqa: E402
from facedoor.detector import create_detector  # noqa: E402
from facedoor.display import create_display  # noqa: E402
from facedoor.embedder import create_embedder  # noqa: E402
from facedoor.enroll import Enroller, EnrollReport, validate_locker_id, validate_person_id  # noqa: E402
from facedoor.errors import ExitCode, FaceDoorError  # noqa: E402
from facedoor.lockers import check_assignment, configured_lockers  # noqa: E402

log = logging.getLogger("enroll")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="facedoor enrollment")
    p.add_argument("--name", help="person_id (ASCII ^[A-Za-z0-9_-]{1,32}$)")
    p.add_argument("--tag", help="condition tag: normal|glasses|noglasses|mask|scarf|lowlight|backlight")
    p.add_argument("--images", help="image folder or glob (instead of live capture)")
    p.add_argument("--source", default=None, help="camera source for live capture (default camera.source)")
    p.add_argument("--count", type=int, default=None, help="live: total samples; --images: max files")
    p.add_argument("--no-display", action="store_true", help="no guidance window")
    p.add_argument("--reembed", action="store_true", help="re-embed the whole gallery with the current embedder")
    p.add_argument("--prune-outliers", action="store_true", help="delete outlier samples of --name")
    p.add_argument("--display-name", default=None, help="person.json display_name (Korean allowed)")
    p.add_argument("--locker-id", default=None,
                   help="person.json locker_id (must match lockers.locker_id_pattern); "
                        "without --tag/--images only updates person.json of an enrolled person")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    return p.parse_args(argv)


def expand_images(spec: str) -> list[Path]:
    p = Path(spec)
    if p.is_dir():
        return sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
    if p.is_file():
        return [p]
    return sorted(Path(f) for f in glob.glob(spec) if Path(f).suffix.lower() in IMAGE_EXTS)


def prune(db: FaceDatabase, person_id: str, min_self_score: float) -> int:
    outs = db.find_outliers(person_id, min_self_score)
    for s in outs:
        log.warning("pruning outlier %s", s.npy_path)
        db.remove_sample(s)
    return len(outs)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config, args.overrides)
        logging.getLogger().setLevel(getattr(logging, cfg.app.log_level.upper(), logging.INFO))
        detector = create_detector(cfg.detector, cfg.app.models_dir)
        embedder = create_embedder(cfg.embedder, cfg.app.models_dir)
        log.info("embedder %s, gallery %s", embedder.model_id, cfg.app.gallery_dir)

        if args.reembed:
            db = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=None)
            n = Enroller(detector, embedder, db, cfg.quality, cfg.enroll).reembed_all()
            print(f"re-embedded {n} samples with {embedder.model_id}")
            return int(ExitCode.OK)

        if not args.name:
            print("error: --name is required (unless --reembed)", file=sys.stderr)
            return int(ExitCode.CONFIG)
        try:
            validate_person_id(args.name, cfg.enroll.person_id_pattern)
            if args.locker_id is not None:
                validate_locker_id(args.locker_id, cfg.lockers.locker_id_pattern)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return int(ExitCode.CONFIG)

        db = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=embedder.model_id)
        if args.locker_id is not None:
            # same rule as assign_locker.py set / admin GUI (INTERFACES 9, 13.6), checked BEFORE any capture
            errs = check_assignment(db, cfg.lockers, configured_lockers(cfg.hardware), args.name, args.locker_id)
            if errs:
                for e in errs:
                    print(f"error: {e}", file=sys.stderr)
                return int(ExitCode.CONFIG)
        gallery_lock = _acquire_lock(cfg, "gallery.lock")
        if gallery_lock is False:
            print("error: another admin task is writing the gallery (gallery.lock held)", file=sys.stderr)
            return int(ExitCode.GALLERY)
        try:
            return _run(args, cfg, db, detector, embedder)
        finally:
            if gallery_lock:
                gallery_lock.release()
    except FaceDoorError as e:
        print(f"error ({type(e).__name__}): {e}", file=sys.stderr)
        return int(e.exit_code)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    except KeyboardInterrupt:
        return int(ExitCode.INTERRUPTED)


def _acquire_lock(cfg, name: str):
    """RunLock on <logs_dir>/locks/<name> (INTERFACES 14.6). Returns the lock, False if held elsewhere,
    None if facedoor.runlock is unavailable (no mutual exclusion; warning logged)."""
    try:
        from facedoor.runlock import RunLock
    except ImportError:
        log.warning("facedoor.runlock not available; running without %s", name)
        return None
    path = Path(cfg.app.logs_dir) / "locks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = RunLock(path)
    return lock if lock.acquire() else False


def _run(args: argparse.Namespace, cfg, db: FaceDatabase, detector, embedder) -> int:
    enroller = Enroller(detector, embedder, db, cfg.quality, cfg.enroll, notify=print)

    info_only = args.locker_id is not None or args.display_name is not None or args.prune_outliers
    capture = args.images is not None or args.tag is not None or not info_only
    if not capture and not db.samples(args.name):
        print(f"error: {args.name!r} is not enrolled (give --tag/--images to enroll first)", file=sys.stderr)
        return int(ExitCode.GALLERY)
    report: EnrollReport | None = None
    if capture:
        if not args.tag:
            print("error: --tag is required for enrollment", file=sys.stderr)
            return int(ExitCode.CONFIG)
        if args.images is not None:
            paths = expand_images(args.images)
            if args.count is not None:
                paths = paths[: max(0, args.count)]
            if not paths:
                print(f"error: no images found for {args.images!r}", file=sys.stderr)
                return int(ExitCode.ERROR)
            report = enroller.enroll_images(args.name, args.tag, paths)
        else:
            poses = list(cfg.enroll.poses)
            spp = (max(1, math.ceil(args.count / len(poses))) if args.count else None)
            cam_lock = _acquire_lock(cfg, "camera.lock")
            if cam_lock is False:
                print("error: camera is in use by the recognition program or the admin GUI (camera.lock)",
                      file=sys.stderr)
                return int(ExitCode.CAMERA)
            try:
                camera = create_camera(cfg.camera, args.source)
                display = create_display(cfg.display, enabled=not args.no_display)
                camera.open()
                try:
                    report = enroller.enroll_live(camera, args.name, args.tag, display, poses, spp)
                finally:
                    camera.close()
                    display.close()
            finally:
                if cam_lock:
                    cam_lock.release()
    info: dict[str, object] = {}
    if args.display_name:
        info["display_name"] = args.display_name
    if args.locker_id is not None:
        info["locker_id"] = args.locker_id
    if info:
        if db.samples(args.name):
            db.set_person_info(args.name, **info)
            print(f"{args.name}: person.json updated ({', '.join(f'{k}={v}' for k, v in info.items())})")
        else:
            print(f"WARNING: no sample of {args.name} saved; person.json not written", file=sys.stderr)

    pruned = prune(db, args.name, cfg.enroll.outlier_min_self_score) if args.prune_outliers else 0
    if report is not None:
        print(report.summary())
        if report.outliers and not args.prune_outliers:
            print("outlier candidates (use --prune-outliers to delete):")
            for p in report.outliers:
                print(f"  {p}")
        if report.saved == 0:
            print("WARNING: no sample saved", file=sys.stderr)
    if args.prune_outliers:
        print(f"pruned {pruned} outlier sample(s) of {args.name}")
    counts: dict[str, int] = {}
    for s in db.samples(args.name):
        counts[s.tag] = counts.get(s.tag, 0) + 1
    print(f"{args.name}: " + (", ".join(f"{t}={c}" for t, c in sorted(counts.items())) or "no samples"))
    missing = [t for t in cfg.enroll.tags if t not in counts]
    if missing:
        print(f"  tags not yet enrolled: {', '.join(missing)}")
    return int(ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
