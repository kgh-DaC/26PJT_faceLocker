"""`python -m facedoor [--config ...] [--set k=v] [--source 0|path] [--no-display] [--tag live] [--max-frames N]`."""

from __future__ import annotations

from facedoor.app import main

if __name__ == "__main__":
    raise SystemExit(main())
