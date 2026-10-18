"""Configuration loading/merging/validation (owner: vision-engineer). Contract: INTERFACES section 2.

Merge order: configs/default.yaml < configs/local.yaml (if use_local) < path < overrides ("a.b=value", value parsed as YAML).
Unknown keys, type errors and range errors raise ConfigError. Relative paths become absolute under project_root().
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from facedoor.errors import ConfigError

if typing.TYPE_CHECKING:  # pragma: no cover
    from facedoor.hardware.base import HardwareConfig

log = logging.getLogger(__name__)


def project_root() -> Path:
    """FACEDOOR_HOME env var, else two levels above src/facedoor."""
    env = os.environ.get("FACEDOOR_HOME")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


DEFAULT_CONFIG: Path = project_root() / "configs" / "default.yaml"


# ----------------------------------------------------------------------------- sections
@dataclass(slots=True)
class AppSection:
    gallery_dir: Path = Path("data/gallery")
    probe_dir: Path = Path("data/probe")
    models_dir: Path = Path("models")
    logs_dir: Path = Path("logs")
    reports_dir: Path = Path("reports")
    calibration_file: Path = Path("models/calibration.json")
    log_level: str = "INFO"
    condition_tag: str = "live"
    allow_empty_gallery: bool = False


@dataclass(slots=True)
class RuntimeSection:
    max_fps: float = 15
    process_every_n: int = 1
    stop_on_camera_error: bool = True


@dataclass(slots=True)
class CameraSection:
    source: str = "0"
    backend: str = "auto"
    width: int = 640
    height: int = 480
    fps: float = 30
    fourcc: str = "MJPG"
    buffer_size: int = 1
    read_retries: int = 5
    retry_delay_sec: float = 0.2
    file_loop: bool = False
    file_fps: float = 0


@dataclass(slots=True)
class DetectorSection:
    backend: str = "yunet"
    model_file: str = "face_detection_yunet_2023mar.onnx"
    score_threshold: float = 0.85
    nms_threshold: float = 0.3
    top_k: int = 50
    input_scale: float = 1.0


@dataclass(slots=True)
class QualitySection:
    min_face_px: int = 80
    min_det_score: float = 0.90
    min_blur_var: float = 60.0
    min_brightness: float = 50
    max_brightness: float = 210
    max_abs_yaw_ratio: float = 0.45
    border_margin_px: int = 4


@dataclass(slots=True)
class EmbedderSection:
    backend: str = "sface"
    sface_model_file: str = "face_recognition_sface_2021dec.onnx"
    arcface_model_file: str = "arcface_w600k_r50.onnx"
    onnx_providers: list[str] = field(default_factory=lambda: ["CPUExecutionProvider"])
    clahe: bool = False
    clahe_clip: float = 2.0
    clahe_grid: int = 8


@dataclass(slots=True)
class RecognitionSection:
    score: str = "cosine"
    aggregation: str = "max"
    topk: int = 3
    margin: float = 0.05
    default_thresholds: dict[str, dict[str, float]] = field(default_factory=lambda: {
        "sface": {"global": 0.50, "occluded": 0.55}, "arcface": {"global": 0.45, "occluded": 0.50}})
    threshold_floor: dict[str, float] = field(default_factory=lambda: {"sface": 0.40, "arcface": 0.30})
    occluded_tags: list[str] = field(default_factory=lambda: ["mask", "scarf"])
    occluded_policy: str = "threshold"
    unreliable_calibration: str = "max_with_default"
    multi_face_policy: str = "largest"


@dataclass(slots=True)
class LivenessSection:
    backend: str = "none"
    required: bool = False


@dataclass(slots=True)
class DecisionSection:
    window_frames: int = 5
    min_votes: int = 3
    window_timeout_sec: float = 2.0
    reset_on_unknown: bool = True
    reset_on_other_person: bool = True
    rearm_sec: float = 5.0
    deny_after_unknown: int = 3
    deny_cooldown_sec: float = 2.0


@dataclass(slots=True)
class DisplaySection:
    enabled: bool = True
    window_name: str = "FaceDoor"
    show_fps: bool = True
    show_scores: bool = True
    scale: float = 1.0
    # (v1.4, INTERFACES 16/17) Korean recognition screen + locker panel
    backend: str = "auto"                 # auto | tk | opencv | web (MJPEG over HTTP for headless devices)
    web_host: str = "0.0.0.0"             # web backend bind address (127.0.0.1 = this device only). No authentication
    web_port: int = 8080                  # web backend port
    locker_panel: bool = True
    locker_owner: str = "when_open"       # none | when_open | always
    font_families: list[str] = field(default_factory=lambda: ["Malgun Gothic", "NanumGothic", "Noto Sans CJK KR"])
    font_size: int = 14


@dataclass(slots=True)
class LoggingSection:
    events_csv: Path = Path("logs/events.csv")
    app_log: Path = Path("logs/facedoor.log")
    log_every_frame: bool = True
    log_no_face: bool = False
    events_max_mb: float = 50
    save_snapshots: bool = False
    snapshot_dir: Path = Path("logs/snapshots")
    door_state_file: Path = Path("logs/door_state.json")   # (v1.4) locker panel state shared with the admin GUI


@dataclass(slots=True)
class EnrollSection:
    poses: list[str] = field(default_factory=lambda: ["front", "left", "right", "up", "down"])
    samples_per_pose: int = 2
    tags: list[str] = field(default_factory=lambda: ["normal", "glasses", "noglasses", "mask", "scarf",
                                                     "lowlight", "backlight"])
    capture_interval_sec: float = 0.4
    crop_margin: float = 0.6
    max_abs_yaw_ratio: float = 0.8
    dedup_score: float = 0.995
    outlier_min_self_score: float = 0.30
    person_id_pattern: str = "^[A-Za-z0-9_-]{1,32}$"


@dataclass(slots=True)
class EvaluationSection:
    target_far: float = 0.001
    calibration_fraction: float = 0.5
    split_by: str = "person"
    seed: int = 42
    bootstrap_iters: int = 1000
    ci: float = 0.95
    conditions: list[str] = field(default_factory=lambda: ["normal", "lowlight", "backlight", "mask", "glasses",
                                                           "noglasses", "scarf", "angle"])
    occluded_conditions: list[str] = field(default_factory=lambda: ["mask", "scarf", "syn_mask", "syn_scarf"])
    synthetic_prefix: str = "syn_"
    impostor_dir_name: str = "_impostor"
    count_hold_as_reject: bool = True
    synthetic_conditions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AdminSection:
    """Admin PIN protection for enroll/delete menus (INTERFACES section 12)."""
    pin_file: Path = Path("data/admin_pin.json")
    min_pin_len: int = 4
    max_attempts: int = 3
    pbkdf2_iterations: int = 200_000
    lockout_sec: float = 300.0          # (v1.3) lock time after max_attempts consecutive failures, 0 < v <= 86400


@dataclass(slots=True)
class LockersSection:
    """Locker routing (INTERFACES sections 12 and 13). mode=single: one solenoid output for everyone.
    mode=per_locker: each person's assigned locker channel only (hardware.locker_outputs)."""
    mode: str = "single"
    require_locker_id: bool = False
    locker_id_pattern: str = "^[A-Za-z0-9_-]{1,16}$"
    allow_shared: bool = False


@dataclass(slots=True)
class GuiSection:
    """Korean admin GUI (INTERFACES 14.9). Fonts moved to display.font_families/font_size (v1.4)."""
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


@dataclass(slots=True)
class ModelSpec:
    file: str
    url: str = ""
    sha256: str | None = None


@dataclass(slots=True)
class AppConfig:
    schema_version: int
    app: AppSection
    runtime: RuntimeSection
    camera: CameraSection
    detector: DetectorSection
    quality: QualitySection
    embedder: EmbedderSection
    recognition: RecognitionSection
    liveness: LivenessSection
    decision: DecisionSection
    hardware: "HardwareConfig"
    display: DisplaySection
    logging: LoggingSection
    enroll: EnrollSection
    evaluation: EvaluationSection
    models: dict[str, ModelSpec]
    admin: AdminSection = field(default_factory=AdminSection)
    lockers: LockersSection = field(default_factory=LockersSection)
    gui: GuiSection = field(default_factory=GuiSection)
    raw: dict = field(default_factory=dict)


_SECTIONS: dict[str, type] = {
    "app": AppSection, "runtime": RuntimeSection, "camera": CameraSection, "detector": DetectorSection,
    "quality": QualitySection, "embedder": EmbedderSection, "recognition": RecognitionSection,
    "liveness": LivenessSection, "decision": DecisionSection, "display": DisplaySection,
    "logging": LoggingSection, "enroll": EnrollSection, "evaluation": EvaluationSection,
    "admin": AdminSection, "lockers": LockersSection, "gui": GuiSection,
}
_OPTIONAL_SECTIONS = {"admin", "lockers", "gui"}


# ----------------------------------------------------------------------------- helpers
def _deep_merge(base: dict, over: Mapping) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _deep_merge(dict(out[k]), v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}") from e
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    return data


def parse_override(item: str) -> tuple[list[str], Any]:
    if "=" not in item:
        raise ConfigError(f"override must be key.sub=value: {item!r}")
    key, val = item.split("=", 1)
    keys = [k for k in key.strip().split(".") if k]
    if not keys:
        raise ConfigError(f"empty override key: {item!r}")
    try:
        value = yaml.safe_load(val) if val.strip() != "" else ""
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid override value {item!r}: {e}") from e
    return keys, value


def _apply_override(d: dict, keys: list[str], value: Any) -> None:
    cur = d
    for k in keys[:-1]:
        nxt = cur.get(k)
        if nxt is None:
            nxt = {}
            cur[k] = nxt
        if not isinstance(nxt, dict):
            raise ConfigError(f"override path {'.'.join(keys)} goes through non-mapping key {k!r}")
        cur = nxt
    cur[keys[-1]] = value


def _resolve_path(v: Any, root: Path, where: str) -> Path:
    if not isinstance(v, (str, Path)):
        raise ConfigError(f"{where}: expected path string, got {type(v).__name__}")
    p = Path(v)
    return p if p.is_absolute() else (root / p)


def _coerce(value: Any, tp: Any, where: str, root: Path) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        non_none = [a for a in args if a is not type(None)]
        last: Exception | None = None
        for a in non_none:
            try:
                return _coerce(value, a, where, root)
            except ConfigError as e:
                last = e
        raise last or ConfigError(f"{where}: invalid value {value!r}")
    if tp is Path:
        return _resolve_path(value, root, where)
    if tp is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{where}: expected bool, got {value!r}")
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected int, got {value!r}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected number, got {value!r}")
        return float(value)
    if tp is str:
        if isinstance(value, bool) or value is None:
            raise ConfigError(f"{where}: expected string, got {value!r}")
        if isinstance(value, (int, float)):
            return str(value)
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected string, got {value!r}")
        return value
    if origin is list:
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where}: expected list, got {value!r}")
        (et,) = args or (Any,)
        return [_coerce(x, et, f"{where}[{i}]", root) for i, x in enumerate(value)]
    if origin is dict:
        if not isinstance(value, Mapping):
            raise ConfigError(f"{where}: expected mapping, got {value!r}")
        kt, vt = args or (Any, Any)
        return {str(k): _coerce(v, vt, f"{where}.{k}", root) for k, v in value.items()}
    if tp is Any:
        return value
    if is_dataclass(tp):
        return _build_dataclass(tp, value, where, root)
    return value


def _build_dataclass(cls: type, data: Any, where: str, root: Path) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{where}: expected mapping, got {data!r}")
    hints = typing.get_type_hints(cls)
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _coerce(data[f.name], hints[f.name], f"{where}.{f.name}", root)
        elif isinstance(hints[f.name], type) and hints[f.name] is Path:
            # default relative paths are also made absolute
            default = f.default
            kwargs[f.name] = _resolve_path(default, root, f"{where}.{f.name}")
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise ConfigError(f"{where}: {e}") from e


_NEW_HW_KEYS: dict[str, tuple[Any, ...]] = {
    "locker_outputs": ([], (), None),
    "max_concurrent_open": (1,),
    "close_mode": ("timer",),
}


def _build_hardware(data: Any, root: Path) -> "HardwareConfig":
    from facedoor.hardware.base import HardwareConfig  # hw-owned

    d = dict(data or {})
    # Keys added in v1.3/v1.4 are parsed by HardwareConfig.from_dict (hw-owned). If the installed hw code is
    # older and does not know a key, accept it only when it carries its "feature off" default; anything else
    # is a ConfigError (never silently ignore e.g. locker_outputs, which would fall back to the single output).
    known = {f.name for f in fields(HardwareConfig)}
    for key, off_values in _NEW_HW_KEYS.items():
        if key in d and key not in known:
            if d[key] in off_values:
                del d[key]
            else:
                raise ConfigError(f"hardware.{key}={d[key]!r} requires hardware support that is not installed "
                                  "(facedoor.hardware.base.HardwareConfig has no such field)")
    if "hardware_log" in d and d["hardware_log"] is not None:
        d["hardware_log"] = _resolve_path(d["hardware_log"], root, "hardware.hardware_log")
    try:
        hw = HardwareConfig.from_dict(d)
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"hardware: {e}") from e
    log = getattr(hw, "hardware_log", None)
    if log is not None and not Path(log).is_absolute():
        try:
            hw.hardware_log = root / Path(log)
        except Exception:  # noqa: BLE001  (frozen dataclass: leave as is)
            pass
    return hw


def _validate_lockers(cfg: AppConfig) -> list[str]:
    """INTERFACES 13.2 cross-section rules owned by config.py (hw-internal rules live in HardwareConfig.validate)."""
    errs: list[str] = []
    lk, hw = cfg.lockers, cfg.hardware
    outputs = tuple(getattr(hw, "locker_outputs", ()) or ())
    try:
        pat = re.compile(lk.locker_id_pattern)
    except (re.error, TypeError) as e:
        errs.append(f"lockers.locker_id_pattern is not a valid regex: {e}")
        pat = None
    if lk.mode not in {"single", "per_locker"}:
        errs.append(f"lockers.mode must be single|per_locker (got {lk.mode!r})")
        return errs
    if lk.mode == "per_locker":
        if not outputs:
            errs.append("lockers.mode=per_locker requires at least one hardware.locker_outputs entry")
        if hw.solenoid_pin is not None:
            errs.append("lockers.mode=per_locker requires hardware.solenoid_pin: null "
                        "(declare BCM17 as a locker channel to reuse it)")
        if not lk.require_locker_id:
            log.info("lockers.mode=per_locker: require_locker_id is treated as true")
    elif outputs:
        errs.append("hardware.locker_outputs is set but lockers.mode is 'single' "
                    "(set lockers.mode: per_locker, or locker_outputs: [])")
    if pat is not None:
        for o in outputs:
            if not isinstance(o.locker_id, str) or pat.fullmatch(o.locker_id) is None:
                errs.append(f"hardware.locker_outputs: locker_id {o.locker_id!r} does not match "
                            f"lockers.locker_id_pattern {lk.locker_id_pattern!r}")
    return errs


def _validate_close_mode(cfg: AppConfig) -> list[str]:
    """INTERFACES 15.2: cross-section checks for close_mode (range checks live in HardwareConfig.validate)
    + rearm recommendations (WARNING only)."""
    errs: list[str] = []
    hw, d = cfg.hardware, cfg.decision
    mode = getattr(hw, "close_mode", "timer")
    if mode not in {"timer", "limit"}:
        errs.append(f"hardware.close_mode must be limit|timer (got {mode!r}; 'hold' was replaced by 'limit')")
        return errs
    if d.rearm_sec < hw.cooldown_sec:
        log.warning("decision.rearm_sec=%.1f < hardware.cooldown_sec=%.1f (recommended >=)", d.rearm_sec,
                    hw.cooldown_sec)
    if mode == "timer" and d.rearm_sec < hw.open_sec + hw.cooldown_sec:
        log.warning("decision.rearm_sec=%.1f < hardware.open_sec + cooldown_sec = %.1f (recommended >=)",
                    d.rearm_sec, hw.open_sec + hw.cooldown_sec)
    if mode == "limit":
        log.info("hardware.close_mode=limit: open_sec/max_open_sec are ignored (unlock_timeout_sec=%.1f, "
                 "max_unlock_sec=%.1f apply)", hw.unlock_timeout_sec, hw.max_unlock_sec)
    return errs


def _validate_display_gui(cfg: AppConfig) -> list[str]:
    errs: list[str] = []
    ds, g = cfg.display, cfg.gui
    if ds.backend not in {"auto", "tk", "opencv", "web"}:
        errs.append(f"display.backend must be auto|tk|opencv|web (got {ds.backend!r})")
    if not (1 <= int(ds.web_port) <= 65535):
        errs.append("display.web_port must be in [1, 65535]")
    if ds.locker_owner not in {"none", "when_open", "always"}:
        errs.append(f"display.locker_owner must be none|when_open|always (got {ds.locker_owner!r})")
    if not (6 <= ds.font_size <= 72):
        errs.append("display.font_size must be in [6, 72]")
    if not (0 < ds.scale <= 4.0):
        errs.append("display.scale must be in (0, 4]")
    n_capture = len(cfg.enroll.poses) * cfg.enroll.samples_per_pose
    if not (1 <= g.min_register_samples <= n_capture):
        errs.append(f"gui.min_register_samples must be in [1, len(enroll.poses) * enroll.samples_per_pose = "
                    f"{n_capture}]")
    if g.preview_width < 64 or not (1 <= g.preview_fps <= 60) or g.pose_countdown_sec < 0:
        errs.append("gui: preview_width >= 64, 1 <= preview_fps <= 60, pose_countdown_sec >= 0 required")
    if g.weak_min_samples < 1 or not (-1.0 <= g.weak_self_score <= 1.0) or not (0.0 <= g.weak_frr <= 1.0):
        errs.append("gui: weak_min_samples >= 1, weak_self_score in [-1,1], weak_frr in [0,1] required")
    missing = [t for t in g.weak_required_tags if t not in cfg.enroll.tags]
    if missing:
        errs.append(f"gui.weak_required_tags {missing} not in enroll.tags")
    if len(set(g.locker_choices)) != len(g.locker_choices):
        errs.append("gui.locker_choices has duplicates")
    try:
        pat = re.compile(cfg.lockers.locker_id_pattern)
        bad = [c for c in g.locker_choices if pat.fullmatch(c) is None]
        if bad:
            errs.append(f"gui.locker_choices {bad} do not match lockers.locker_id_pattern")
    except (re.error, TypeError):
        pass  # reported by _validate_lockers
    return errs


def _validate(cfg: AppConfig) -> None:
    d, hw, q = cfg.decision, cfg.hardware, cfg.quality
    errs: list[str] = []
    if cfg.schema_version != 1:
        errs.append(f"schema_version must be 1 (got {cfg.schema_version})")
    if not (1 <= d.min_votes <= d.window_frames):
        errs.append("decision: require 1 <= min_votes <= window_frames")
    if d.window_timeout_sec <= 0 or d.rearm_sec < 0 or d.deny_after_unknown < 1 or d.deny_cooldown_sec < 0:
        errs.append("decision: timing/count values out of range")
    if getattr(hw, "close_mode", "timer") == "timer" and not (0 < hw.open_sec <= hw.max_open_sec <= 10.0):
        errs.append("hardware: close_mode=timer requires 0 < open_sec <= max_open_sec <= 10.0")
    if hw.mode not in {"mock", "pi", "auto"}:
        errs.append(f"hardware.mode must be mock|pi|auto (got {hw.mode!r})")
    pins = [p for p in (hw.solenoid_pin, hw.led_green_pin, hw.led_red_pin, hw.led_amber_pin,
                        hw.exit_button_pin, getattr(hw, "limit_switch_pin", None)) if p is not None]
    pins += [o.pin for o in getattr(hw, "locker_outputs", ()) or ()]
    pins += [o.limit_pin for o in getattr(hw, "locker_outputs", ()) or () if getattr(o, "limit_pin", None) is not None]
    if len(set(pins)) != len(pins):
        errs.append(f"hardware: duplicate pin numbers {pins}")
    errs += _validate_lockers(cfg)
    errs += _validate_close_mode(cfg)
    errs += _validate_display_gui(cfg)
    if cfg.recognition.margin < 0:
        errs.append("recognition.margin must be >= 0")
    if cfg.recognition.score != "cosine":
        errs.append("recognition.score must be 'cosine'")
    if cfg.recognition.aggregation not in {"max", "topk_mean"}:
        errs.append("recognition.aggregation must be max|topk_mean")
    if cfg.recognition.topk < 1:
        errs.append("recognition.topk must be >= 1")
    if cfg.recognition.occluded_policy not in {"threshold", "deny"}:
        errs.append("recognition.occluded_policy must be threshold|deny")
    if cfg.recognition.unreliable_calibration not in {"max_with_default", "ignore"}:
        errs.append("recognition.unreliable_calibration must be max_with_default|ignore")
    if cfg.recognition.multi_face_policy not in {"largest", "hold"}:
        errs.append("recognition.multi_face_policy must be largest|hold")
    if cfg.embedder.backend not in {"sface", "arcface"}:
        errs.append(f"embedder.backend must be sface|arcface (got {cfg.embedder.backend!r})")
    for b in ("sface", "arcface"):
        dt = cfg.recognition.default_thresholds.get(b)
        if not dt or "global" not in dt or "occluded" not in dt:
            errs.append(f"recognition.default_thresholds.{b} must have global and occluded")
        if b not in cfg.recognition.threshold_floor:
            errs.append(f"recognition.threshold_floor.{b} missing")
    if q.min_det_score < cfg.detector.score_threshold:
        errs.append("quality.min_det_score must be >= detector.score_threshold")
    if q.min_brightness >= q.max_brightness:
        errs.append("quality.min_brightness must be < max_brightness")
    if not (0 < cfg.detector.input_scale <= 1.0):
        errs.append("detector.input_scale must be in (0, 1]")
    if cfg.runtime.process_every_n < 1:
        errs.append("runtime.process_every_n must be >= 1")
    if cfg.camera.read_retries < 1:
        errs.append("camera.read_retries must be >= 1")
    if cfg.liveness.backend not in {"none"}:
        errs.append("liveness.backend: only 'none' is implemented")
    if cfg.admin.min_pin_len < 4:
        errs.append("admin.min_pin_len must be >= 4")
    if not (0 < cfg.admin.lockout_sec <= 86400) or not math.isfinite(cfg.admin.lockout_sec):
        errs.append("admin.lockout_sec must be in (0, 86400]")
    if cfg.admin.max_attempts < 1:
        errs.append("admin.max_attempts must be >= 1")
    if not (0 < cfg.evaluation.target_far < 1):
        errs.append("evaluation.target_far must be in (0,1)")
    if errs:
        raise ConfigError("invalid configuration: " + "; ".join(errs))


def build_config(raw: dict, root: Path | None = None) -> AppConfig:
    """Build AppConfig from an already-merged dict (validation included)."""
    root = root or project_root()
    allowed = set(_SECTIONS) | {"schema_version", "hardware", "models"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown top-level key(s) {sorted(unknown)}")
    missing = (set(_SECTIONS) - _OPTIONAL_SECTIONS | {"schema_version", "hardware", "models"}) - set(raw)
    if missing:
        raise ConfigError(f"missing section(s) {sorted(missing)}")
    sections = {name: _build_dataclass(cls, raw.get(name), name, root) for name, cls in _SECTIONS.items()}
    models_raw = raw.get("models") or {}
    if not isinstance(models_raw, Mapping):
        raise ConfigError("models must be a mapping")
    models = {str(k): _build_dataclass(ModelSpec, v, f"models.{k}", root) for k, v in models_raw.items()}
    sv = raw.get("schema_version")
    if isinstance(sv, bool) or not isinstance(sv, int):
        raise ConfigError("schema_version must be int")
    cfg = AppConfig(schema_version=sv, hardware=_build_hardware(raw.get("hardware"), root), models=models,
                    raw=copy.deepcopy(raw), **sections)
    _validate(cfg)
    return cfg


def load_config(path: str | Path | None = None, overrides: Sequence[str] = (), use_local: bool = True) -> AppConfig:
    root = project_root()
    default = root / "configs" / "default.yaml"
    if not default.is_file():
        raise ConfigError(f"default config not found: {default}")
    raw = _read_yaml(default)
    local = root / "configs" / "local.yaml"
    if use_local and local.is_file():
        raw = _deep_merge(raw, _read_yaml(local))
    if path is not None:
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p) if (Path.cwd() / p).is_file() else (root / p)
        if p.resolve() != default.resolve():
            raw = _deep_merge(raw, _read_yaml(p))
    for item in overrides:
        keys, value = parse_override(item)
        _apply_override(raw, keys, value)
    return build_config(raw, root)


def resolve_model_path(cfg: AppConfig, file_name: str) -> Path:
    p = Path(file_name)
    return p if p.is_absolute() else cfg.app.models_dir / p
