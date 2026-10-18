"""display.backend=web: panel rendering and the MJPEG/snapshot server (no camera needed)."""
from __future__ import annotations

import socket
import urllib.request

import numpy as np

from facedoor.config import DisplaySection
from facedoor.datatypes import DecisionOutput, DecisionState, Frame
from facedoor.display import create_display
from facedoor.locker_panel import LockerPanelRow
from facedoor.webdisplay import PANEL_W, WebDisplay, render_panels


def _row(lid: str, state: str = "LOCKED_CLOSED", door: str = "closed", unlocked: bool = False) -> LockerPanelRow:
    return LockerPanelRow(lid, state, door, unlocked, "", 0.0, 0.0, None, False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_render_panels_shape_and_state_colors():
    rows = [_row("1"), _row("2", "OPEN", "open", True), _row("3"), _row("4")]
    img = render_panels(rows, 480)
    assert img.shape == (480, PANEL_W, 3)
    # panel 2 (OPEN) is green, panel 1 (locked) is gray: different background pixel
    assert tuple(img[120 + 8, 8]) != tuple(img[8, 8])


def test_create_display_web_and_snapshot():
    port = _free_port()
    cfg = DisplaySection(backend="web", web_host="127.0.0.1", web_port=port)
    d = create_display(cfg, enabled=True)
    try:
        assert isinstance(d, WebDisplay)
        d.set_panel([_row("1"), _row("2"), _row("3"), _row("4")])
        frame = Frame(image=np.zeros((480, 640, 3), np.uint8), index=0, ts=0.0, source="test")
        dec = DecisionOutput(state=DecisionState.IDLE, should_open=False, should_deny=False, person_id=None,
                             votes=0, window=5, best_score=None, reason="")
        assert d.show(frame, None, dec, None, None, 15.0) == -1
        data = urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot.jpg", timeout=5).read()
        assert data[:2] == b"\xff\xd8"          # JPEG
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        assert b"/stream" in page
    finally:
        d.close()

