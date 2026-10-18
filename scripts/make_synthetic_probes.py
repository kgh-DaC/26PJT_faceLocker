"""Make synthetic probe conditions from real probe images (owner: eval-engineer). INTERFACES sections 6.1, 9.

    python scripts/make_synthetic_probes.py --src data/probe/normal --out data/probe
                                            [--conditions syn_lowlight,syn_mask,...] [--seed 42] [--per-image 1]
                                            [--gallery DIR] [--no-detect] [--overwrite] [--config FILE] [--set k=v ...]

Input layout : <src>/<person_id>/*.jpg|png
Output layout: <out>/<cond>/<person_id>/<stem>_<k>.jpg              (genuine, e.g. data/probe/syn_mask/alice/)
               <out>/_impostor_<cond>/<impostor_id>/<stem>_<k>.jpg  (when <src> is an _impostor* folder)
               <out>/<cond>/_manifest.json                           (provenance; ignored by scan_probes)

Synthetic images are crude approximations (gamma/noise/blur/overlays) and are reported as "합성 조건 (참고용)".
They never replace real captures and calibration built only from them is reliable=false.
Never use gallery images as --src: the synthetic copies would be near-duplicates of enrolled samples (leakage).
Source images byte-identical to a gallery sample (sha256, see --gallery) are skipped for the same reason.
Face landmarks from YuNet place mask/glasses/scarf overlays; without a detected face a centred face is assumed.
Output is reproducible: each image's RNG is seeded from (seed, condition, relative path, k).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from facedoor.errors import ConfigError, ExitCode, FaceDoorError  # noqa: E402

log = logging.getLogger("make_synthetic_probes")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="source condition folder, e.g. data/probe/normal or data/probe/_impostor")
    ap.add_argument("--out", required=True, help="probe root to write syn_<cond>/ folders into, e.g. data/probe")
    ap.add_argument("--conditions", default=None,
                    help="comma separated syn_* conditions (default: evaluation.synthetic_conditions)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--per-image", type=int, default=1, help="variants per source image and condition")
    ap.add_argument("--gallery", default=None,
                    help="gallery dir for the leakage check of source images (default: app.gallery_dir)")
    ap.add_argument("--no-detect", action="store_true", help="do not run YuNet; assume a centred face")
    ap.add_argument("--overwrite", action="store_true", help="overwrite existing output files")
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    return ap.parse_args(argv)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _image_rng(seed: int, cond: str, rel: str, k: int) -> np.random.Generator:
    return np.random.default_rng([seed, zlib.crc32(cond.encode()), zlib.crc32(rel.encode("utf-8")), k])


def run(args: argparse.Namespace) -> int:
    from facedoor.config import load_config
    from facedoor.evaluation._util import IMAGE_EXTS, sha256_file
    from facedoor.evaluation.augment import SYNTHETIC_CONDITIONS, apply_condition
    from facedoor.evaluation.dataset import ProbeSample, check_leakage, scan_gallery
    from facedoor.imageio import imread, imwrite
    from facedoor.timeutil import now_iso

    cfg = load_config(args.config, args.overrides)
    ev = cfg.evaluation
    src, out = Path(args.src).resolve(), Path(args.out).resolve()
    if not src.is_dir():
        raise ConfigError(f"--src not a directory: {src}")
    if _is_within(src, cfg.app.gallery_dir):
        raise ConfigError(f"--src {src} is inside the gallery ({cfg.app.gallery_dir}): synthetic probes made from "
                          "enrolled images leak into the evaluation. Use probe images (e.g. data/probe/normal).")
    if args.per_image < 1:
        raise ConfigError("--per-image must be >= 1")
    conds = ([c.strip() for c in args.conditions.split(",") if c.strip()] if args.conditions
             else list(ev.synthetic_conditions or SYNTHETIC_CONDITIONS))
    bad = [c for c in conds if c not in SYNTHETIC_CONDITIONS]
    if bad:
        raise ConfigError(f"unknown synthetic condition(s) {bad}; expected {list(SYNTHETIC_CONDITIONS)}")
    prefix = ev.synthetic_prefix
    if src.name.startswith(prefix) or src.name.startswith(f"{ev.impostor_dir_name}_{prefix}"):
        log.warning("--src %s is already synthetic; stacking synthetic conditions is not meaningful", src)
    impostor = src.name == ev.impostor_dir_name or src.name.startswith(ev.impostor_dir_name + "_")

    detector = None
    if not args.no_detect:
        try:
            from facedoor.detector import create_detector
            detector = create_detector(cfg.detector, cfg.app.models_dir)
        except FaceDoorError as exc:
            log.warning("detector unavailable (%s): overlays assume a centred face", exc)

    images = sorted(p for pdir in sorted(src.iterdir()) if pdir.is_dir()
                    for p in pdir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    if not images:
        log.error("no images under %s/<person_id>/", src)
        return int(ExitCode.ERROR)
    # A synthetic copy of a leaked probe (identical to a gallery sample) would no longer match by sha256 and
    # would slip past evaluate.py's leakage check -> skip such sources here.
    gallery_dir = Path(args.gallery).resolve() if args.gallery else cfg.app.gallery_dir
    leaked = {p for _, p in check_leakage(scan_gallery(gallery_dir), [
        ProbeSample(p, "src", p.parent.name, None, False, sha256_file(p)) for p in images])}
    if leaked:
        log.warning("%d source image(s) identical to gallery samples in %s skipped (leakage): %s",
                    len(leaked), gallery_dir, sorted(str(p) for p in leaked)[:5])
        images = [p for p in images if p not in leaked]
        if not images:
            log.error("every source image is a gallery duplicate; nothing to do")
            return int(ExitCode.ERROR)
    log.info("source %s: %d images (%s), conditions %s, seed %d, per-image %d", src, len(images),
             "impostor" if impostor else "genuine", conds, args.seed, args.per_image)

    landmarks: dict[Path, np.ndarray | None] = {}
    no_face: list[str] = []
    loaded: dict[Path, np.ndarray] = {}
    for p in images:
        img = imread(p)
        if img is None:
            log.warning("unreadable image skipped: %s", p)
            continue
        loaded[p] = img
        lm = None
        if detector is not None:
            dets = detector.detect(img)
            if dets:
                lm = max(dets, key=lambda d: d.bbox[2] * d.bbox[3]).landmarks
            else:
                no_face.append(str(p))
        landmarks[p] = lm

    written = skipped = 0
    for cond in conds:
        folder = f"{ev.impostor_dir_name}_{cond}" if impostor else cond
        cdir = out / folder
        files: list[dict] = []
        for p, img in loaded.items():
            rel = f"{p.parent.name}/{p.name}"
            for k in range(args.per_image):
                dst = cdir / p.parent.name / f"{p.stem}_{k:02d}.jpg"
                if dst.exists() and not args.overwrite:
                    skipped += 1
                    continue
                syn = apply_condition(img, cond, _image_rng(args.seed, cond, rel, k), landmarks.get(p))
                if not imwrite(dst, syn):
                    raise FaceDoorError(f"failed to write {dst}")
                written += 1
                files.append({"file": f"{p.parent.name}/{dst.name}", "source": str(p),
                              "landmarks": "yunet" if landmarks.get(p) is not None else "assumed_centred"})
        if files:
            manifest = cdir / "_manifest.json"
            old = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
            known = {f["file"]: f for f in old.get("files", [])}
            known.update({f["file"]: f for f in files})
            manifest.write_text(json.dumps({
                "synthetic": True,
                "note": "합성 조건(참고용): 실촬영 결과를 대체하지 못함",
                "condition": cond, "impostor": impostor, "src": str(src), "seed": args.seed,
                "per_image": args.per_image, "created_at": now_iso(),
                "files": sorted(known.values(), key=lambda f: f["file"]),
            }, ensure_ascii=False, indent=1), encoding="utf-8")
    if no_face:
        log.warning("%d source image(s) without a detected face (overlays placed at the image centre): %s",
                    len(no_face), no_face[:5])
    log.info("written %d synthetic images, skipped %d existing (use --overwrite) -> %s", written, skipped, out)
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
