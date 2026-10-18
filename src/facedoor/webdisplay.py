"""Browser display (display.backend=web): MJPEG stream of the recognition screen for headless devices.

Layout matches the Tk screen: left = webcam with face boxes, right = locker panels 1..N (door open/closed from the
limit switch, lock state, countdown). cv2.putText cannot draw Korean, so panel text is ASCII.
Endpoints: ``/`` (page), ``/stream`` (multipart MJPEG), ``/snapshot.jpg``. No authentication: bind to a trusted LAN only
(``display.web_host``; 127.0.0.1 = this device only, use an SSH tunnel).
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence

import cv2
import numpy as np

from facedoor.config import DisplaySection
from facedoor.datatypes import DecisionOutput, Frame, RecognitionResult
from facedoor.display import BLACK, FONT, GRAY, GREEN, RED, WHITE, YELLOW, LabelFn, NullDisplay, render_overlay
from facedoor.errors import FaceDoorError
from facedoor.locker_panel import LockerPanelRow

log = logging.getLogger(__name__)

PANEL_W = 250
_PAGE = (b"<!doctype html><meta charset=utf-8><title>FaceDoor</title>"
         b"<body style='margin:0;background:#111;color:#ddd;font-family:sans-serif'>"
         b"<img src='/stream' style='display:block;margin:0 auto;max-width:100%'>"
         b"<p style='text-align:center'>FaceDoor live view</p></body>")

_SUB = {"LOCKED_CLOSED": "locked", "UNLOCKED_WAITING": "unlocked - waiting", "OPEN": "unlocked - door open",
        "CLOSING_CONFIRM": "closing...", "COOLDOWN": "cooldown", "LOCKED_OPEN": "FORCED OPEN",
        "SENSOR_FAULT": "SENSOR FAULT", "FAULT": "FAULT", "UNKNOWN": "unknown"}


class WebDisplayError(FaceDoorError):
    """display.backend=web but the HTTP server cannot start (exit code 1)."""


def _badge(row: LockerPanelRow) -> tuple[str, tuple[int, int, int]]:
    if row.sensorless:
        return ("UNLOCKED", GREEN) if row.unlocked else ("LOCKED", GRAY)
    return {"closed": ("CLOSED", (89, 89, 89)), "open": ("OPEN", (36, 120, 35))}.get(row.door, ("?", (6, 136, 212)))


def render_panels(rows: Sequence[LockerPanelRow], height: int, width: int = PANEL_W) -> np.ndarray:
    """Right column: one panel per locker, top to bottom (pure, ASCII)."""
    col = np.full((height, width, 3), 30, np.uint8)
    n = max(1, len(rows))
    ph = height // n
    for i, r in enumerate(rows):
        y0 = i * ph
        bg = {"UNLOCKED_WAITING": (176, 243, 255), "OPEN": (143, 235, 183), "CLOSING_CONFIRM": (201, 247, 227),
              "LOCKED_OPEN": (145, 213, 255), "SENSOR_FAULT": (176, 243, 255), "FAULT": (156, 156, 255),
              "UNKNOWN": (176, 243, 255)}.get(r.state, (217, 217, 217))
        cv2.rectangle(col, (4, y0 + 4), (width - 4, y0 + ph - 4), bg, -1)
        name = "DOOR" if r.locker_id is None else f"LOCKER {r.locker_id}"
        cv2.putText(col, name, (14, y0 + 28), FONT, 0.7, BLACK, 2, cv2.LINE_AA)
        text, bcol = _badge(r)
        cv2.rectangle(col, (14, y0 + 38), (width - 14, y0 + 38 + 40), bcol, -1)
        cv2.putText(col, text, (24, y0 + 38 + 29), FONT, 0.9, WHITE, 2, cv2.LINE_AA)
        sub = _SUB.get(r.state, r.state)
        if r.countdown_kind in ("unlock_wait", "close_confirm") and r.countdown_sec > 0:
            sub += f" {r.countdown_sec:.1f}s"
        cv2.putText(col, sub, (14, y0 + 38 + 40 + 24), FONT, 0.5, BLACK, 1, cv2.LINE_AA)
        if r.owner_label:
            cv2.putText(col, f"user: {r.owner_label}", (14, y0 + ph - 12), FONT, 0.45, BLACK, 1, cv2.LINE_AA)
    return col


class WebDisplay(NullDisplay):
    """Same Display interface as the others; ``show`` publishes the composed image, never blocks on clients."""

    def __init__(self, cfg: DisplaySection) -> None:
        self.cfg = cfg
        self.label_fn: LabelFn | None = None
        self._rows: list[LockerPanelRow] = []
        self._notice = ("", 0.0)
        self._cond = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._latest: np.ndarray | None = None
        host = getattr(cfg, "web_host", "0.0.0.0")
        port = int(getattr(cfg, "web_port", 8080))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # noqa: D401 - silence access log
                return None

            def do_GET(self) -> None:  # noqa: N802
                if self.path in ("/", "/index.html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(_PAGE)))
                    self.end_headers()
                    self.wfile.write(_PAGE)
                elif self.path.startswith("/snapshot"):
                    jpg = outer._wait_jpeg(0, 3.0)[1]
                    if jpg is None:
                        self.send_error(503, "no frame yet")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpg)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(jpg)
                elif self.path.startswith("/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    seq = 0
                    try:
                        while not outer._closed:
                            seq, jpg = outer._wait_jpeg(seq, 2.0)
                            if jpg is None:
                                continue
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                             + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                else:
                    self.send_error(404)

        self._closed = False
        try:
            self._server = ThreadingHTTPServer((host, port), Handler)
        except OSError as e:
            raise WebDisplayError(f"display.backend=web: cannot listen on {host}:{port}: {e}") from e
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="web-display", daemon=True).start()
        log.info("web display: http://%s:%d/ (MJPEG /stream, /snapshot.jpg). No authentication: trusted LAN only",
                 host, port)

    def _wait_jpeg(self, last_seq: int, timeout: float) -> tuple[int, bytes | None]:
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            return self._seq, self._jpeg

    # --- optional Display extensions
    def set_panel(self, rows: Sequence[LockerPanelRow]) -> None:
        self._rows = list(rows)

    def set_notice(self, text: str, ttl: float = 4.0) -> None:
        self._notice = (text, time.monotonic() + ttl)

    def show(self, frame: Frame, result: RecognitionResult | None, decision: DecisionOutput,
             door: Any, leds: Any, fps: float) -> int:
        img = render_overlay(frame.image, result, decision, door, leds, fps if self.cfg.show_fps else 0.0,
                             self.cfg.show_scores, self.label_fn)
        if self._rows and self.cfg.locker_panel:
            img = np.hstack([img, render_panels(self._rows, img.shape[0])])
        text, until = self._notice
        if text.isascii() and text and time.monotonic() < until:
            cv2.putText(img, text, (8, img.shape[0] - 10), FONT, 0.55, YELLOW, 2, cv2.LINE_AA)
        if self.cfg.scale != 1.0:
            img = cv2.resize(img, None, fx=self.cfg.scale, fy=self.cfg.scale)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            with self._cond:
                self._jpeg = buf.tobytes()
                self._seq += 1
                self._cond.notify_all()
        return -1

    def show_image(self, image: np.ndarray, lines: list[str] | None = None) -> int:
        return -1

    def close(self) -> None:
        self._closed = True
        with self._cond:
            self._cond.notify_all()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass
