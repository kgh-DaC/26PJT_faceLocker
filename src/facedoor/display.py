"""On-screen display (owner: vision-engineer). Contract: INTERFACES sections 3.10, 15.4, 16, 17.

- ``TkDisplay`` (tkinter, Korean): left webcam image (face rectangle only), right column = locker panels 1..N,
  bottom status line (recognition text, threshold source, FPS) and the simulation hint.
- ``OpenCVDisplay`` (ASCII fallback): green box = MATCH, red = UNKNOWN, yellow = HOLD, top-right status panel and
  short locker lines (``L2 OPEN unlocked``). cv2.putText cannot draw Korean.
- ``NullDisplay``: --no-display.
Optional Display extensions (all displays implement them as no-ops where meaningless): ``set_panel(rows)``,
``set_sim_handler(cb)``, ``set_notice(text, ttl)``. The app calls them through getattr.
"""

from __future__ import annotations

import collections
import importlib.util
import logging
import os
import sys
import time
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

import cv2
import numpy as np

from facedoor import texts as T
from facedoor.config import DisplaySection
from facedoor.datatypes import DecisionOutput, DecisionState, Frame, FrameVerdict, RecognitionResult
from facedoor.errors import FaceDoorError
from facedoor.locker_panel import LockerPanelRow, RowView, ascii_line, describe_row

log = logging.getLogger(__name__)

GREEN = (0, 200, 0)
RED = (0, 0, 230)
YELLOW = (0, 220, 255)
WHITE = (255, 255, 255)
GRAY = (90, 90, 90)
BLACK = (0, 0, 0)
FONT = cv2.FONT_HERSHEY_SIMPLEX

LabelFn = Callable[[str], str]


@runtime_checkable
class Display(Protocol):
    def show(self, frame: Frame, result: RecognitionResult | None, decision: DecisionOutput,
             door: Any, leds: Any, fps: float) -> int: ...
    def close(self) -> None: ...


def _text(img: np.ndarray, s: str, org: tuple[int, int], color: tuple[int, int, int], scale: float = 0.55,
          thick: int = 1) -> None:
    s = s.encode("ascii", "replace").decode("ascii")
    cv2.putText(img, s, org, FONT, scale, BLACK, thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, FONT, scale, color, thick, cv2.LINE_AA)


def draw_face_boxes(out: np.ndarray, result: RecognitionResult | None, label_fn: LabelFn | None = None,
                    show_scores: bool = True, with_text: bool = True) -> None:
    """Draws the face rectangles in place. with_text=False: rectangle + landmarks only (TkDisplay)."""
    if result is None:
        return
    for d in result.all_detections:
        if d is not result.detection:
            x, y, w, h = d.bbox
            cv2.rectangle(out, (x, y), (x + w, y + h), GRAY, 1)
    d = result.detection
    if d is None:
        return
    x, y, w, h = d.bbox
    m = result.match
    if result.verdict is FrameVerdict.MATCH and m is not None and m.best is not None:
        color = GREEN
        name = label_fn(m.best.person_id) if label_fn else m.best.person_id
        label = name + (f" {m.best.score:.3f}/{m.threshold:.3f}" if show_scores else "")
    elif result.verdict is FrameVerdict.UNKNOWN:
        color = RED
        label = "Unknown"
        if show_scores and m is not None and m.best is not None:
            label += f" {m.best.score:.3f}/{m.threshold:.3f}"
    else:
        color = YELLOW
        label = f"HOLD: {result.hold_reason.value if result.hold_reason else '?'}"
    cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
    for px, py in np.asarray(d.landmarks).reshape(-1, 2):
        cv2.circle(out, (int(px), int(py)), 2, color, -1)
    if with_text:
        _text(out, label, (x, max(15, y - 8)), color)


def render_overlay(image: np.ndarray, result: RecognitionResult | None, decision: DecisionOutput,
                   door: Any, leds: Any, fps: float, show_scores: bool = True,
                   label_fn: LabelFn | None = None) -> np.ndarray:
    """Pure function: draws on a copy. label_fn(person_id) -> display label (e.g. 'alice [L:12]')."""
    out = image.copy()
    H, W = out.shape[:2]
    draw_face_boxes(out, result, label_fn, show_scores)

    # top-right status panel
    is_open = bool(getattr(door, "is_open", False))
    remaining = float(getattr(door, "remaining_sec", 0.0) or 0.0)
    sig = f"DOOR SIGNAL: ON ({remaining:.1f}s)" if is_open else "DOOR SIGNAL: OFF"
    st = decision.state
    state_txt = {DecisionState.OPEN: "OPEN", DecisionState.DENIED: "DENIED", DecisionState.HOLD: "HOLD"}.get(
        st, st.value)
    lines: list[tuple[str, tuple[int, int, int]]] = [
        (sig, GREEN if is_open else WHITE),
        (f"STATE: {state_txt}  {decision.votes}/{decision.window}",
         GREEN if st in (DecisionState.OPEN, DecisionState.REARM) else RED if st is DecisionState.DENIED
         else YELLOW if st is DecisionState.HOLD else WHITE),
        (f"FPS: {fps:.1f}", WHITE),
    ]
    if result is not None and result.match is not None:
        lines.append((f"thr: {result.match.threshold_source}", WHITE))
    if decision.person_id and label_fn and st in (DecisionState.OPEN, DecisionState.VERIFYING, DecisionState.REARM):
        lines.append((label_fn(decision.person_id), GREEN))
    x0 = max(5, W - 260)
    y = 22
    for s, c in lines:
        _text(out, s, (x0, y), c)
        y += 22
    # virtual LEDs
    g, r, a = (bool(getattr(leds, k, False)) for k in ("green", "red", "amber"))
    for i, (on, col, ch) in enumerate(((g, GREEN, "G"), (r, RED, "R"), (a, YELLOW, "A"))):
        cx, cy = x0 + 12 + i * 34, y + 6
        cv2.circle(out, (cx, cy), 10, col if on else GRAY, -1)
        cv2.circle(out, (cx, cy), 10, WHITE, 1)
        _text(out, ch, (cx - 5, cy + 5), BLACK if on else WHITE, 0.45)
    return out


def recognition_text(result: RecognitionResult | None, decision: DecisionOutput, show_scores: bool = True,
                     name_fn: LabelFn | None = None) -> tuple[str, str]:
    """(Korean status phrase, colour key green|red|orange|gray) for the bottom status line (17.1)."""
    st = decision.state
    if st is DecisionState.DENIED:
        return T.REC_DENIED, "red"
    if result is None or result.verdict is FrameVerdict.NO_FACE:
        return T.REC_IDLE, "gray"
    m = result.match
    if result.verdict is FrameVerdict.HOLD:
        reason = result.hold_reason.value if result.hold_reason else None
        return T.REC_HOLD.format(reason=T.hold_reason_text(reason)), "orange"
    if result.verdict is FrameVerdict.MATCH and m is not None and m.best is not None:
        if st is DecisionState.VERIFYING:
            return T.REC_VERIFYING.format(votes=decision.votes, window=decision.window), "green"
        name = name_fn(m.best.person_id) if name_fn else m.best.person_id
        if show_scores:
            return T.REC_MATCH_SCORE.format(name=name, score=m.best.score, thr=m.threshold), "green"
        return T.REC_MATCH.format(name=name), "green"
    if result.verdict is FrameVerdict.UNKNOWN:
        if show_scores and m is not None and m.best is not None:
            return T.REC_UNKNOWN_SCORE.format(score=m.best.score, thr=m.threshold), "red"
        return T.REC_UNKNOWN, "red"
    return T.REC_IDLE, "gray"


class _OptionalMixin:
    """No-op defaults of the optional Display extensions."""

    def set_panel(self, rows: Sequence[LockerPanelRow]) -> None:
        return None

    def set_sim_handler(self, cb: Callable[[str | None, str], None] | None) -> None:
        return None

    def set_notice(self, text: str, ttl: float = 4.0) -> None:
        return None

    def set_timings(self, unlock_timeout: float, close_confirm: float, max_unlock: float) -> None:
        return None


class OpenCVDisplay(_OptionalMixin):
    def __init__(self, cfg: DisplaySection) -> None:
        self.cfg = cfg
        self.label_fn: LabelFn | None = None
        self._created = False
        self._rows: list[LockerPanelRow] = []
        self._sim = False

    def set_panel(self, rows: Sequence[LockerPanelRow]) -> None:
        self._rows = list(rows)

    def set_sim_handler(self, cb: Callable[[str | None, str], None] | None) -> None:
        self._sim = cb is not None

    def _imshow(self, img: np.ndarray) -> int:
        if self.cfg.scale != 1.0:
            img = cv2.resize(img, None, fx=self.cfg.scale, fy=self.cfg.scale)
        if not self._created:
            cv2.namedWindow(self.cfg.window_name, cv2.WINDOW_AUTOSIZE)
            self._created = True
        cv2.imshow(self.cfg.window_name, img)
        return cv2.waitKey(1) & 0xFF

    def show(self, frame: Frame, result: RecognitionResult | None, decision: DecisionOutput,
             door: Any, leds: Any, fps: float) -> int:
        img = render_overlay(frame.image, result, decision, door, leds, fps if self.cfg.show_fps else 0.0,
                             self.cfg.show_scores, self.label_fn)
        if self._rows and self.cfg.locker_panel:
            H, W = img.shape[:2]
            x0 = max(5, W - 260)
            y = 22 * 6 + 26
            for r in self._rows:
                bad = r.state in ("SENSOR_FAULT", "FAULT", "LOCKED_OPEN", "UNKNOWN")
                _text(img, ascii_line(r), (x0, y), YELLOW if bad else (GREEN if r.unlocked else WHITE))
                y += 22
            if self._sim:
                _text(img, "SIM: keys 1-4 door, 5-8 sensor fault", (5, H - 8), YELLOW, 0.45)
        k = self._imshow(img)
        return -1 if k == 255 else k

    def show_image(self, image: np.ndarray, lines: list[str] | None = None) -> int:
        """Free-form screen (enrollment guidance / menu)."""
        img = image.copy()
        for i, s in enumerate(lines or []):
            _text(img, s, (10, 28 + 26 * i), YELLOW, 0.7, 2)
        k = self._imshow(img)
        return -1 if k == 255 else k

    def close(self) -> None:
        if self._created:
            try:
                cv2.destroyWindow(self.cfg.window_name)
                cv2.waitKey(1)
            except cv2.error:
                pass
            self._created = False


class NullDisplay(_OptionalMixin):
    label_fn: LabelFn | None = None

    def show(self, frame: Frame, result: RecognitionResult | None, decision: DecisionOutput,
             door: Any, leds: Any, fps: float) -> int:
        return -1

    def show_image(self, image: np.ndarray, lines: list[str] | None = None) -> int:
        return -1

    def close(self) -> None:
        return None


# ------------------------------------------------------------------------------------------------ TkDisplay
_STATUS_FG = {"green": "#237804", "red": "#cf1322", "orange": "#d46b08", "gray": "#595959"}
_KEY_ESC = 27
_FONT_ALIASES = {"malgun gothic": ("맑은 고딕",), "nanumgothic": ("나눔고딕", "NanumGothic"),
                 "noto sans cjk kr": ("Noto Sans CJK KR", "Noto Sans KR")}


class DisplayError(FaceDoorError):
    """display.backend=tk but the window cannot be opened (exit code 1, no silent fallback)."""


class TkDisplay(_OptionalMixin):
    """Korean recognition window (INTERFACES 17.1). Video via cv2.imencode('.ppm') -> tk.PhotoImage; text in
    tk widgets. No mainloop: show() runs root.update_idletasks(); root.update() once per frame (main thread)."""

    PANEL_MIN_W, PANEL_MAX_W = 320, 360
    korean = True      # can draw Hangul: app passes display names as labels

    def __init__(self, cfg: DisplaySection, texts: Any = T) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self.cfg = cfg
        self.T = texts
        self.label_fn: LabelFn | None = None
        self.name_fn: LabelFn | None = None
        self._tk = tk
        self._closed = False
        self._destroyed = False
        self._keys: collections.deque[int] = collections.deque()
        self._sim_cb: Callable[[str | None, str], None] | None = None
        self._notice: tuple[str, float] | None = None
        self._rows: list[LockerPanelRow] = []
        self._timings = (10.0, 2.0, 30.0)
        self._panels: list[dict[str, Any]] = []
        self._panel_ids: tuple[str | None, ...] = ()
        self._photo: Any = None
        self._img_h = 0
        self._ui = 1.0
        self.last_render_ms = 0.0

        self.root = tk.Tk()
        self.root.title(f"{texts.WINDOW_TITLE} - {cfg.window_name}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Key>", self._on_key)
        self.root.configure(bg="#f0f0f0")
        try:
            self._ui = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)   # HiDPI scaling of pixel sizes
        except Exception:  # noqa: BLE001
            self._ui = 1.0
        self.family = self._pick_family(tkfont)
        sz = int(cfg.font_size)
        self.f_normal = tkfont.Font(root=self.root, family=self.family, size=sz)
        self.f_bold = tkfont.Font(root=self.root, family=self.family, size=sz, weight="bold")
        self.f_badge = tkfont.Font(root=self.root, family=self.family, size=int(sz * 1.5), weight="bold")
        self.f_small = tkfont.Font(root=self.root, family=self.family, size=max(8, int(sz * 0.8)))

        self.root.columnconfigure(0, weight=1)
        self.video = tk.Label(self.root, bg="#000000", width=64, height=24)
        self.video.grid(row=0, column=0, sticky="nw")
        self.panel_col = tk.Frame(self.root, bg="#f0f0f0", width=int(self.PANEL_MIN_W * self._ui))
        self.panel_col.grid(row=0, column=1, sticky="ns")
        self.panel_col.grid_propagate(False)
        self.status = tk.Label(self.root, text=texts.REC_IDLE, anchor="w", font=self.f_bold, bg="#f0f0f0",
                               fg=_STATUS_FG["gray"], padx=8, pady=4)
        self.status.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.hint = tk.Label(self.root, text="", anchor="w", font=self.f_small, bg="#f0f0f0", fg="#876800",
                             padx=8)
        self.hint.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.root.update_idletasks()
        self.root.update()

    # ---------------------------------------------------------------- setup helpers
    def _pick_family(self, tkfont: Any) -> str:
        try:
            have = {f.lower(): f for f in tkfont.families(self.root)}
        except Exception:  # noqa: BLE001
            have = {}
        for fam in self.cfg.font_families:
            # Windows lists Korean fonts under their localized names
            for cand in (fam, *_FONT_ALIASES.get(fam.lower(), ())):
                if cand.lower() in have:
                    return have[cand.lower()]
        log.warning(self.T.TK_FONT_MISSING)
        return "TkDefaultFont"

    def _on_close(self) -> None:
        self._keys.append(_KEY_ESC)

    def _on_key(self, event: Any) -> None:
        ch = getattr(event, "char", "") or ""
        if getattr(event, "keysym", "") == "Escape":
            self._keys.append(_KEY_ESC)
        elif len(ch) == 1 and ord(ch) < 128:
            self._keys.append(ord(ch))

    # ---------------------------------------------------------------- optional API
    def set_sim_handler(self, cb: Callable[[str | None, str], None] | None) -> None:
        self._sim_cb = cb
        for p in self._panels:
            self._apply_cursor(p)

    def set_notice(self, text: str, ttl: float = 4.0) -> None:
        self._notice = (text, time.monotonic() + max(0.0, ttl)) if text else None

    def set_timings(self, unlock_timeout: float, close_confirm: float, max_unlock: float) -> None:
        self._timings = (unlock_timeout, close_confirm, max_unlock)

    def set_panel(self, rows: Sequence[LockerPanelRow]) -> None:
        self._rows = list(rows)

    # ---------------------------------------------------------------- panels
    def _ensure_panels(self) -> None:
        ids = tuple(r.locker_id for r in self._rows)
        if ids == self._panel_ids:
            return
        tk = self._tk
        for p in self._panels:
            p["frame"].destroy()
        self._panels = []
        self._panel_ids = ids
        self.panel_col.columnconfigure(0, weight=1)
        self._fit_panel_col()
        for i, lid in enumerate(ids):
            fr = tk.Frame(self.panel_col, bd=1, relief="solid", bg="#d9d9d9")
            self.panel_col.rowconfigure(i, weight=1, uniform="p")
            fr.grid(row=i, column=0, sticky="nsew", padx=2, pady=2)
            top = tk.Frame(fr, bg="#d9d9d9")
            title = tk.Label(top, anchor="w", font=self.f_bold, bg="#d9d9d9")
            badge = tk.Label(top, font=self.f_badge, fg="#ffffff", bg="#595959", width=5)
            sub = tk.Label(fr, anchor="w", justify="left", font=self.f_normal, bg="#d9d9d9",
                           wraplength=int(self.PANEL_MIN_W * self._ui) - 24)
            small = tk.Label(fr, anchor="w", font=self.f_small, bg="#d9d9d9")
            bar = tk.Canvas(fr, height=8, highlightthickness=0, bg="#bfbfbf")
            top.pack(fill="x", padx=6, pady=(4, 0))
            badge.pack(side="right")
            title.pack(side="left", fill="x", expand=True)
            sub.pack(fill="x", padx=6)
            small.pack(fill="x", padx=6)
            bar.pack(fill="x", padx=6, pady=(2, 4))
            p = {"frame": fr, "top": top, "title": title, "badge": badge, "sub": sub, "small": small,
                 "bar": bar, "locker_id": lid, "sig": None}
            self._panels.append(p)
            for w in (fr, top, title, badge, sub, small, bar):
                w.bind("<Button-1>", lambda _e, _lid=lid: self._on_click(_lid))
            self._apply_cursor(p)

    def _panel_h(self) -> int:
        """Height of one panel from the font metrics (title/badge row + 2 wrapped lines + small line + bar)."""
        ls = lambda f: int(f.metrics("linespace"))  # noqa: E731
        return ls(self.f_badge) + 2 * ls(self.f_normal) + ls(self.f_small) + int(24 * self._ui)

    def _fit_panel_col(self) -> None:
        """Right column height: video height, but at least ~140 px per locker panel (the window grows)."""
        h = max(self._img_h, len(self._panel_ids) * self._panel_h())
        if h > 0:
            self.panel_col.configure(height=h)

    def _apply_cursor(self, p: dict[str, Any]) -> None:
        cur = "hand2" if self._sim_cb is not None else ""
        for k in ("frame", "top", "title", "badge", "sub", "small", "bar"):
            try:
                p[k].configure(cursor=cur)
            except self._tk.TclError:
                pass

    def _on_click(self, locker_id: str | None) -> None:
        cb = self._sim_cb
        if cb is not None:           # real hardware: handler is None, clicks are ignored
            cb(locker_id, "toggle")

    def _update_panels(self) -> None:
        if not self.cfg.locker_panel:
            return
        self._ensure_panels()
        ut, cc, mu = self._timings
        for p, row in zip(self._panels, self._rows):
            v = describe_row(row, ut, cc, mu)
            if p["sig"] == v:
                continue
            p["sig"] = v
            for k in ("frame", "top", "title", "sub", "small"):
                p[k].configure(bg=v.bg)
            p["title"].configure(text=v.title)
            p["badge"].configure(text=v.badge, bg=v.badge_bg)
            p["sub"].configure(text=v.sub, font=self.f_bold if v.bold else self.f_normal)
            p["small"].configure(text=v.small)
            bar = p["bar"]
            bar.delete("all")
            if v.progress is not None:
                w = bar.winfo_width()
                if w <= 1:      # not laid out yet
                    w = int(self.PANEL_MIN_W * self._ui) - 24
                bar.create_rectangle(0, 0, int(w * v.progress), 8, fill="#52c41a", width=0)

    # ---------------------------------------------------------------- main entry
    def show(self, frame: Frame, result: RecognitionResult | None, decision: DecisionOutput,
             door: Any, leds: Any, fps: float) -> int:
        if self._closed:
            return _KEY_ESC
        t0 = time.perf_counter()
        try:
            img = frame.image.copy()
            draw_face_boxes(img, result, None, False, with_text=False)
            if self.cfg.scale != 1.0:
                img = cv2.resize(img, None, fx=self.cfg.scale, fy=self.cfg.scale)
            ok, buf = cv2.imencode(".ppm", img)
            if ok:
                data = buf.tobytes()
                if self._photo is None:
                    self._photo = self._tk.PhotoImage(master=self.root, data=data)
                    self.video.configure(image=self._photo, width=img.shape[1], height=img.shape[0])
                    self._img_h = img.shape[0]
                    self.panel_col.configure(
                        width=int(self._ui * min(self.PANEL_MAX_W, max(self.PANEL_MIN_W, img.shape[1] * 0.55))))
                    self._fit_panel_col()
                else:
                    self._photo.configure(data=data)
            self._update_panels()
            self._update_status(result, decision, fps)
            hint = self.T.SIM_HINT if self._sim_cb is not None else ""
            if self.hint.cget("text") != hint:
                self.hint.configure(text=hint)
            self.root.update_idletasks()
            self.root.update()
        except self._tk.TclError:
            self._closed = True
            return _KEY_ESC
        self.last_render_ms = (time.perf_counter() - t0) * 1000.0
        return self._keys.popleft() if self._keys else -1

    def _update_status(self, result: RecognitionResult | None, decision: DecisionOutput, fps: float) -> None:
        now = time.monotonic()
        if self._notice is not None and now >= self._notice[1]:
            self._notice = None
        if self._notice is not None:
            text, color = self._notice[0], "orange"
        else:
            text, color = recognition_text(result, decision, self.cfg.show_scores, self.label_fn)
        extra = []
        if result is not None and result.match is not None and result.match.threshold_source:
            extra.append(self.T.REC_THR_SOURCE.format(source=result.match.threshold_source))
        if self.cfg.show_fps:
            extra.append(self.T.REC_FPS.format(fps=fps))
        full = "  ·  ".join([text] + extra)
        if self.status.cget("text") != full:
            self.status.configure(text=full)
        fg = _STATUS_FG[color]
        if str(self.status.cget("fg")) != fg:
            self.status.configure(fg=fg)

    def show_image(self, image: np.ndarray, lines: list[str] | None = None) -> int:
        return -1

    def close(self) -> None:
        self._closed = True
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------------------------------------ factory
def tk_available() -> tuple[bool, str]:
    """(usable, reason). tkinter importable and a display exists (Windows/macOS, or DISPLAY/WAYLAND_DISPLAY)."""
    if importlib.util.find_spec("tkinter") is None:
        return False, "tkinter is not installed (sudo apt install python3-tk)"
    if sys.platform not in ("win32", "darwin") and not (os.environ.get("DISPLAY") or
                                                         os.environ.get("WAYLAND_DISPLAY")):
        return False, "no DISPLAY/WAYLAND_DISPLAY"
    return True, ""


def create_display(cfg: DisplaySection, enabled: bool) -> Display:
    """display.backend: auto (tk when possible, else WARNING + opencv) | tk (failure = error exit 1) | opencv."""
    if not (enabled and cfg.enabled):
        return NullDisplay()
    backend = getattr(cfg, "backend", "auto")
    if backend == "opencv":
        return OpenCVDisplay(cfg)
    if backend == "web":
        from facedoor.webdisplay import WebDisplay   # lazy: webdisplay imports this module
        return WebDisplay(cfg)
    if backend == "tk":
        ok, why = tk_available()
        if not ok:
            raise DisplayError(T.TK_BACKEND_FAILED.format(detail=why))
        try:
            return TkDisplay(cfg)
        except Exception as e:  # noqa: BLE001 - TclError etc.
            raise DisplayError(T.TK_BACKEND_FAILED.format(detail=f"{type(e).__name__}: {e}")) from e
    ok, why = tk_available()
    if ok:
        try:
            return TkDisplay(cfg)
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {e}"
    log.warning(T.TK_FALLBACK.format(detail=why))
    return OpenCVDisplay(cfg)
