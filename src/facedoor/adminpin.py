"""Admin PIN for the Korean admin GUI (owner: vision-engineer). Contract: INTERFACES section 12.1.

Standard library only (hashlib.pbkdf2_hmac, secrets, hmac.compare_digest).

- data/admin_pin.json: {"schema_version":1, "algo":"pbkdf2_sha256", "iterations":N, "salt":hex, "hash":hex,
  "created_at":...}. Written with a temp file + os.replace, POSIX mode 0600.
- <pin_file>.state: {"failures": n, "locked_until": epoch_seconds} so that restarting the program does not reset
  the failure counter / lockout.
- A corrupt pin file is NOT treated as "not set" (that would let anyone set a new PIN): the GUI must refuse
  entry (PinFileError). Resetting the PIN = an OS administrator deletes the file (file permissions are the
  final trust boundary). The PIN protects the GUI only; CLIs rely on OS permissions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

from facedoor.timeutil import now_iso

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ALGO = "pbkdf2_sha256"
SALT_BYTES = 16
DEFAULT_LOCKOUT_SEC = 300.0

MSG_PIN_DIGITS = "PIN은 숫자만 사용할 수 있습니다."
MSG_PIN_SHORT = "PIN은 숫자 {min}자리 이상이어야 합니다."
MSG_PIN_CORRUPT = ("PIN 파일이 손상되었습니다. 관리자가 {path} 을(를) 삭제한 뒤 다시 설정하세요.")
MSG_PIN_EXISTS = "이미 관리자 PIN이 설정되어 있습니다."


class PinResult(str, Enum):
    OK = "ok"
    WRONG = "wrong"
    LOCKED = "locked"
    NOT_SET = "not_set"


class PinFileError(ValueError):
    """pin file exists but is unreadable / has an invalid schema. Message is Korean (user-facing)."""


def _cfg_get(cfg: Any, name: str, default: Any) -> Any:
    return getattr(cfg, name, default)


def _pin_path(cfg: Any) -> Path:
    return Path(_cfg_get(cfg, "pin_file", Path("data/admin_pin.json")))


def _state_path(cfg: Any) -> Path:
    p = _pin_path(cfg)
    return p.with_name(p.name + ".state")


def _lockout_sec(cfg: Any) -> float:
    v = float(_cfg_get(cfg, "lockout_sec", DEFAULT_LOCKOUT_SEC))
    return v if 0 < v <= 86400 else DEFAULT_LOCKOUT_SEC


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, payload)
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    if not sys.platform.startswith("win"):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _hash(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, int(iterations))


def _load_pin_record(cfg: Any) -> dict | None:
    """None if the file does not exist; PinFileError if it exists but is invalid."""
    path = _pin_path(cfg)
    if not path.exists():
        return None
    corrupt = PinFileError(MSG_PIN_CORRUPT.format(path=path))
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise corrupt from e
    try:
        ok = (isinstance(d, dict) and d.get("schema_version") == SCHEMA_VERSION and d.get("algo") == ALGO
              and isinstance(d.get("iterations"), int) and not isinstance(d.get("iterations"), bool)
              and 1000 <= d["iterations"] <= 10_000_000
              and isinstance(d.get("salt"), str) and len(bytes.fromhex(d["salt"])) >= 8
              and isinstance(d.get("hash"), str) and len(bytes.fromhex(d["hash"])) == 32)
    except ValueError:
        ok = False
    if not ok:
        raise corrupt
    return d


def pin_file_error(cfg: Any) -> str | None:
    """Korean message if the pin file exists but is corrupt, else None."""
    try:
        _load_pin_record(cfg)
    except PinFileError as e:
        return str(e)
    return None


def pin_is_set(cfg: Any) -> bool:
    """pin_file exists and has a valid schema (a corrupt file -> False; check pin_file_error() first)."""
    try:
        return _load_pin_record(cfg) is not None
    except PinFileError:
        return False


def validate_new_pin(cfg: Any, pin: str) -> None:
    """ValueError (Korean) unless pin is digits only and len >= min_pin_len."""
    min_len = max(4, int(_cfg_get(cfg, "min_pin_len", 4)))
    if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit():
        raise ValueError(MSG_PIN_DIGITS)
    if len(pin) < min_len:
        raise ValueError(MSG_PIN_SHORT.format(min=min_len))


def set_pin(cfg: Any, pin: str, *, overwrite: bool = False) -> None:
    """Set the admin PIN. Digits only, len >= min_pin_len, else ValueError (Korean).
    Refuses to replace an existing valid PIN unless overwrite=True; refuses a corrupt file (PinFileError)."""
    validate_new_pin(cfg, pin)
    existing = _load_pin_record(cfg)  # raises PinFileError on corrupt file
    if existing is not None and not overwrite:
        raise ValueError(MSG_PIN_EXISTS)
    iterations = int(_cfg_get(cfg, "pbkdf2_iterations", 200_000))
    salt = secrets.token_bytes(SALT_BYTES)
    rec = {"schema_version": SCHEMA_VERSION, "algo": ALGO, "iterations": iterations, "salt": salt.hex(),
           "hash": _hash(pin, salt, iterations).hex(), "created_at": now_iso()}
    _write_private_json(_pin_path(cfg), rec)
    _write_state(cfg, 0, 0.0)
    log.info("admin PIN set (%s)", _pin_path(cfg))


# ---------------------------------------------------------------------------------- failure state
def _read_state(cfg: Any, now: float) -> tuple[int, float]:
    p = _state_path(cfg)
    if not p.exists():
        return 0, 0.0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        failures = int(d.get("failures", 0))
        locked_until = float(d.get("locked_until", 0.0))
        if failures < 0 or locked_until != locked_until:  # NaN
            raise ValueError("bad state")
    except (OSError, ValueError, TypeError, AttributeError):
        # fail-closed: an unreadable state file counts as a lockout (cannot be bypassed by corrupting it)
        log.warning("admin PIN state file %s unreadable; applying lockout", p)
        return 0, now + _lockout_sec(cfg)
    # clock moved backwards: never lock longer than lockout_sec from now
    locked_until = min(locked_until, now + _lockout_sec(cfg))
    return failures, locked_until


def _write_state(cfg: Any, failures: int, locked_until: float) -> None:
    try:
        _write_private_json(_state_path(cfg), {"failures": int(failures), "locked_until": float(locked_until),
                                               "updated_at": now_iso()})
    except OSError as e:  # pragma: no cover - disk problems
        log.error("cannot write admin PIN state %s: %s", _state_path(cfg), e)


def lock_remaining_sec(cfg: Any, now: float | None = None) -> float:
    now = time.time() if now is None else now
    _, until = _read_state(cfg, now)
    return max(0.0, until - now)


def attempts_left(cfg: Any, now: float | None = None) -> int:
    now = time.time() if now is None else now
    failures, until = _read_state(cfg, now)
    if until > now:
        return 0
    return max(0, int(_cfg_get(cfg, "max_attempts", 3)) - failures)


def verify_pin(cfg: Any, pin: str, now: float | None = None) -> PinResult:
    """LOCKED immediately while locked out. Failure counter and lock time persist in <pin_file>.state.
    The attempt that reaches max_attempts returns LOCKED. Corrupt pin file -> PinFileError."""
    now = time.time() if now is None else now
    rec = _load_pin_record(cfg)
    if rec is None:
        return PinResult.NOT_SET
    failures, until = _read_state(cfg, now)
    if until > now:
        return PinResult.LOCKED
    expected = bytes.fromhex(rec["hash"])
    got = _hash(pin if isinstance(pin, str) else "", bytes.fromhex(rec["salt"]), rec["iterations"])
    if hmac.compare_digest(expected, got):
        _write_state(cfg, 0, 0.0)
        return PinResult.OK
    failures += 1
    max_attempts = max(1, int(_cfg_get(cfg, "max_attempts", 3)))
    if failures >= max_attempts:
        _write_state(cfg, 0, now + _lockout_sec(cfg))
        log.warning("admin PIN: %d failed attempts, locked for %.0f s", failures, _lockout_sec(cfg))
        return PinResult.LOCKED
    _write_state(cfg, failures, 0.0)
    return PinResult.WRONG
