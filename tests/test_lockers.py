"""lockers section validation and locker_id checks (QA D-1, INTERFACES section 12). No models needed."""

from __future__ import annotations

import pytest

from facedoor.enroll import validate_locker_id
from facedoor.errors import ConfigError


def test_single_mode_is_accepted(make_cfg):
    cfg = make_cfg("lockers.mode=single", "lockers.require_locker_id=true")
    assert cfg.lockers.mode == "single" and cfg.lockers.require_locker_id is True


def test_per_locker_mode_needs_locker_outputs(make_cfg):
    # per_locker (INTERFACES 13) is implemented: it requires hardware.locker_outputs and solenoid_pin: null
    with pytest.raises(ConfigError, match="locker_outputs"):
        make_cfg("lockers.mode=per_locker")


def test_per_locker_mode_is_accepted_with_outputs(make_cfg):
    cfg = make_cfg("lockers.mode=per_locker", "hardware.solenoid_pin=null",
                   "hardware.locker_outputs=[{locker_id: '1', pin: 24}, {locker_id: '2', pin: 25}]")
    assert cfg.lockers.mode == "per_locker"
    assert [o.locker_id for o in cfg.hardware.locker_outputs] == ["1", "2"]


def test_unknown_locker_mode_is_rejected(make_cfg):
    with pytest.raises(ConfigError, match="lockers.mode"):
        make_cfg("lockers.mode=multi")


def test_invalid_locker_id_pattern_is_rejected(make_cfg):
    with pytest.raises(ConfigError, match="locker_id_pattern"):
        make_cfg("lockers.locker_id_pattern='[unclosed'")


@pytest.mark.parametrize("lid", ["L01", "B-12", "a_b", "1234567890123456"])
def test_valid_locker_ids(lid):
    validate_locker_id(lid, "^[A-Za-z0-9_-]{1,16}$")


@pytest.mark.parametrize("lid", ["", "bad id", "사물함1", "12345678901234567", "a/b"])
def test_invalid_locker_ids(lid):
    with pytest.raises(ValueError, match="invalid locker_id"):
        validate_locker_id(lid, "^[A-Za-z0-9_-]{1,16}$")


def test_locker_id_must_fully_match():
    with pytest.raises(ValueError):
        validate_locker_id("0071", "[0-9]{3}")
