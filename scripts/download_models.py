"""Download ONNX models (YuNet, SFace) from OpenCV zoo and verify sha256 (INTERFACES section 9).

Checksum policy (TOFU): if configs set models.<name>.sha256 it must match. Otherwise the first download's
sha256 is recorded in models/models.lock.json and later runs verify against it.

    python scripts/download_models.py [--config FILE] [--set k=v] [--force] [--only yunet,sface]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facedoor.config import load_config  # noqa: E402
from facedoor.errors import ConfigError, ExitCode  # noqa: E402
from facedoor.imageio import sha256_file  # noqa: E402

LOCK_NAME = "models.lock.json"


def _load_lock(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"WARNING: cannot parse {path}; ignoring")
    return {}


def _download(url: str, dst: Path, retries: int = 3) -> None:
    tmp = dst.with_suffix(dst.suffix + ".part")
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "facedoor-model-downloader"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dst)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  attempt {attempt}/{retries} failed: {e}")
            time.sleep(1.5 * attempt)
    if tmp.exists():
        tmp.unlink()
    raise RuntimeError(f"download failed: {url}: {last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--force", action="store_true", help="re-download even if file exists")
    ap.add_argument("--only", default="", help="comma separated model names (default: all in config)")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config, args.overrides)
    except ConfigError as e:
        print(f"config error: {e}")
        return int(ExitCode.CONFIG)

    models_dir = cfg.app.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    lock_path = models_dir / LOCK_NAME
    lock = _load_lock(lock_path)
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(cfg.models)
    failed = 0
    for name in names:
        spec = cfg.models.get(name)
        if spec is None:
            print(f"[{name}] unknown model name (configured: {list(cfg.models)})")
            failed += 1
            continue
        dst = models_dir / spec.file
        print(f"[{name}] {spec.file}")
        if dst.is_file() and not args.force:
            print("  exists, verifying")
        else:
            if not spec.url:
                print("  no url configured; place the file manually")
                failed += 1
                continue
            print(f"  downloading {spec.url}")
            try:
                _download(spec.url, dst)
            except RuntimeError as e:
                print(f"  ERROR: {e}")
                failed += 1
                continue
        digest = sha256_file(dst)
        expected = spec.sha256 or (lock.get(spec.file) or {}).get("sha256")
        if expected and digest.lower() != expected.lower():
            print(f"  ERROR: sha256 mismatch: got {digest}, expected {expected}")
            print("  (file kept for inspection; delete it and rerun with --force, or fix configs)")
            failed += 1
            continue
        if expected:
            print(f"  sha256 OK {digest}")
        else:
            print(f"  sha256 recorded (TOFU) {digest} -> {lock_path.name}; pin it in configs/default.yaml after checking")
        lock[spec.file] = {"name": name, "sha256": digest, "size": dst.stat().st_size, "url": spec.url,
                           "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    lock_path.write_text(json.dumps(lock, indent=2, ensure_ascii=False), encoding="utf-8")
    if failed:
        print(f"{failed} model(s) failed")
        return int(ExitCode.MODEL)
    print("all models OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
