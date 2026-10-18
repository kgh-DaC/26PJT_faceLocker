"""Admin business logic (no tkinter). Contract: INTERFACES section 14.3 (+ 14.6, 14.7, 16).

AdminService is used by gui/admin_app.py and tested headless. Gallery / locker changes go through the same
functions as the CLIs (Enroller, FaceDatabase, lockers.check_assignment / assignment_issues).
The admin GUI never opens a door and never imports facedoor.hardware.

Dependencies implemented by other modules (imported lazily so the list screen works even if one is missing):
facedoor.runlock (RunLock, lock_holder), facedoor.locker_panel (read_state).
"""

from __future__ import annotations

import getpass
import importlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from facedoor import lockers as lockers_mod
from facedoor.config import AppConfig
from facedoor.database import FaceDatabase
from facedoor.enroll import EnrollProgress, EnrollReport, Enroller, validate_person_id
from facedoor.errors import CameraError, FaceDoorError, GalleryError, ModelError
from facedoor.gui import texts as T
from facedoor.timeutil import now_iso

log = logging.getLogger(__name__)

NAME_MAX_LEN = 20
NOTE_MAX_LEN = 100
_NAME_RE = re.compile(r"[0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ ._\-]+")
_AUTO_ID_RE = re.compile(r"^u(\d{4,})$")


# ------------------------------------------------------------------------------------- data classes
@dataclass(slots=True)
class UserSummary:
    person_id: str
    display_name: str
    locker_id: str | None
    n_samples: int
    tag_counts: dict[str, int]
    self_score_mean: float | None
    n_outliers: int
    weak_reasons: list[str]                     # Korean; empty = no reinforcement needed
    locker_problem: str | None = None           # (addition) LockerMap problem message (English) or None


@dataclass(slots=True)
class LockerRow:
    locker_id: str
    pin: int | None                             # single mode None
    owners: list[str]                           # person_id
    status: str                                 # "free" | "assigned" | "shared" | "shared_denied" | "unknown"


@dataclass(slots=True)
class PanelRow:
    """Door state for the admin panel (from locker_panel.read_state; INTERFACES section 16)."""
    locker_id: str | None                       # None = single-mode door
    state: str                                  # open | closed | cooldown | fault | unknown
    hold_remaining_sec: float = 0.0
    cooldown_remaining_sec: float = 0.0
    owner_label: str | None = None


@dataclass(slots=True)
class _GuiDefaults:
    """Fallback for cfg.gui (INTERFACES 14.9) while the config section is not available."""
    require_pin: bool = True
    preview_width: int = 480
    preview_fps: int = 15
    pose_countdown_sec: float = 2.0
    min_register_samples: int = 5
    weak_min_samples: int = 10
    weak_self_score: float = 0.45
    weak_required_tags: list[str] = field(default_factory=lambda: ["normal"])
    weak_frr: float = 0.2
    locker_choices: list[str] = field(default_factory=lambda: ["1", "2", "3", "4"])


class AdminError(Exception):
    """User-facing failure; str(e) is Korean."""


def _import_optional(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def gui_settings(cfg: AppConfig) -> Any:
    return getattr(cfg, "gui", None) or _GuiDefaults()


def font_settings(cfg: AppConfig) -> tuple[list[str], int]:
    """display.font_families / font_size (v1.4, shared with the recognition screen); legacy gui.* fallback."""
    disp = getattr(cfg, "display", None)
    gui = getattr(cfg, "gui", None)
    fams = (getattr(disp, "font_families", None) or getattr(gui, "font_families", None)
            or ["Malgun Gothic", "NanumGothic", "Noto Sans CJK KR"])
    size = int(getattr(disp, "font_size", None) or getattr(gui, "font_size", None) or 12)
    return list(fams), size


# ------------------------------------------------------------------------------------- service
class AdminService:
    def __init__(self, cfg: AppConfig, *, source: str | None = None,
                 camera_factory: Callable[[Any, str | None], Any] | None = None,
                 detector: Any = None, embedder: Any = None) -> None:
        """Loads the FaceDatabase (expected_model_id=None, so listing always works). Detector/embedder are loaded
        lazily on the first capture. Model file missing -> camera_status() False + MODEL_MISSING; gallery
        model_id != embedder model_id -> GALLERY_MISMATCH (capture blocked, list/assign/remove still work).
        source / camera_factory / detector / embedder are injection points (CLI --source, tests)."""
        self.cfg = cfg
        self.gui = gui_settings(cfg)
        self.source = source
        if camera_factory is None:
            from facedoor.camera import create_camera
            camera_factory = create_camera
        self._camera_factory = camera_factory
        self._detector = detector
        self._embedder = embedder
        self._capture_active = threading.Event()
        self.logs_dir = Path(cfg.app.logs_dir)
        self.camera_lock_path = self.logs_dir / "locks" / "camera.lock"
        self.gallery_lock_path = self.logs_dir / "locks" / "gallery.lock"
        self.locker_log = self.logs_dir / "locker_assign.log"
        self.admin_log = self.logs_dir / "admin.log"
        self.db = FaceDatabase.load(cfg.app.gallery_dir, expected_model_id=None)
        self.model_problem: str | None = None    # Korean message blocking capture
        self._check_models()

    # ---------------------------------------------------------------- model / gallery compatibility
    def _expected_model_id(self) -> str | None:
        if self._embedder is not None:
            return str(self._embedder.model_id)
        from facedoor.embedder import model_id_for
        e = self.cfg.embedder
        mdir = Path(self.cfg.app.models_dir)
        det_file = mdir / self.cfg.detector.model_file
        emb_file = mdir / (e.sface_model_file if e.backend == "sface" else e.arcface_model_file)
        if (self._detector is None and not det_file.is_file()) or not emb_file.is_file():
            raise ModelError(f"model file missing: {det_file if not det_file.is_file() else emb_file}")
        return model_id_for(e.backend, emb_file)

    def _check_models(self) -> None:
        self.model_problem = None
        try:
            mid = self._expected_model_id()
        except (ModelError, OSError) as e:
            log.warning("admin: %s", e)
            self.model_problem = T.MODEL_MISSING
            return
        if self.db.model_id is not None and len(self.db) and mid != self.db.model_id:
            log.warning("admin: gallery model_id %s != embedder %s", self.db.model_id, mid)
            self.model_problem = T.GALLERY_MISMATCH

    def _ensure_models(self) -> tuple[Any, Any]:
        try:
            if self._detector is None:
                from facedoor.detector import create_detector
                self._detector = create_detector(self.cfg.detector, self.cfg.app.models_dir)
            if self._embedder is None:
                from facedoor.embedder import create_embedder
                self._embedder = create_embedder(self.cfg.embedder, self.cfg.app.models_dir)
        except ModelError as e:
            log.error("admin: model load failed: %s", e)
            raise AdminError(T.MODEL_MISSING) from e
        return self._detector, self._embedder

    # ---------------------------------------------------------------- locks
    def _runlock(self) -> ModuleType:
        mod = _import_optional("facedoor.runlock")
        if mod is None:
            raise AdminError(T.ERROR_GENERIC.format(detail="facedoor.runlock 모듈이 없습니다"))
        return mod

    def _try_lock(self, path: Path, busy_msg: str) -> Any:
        mod = self._runlock()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = mod.RunLock(path, "admin_gui.py")
        if not lock.acquire():
            holder = None
            try:
                holder = mod.lock_holder(path)
            except Exception:  # noqa: BLE001
                pass
            raise AdminError(busy_msg.format(holder=holder or "?"))
        return lock

    def _gallery_lock(self) -> Any:
        return self._try_lock(self.gallery_lock_path, T.GALLERY_BUSY)

    def camera_holder(self) -> str | None:
        mod = _import_optional("facedoor.runlock")
        if mod is None:
            return None
        try:
            return mod.lock_holder(self.camera_lock_path)
        except Exception:  # noqa: BLE001
            log.exception("lock_holder failed")
            return None

    def camera_status(self) -> tuple[bool, str]:
        """(usable, Korean reason) - section 14.6."""
        if self.model_problem:
            return False, self.model_problem
        if _import_optional("facedoor.runlock") is None:
            return False, T.ERROR_GENERIC.format(detail="facedoor.runlock 모듈이 없습니다")
        if self._capture_active.is_set():
            return False, T.CAPTURE_RUNNING
        holder = self.camera_holder()
        if holder:
            return False, T.CAMERA_BUSY.format(holder=holder)
        return True, T.CAMERA_READY

    # ---------------------------------------------------------------- logs
    def _append(self, path: Path, cols: list[str]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            clean = [str(c).replace("\t", " ").replace("\n", " ") for c in cols]
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write("\t".join(clean) + "\n")
        except OSError as e:
            log.error("cannot append %s: %s", path, e)

    def _log_assign(self, action: str, pid: str, old: str | None, new: str | None, reason: str = "") -> None:
        """locker_assign.log (13.6): <ts> <os user> <set|clear> <pid> <old> <new> [+ reason column]."""
        cols = [now_iso(), _os_user(), action, pid, old or "-", new or "-"]
        if reason:
            cols.append(f"reason={reason}")
        self._append(self.locker_log, cols)

    def _log_admin(self, action: str, pid: str, detail: str = "") -> None:
        self._append(self.admin_log, [now_iso(), _os_user(), "admin_gui", action, pid, detail])

    # ---------------------------------------------------------------- read side
    def reload(self) -> None:
        self.db = FaceDatabase.load(self.cfg.app.gallery_dir, expected_model_id=None)
        self._check_models()

    @property
    def per_locker(self) -> bool:
        return getattr(self.cfg.lockers, "mode", "single") == lockers_mod.MODE_PER_LOCKER

    def configured(self) -> tuple[str, ...]:
        return lockers_mod.configured_lockers(self.cfg.hardware) if self.per_locker else ()

    def locker_choices(self) -> list[str]:
        if self.per_locker:
            return list(self.configured())
        return [str(c) for c in self.gui.locker_choices]

    def _pins(self) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for o in getattr(self.cfg.hardware, "locker_outputs", None) or ():
            lid = o.get("locker_id") if isinstance(o, dict) else getattr(o, "locker_id", None)
            pin = o.get("pin") if isinstance(o, dict) else getattr(o, "pin", None)
            if lid is not None:
                out[str(lid)] = pin
        return out

    def _latest_frr(self) -> dict[str, float]:
        rdir = Path(self.cfg.app.reports_dir)
        try:
            cands = sorted((p for p in rdir.iterdir() if (p / "metrics.json").is_file()), key=lambda p: p.name)
        except OSError:
            return {}
        if not cands:
            return {}
        try:
            d = json.loads((cands[-1] / "metrics.json").read_text(encoding="utf-8"))
            pp = d.get("per_person") or {}
            return {str(k): float(v["frr"]) for k, v in pp.items()
                    if isinstance(v, dict) and isinstance(v.get("frr"), (int, float))}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _locker_problems(self) -> dict[str, str]:
        try:
            lm = lockers_mod.LockerMap.build(self.db, self.cfg.lockers, self.configured())
            return {r.person_id: r.message for r in lm.problems()}
        except Exception:  # noqa: BLE001
            log.exception("LockerMap build failed")
            return {}

    def user(self, person_id: str) -> UserSummary | None:
        for u in self.users():
            if u.person_id == person_id:
                return u
        return None

    def users(self) -> list[UserSummary]:
        g = self.gui
        frr = self._latest_frr()
        problems = self._locker_problems()
        out: list[UserSummary] = []
        for pid in self.db.all_person_ids():
            samples = self.db.samples(pid)
            counts: dict[str, int] = {}
            for s in samples:
                counts[s.tag] = counts.get(s.tag, 0) + 1
            ss = self.db.self_score_mean(pid)
            n_out = len(self.db.find_outliers(pid, self.cfg.enroll.outlier_min_self_score))
            reasons: list[str] = []
            if len(samples) < g.weak_min_samples:
                reasons.append(T.WEAK_FEW_SAMPLES.format(n=len(samples), need=g.weak_min_samples))
            for t in g.weak_required_tags:
                if counts.get(t, 0) == 0:
                    reasons.append(T.WEAK_MISSING_TAG.format(tag=T.tag_label(t)))
            if ss is not None and ss < g.weak_self_score:
                reasons.append(T.WEAK_LOW_SELF.format(score=ss))
            if n_out:
                reasons.append(T.WEAK_OUTLIERS.format(n=n_out))
            if pid in frr and frr[pid] > g.weak_frr:
                reasons.append(T.WEAK_FRR.format(pct=frr[pid] * 100))
            out.append(UserSummary(pid, self.db.display_name(pid), self.db.locker_id(pid), len(samples), counts,
                                   ss, n_out, reasons, problems.get(pid)))
        return out

    def owners_of(self, locker_id: str) -> list[str]:
        return sorted(pid for pid in self.db.all_person_ids() if self.db.locker_id(pid) == locker_id)

    def locker_rows(self) -> list[LockerRow]:
        allow_shared = bool(getattr(self.cfg.lockers, "allow_shared", False))
        pins = self._pins()
        rows: list[LockerRow] = []
        choices = self.locker_choices()
        for lid in choices:
            owners = self.owners_of(lid)
            status = ("free" if not owners else "assigned" if len(owners) == 1
                      else "shared" if allow_shared else "shared_denied")
            rows.append(LockerRow(lid, pins.get(lid) if self.per_locker else None, owners, status))
        stray: dict[str, list[str]] = {}
        for pid in self.db.all_person_ids():
            lid = self.db.locker_id(pid)
            if lid is not None and lid not in choices:
                stray.setdefault(lid, []).append(pid)
        for lid in sorted(stray):
            rows.append(LockerRow(lid, None, sorted(stray[lid]), "unknown"))
        return rows

    def name_of(self, pid: str) -> str:
        return self.db.display_name(pid)

    # ---------------------------------------------------------------- door panel (section 16)
    def door_panel(self) -> tuple[list[PanelRow], str | None]:
        """(rows, Korean notice or None). Unknown (never 'closed') if the recognition program is not running,
        the state file is missing/stale, or locker_panel is unavailable."""
        ids: list[str | None] = list(self.locker_choices()) if self.per_locker else [None]
        unknown = [PanelRow(lid, "unknown") for lid in ids]
        holder = self.camera_holder()
        if not holder or self._capture_active.is_set():   # our own capture holds it -> program is off
            return unknown, T.PANEL_PROGRAM_OFF
        mod = _import_optional("facedoor.locker_panel")
        if mod is None:
            return unknown, T.PANEL_PROGRAM_OFF
        path = getattr(self.cfg.logging, "door_state_file", None) or (self.logs_dir / "door_state.json")
        try:
            rows = mod.read_state(Path(path), max_age_sec=3.0)
        except Exception:  # noqa: BLE001
            log.exception("read_state failed")
            rows = None
        if not rows:
            return unknown, T.PANEL_PROGRAM_OFF
        out = []
        for r in rows:
            state = str(getattr(r, "state", "unknown"))
            if state not in ("open", "closed", "cooldown", "fault", "unknown"):
                state = "unknown"
            out.append(PanelRow(getattr(r, "locker_id", None), state,
                                float(getattr(r, "hold_remaining_sec", 0.0) or 0.0),
                                float(getattr(r, "cooldown_remaining_sec", 0.0) or 0.0),
                                getattr(r, "owner_label", None)))
        return out, None

    # ---------------------------------------------------------------- validation
    def suggest_person_id(self) -> str:
        nums = [0]
        ids = set(self.db.all_person_ids())
        gdir = Path(self.cfg.app.gallery_dir)
        if gdir.is_dir():
            ids |= {p.name for p in gdir.iterdir() if p.is_dir()}
        for pid in ids:
            m = _AUTO_ID_RE.match(pid)
            if m:
                nums.append(int(m.group(1)))
        return f"u{max(nums) + 1:04d}"

    def _person_exists(self, pid: str) -> bool:
        gdir = Path(self.cfg.app.gallery_dir)
        return pid in self.db.all_person_ids() or (gdir / pid).exists() or \
            any(p.name.lower() == pid.lower() for p in (gdir.iterdir() if gdir.is_dir() else []))

    def validate_new_user(self, display_name: str, person_id: str) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        name = (display_name or "").strip()
        if not name:
            errors.append(T.NAME_EMPTY)
        elif len(name) > NAME_MAX_LEN:
            errors.append(T.NAME_TOO_LONG.format(max=NAME_MAX_LEN))
        elif not _NAME_RE.fullmatch(name):
            errors.append(T.NAME_INVALID)
        pid = (person_id or "").strip()
        if not pid:
            errors.append(T.ID_EMPTY)
        else:
            try:
                validate_person_id(pid, self.cfg.enroll.person_id_pattern)
            except ValueError:
                errors.append(T.ID_INVALID.format(pid=pid))
            else:
                if self._person_exists(pid):
                    errors.append(T.ID_EXISTS.format(pid=pid))
        if name and not errors:
            for other in self.db.all_person_ids():
                if self.db.display_name(other).strip() == name:
                    warnings.append(T.NAME_DUPLICATE.format(name=name, pid=other))
        return errors, warnings

    # ---------------------------------------------------------------- capture
    def capture(self, person_id: str, tag: str, *, new_user: bool, display_name: str | None = None,
                locker_id: str | None = None, note: str | None = None,
                progress: Callable[[EnrollProgress], None] | None = None,
                cancel: threading.Event | None = None) -> EnrollReport:
        """Worker thread. camera.lock -> gallery.lock -> create_camera/open -> Enroller.enroll_live -> close
        -> unlock (finally). New user: saved < gui.min_register_samples or cancelled -> remove_person rollback
        (report.rolled_back=True). Success: display_name / locker (locker failure keeps the registration and
        adds a Korean warning to report.warnings). Raises AdminError (Korean) for anything that prevents capture."""
        if self.model_problem:
            raise AdminError(self.model_problem)
        if tag not in self.cfg.enroll.tags:
            raise AdminError(T.TAG_INVALID.format(tag=tag))
        if note is not None and len(note) > NOTE_MAX_LEN:
            raise AdminError(T.NOTE_TOO_LONG.format(max=NOTE_MAX_LEN))
        if self._capture_active.is_set():
            raise AdminError(T.CAPTURE_RUNNING)
        self._capture_active.set()
        cam_lock = gal_lock = None
        camera = None
        try:
            # camera lock first: when the recognition program runs, the camera is never opened (G7)
            cam_lock = self._try_lock(self.camera_lock_path, T.CAMERA_BUSY)
            gal_lock = self._gallery_lock()
            self.reload()
            if self.model_problem:
                raise AdminError(self.model_problem)
            if new_user:
                errors, _ = self.validate_new_user(display_name or "", person_id)
                if errors:
                    raise AdminError("\n".join(errors))
            elif not self.db.samples(person_id):
                raise AdminError(T.USER_NOT_FOUND.format(pid=person_id))
            detector, embedder = self._ensure_models()
            if self.db.model_id is not None and len(self.db) and self.db.model_id != embedder.model_id:
                self.model_problem = T.GALLERY_MISMATCH
                raise AdminError(T.GALLERY_MISMATCH)
            enroller = Enroller(detector, embedder, self.db, self.cfg.quality, self.cfg.enroll)
            try:
                camera = self._camera_factory(self.cfg.camera, self.source)
                camera.open()
            except CameraError as e:
                log.error("admin: camera open failed: %s", e)
                raise AdminError(T.CAMERA_FAIL) from e
            try:
                report = enroller.enroll_live(camera, person_id, tag, None, list(self.cfg.enroll.poses), None,
                                              progress=progress, cancel=cancel,
                                              pose_countdown_sec=float(self.gui.pose_countdown_sec), note=note)
            except CameraError as e:
                log.error("admin: camera read failed: %s", e)
                if new_user:
                    self.db.remove_person(person_id)
                raise AdminError(T.CAMERA_FAIL) from e
            except BaseException:
                if new_user:
                    self.db.remove_person(person_id)
                raise
            finally:
                try:
                    camera.close()
                except Exception:  # noqa: BLE001
                    log.exception("camera close failed")
            if new_user:
                need = int(self.gui.min_register_samples)
                if report.cancelled or report.saved < need:
                    self.db.remove_person(person_id)
                    report.rolled_back = True
                    self._log_admin("register_rollback", person_id,
                                    f"saved={report.saved} need={need} cancelled={report.cancelled}")
                else:
                    self.db.set_person_info(person_id, display_name=(display_name or "").strip())
                    self._log_admin("register", person_id, f"tag={tag} saved={report.saved}")
                    if locker_id:
                        errs = self._assign_unlocked(person_id, locker_id, take_over=False, shared=False)
                        if errs:
                            report.warnings.append(T.LOCKER_ASSIGN_WARN.format(detail=" / ".join(errs)))
            else:
                self._log_admin("augment", person_id,
                                f"tag={tag} saved={report.saved} cancelled={report.cancelled}"
                                + (f" note={note}" if note else ""))
            return report
        except FaceDoorError as e:
            log.exception("admin capture failed")
            raise AdminError(T.ERROR_GENERIC.format(detail=e)) from e
        finally:
            for lk in (gal_lock, cam_lock):
                if lk is not None:
                    try:
                        lk.release()
                    except Exception:  # noqa: BLE001
                        log.exception("lock release failed")
            self._capture_active.clear()

    # ---------------------------------------------------------------- remove / lockers
    def remove_user(self, person_id: str) -> None:
        lock = self._gallery_lock()
        try:
            self.reload()
            if person_id not in self.db.all_person_ids() and not (Path(self.cfg.app.gallery_dir) / person_id).is_dir():
                raise AdminError(T.USER_NOT_FOUND.format(pid=person_id))
            old = self.db.locker_id(person_id)
            n = len(self.db.samples(person_id))
            name = self.db.display_name(person_id)
            self.db.remove_person(person_id)
            if old is not None:
                self._log_assign("clear", person_id, old, None, reason="remove")
            self._log_admin("remove", person_id, f"name={name} samples={n} locker={old or '-'}")
            log.info("admin: removed %s (%d samples, locker %s)", person_id, n, old)
        finally:
            lock.release()

    def _issue_text(self, issue: Any, locker_id: str) -> str:
        code = getattr(issue, "code", "")
        if code == "pattern" or code == "not_configured":
            return T.LOCKER_NOT_CONFIGURED.format(locker=locker_id)
        if code in ("taken", "shared_needs_flag"):
            owners = list(getattr(issue, "owners", ()) or ())
            owner = self.db.display_name(owners[0]) if owners else "?"
            return T.LOCKER_TAKEN.format(locker=locker_id, owner=owner)
        return T.LOCKER_REJECTED.format(detail=getattr(issue, "message", issue))

    def _assign_unlocked(self, person_id: str, locker_id: str, *, take_over: bool, shared: bool) -> list[str]:
        if not self.db.samples(person_id):
            return [T.USER_NOT_FOUND.format(pid=person_id)]
        locker_id = str(locker_id)
        if locker_id not in self.locker_choices():
            return [T.LOCKER_NOT_CONFIGURED.format(locker=locker_id)]
        allow_shared = bool(getattr(self.cfg.lockers, "allow_shared", False))
        if shared and not allow_shared:
            return [T.LOCKER_SHARED_NOT_ALLOWED]
        others = [p for p in self.owners_of(locker_id) if p != person_id]
        if others and not take_over and not shared:
            # duplicate warning in both modes (single mode check_assignment checks the format only)
            return [T.LOCKER_TAKEN.format(locker=locker_id, owner=self.db.display_name(others[0]))]
        cleared: list[str] = []
        if others and take_over:
            for o in others:
                self.db.set_person_info(o, locker_id=None)
                self._log_assign("clear", o, locker_id, None, reason=f"take_over_by={person_id}")
                cleared.append(o)
        issues = lockers_mod.assignment_issues(self.db, self.cfg.lockers, self.configured(), person_id, locker_id,
                                               shared_ok=shared)
        if issues:
            for o in cleared:  # restore what take_over removed
                self.db.set_person_info(o, locker_id=locker_id)
                self._log_assign("set", o, None, locker_id, reason="take_over_rollback")
            return [self._issue_text(i, locker_id) for i in issues]
        old = self.db.locker_id(person_id)
        self.db.set_person_info(person_id, locker_id=locker_id)
        self._log_assign("set", person_id, old, locker_id, reason="shared" if shared and others else "")
        self._log_admin("assign_locker", person_id, f"{old or '-'} -> {locker_id}")
        return []

    def assign_locker(self, person_id: str, locker_id: str, *, take_over: bool = False,
                      shared: bool = False) -> list[str]:
        """[] = success, else Korean errors. take_over=True: previous owners' locker_id -> null first
        (each change is a locker_assign.log row)."""
        try:
            lock = self._gallery_lock()
        except AdminError as e:
            return [str(e)]
        try:
            self.reload()
            return self._assign_unlocked(person_id, locker_id, take_over=take_over, shared=shared)
        finally:
            lock.release()

    def clear_locker(self, person_id: str) -> None:
        lock = self._gallery_lock()
        try:
            self.reload()
            if not self.db.samples(person_id):
                raise AdminError(T.USER_NOT_FOUND.format(pid=person_id))
            old = self.db.locker_id(person_id)
            if old is None:
                return
            self.db.set_person_info(person_id, locker_id=None)
            self._log_assign("clear", person_id, old, None)
            self._log_admin("clear_locker", person_id, old)
        finally:
            lock.release()


__all__ = ["AdminService", "AdminError", "UserSummary", "LockerRow", "PanelRow", "gui_settings", "font_settings",
           "GalleryError"]
