#!/usr/bin/env python3
"""Run the face door (INTERFACES section 9). Exit code = facedoor.errors.ExitCode.

    python scripts/run.py [--config FILE] [--set k=v ...] [--source 0|path] [--no-display] [--tag live]
                          [--max-frames N]

Examples:
    python scripts/run.py                                   # webcam 0, mock GPIO on PC, window
    python scripts/run.py --source data/probe/normal/alice --no-display --max-frames 50
    python scripts/run.py --set hardware.mode=pi --no-display   # Raspberry Pi (or configs/local.yaml)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from facedoor.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
