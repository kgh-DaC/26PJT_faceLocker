#!/usr/bin/env python3
"""한글 관리 화면 (INTERFACES section 14). 실행해도 카메라를 열지 않는다.

    python scripts/admin_gui.py [--config FILE] [--set k=v ...] [--source 0]

Exit code = facedoor.errors.ExitCode (설정 오류 2, 기타 1).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from facedoor.errors import ConfigError, ExitCode, FaceDoorError  # noqa: E402
from facedoor.gui import texts as T  # noqa: E402

log = logging.getLogger("admin_gui")


def setup_console() -> None:
    """texts.setup_console() (v1.4, facedoor/texts.py) if available, else the same reconfigure locally."""
    try:
        from facedoor import texts as core_texts  # type: ignore[attr-defined]
        fn = getattr(core_texts, "setup_console", None)
        if callable(fn):
            fn()
            return
    except ImportError:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def print_error(msg: str, exc: BaseException | None = None) -> None:
    print(T.CLI_ERROR.format(msg=msg), file=sys.stderr)
    if exc is not None:
        print(T.CLI_ERROR_ORIGINAL.format(cls=type(exc).__name__, detail=exc), file=sys.stderr)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=T.CLI_DESCRIPTION)
    p.add_argument("--config", type=Path, default=None, help=T.CLI_HELP_CONFIG)
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help=T.CLI_HELP_SET)
    p.add_argument("--source", default=None, help=T.CLI_HELP_SOURCE)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from facedoor.config import load_config

        cfg = load_config(args.config, args.overrides)
        logging.getLogger().setLevel(getattr(logging, cfg.app.log_level.upper(), logging.INFO))
    except ConfigError as e:
        print_error(T.CLI_CONFIG_ERROR, e)
        return int(ExitCode.CONFIG)
    try:
        import tkinter  # noqa: F401
    except ImportError as e:
        print_error(T.CLI_TK_MISSING, e)
        return int(ExitCode.ERROR)
    try:
        from facedoor.gui.service import AdminService

        service = AdminService(cfg, source=args.source)
        log.info("admin GUI: gallery=%s mode=%s source=%s", cfg.app.gallery_dir, cfg.lockers.mode,
                 args.source or cfg.camera.source)
        from facedoor.gui.admin_app import run_app

        print(T.CLI_STARTED)
        code = run_app(service)
        print(T.CLI_EXITED)
        return code
    except FaceDoorError as e:
        print_error(T.ERROR_GENERIC.format(detail=e), e)
        return int(e.exit_code)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "TclError":
            print_error(T.CLI_NO_DISPLAY, e)
            return int(ExitCode.ERROR)
        raise
    except KeyboardInterrupt:
        return int(ExitCode.INTERRUPTED)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
