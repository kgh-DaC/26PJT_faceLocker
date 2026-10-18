"""Korean admin GUI (tkinter, no PIL). Contract: INTERFACES section 14 (+ 16 door panel).

- Starts with PIN (setup on first run) -> main menu. The camera is opened only when [등록 시작] /
  [추가 촬영 시작] is pressed, and closed as soon as the capture ends or is cancelled.
- Widgets are touched only on the main thread. One worker thread runs AdminService.capture(); its progress
  callback encodes preview frames (PPM) into a size-1 queue that the main thread polls with root.after().
- All user-visible strings come from facedoor.gui.texts.
"""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk
from typing import Any, Callable

from facedoor import adminpin
from facedoor.enroll import EnrollProgress, EnrollReport
from facedoor.gui import preview as pv
from facedoor.gui import texts as T
from facedoor.gui.service import AdminError, AdminService, font_settings

log = logging.getLogger(__name__)

# Tk on Korean Windows reports localized family names ("맑은 고딕"), on Linux "NanumGothic"/"나눔고딕".
FONT_ALIASES: dict[str, list[str]] = {
    "Malgun Gothic": ["맑은 고딕"],
    "NanumGothic": ["나눔고딕", "Nanum Gothic"],
    "Noto Sans CJK KR": ["Noto Sans KR", "Noto Sans CJK KR Regular"],
}
COLOR_RED = "#c62828"
COLOR_GREEN = "#2e7d32"
COLOR_GRAY = "#616161"
COLOR_YELLOW = "#b28704"


def pick_font_family(wanted: list[str], available: list[str] | tuple[str, ...]) -> str | None:
    """First family of `wanted` (or one of its known aliases) present in tkinter.font.families()."""
    avail = {a.lower(): a for a in available}
    for fam in wanted:
        for cand in [fam] + FONT_ALIASES.get(fam, []):
            if cand.lower() in avail:
                return avail[cand.lower()]
    return None


# ================================================================================ small widgets / dialogs
class ChoiceDialog:
    """Modal dialog with custom buttons. Returns the key of the pressed button (None = closed)."""

    def __init__(self, parent: tk.Misc, title: str, message: str, buttons: list[tuple[str, str]],
                 default: str, bold: str | None = None) -> None:
        self.result: str | None = None
        top = self.top = tk.Toplevel(parent)
        top.title(title)
        top.transient(parent.winfo_toplevel())
        top.resizable(False, False)
        body = ttk.Frame(top, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, wraplength=460, justify="left").pack(anchor="w")
        if bold:
            f = tkfont.nametofont("TkDefaultFont").copy()
            f.configure(weight="bold")
            self._bold_font = f
            tk.Label(body, text=bold, font=f, fg=COLOR_RED, wraplength=460, justify="left").pack(anchor="w",
                                                                                                pady=(10, 0))
        row = ttk.Frame(body)
        row.pack(anchor="e", pady=(14, 0))
        for key, label in buttons:
            b = ttk.Button(row, text=label, command=lambda k=key: self._done(k))
            b.pack(side="left", padx=4)
            if key == default:
                b.focus_set()
                top.bind("<Return>", lambda _e, k=key: self._done(k))
        top.bind("<Escape>", lambda _e: self._done(None))
        top.protocol("WM_DELETE_WINDOW", lambda: self._done(None))
        top.grab_set()
        parent.wait_window(top)

    def _done(self, key: str | None) -> None:
        self.result = key
        self.top.grab_release()
        self.top.destroy()


def ask_choice(parent: tk.Misc, title: str, message: str, buttons: list[tuple[str, str]], default: str,
               bold: str | None = None) -> str | None:
    return ChoiceDialog(parent, title, message, buttons, default, bold).result


# ================================================================================ capture runner
class CaptureRunner:
    """Runs AdminService.capture on one worker thread; preview + progress go through a size-1 queue."""

    def __init__(self, app: "AdminApp") -> None:
        self.app = app
        self.thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.queue = pv.LatestQueue()
        self.result: EnrollReport | None = None
        self.error: BaseException | None = None
        self._on_done: Callable[[EnrollReport | None, BaseException | None], None] | None = None
        self._view: CaptureView | None = None

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, view: "CaptureView", on_done: Callable[[EnrollReport | None, BaseException | None], None],
              **kwargs: Any) -> None:
        if self.active:
            return
        self.cancel = threading.Event()
        self.queue = pv.LatestQueue()
        self.result = self.error = None
        self._on_done = on_done
        self._view = view
        width = int(self.app.service.gui.preview_width)
        q = self.queue

        def progress(p: EnrollProgress) -> None:   # worker thread: no tkinter here
            img = None
            if p.frame is not None:
                color = (pv.COLOR_SAVED if p.just_saved else pv.COLOR_REJECT if p.last_reject else pv.COLOR_WAIT)
                try:
                    img = pv.encode_preview(p.frame, width, p.bbox, color, fmt=pv.preferred_format())
                except Exception:  # noqa: BLE001
                    log.exception("preview encode failed")
            p.frame = None
            q.put((img, p))

        def work() -> None:
            try:
                self.result = self.app.service.capture(progress=progress, cancel=self.cancel, **kwargs)
            except BaseException as e:  # noqa: BLE001 - reported on the main thread
                self.error = e

        view.begin()
        self.thread = threading.Thread(target=work, name="admin-capture", daemon=True)
        self.thread.start()
        self.app.on_capture_state(True)
        self._poll()

    def _poll(self) -> None:
        view = self._view
        item = self.queue.get()
        if item is not None and view is not None:
            img, p = item
            view.update_progress(img, p)
        if self.thread is not None and not self.thread.is_alive():
            self.thread = None
            if view is not None:
                view.end()
            self.app.on_capture_state(False)
            if self._on_done is not None:
                self._on_done(self.result, self.error)
            return
        fps = max(1, int(self.app.service.gui.preview_fps))
        self.app.root.after(max(10, 1000 // fps), self._poll)

    def request_cancel(self) -> None:
        self.cancel.set()

    def shutdown(self, timeout: float = 3.0) -> None:
        self.cancel.set()
        if self.thread is not None:
            self.thread.join(timeout)


class CaptureView(ttk.Frame):
    """Preview (frame + colored box only) and Korean guidance labels."""

    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master)
        self.app = app
        w = int(app.service.gui.preview_width)
        self._idle_size = (max(20, w // 9), max(8, (w * 3 // 4) // 20))   # in characters (text mode)
        self.preview = tk.Label(self, text=T.PREVIEW_PLACEHOLDER, bg="#202020", fg="#dddddd",
                                width=self._idle_size[0], height=self._idle_size[1])
        self.preview.grid(row=0, column=0, sticky="nw")
        self._photo: Any = None
        self.guide = ttk.Label(self, text="", style="Guide.TLabel", wraplength=w)
        self.guide.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.progress_text = ttk.Label(self, text="")
        self.progress_text.grid(row=2, column=0, sticky="w")
        self.bar = ttk.Progressbar(self, length=w, mode="determinate")
        self.bar.grid(row=3, column=0, sticky="w", pady=4)
        self.reject = tk.Label(self, text="", fg=COLOR_RED, wraplength=w, justify="left")
        self.reject.grid(row=4, column=0, sticky="w")

    def idle(self, text: str) -> None:
        self.guide.configure(text=text)

    def begin(self) -> None:
        self.guide.configure(text=T.CAPTURE_PREPARING)
        self.progress_text.configure(text="")
        self.reject.configure(text="")
        self.bar.configure(value=0, maximum=1)

    def update_progress(self, img: pv.PreviewImage | None, p: EnrollProgress) -> None:
        if img is not None:
            photo = pv.photo_from_preview(img, self)
            if photo is not None:
                self._photo = photo   # keep a reference
                self.preview.configure(image=photo, text="", width=img.width, height=img.height)
        if p.phase in ("capturing", "pose_change") and p.pose:
            if p.countdown_sec is not None and p.countdown_sec > 0.05:
                self.guide.configure(text=f"{T.pose_text(p.pose)} — "
                                          + T.CAPTURE_COUNTDOWN.format(sec=int(p.countdown_sec + 0.99)))
            else:
                self.guide.configure(text=T.pose_text(p.pose))
            self.progress_text.configure(text=T.CAPTURE_PROGRESS.format(
                pose=T.POSE_SHORT.get(p.pose, p.pose), idx=p.pose_index + 1, n=p.n_poses,
                saved=p.saved, target=p.target))
            self.bar.configure(maximum=max(1, p.target), value=p.saved)
            if p.just_saved:
                self.reject.configure(text=T.CAPTURE_SAVED, fg=COLOR_GREEN)
            elif p.last_reject:
                self.reject.configure(text=T.reject_text(p.last_reject), fg=COLOR_RED)

    def end(self) -> None:
        self._photo = None
        self.preview.configure(image="", text=T.PREVIEW_PLACEHOLDER, width=self._idle_size[0],
                               height=self._idle_size[1])
        self.reject.configure(text="")


# ================================================================================ door panel
class DoorPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master, text=T.PANEL_TITLE, padding=8)
        self.app = app
        self.rows: list[tk.Label] = []
        self.notice = tk.Label(self, text="", fg=COLOR_YELLOW, wraplength=220, justify="left")
        self.notice.pack(side="bottom", anchor="w", pady=(6, 0))

    def refresh(self) -> None:
        try:
            rows, notice = self.app.service.door_panel()
        except Exception:  # noqa: BLE001
            log.exception("door panel refresh failed")
            return
        while len(self.rows) < len(rows):
            lb = tk.Label(self, text="", anchor="w", justify="left")
            lb.pack(anchor="w", fill="x")
            self.rows.append(lb)
        while len(self.rows) > len(rows):
            self.rows.pop().destroy()
        colors = {"open": COLOR_GREEN, "closed": COLOR_GRAY, "cooldown": COLOR_GRAY, "fault": COLOR_RED,
                  "unknown": COLOR_YELLOW}
        for lb, r in zip(self.rows, rows):
            lb.configure(text=T.panel_text(r.locker_id, r.state, r.hold_remaining_sec, r.cooldown_remaining_sec,
                                           r.owner_label), fg=colors.get(r.state, COLOR_YELLOW))
        self.notice.configure(text=notice or "")


# ================================================================================ screens
class Screen(ttk.Frame):
    title = ""

    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master, padding=10)
        self.app = app
        self.svc = app.service

    def on_show(self) -> None:  # pragma: no cover - GUI
        pass

    def set_capture_buttons(self, usable: bool, running: bool) -> None:  # pragma: no cover - GUI
        pass


def _fmt_tags(counts: dict[str, int]) -> str:
    return ", ".join(f"{T.tag_label(t)} {n}" for t, n in sorted(counts.items())) or T.NO_VALUE


def _fmt_score(v: float | None) -> str:
    return T.NO_VALUE if v is None else f"{v:.2f}"


class HomeScreen(Screen):
    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master, app)
        ttk.Label(self, text=T.APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, text=T.HOME_GUIDE, wraplength=520).pack(anchor="w", pady=10)
        self.cam = tk.Label(self, text="", wraplength=520, justify="left")
        self.cam.pack(anchor="w")

    def on_show(self) -> None:
        ok, why = self.svc.camera_status()
        self.cam.configure(text=why, fg=COLOR_GREEN if ok else COLOR_RED)


class RegisterScreen(Screen):
    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master, app)
        ttk.Label(self, text=T.REG_TITLE, style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        form = ttk.Frame(self)
        form.grid(row=1, column=0, sticky="nw", pady=8)
        ttk.Label(form, text=T.REG_NAME).grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(form, textvariable=self.name_var, width=24)
        self.name_entry.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(form, text=T.REG_ID).grid(row=1, column=0, sticky="w")
        self.id_var = tk.StringVar()
        self.id_entry = ttk.Entry(form, textvariable=self.id_var, width=24, state="readonly")
        self.id_entry.grid(row=1, column=1, sticky="w", pady=2)
        self.adv_btn = ttk.Button(form, text=T.BTN_ADVANCED, command=self._advanced)
        self.adv_btn.grid(row=1, column=2, padx=4)
        ttk.Label(form, text=T.REG_ID_HINT, style="Hint.TLabel").grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Label(form, text=T.REG_LOCKER).grid(row=3, column=0, sticky="w")
        self.locker_var = tk.StringVar()
        self.locker_box = ttk.Combobox(form, textvariable=self.locker_var, state="readonly", width=30)
        self.locker_box.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        self._locker_map: dict[str, str | None] = {}
        btns = ttk.Frame(form)
        btns.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text=T.BTN_START_REGISTER, command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text=T.BTN_CANCEL, command=self._cancel)
        self.cancel_btn.pack(side="left", padx=6)
        self.cam_msg = tk.Label(form, text="", fg=COLOR_RED, wraplength=320, justify="left")
        self.cam_msg.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.view = CaptureView(self, app)
        self.view.grid(row=1, column=1, sticky="nw", padx=(16, 0), pady=8)
        self.view.idle(T.CAPTURE_IDLE)

    def _advanced(self) -> None:
        self.id_entry.configure(state="normal")
        self.id_entry.focus_set()

    def on_show(self) -> None:
        if self.app.runner.active:
            return
        self.svc.reload()
        self.id_var.set(self.svc.suggest_person_id())
        self.id_entry.configure(state="readonly")
        values = [T.LOCKER_NONE]
        self._locker_map = {T.LOCKER_NONE: None}
        for lid in self.svc.locker_choices():
            owners = self.svc.owners_of(lid)
            label = (T.LOCKER_IN_USE.format(locker=lid, owner=", ".join(self.svc.name_of(o) for o in owners))
                     if owners else lid)
            values.append(label)
            self._locker_map[label] = lid
        self.locker_box.configure(values=values)
        self.locker_var.set(T.LOCKER_NONE)
        self.view.idle(T.CAPTURE_IDLE)
        self.name_entry.focus_set()

    def set_capture_buttons(self, usable: bool, running: bool) -> None:
        self.start_btn.configure(state="normal" if usable and not running else "disabled")
        self.cam_msg.configure(text="" if usable or running else self.svc.camera_status()[1])

    def _start(self) -> None:
        if self.app.runner.active:
            return
        name = self.name_var.get().strip()
        pid = self.id_var.get().strip()
        errors, warnings = self.svc.validate_new_user(name, pid)
        if errors:
            messagebox.showerror(T.TITLE_ERROR, "\n".join(errors), parent=self)
            return
        for w in warnings:
            if not messagebox.askyesno(T.TITLE_CONFIRM, w, default=messagebox.NO, parent=self):
                return
        locker = self._locker_map.get(self.locker_var.get())
        if locker is not None:
            others = self.svc.owners_of(locker)
            if others:
                messagebox.showwarning(T.TITLE_WARNING, T.LOCKER_TAKEN.format(
                    locker=locker, owner=self.svc.name_of(others[0])), parent=self)
                return
        ok, why = self.svc.camera_status()
        if not ok:
            messagebox.showerror(T.TITLE_ERROR, why, parent=self)
            return
        self.app.runner.start(self.view, lambda rep, err: self._done(rep, err, name),
                              person_id=pid, tag="normal", new_user=True, display_name=name, locker_id=locker)

    def _cancel(self) -> None:
        if self.app.runner.active:
            self.view.idle(T.CAPTURE_CANCELLING)
            self.app.runner.request_cancel()
        else:
            self.name_var.set("")
            self.on_show()

    def _done(self, rep: EnrollReport | None, err: BaseException | None, name: str) -> None:
        if err is not None:
            self.app.show_error(err)
        elif rep is not None:
            if rep.rolled_back:
                msg = T.REGISTER_CANCELLED if rep.cancelled else T.REGISTER_FAIL.format(
                    saved=rep.saved, need=self.svc.gui.min_register_samples)
                messagebox.showwarning(T.TITLE_WARNING, msg, parent=self)
            else:
                msg = T.REGISTER_OK.format(name=name, saved=rep.saved)
                if rep.warnings:
                    msg += "\n\n" + "\n".join(rep.warnings)
                messagebox.showinfo(T.TITLE_INFO, msg + "\n\n" + T.RESTART_NOTICE, parent=self)
                self.name_var.set("")
        self.on_show()
        self.app.refresh_status()


class AugmentScreen(Screen):
    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master, app)
        ttk.Label(self, text=T.AUG_TITLE, style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        form = ttk.Frame(self)
        form.grid(row=1, column=0, sticky="nw", pady=8)
        ttk.Label(form, text=T.AUG_USER).grid(row=0, column=0, sticky="w")
        self.user_var = tk.StringVar()
        self.user_box = ttk.Combobox(form, textvariable=self.user_var, state="readonly", width=28)
        self.user_box.grid(row=0, column=1, sticky="w")
        self.user_box.bind("<<ComboboxSelected>>", lambda _e: self._show_user())
        self._users: dict[str, str] = {}
        self.info = ttk.Label(form, text="", wraplength=340, justify="left")
        self.info.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.weak = tk.Label(form, text="", fg=COLOR_RED, wraplength=340, justify="left")
        self.weak.grid(row=2, column=0, columnspan=2, sticky="w")
        cond = ttk.LabelFrame(form, text=T.AUG_CONDITION, padding=6)
        cond.grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        self.tag_var = tk.StringVar(value="normal")
        tags = [t for t in T.AUGMENT_TAG_ORDER if t in self.svc.cfg.enroll.tags]
        for i, t in enumerate(tags):
            ttk.Radiobutton(cond, text=T.tag_label(t), value=t, variable=self.tag_var,
                            command=self._tag_changed).grid(row=i // 3, column=i % 3, sticky="w", padx=4)
        ttk.Label(form, text=T.AUG_NOTE).grid(row=4, column=0, sticky="w")
        self.note_var = tk.StringVar()
        self.note_entry = ttk.Entry(form, textvariable=self.note_var, width=28, state="disabled")
        self.note_entry.grid(row=4, column=1, sticky="w")
        ttk.Label(form, text=T.MASK_HAT_HINT, style="Hint.TLabel", wraplength=340).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(form, text=T.LOWLIGHT_HINT, style="Hint.TLabel", wraplength=340).grid(
            row=6, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(form)
        btns.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text=T.BTN_START_AUGMENT, command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text=T.BTN_STOP_CAPTURE, command=self._cancel)
        self.cancel_btn.pack(side="left", padx=6)
        self.cam_msg = tk.Label(form, text="", fg=COLOR_RED, wraplength=340, justify="left")
        self.cam_msg.grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.view = CaptureView(self, app)
        self.view.grid(row=1, column=1, sticky="nw", padx=(16, 0), pady=8)
        self.view.idle(T.CAPTURE_IDLE_AUGMENT)

    def _tag_changed(self) -> None:
        self.note_entry.configure(state="normal" if self.tag_var.get() == "other" else "disabled")

    def on_show(self) -> None:
        if self.app.runner.active:
            return
        self.svc.reload()
        current = self._users.get(self.user_var.get())
        self._users = {}
        labels = []
        for u in self.svc.users():
            if u.n_samples == 0:
                continue
            label = f"{u.display_name} ({u.person_id})" + (f" — {T.WEAK_MARK}" if u.weak_reasons else "")
            labels.append(label)
            self._users[label] = u.person_id
        self.user_box.configure(values=labels)
        keep = [lb for lb, pid in self._users.items() if pid == current]
        self.user_var.set(keep[0] if keep else (labels[0] if labels else ""))
        self._show_user()

    def _show_user(self) -> None:
        pid = self._users.get(self.user_var.get())
        u = self.svc.user(pid) if pid else None
        if u is None:
            self.info.configure(text=T.USER_NOT_SELECTED)
            self.weak.configure(text="")
            return
        self.info.configure(text=T.AUG_INFO.format(n=u.n_samples, tags=_fmt_tags(u.tag_counts),
                                                   score=_fmt_score(u.self_score_mean)))
        self.weak.configure(text=T.AUG_WEAK.format(reasons=" / ".join(u.weak_reasons)) if u.weak_reasons
                            else T.AUG_NOT_WEAK, fg=COLOR_RED if u.weak_reasons else COLOR_GREEN)

    def set_capture_buttons(self, usable: bool, running: bool) -> None:
        self.start_btn.configure(state="normal" if usable and not running else "disabled")
        self.cam_msg.configure(text="" if usable or running else self.svc.camera_status()[1])

    def _start(self) -> None:
        if self.app.runner.active:
            return
        pid = self._users.get(self.user_var.get())
        if not pid:
            messagebox.showwarning(T.TITLE_WARNING, T.USER_NOT_SELECTED, parent=self)
            return
        ok, why = self.svc.camera_status()
        if not ok:
            messagebox.showerror(T.TITLE_ERROR, why, parent=self)
            return
        tag = self.tag_var.get()
        note = (self.note_var.get().strip() or None) if tag == "other" else None
        name = self.svc.name_of(pid)
        self.app.runner.start(self.view, lambda rep, err: self._done(rep, err, name, tag),
                              person_id=pid, tag=tag, new_user=False, note=note)

    def _cancel(self) -> None:
        if self.app.runner.active:
            self.view.idle(T.CAPTURE_CANCELLING)
            self.app.runner.request_cancel()

    def _done(self, rep: EnrollReport | None, err: BaseException | None, name: str, tag: str) -> None:
        if err is not None:
            self.app.show_error(err)
        elif rep is not None:
            if rep.cancelled:
                messagebox.showinfo(T.TITLE_INFO, T.AUGMENT_CANCELLED.format(saved=rep.saved), parent=self)
            elif rep.saved == 0:
                messagebox.showwarning(T.TITLE_WARNING, T.AUGMENT_NONE, parent=self)
            else:
                messagebox.showinfo(T.TITLE_INFO, T.AUGMENT_OK.format(name=name, tag=T.tag_label(tag),
                                                                      saved=rep.saved)
                                    + "\n\n" + T.RESTART_NOTICE, parent=self)
        self.on_show()


class ListScreen(Screen):
    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master, app)
        ttk.Label(self, text=T.LIST_TITLE, style="Title.TLabel").pack(anchor="w")
        cols = ("name", "pid", "locker", "n", "tags", "self", "weak")
        heads = (T.COL_NAME, T.COL_ID, T.COL_LOCKER, T.COL_SAMPLES, T.COL_TAGS, T.COL_SELF, T.COL_WEAK)
        widths = (110, 70, 60, 60, 220, 70, 260)
        self.users = ttk.Treeview(self, columns=cols, show="headings", height=9, selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.users.heading(c, text=h)
            self.users.column(c, width=w, anchor="w")
        self.users.tag_configure("problem", foreground=COLOR_RED)
        self.users.pack(fill="x", pady=(8, 4))
        ctl = ttk.Frame(self)
        ctl.pack(anchor="w", pady=4)
        ttk.Label(ctl, text=T.LOCKER_SELECT).pack(side="left")
        self.locker_var = tk.StringVar()
        self.locker_box = ttk.Combobox(ctl, textvariable=self.locker_var, state="readonly", width=8)
        self.locker_box.pack(side="left", padx=4)
        ttk.Button(ctl, text=T.BTN_ASSIGN, command=self._assign).pack(side="left", padx=4)
        ttk.Button(ctl, text=T.BTN_UNASSIGN, command=self._clear).pack(side="left", padx=4)
        ttk.Button(ctl, text=T.BTN_REFRESH, command=self.on_show).pack(side="left", padx=4)
        self.mode_note = ttk.Label(self, text="", style="Hint.TLabel", wraplength=700)
        self.mode_note.pack(anchor="w")
        lcols = ("locker", "pin", "owner", "status")
        lheads = (T.COL_LOCKER_ID, T.COL_PIN, T.COL_OWNER, T.COL_STATUS)
        self.lockers = ttk.Treeview(self, columns=lcols, show="headings", height=5, selectmode="none")
        for c, h, w in zip(lcols, lheads, (90, 70, 260, 120)):
            self.lockers.heading(c, text=h)
            self.lockers.column(c, width=w, anchor="w")
        self.lockers.tag_configure("problem", foreground=COLOR_RED)
        self.lockers.pack(fill="x", pady=(8, 0))

    def on_show(self) -> None:
        self.svc.reload()
        self.users.delete(*self.users.get_children())
        for u in self.svc.users():
            weak = (T.WEAK_MARK + ": " + " / ".join(u.weak_reasons)) if u.weak_reasons else T.WEAK_OK
            if u.locker_problem:
                weak = T.LOCKER_PROBLEM.format(detail=u.locker_problem) + " · " + weak
            self.users.insert("", "end", iid=u.person_id, values=(
                u.display_name, u.person_id, u.locker_id or T.NO_VALUE, u.n_samples, _fmt_tags(u.tag_counts),
                _fmt_score(u.self_score_mean), weak), tags=("problem",) if u.locker_problem else ())
        self.lockers.delete(*self.lockers.get_children())
        for r in self.svc.locker_rows():
            owners = ", ".join(f"{self.svc.name_of(o)}({o})" for o in r.owners) or T.NO_VALUE
            bad = r.status in ("shared_denied", "unknown")
            self.lockers.insert("", "end", values=(r.locker_id, T.NO_VALUE if r.pin is None else f"BCM{r.pin}",
                                                   owners, T.LOCKER_STATUS.get(r.status, r.status)),
                                tags=("problem",) if bad else ())
        self.locker_box.configure(values=self.svc.locker_choices())
        if not self.locker_var.get() and self.svc.locker_choices():
            self.locker_var.set(self.svc.locker_choices()[0])
        self.mode_note.configure(text="" if self.svc.per_locker else T.LOCKER_SINGLE_MODE)

    def _selected(self) -> str | None:
        sel = self.users.selection()
        if not sel:
            messagebox.showwarning(T.TITLE_WARNING, T.USER_NOT_SELECTED, parent=self)
            return None
        return sel[0]

    def _assign(self) -> None:
        pid = self._selected()
        lid = self.locker_var.get()
        if not pid or not lid:
            return
        take_over = shared = False
        others = [o for o in self.svc.owners_of(lid) if o != pid]
        if others:
            owner = self.svc.name_of(others[0])
            buttons = [("take_over", T.BTN_TAKE_OVER.format(owner=owner))]
            if getattr(self.svc.cfg.lockers, "allow_shared", False):
                buttons.append(("shared", T.BTN_SHARE))
            buttons.append(("cancel", T.BTN_CANCEL))
            choice = ask_choice(self, T.TITLE_WARNING, T.LOCKER_TAKEN.format(locker=lid, owner=owner), buttons,
                                default="cancel")
            if choice not in ("take_over", "shared"):
                return
            take_over, shared = choice == "take_over", choice == "shared"
        errs = self.svc.assign_locker(pid, lid, take_over=take_over, shared=shared)
        if errs:
            messagebox.showerror(T.TITLE_ERROR, "\n".join(errs), parent=self)
        else:
            msg = T.LOCKER_ASSIGNED.format(name=self.svc.name_of(pid), locker=lid)
            if not self.svc.per_locker:
                msg += "\n" + T.LOCKER_SINGLE_MODE
            messagebox.showinfo(T.TITLE_INFO, msg + "\n\n" + T.RESTART_NOTICE, parent=self)
        self.on_show()

    def _clear(self) -> None:
        pid = self._selected()
        if not pid:
            return
        name = self.svc.name_of(pid)
        if self.svc.db.locker_id(pid) is None:
            messagebox.showinfo(T.TITLE_INFO, T.LOCKER_NOTHING.format(name=name), parent=self)
            return
        try:
            self.svc.clear_locker(pid)
        except AdminError as e:
            messagebox.showerror(T.TITLE_ERROR, str(e), parent=self)
        else:
            messagebox.showinfo(T.TITLE_INFO, T.LOCKER_CLEARED.format(name=name) + "\n\n" + T.RESTART_NOTICE,
                                parent=self)
        self.on_show()


class RemoveScreen(Screen):
    def __init__(self, master: tk.Misc, app: "AdminApp") -> None:
        super().__init__(master, app)
        ttk.Label(self, text=T.REMOVE_TITLE, style="Title.TLabel").pack(anchor="w")
        cols = ("name", "pid", "locker", "n")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12, selectmode="browse")
        for c, h, w in zip(cols, (T.COL_NAME, T.COL_ID, T.COL_LOCKER, T.COL_SAMPLES), (160, 100, 80, 80)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", pady=8)
        ttk.Button(self, text=T.BTN_REMOVE_SELECTED, command=self._remove).pack(anchor="w")

    def on_show(self) -> None:
        self.svc.reload()
        self.tree.delete(*self.tree.get_children())
        for u in self.svc.users():
            self.tree.insert("", "end", iid=u.person_id,
                             values=(u.display_name, u.person_id, u.locker_id or T.NO_VALUE, u.n_samples))

    def _remove(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning(T.TITLE_WARNING, T.USER_NOT_SELECTED, parent=self)
            return
        pid = sel[0]
        u = self.svc.user(pid)
        if u is None:
            self.on_show()
            return
        msg = T.REMOVE_CONFIRM.format(name=u.display_name, pid=pid, n=u.n_samples, locker=u.locker_id or T.NO_VALUE)
        if not messagebox.askyesno(T.TITLE_CONFIRM, msg, icon=messagebox.WARNING, default=messagebox.NO,
                                   parent=self):
            return
        try:
            self.svc.remove_user(pid)
        except AdminError as e:
            messagebox.showerror(T.TITLE_ERROR, str(e), parent=self)
        except Exception as e:  # noqa: BLE001
            self.app.show_error(e)
        else:
            ask_choice(self, T.TITLE_INFO, T.REMOVE_DONE.format(name=u.display_name, pid=pid),
                       [("ok", T.BTN_OK)], default="ok", bold=T.REMOVE_RESTART_WARN + "\n" + T.RESTART_NOTICE)
        self.on_show()
        self.app.refresh_status()


# ================================================================================ PIN
class PinScreen(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "AdminApp", on_ok: Callable[[], None]) -> None:
        super().__init__(master, padding=30)
        self.app = app
        self.on_ok = on_ok
        self.acfg = app.service.cfg.admin
        self.setup = not adminpin.pin_is_set(self.acfg)
        ttk.Label(self, text=T.PIN_TITLE, style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        min_len = max(4, int(self.acfg.min_pin_len))
        prompt = T.PIN_SETUP.format(min=min_len) if self.setup else T.PIN_PROMPT
        ttk.Label(self, text=prompt, wraplength=420).grid(row=1, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Label(self, text=T.PIN_LABEL).grid(row=2, column=0, sticky="w")
        self.pin1 = ttk.Entry(self, show="*", width=16)
        self.pin1.grid(row=2, column=1, sticky="w", pady=2)
        self.pin2: ttk.Entry | None = None
        if self.setup:
            ttk.Label(self, text=T.PIN_LABEL_AGAIN).grid(row=3, column=0, sticky="w")
            self.pin2 = ttk.Entry(self, show="*", width=16)
            self.pin2.grid(row=3, column=1, sticky="w", pady=2)
        self.msg = tk.Label(self, text="", fg=COLOR_RED, wraplength=420, justify="left")
        self.msg.grid(row=4, column=0, columnspan=2, sticky="w", pady=6)
        self.ok_btn = ttk.Button(self, text=T.BTN_OK, command=self._submit)
        self.ok_btn.grid(row=5, column=0, sticky="w")
        ttk.Button(self, text=T.MENU_EXIT, command=app.close).grid(row=5, column=1, sticky="w")
        self.pin1.focus_set()
        self.bind_all("<Return>", lambda _e: self._submit())

    def _submit(self) -> None:
        pin = self.pin1.get()
        try:
            if self.setup:
                if self.pin2 is not None and pin != self.pin2.get():
                    self.msg.configure(text=T.PIN_SETUP_MISMATCH)
                    return
                adminpin.set_pin(self.acfg, pin)
                messagebox.showinfo(T.TITLE_INFO, T.PIN_SETUP_DONE, parent=self)
                self._ok()
                return
            res = adminpin.verify_pin(self.acfg, pin)
        except adminpin.PinFileError as e:
            messagebox.showerror(T.TITLE_ERROR, str(e), parent=self)
            self.app.close()
            return
        except ValueError as e:
            self.msg.configure(text=str(e))
            return
        self.pin1.delete(0, "end")
        if res is adminpin.PinResult.OK:
            self._ok()
        elif res is adminpin.PinResult.LOCKED:
            mins = max(1, int(adminpin.lock_remaining_sec(self.acfg) / 60 + 0.999))
            self.msg.configure(text=T.PIN_LOCKED.format(min=mins))
        elif res is adminpin.PinResult.WRONG:
            self.msg.configure(text=T.PIN_WRONG.format(n=adminpin.attempts_left(self.acfg)))
        else:  # NOT_SET (file deleted meanwhile) -> restart setup flow
            self.msg.configure(text=T.PIN_SETUP.format(min=max(4, int(self.acfg.min_pin_len))))

    def _ok(self) -> None:
        self.unbind_all("<Return>")
        self.on_ok()


# ================================================================================ main app
class AdminApp:
    def __init__(self, root: tk.Tk, service: AdminService) -> None:
        self.root = root
        self.service = service
        self.runner = CaptureRunner(self)
        self.font_family = self._setup_fonts()
        root.title(T.APP_TITLE)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.minsize(980, 640)
        self.screens: dict[str, Screen] = {}
        self.current: str | None = None
        self.menu_buttons: list[ttk.Button] = []
        self._closing = False
        self._pin_frame: PinScreen | None = None

    # ---------------------------------------------------------------- setup
    def _setup_fonts(self) -> str | None:
        wanted, size = font_settings(self.service.cfg)
        fam = pick_font_family(wanted, tkfont.families(self.root))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                     "TkTooltipFont", "TkSmallCaptionFont", "TkIconFont"):
            try:
                f = tkfont.nametofont(name)
                if fam:
                    f.configure(family=fam)
                f.configure(size=size)
            except tk.TclError:
                pass
        style = ttk.Style(self.root)
        base = (fam or "TkDefaultFont", size)
        style.configure("Title.TLabel", font=(base[0], size + 4, "bold"))
        style.configure("Guide.TLabel", font=(base[0], size + 3, "bold"))
        style.configure("Hint.TLabel", foreground=COLOR_GRAY)
        style.configure("Treeview", rowheight=int(size * 2.2))
        if fam is None:
            log.warning("no Korean font among %s; install fonts-nanum (sudo apt install fonts-nanum)", wanted)
        return fam

    def start(self) -> None:
        if self.font_family is None:
            messagebox.showwarning(T.TITLE_WARNING, T.TK_FONT_MISSING, parent=self.root)
        err = adminpin.pin_file_error(self.service.cfg.admin)
        if bool(getattr(self.service.gui, "require_pin", True)):
            if err:
                messagebox.showerror(T.TITLE_ERROR, err, parent=self.root)
                self.close()
                return
            self._pin_frame = PinScreen(self.root, self, self._enter_main)
            self._pin_frame.pack(fill="both", expand=True)
        else:
            log.warning("gui.require_pin=false: admin GUI without PIN (development only)")
            self._enter_main(dev_warning=True)

    def _enter_main(self, dev_warning: bool = False) -> None:
        if self._pin_frame is not None:
            self._pin_frame.destroy()
            self._pin_frame = None
        root = self.root
        self.status = ttk.Label(root, text="", padding=(10, 4), relief="groove")
        self.status.pack(side="top", fill="x")
        if dev_warning:
            tk.Label(root, text=T.PIN_DISABLED_WARNING, fg=COLOR_RED).pack(side="top", anchor="w", padx=10)
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        menu = ttk.Frame(body, padding=10)
        menu.pack(side="left", fill="y")
        ttk.Label(menu, text=T.MENU_HEADER, style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        content = ttk.Frame(body)
        content.pack(side="left", fill="both", expand=True)
        self.panel = DoorPanel(body, self)
        self.panel.pack(side="right", fill="y", padx=10, pady=10)
        classes: list[tuple[str, str, type[Screen]]] = [
            ("register", T.MENU_REGISTER, RegisterScreen), ("list", T.MENU_LIST, ListScreen),
            ("augment", T.MENU_AUGMENT, AugmentScreen), ("remove", T.MENU_REMOVE, RemoveScreen)]
        self.screens["home"] = HomeScreen(content, self)
        for key, label, cls in classes:
            self.screens[key] = cls(content, self)
            b = ttk.Button(menu, text=label, width=22, command=lambda k=key: self.show(k))
            b.pack(anchor="w", pady=3, fill="x")
            self.menu_buttons.append(b)
        ttk.Button(menu, text=T.MENU_EXIT, width=22, command=self.close).pack(anchor="w", pady=(18, 3), fill="x")
        self.show("home")
        self._tick()

    def show(self, key: str) -> None:
        if self.runner.active:
            return
        if self.current:
            self.screens[self.current].pack_forget()
        self.current = key
        scr = self.screens[key]
        scr.pack(fill="both", expand=True)
        try:
            scr.on_show()
        except Exception as e:  # noqa: BLE001
            self.show_error(e)
        self.refresh_status()

    # ---------------------------------------------------------------- periodic status
    def refresh_status(self) -> None:
        svc = self.service
        mode = (T.STATUS_MODE_PER_LOCKER.format(n=len(svc.locker_choices())) if svc.per_locker
                else T.STATUS_MODE_SINGLE)
        n = sum(1 for u in svc.db.all_person_ids() if svc.db.samples(u))
        ok, why = svc.camera_status()
        running = self.runner.active
        cam = T.STATUS_CAMERA_OK if ok else T.STATUS_CAMERA_NO
        text = f"{mode}   |   {T.STATUS_USERS.format(n=n)}   |   {cam}"
        if not ok and not running:
            text += f"  — {why}"
        self.status.configure(text=text)
        for scr in self.screens.values():
            scr.set_capture_buttons(ok, running)

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            self.refresh_status()
            self.panel.refresh()
        except Exception:  # noqa: BLE001
            log.exception("status refresh failed")
        self.root.after(1000, self._tick)

    def on_capture_state(self, running: bool) -> None:
        for b in self.menu_buttons:
            b.configure(state="disabled" if running else "normal")
        if hasattr(self, "status"):
            self.refresh_status()

    # ---------------------------------------------------------------- errors / close
    def show_error(self, err: BaseException) -> None:
        if isinstance(err, AdminError):
            msg = str(err)
        else:
            log.error("admin GUI error: %r", err, exc_info=err)
            msg = T.ERROR_GENERIC.format(detail=f"{type(err).__name__}: {err}")
        messagebox.showerror(T.TITLE_ERROR, msg, parent=self.root)

    def close(self) -> None:
        if self._closing:
            return
        if self.runner.active:
            if not messagebox.askyesno(T.TITLE_CONFIRM, T.CLOSE_WHILE_CAPTURE, default=messagebox.NO,
                                       parent=self.root):
                return
            # cancel -> join worker (max 3 s); capture() closes the camera and releases locks in finally
            self.runner.shutdown(3.0)
        self._closing = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def run_app(service: AdminService) -> int:
    root = tk.Tk()
    app = AdminApp(root, service)
    root.after(0, app.start)
    t0 = time.monotonic()
    root.mainloop()
    log.info("admin GUI closed after %.0f s", time.monotonic() - t0)
    return 0
