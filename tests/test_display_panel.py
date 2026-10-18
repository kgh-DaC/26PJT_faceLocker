"""Locker panel model, Korean texts and the display backends (INTERFACES 16, 17)."""

from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from conftest import make_result
from facedoor import display as D
from facedoor import locker_panel as LP
from facedoor import texts as T
from facedoor.datatypes import DecisionOutput, DecisionState, Frame, FrameVerdict, HoldReason
from facedoor.errors import FaceDoorError


def st(lid="1", state="LOCKED_CLOSED", closed=True, on=False, uw=0.0, cc=0.0, hard=0.0, cd=0.0):
    return NS(locker_id=lid, state=NS(value=state), door_closed=closed, solenoid_on=on,
              unlock_wait_remaining_sec=uw, close_confirm_remaining_sec=cc, hard_remaining_sec=hard,
              cooldown_remaining_sec=cd)


class FakeDoor:
    def __init__(self, *states):
        self.states = states

    def locker_states(self):
        return self.states


def rows_for(*states, limits=None, opener=None, policy="when_open", names=None):
    return LP.build_rows(FakeDoor(*states), limits, None, opener or {}, policy, names or {})


def test_texts_all_hangul():
    import re

    hangul = re.compile("[가-힣]")
    bad = {k: v for k, v in T.all_texts().items() if not hangul.search(str(v))}
    assert not bad, bad


def test_rows_state_table():
    rows = rows_for(
        st("1", "LOCKED_CLOSED"),
        st("2", "UNLOCKED_WAITING", True, True, uw=7.2, hard=25.0),
        st("3", "OPEN", False, True, hard=25.0),
        st("4", "SENSOR_FAULT", None),
    )
    assert [r.door for r in rows] == ["closed", "closed", "open", "unknown"]
    assert [r.unlocked for r in rows] == [False, True, True, False]
    assert rows[1].countdown_kind == "unlock_wait" and rows[1].countdown_sec == 7.2
    assert rows[2].countdown_kind == "hard"
    v = LP.describe_row(rows[1])
    assert v.badge == "닫힘" and "문을 여세요 (7.2초 후 재잠금)" in v.sub and v.bg == LP.YELLOW and v.progress
    v = LP.describe_row(rows[2])
    assert v.badge == "열림" and v.sub == "해제 유지 중" and "최대 25초" in v.small and v.bg == LP.GREEN
    v = LP.describe_row(rows[3])
    assert v.badge == "불명" and "센서 오류" in v.sub and v.bold


def test_remaining_states_text():
    r = rows_for(st("1", "CLOSING_CONFIRM", True, True, cc=1.2), st("2", "COOLDOWN", True, cd=1.2),
                 st("3", "LOCKED_OPEN", False), st("4", "FAULT", None))
    assert "닫힘 확인 중 1.2초 후 잠김" in LP.describe_row(r[0]).sub
    assert "재해제 대기 1.2초" in LP.describe_row(r[1]).sub
    assert "확인 필요" in LP.describe_row(r[2]).sub and LP.describe_row(r[2]).badge == "열림"
    assert r[3].door == "unknown" and LP.describe_row(r[3]).bg == LP.RED


def test_sensorless_timer_mode_shows_lock_state():
    r = rows_for(st(None, "OPEN", None, True, hard=2.0), st(None, "LOCKED_CLOSED", None))[0]
    assert r.sensorless and r.door == "unknown"
    assert LP.describe_row(r).badge == "해제"
    assert LP.describe_row(rows_for(st(None, "LOCKED_CLOSED", None))[0]).badge == "잠김"


def test_owner_policy():
    s = st("2", "OPEN", False, True)
    assert rows_for(s, opener={"2": "alice"}, names={"alice": "앨리스"})[0].owner_label == "앨리스"
    assert rows_for(s, opener={"2": "alice"}, policy="none")[0].owner_label is None
    assert rows_for(st("2", "LOCKED_CLOSED"), opener={"2": "alice"})[0].owner_label is None   # only while open


def test_ascii_lines():
    r = rows_for(st("2", "OPEN", False, True), st("3", "SENSOR_FAULT", None), st("4", "LOCKED_CLOSED"))
    assert LP.ascii_line(r[0]) == "L2 OPEN unlocked"
    assert LP.ascii_line(r[1]) == "L3 SENSOR?"
    assert LP.ascii_line(r[2]) == "L4 CLOSED locked"


def test_state_file_roundtrip_stale_and_schema(tmp_path):
    p = tmp_path / "s.json"
    rows = rows_for(st("1"), st("2", "OPEN", False, True))
    LP.write_state(p, rows, "sess")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2 and data["session_id"] == "sess"
    back = LP.read_state(p)
    assert back == rows
    data["ts"] = time.time() - 10
    p.write_text(json.dumps(data), encoding="utf-8")
    assert LP.read_state(p, 3.0) is None                     # stale
    data["ts"], data["schema_version"] = time.time(), 1
    p.write_text(json.dumps(data), encoding="utf-8")
    assert LP.read_state(p) is None                          # v1.4 draft is not read
    assert LP.read_state(tmp_path / "missing.json") is None
    assert all(r.state == "UNKNOWN" and r.door == "unknown" for r in LP.unknown_rows(rows))


def test_build_rows_without_locker_states_is_empty():
    assert LP.build_rows(object(), None, None, {}, "none", {}) == []


def test_recognition_text():
    def dec(state=DecisionState.IDLE, votes=0, window=0):
        return DecisionOutput(state, False, False, None, votes, window, None, "")

    m = make_result(FrameVerdict.MATCH, "alice", 0.62)
    assert D.recognition_text(m, dec(DecisionState.OPEN), True, lambda p: "홍길동")[0] == "인식됨: 홍길동 (0.62 / 기준 0.50)"
    assert D.recognition_text(m, dec(DecisionState.VERIFYING, 2, 3))[0] == "확인 중 2/3"
    assert D.recognition_text(make_result(FrameVerdict.UNKNOWN, "x", 0.2), dec(), False)[0] == "미등록 얼굴"
    h = make_result(FrameVerdict.HOLD, hold=HoldReason.TOO_DARK)
    assert D.recognition_text(h, dec(DecisionState.HOLD)) == ("판정 보류: 너무 어둡습니다", "orange")
    assert D.recognition_text(m, dec(DecisionState.DENIED))[0] == "출입 거부"


def test_create_display_backends(monkeypatch):
    cfg = NS(enabled=True, backend="opencv", window_name="x", scale=1.0, show_fps=True, show_scores=True,
             locker_panel=True, font_families=["Malgun Gothic"], font_size=14)
    assert isinstance(D.create_display(cfg, enabled=False), D.NullDisplay)
    assert isinstance(D.create_display(cfg, enabled=True), D.OpenCVDisplay)
    monkeypatch.setattr(D, "tk_available", lambda: (False, "no DISPLAY"))
    cfg.backend = "tk"
    with pytest.raises(FaceDoorError):
        D.create_display(cfg, True)
    cfg.backend = "auto"
    assert isinstance(D.create_display(cfg, True), D.OpenCVDisplay)       # warning fallback


def test_optional_methods_are_noops_on_null_and_opencv():
    n = D.NullDisplay()
    n.set_panel([]), n.set_sim_handler(None), n.set_notice("x")
    cv = D.OpenCVDisplay(NS(scale=1.0, window_name="w", show_fps=True, show_scores=True, locker_panel=True))
    cv.set_panel(rows_for(st("1")))
    cv.set_sim_handler(lambda a, b: None)
    assert cv._sim and len(cv._rows) == 1


def _tk_cfg():
    return NS(enabled=True, backend="tk", window_name="FaceDoor", scale=1.0, show_fps=True, show_scores=True,
              locker_panel=True, font_families=["Malgun Gothic", "NanumGothic"], font_size=12)


def _tk_ok():
    ok, _ = D.tk_available()
    if not ok:
        return False
    try:
        import tkinter

        r = tkinter.Tk()
        r.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _tk_ok(), reason="tkinter / display not available")
def test_tk_display_smoke_panels_click_keys_close():
    d = D.TkDisplay(_tk_cfg())
    try:
        clicks = []
        d.set_sim_handler(lambda lid, act: clicks.append((lid, act)))
        rows = rows_for(st("1"), st("2", "OPEN", False, True, hard=20.0), st("3", "SENSOR_FAULT", None),
                        st("4", "UNLOCKED_WAITING", True, True, uw=5.0, hard=25.0))
        d.set_panel(rows)
        frame = Frame(np.zeros((240, 320, 3), np.uint8), 0, 0.0, "t")
        dec = DecisionOutput(DecisionState.IDLE, False, False, None, 0, 0, None, "")
        res = make_result(FrameVerdict.MATCH, "alice", 0.7)
        assert d.show(frame, res, dec, NS(is_open=False), NS(), 12.0) == -1
        assert len(d._panels) == 4 and d._panels[1]["badge"].cget("text") == "열림"
        assert d._panels[2]["badge"].cget("text") == "불명"
        assert "인식됨" in d.status.cget("text") and "속도 12.0 FPS" in d.status.cget("text")
        assert d.hint.cget("text").startswith("[시뮬레이션]")
        d.set_notice("사물함 2 잠금 해제 — 10초 안에 문을 여세요", 5.0)
        d.show(frame, res, dec, NS(is_open=False), NS(), 12.0)
        assert d.status.cget("text").startswith("사물함 2 잠금 해제")
        # panel click -> handler; no handler (real hardware) -> ignored, hint hidden
        d._panels[0]["badge"].event_generate("<Button-1>")
        d.root.update()
        assert clicks == [("1", "toggle")]
        d.set_sim_handler(None)
        d._panels[0]["badge"].event_generate("<Button-1>")
        d.root.update()
        d.show(frame, res, dec, NS(is_open=False), NS(), 12.0)
        assert clicks == [("1", "toggle")] and d.hint.cget("text") == ""
        # key codes come back through show()
        d.root.event_generate("<Key>", keysym="3")
        d.root.update()
        k = d.show(frame, res, dec, NS(is_open=False), NS(), 12.0)
        assert k in (-1, ord("3"))
    finally:
        d.close()
    assert d.show(frame, res, dec, NS(is_open=False), NS(), 1.0) == 27     # closed window -> quit key
