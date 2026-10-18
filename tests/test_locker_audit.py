"""evaluation/locker_audit.py, the locker parts of evaluate.py / label_events.py / records_from_events
(owner: eval-engineer). INTERFACES 5 (33/35/37 columns), 13.5 + 15.5 (hardware.log), 13.8 + 15 (metrics),
13.9 scenarios 23-25, 15.6 L17.

All inputs are synthetic events.csv / hardware.log files written into tmp_path; no models, camera or GPIO needed.
The column lists are spelled out here (not imported from facedoor.events) so these tests do not depend on the
events.py version being edited in parallel.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from facedoor.evaluation import locker_audit as LA
from facedoor.evaluation.scoring import ScoreRecord, records_from_events
from facedoor.evaluation.summary import compute_metrics, condition_order

ROOT = Path(__file__).resolve().parents[1]
COLS33 = LA.EVENT_COLUMNS_V12
COLS35 = LA.EVENT_COLUMNS_V13
COLS37 = LA.EVENT_COLUMNS_V15


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_la_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


label_events = load_script("label_events")
evaluate = load_script("evaluate")

TS = "2026-10-16T09:30:{:06.3f}+09:00"


def ts(sec: float) -> str:
    return TS.format(sec)


# --------------------------------------------------------------------------- synthetic writers
def frame(i: int, sec: float, *, event: str = "frame", verdict: str = "MATCH", best: str = "alice", score: float = 0.7,
          door: int = 0, message: str = "", locker_id: str = "", open_lockers: str = "", label: str = "",
          session: str = "S1") -> dict[str, str]:
    return {"ts": ts(sec), "session_id": session, "frame_idx": str(i), "event": event, "verdict": verdict,
            "decision_state": "IDLE", "best_id": best, "best_score": f"{score:.4f}", "second_id": "bob",
            "second_score": "0.2000", "threshold": "0.5000", "threshold_group": "global", "condition_tag": "live",
            "door_signal": str(door), "total_ms": "40.0000", "message": message, "locker_id": locker_id,
            "open_lockers": open_lockers, "label": label}


def write_csv(path: Path, cols: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def write_hw(path: Path, lines: list[tuple[float, str, str]], source: str = "mock-door") -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for sec, ev, detail in lines:
            fh.write(f"{ts(sec)}\t{source}\t{ev}\t{detail}\n")


R_ALICE = "face:alice:0.7000:3/3:locker=2"
R_CAROL = "face:carol:0.6500:3/3:locker=3"
INIT = (0.0, "INIT", "per_locker channels=1:BCM24:H,2:BCM25:H,3:BCM16:H,4:BCM26:H green=BCM27 "
                     "max_concurrent_open=1; outputs OFF")


def per_locker_events() -> list[dict[str, str]]:
    """A clean per_locker session: alice (locker 2) opens, later bob (no locker) is suppressed, alice re-recognised
    while locker 2 is in cooldown -> rejected, carol (locker 3) opens after locker 2 closed."""
    return [
        {"ts": ts(0.1), "session_id": "S1", "frame_idx": "0", "event": "start",
         "message": "per_locker n=4 assigned=3 free=1 problems=1"},
        frame(1, 1.0), frame(2, 1.1),
        frame(3, 1.2, event="open", door=1, message=R_ALICE, locker_id="2", open_lockers="2"),
        frame(4, 1.3, door=1, open_lockers="2"),
        {"ts": ts(4.3), "session_id": "S1", "frame_idx": "4", "event": "door_close", "door_signal": "0",
         "locker_id": "2", "close_cause": "close_confirmed",
         "message": "cause=close_confirmed unlocked_for=3.000s door_open_for=2.700s"},
        frame(5, 5.0, event="open", best="bob", message="open suppressed: bob has no locker_id (lockers.mode=per_locker)"),
        frame(6, 5.2, event="open", door=0,
              message=f"{R_ALICE} (rejected by door: cooldown)", locker_id="2"),
        frame(7, 8.0, event="open", best="carol", score=0.65, door=1, message=R_CAROL, locker_id="3", open_lockers="3"),
        {"ts": ts(11.0), "session_id": "S1", "frame_idx": "7", "event": "door_close", "door_signal": "0",
         "locker_id": "3", "close_cause": "close_confirmed",
         "message": "cause=close_confirmed unlocked_for=3.000s door_open_for=2.500s"},
        {"ts": ts(12.0), "session_id": "S1", "frame_idx": "7", "event": "stop", "message": "max frames"},
    ]


def relock(cause: str, unlocked: float, door_open: float) -> str:
    return f"signal=OFF cause={cause} unlocked_for={unlocked:.3f}s door_open_for={door_open:.3f}s open_for={unlocked:.3f}s"


def per_locker_hw() -> list[tuple[float, str, str]]:
    """v1.5 limit-mode log: unlock -> door opens 0.3 s later -> door closes -> 2.2 s close_confirm -> relock."""
    return [
        INIT,
        (1.2, "OPEN", f"locker=2 pin=BCM25 {R_ALICE} signal=ON duration=30.000s"),
        (1.2, "STATE", "locker=2 pin=BCM25 LOCKED_CLOSED->UNLOCKED_WAITING limit=pressed"),
        (1.5, "STATE", "locker=2 pin=BCM25 UNLOCKED_WAITING->OPEN limit=released"),
        (2.0, "STATE", "locker=2 pin=BCM25 OPEN->CLOSING_CONFIRM limit=pressed"),
        (4.2, "STATE", "locker=2 pin=BCM25 CLOSING_CONFIRM->COOLDOWN limit=pressed"),
        (4.2, "CLOSE", f"locker=2 pin=BCM25 close_confirmed {relock('close_confirmed', 3.0, 2.7)}"),
        (5.2, "REJECT", f"locker=2 pin=BCM25 {R_ALICE} code=cooldown"),
        (6.0, "REJECT", "locker=- pin=BCM- manual-test code=no_locker_id"),
        (8.0, "OPEN", f"locker=3 pin=BCM16 {R_CAROL} signal=ON duration=30.000s"),
        (8.0, "STATE", "locker=3 pin=BCM16 LOCKED_CLOSED->UNLOCKED_WAITING limit=pressed"),
        (8.5, "STATE", "locker=3 pin=BCM16 UNLOCKED_WAITING->OPEN limit=released"),
        (9.0, "STATE", "locker=3 pin=BCM16 OPEN->CLOSING_CONFIRM limit=pressed"),
        (11.0, "STATE", "locker=3 pin=BCM16 CLOSING_CONFIRM->COOLDOWN limit=pressed"),
        (11.0, "CLOSE", f"locker=3 pin=BCM16 close_confirmed {relock('close_confirmed', 3.0, 2.5)}"),
        (12.0, "CLEANUP", "outputs OFF (all channels)"),
    ]


# =========================================================================== parsing
def test_parse_hw_line_contract_examples() -> None:
    h = LA.parse_hw_line(f"{ts(1)}\tpi-door\tOPEN\tlocker=2 pin=BCM25 face:u0001:0.6123:3/3:locker=2 signal=ON duration=3.000s")
    assert (h.event, h.locker, h.pin, h.reason, h.duration) == ("OPEN", "2", "25", "face:u0001:0.6123:3/3:locker=2", 3.0)
    h = LA.parse_hw_line(f"{ts(1)}\tpi-door\tREJECT\tlocker=- pin=BCM- manual-test code=no_locker_id")
    assert (h.locker, h.pin, h.reason, h.code) == ("", None, "manual-test", "no_locker_id")
    h = LA.parse_hw_line(f"{ts(1)}\tmock\tOPEN\tface:alice:0.7000:3/3 signal=ON duration=3.000s")   # single mode
    assert h.locker is None and h.reason == "face:alice:0.7000:3/3"
    assert LA.reason_locker("face:a:0.7:3/3:locker=A02") == "A02" and LA.reason_locker("face:a:0.7:3/3") is None
    assert LA.reason_person("face:alice:0.7:3/3:locker=2") == "alice"
    assert LA.classify_suppression("open suppressed: x locker_id=Z99 not configured in hardware.locker_outputs") == "unknown_locker"
    assert LA.classify_suppression("open suppressed: x locker_id=1 shared with y (lockers.allow_shared=false)") == "shared_denied"
    assert LA.classify_suppression("open suppressed: x locker_id invalid (int 12)") == "invalid_locker_id"


# =========================================================================== scenario 24: clean log -> all zero
def test_scenario24_clean_logs_all_integrity_zero(tmp_path: Path) -> None:
    ev, hw = tmp_path / "events.csv", tmp_path / "hardware.log"
    write_csv(ev, COLS35, per_locker_events())
    write_hw(hw, per_locker_hw())
    a = LA.audit_lockers(ev, hw, max_unlock_sec=30.0, unlock_timeout_sec=10.0, close_mode="limit",
                         configured_lockers=["1", "2", "3", "4"])
    assert a["per_locker_data"] is True
    # v1.5 (INTERFACES 15): chatter_reopens (v1.4 close-hold) is gone, on_while_closed_overtime is the 5th zero-metric
    assert a["integrity"] == {"wrong_locker_opens": 0, "extra_channel_on": 0, "unassigned_opens": 0,
                              "watchdog_overruns": 0, "on_while_closed_overtime": 0}
    assert tuple(a["integrity"]) == LA.INTEGRITY_KEYS and "chatter_reopens" not in a["integrity"]
    assert a["integrity_ok"] is True and not LA.integrity_failed(a)
    assert a["suppressed"]["no_locker_id"] == 1
    assert a["rejected"] == {"cooldown": 1, "no_locker_id": 1} and a["rejected_source"] == "hardware_log"
    assert a["actual_opens"] == {"events": 2, "hardware_log": 2}
    assert a["per_locker_opens"] == {"2": 1, "3": 1}
    assert a["params"]["max_concurrent_open"] == 1 and a["params"]["mode_used"] == "limit"
    assert a["close_causes"] == {"close_confirmed": 2} and a["unlock_timeouts"] == 0 and a["watchdog_relocks"] == 0
    assert a["forced_opens"] == 0 and a["sensor_faults"] == 0
    assert a["open_duration"]["n"] == 2 and a["open_duration"]["max"] == pytest.approx(3.0)
    assert a["misid_locker_opens"] is None          # no labels -> not computable, not 0
    assert not any("대응되지 않음" in w for w in a["warnings"])   # every event open matched a hardware OPEN


# =========================================================================== scenario 25: tampered log
def test_scenario25_tampered_hw_log_wrong_locker_exit_1(tmp_path: Path) -> None:
    ev, hw = tmp_path / "events.csv", tmp_path / "hardware.log"
    rows = [frame(1, 1.0, event="open", door=1, message="face:alice:0.7000:3/3:locker=A02", locker_id="A02",
                  open_lockers="A02")]
    write_csv(ev, COLS35, rows)
    write_hw(hw, [(0.0, "INIT", "per_locker channels=A01:BCM24:H,A02:BCM25:H,A03:BCM16:H green=BCM27 max_concurrent_open=1; outputs OFF"),
                  (1.0, "OPEN", "locker=A03 pin=BCM16 face:alice:0.7000:3/3:locker=A02 signal=ON duration=3.000s"),
                  (4.0, "WATCHDOG", "locker=A03 pin=BCM16 timeout signal=OFF")])
    a = LA.audit_lockers(ev, hw, max_open_sec=5.0)
    assert a["integrity"]["wrong_locker_opens"] == 1           # one incident, not double counted with events row
    assert LA.integrity_failed(a) and a["integrity_ok"] is False
    assert any("A03" in d for d in a["details"]["wrong_locker_opens"])

    out = tmp_path / "reports"
    rc = evaluate.main(["--gallery", str(tmp_path / "g"), "--probe", str(tmp_path / "p"), "--events", str(ev),
                        "--hardware-log", str(hw), "--out", str(out), "--set", "evaluation.bootstrap_iters=10"])
    assert rc == 1
    (rep,) = list(out.iterdir())
    m = json.loads((rep / "metrics.json").read_text(encoding="utf-8"))
    assert m["locker"]["integrity"]["wrong_locker_opens"] == 1
    assert m["data_origin"] == "audit-only"
    assert "사물함 무결성" in m["warnings"][0]
    html = (rep / "report.html").read_text(encoding="utf-8")
    assert "lockbad" in html and "사물함 감사" in html
    assert html.index("사물함 무결성 오류") < html.index("1. 요약")      # red banner at the very top


def test_hw_only_mismatch_without_events(tmp_path: Path) -> None:
    hw = tmp_path / "hardware.log"
    write_hw(hw, [INIT, (1.0, "OPEN", "locker=3 pin=BCM16 face:alice:0.7000:3/3:locker=2 signal=ON duration=3.000s"),
                  (4.0, "CLOSE", "locker=3 pin=BCM16 face:alice signal=OFF")])
    a = LA.audit_lockers(None, hw, max_open_sec=5.0)
    assert a["integrity"]["wrong_locker_opens"] == 1
    assert a["misid_locker_opens"] is None and any("events.csv 없음" in w for w in a["warnings"])


def test_events_only_mismatch_between_locker_id_and_reason(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"
    write_csv(ev, COLS35, [frame(1, 1.0, event="open", door=1, message=R_ALICE, locker_id="3", open_lockers="3")])
    a = LA.audit_lockers(ev, None, max_open_sec=5.0)
    assert a["integrity"]["wrong_locker_opens"] == 1
    assert a["integrity"]["watchdog_overruns"] is None        # needs hardware.log -> not computable, never 0
    assert a["integrity_ok"] is False


# =========================================================================== other integrity metrics
def test_extra_channel_unassigned_and_watchdog(tmp_path: Path) -> None:
    ev, hw = tmp_path / "events.csv", tmp_path / "hardware.log"
    rows = [frame(1, 1.0, event="open", door=1, message=R_ALICE, locker_id="2", open_lockers="2"),
            frame(2, 1.5, event="open", best="dave", door=1, message="face:dave:0.7000:3/3:locker=9",
                  locker_id="9", open_lockers="2;9"),              # 2 channels ON > max 1, locker 9 not configured
            frame(3, 2.0, door=1, open_lockers="2;9")]              # still 2 ON
    write_csv(ev, COLS35, rows)
    write_hw(hw, [INIT,
                  (1.0, "OPEN", f"locker=2 pin=BCM25 {R_ALICE} signal=ON duration=3.000s"),
                  (1.5, "OPEN", "locker=9 pin=BCM5 face:dave:0.7000:3/3:locker=9 signal=ON duration=3.000s"),
                  (4.0, "WATCHDOG", "locker=2 pin=BCM25 timeout signal=OFF"),
                  (9.0, "CLOSE", "locker=9 pin=BCM5 stuck signal=OFF")])  # 7.5 s > 5.0 + 0.5
    a = LA.audit_lockers(ev, hw, max_open_sec=5.0)
    integ = a["integrity"]
    assert integ["extra_channel_on"] == 3          # 2 event rows + 1 hardware overlap
    assert integ["unassigned_opens"] == 1          # locker 9 not in INIT channels (matched pair counted once)
    assert integ["watchdog_overruns"] == 1
    assert integ["wrong_locker_opens"] == 0


def test_unassigned_empty_locker_and_unclosed(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"
    write_csv(ev, COLS35, [
        {"ts": ts(0), "session_id": "S1", "event": "start", "message": "per_locker n=4"},
        frame(1, 1.0, event="open", door=1, message="face:alice:0.7000:3/3", locker_id="", open_lockers="1")])
    a = LA.audit_lockers(ev, None, max_open_sec=5.0, configured_lockers=["1", "2"])
    assert a["integrity"]["unassigned_opens"] == 1

    hw = tmp_path / "hardware.log"
    write_hw(hw, [INIT, (1.0, "OPEN", f"locker=2 pin=BCM25 {R_ALICE} signal=ON duration=3.000s")])
    a = LA.audit_lockers(None, hw, max_open_sec=5.0)
    assert a["unclosed_opens"] == 1 and any("닫힘 기록" in w for w in a["warnings"])
    assert a["integrity"]["watchdog_overruns"] == 0


def test_critical_rejects_and_fault_are_warned(tmp_path: Path) -> None:
    hw = tmp_path / "hardware.log"
    write_hw(hw, [INIT, (1.0, "REJECT", f"locker=2 pin=BCM25 {R_ALICE} code=invariant"),
                  (1.0, "FAULT", "locker=- pin=BCM- invariant violated")])
    a = LA.audit_lockers(None, hw, max_open_sec=5.0)
    assert a["critical_rejects"]["invariant"] == 1 and a["faults"] == 1
    assert any("invariant=1" in w for w in a["warnings"])


# =========================================================================== v1.5 limit-switch metrics
def test_parse_v15_hw_rows() -> None:
    h = LA.parse_hw_line(f"{ts(1)}\tmock\tCLOSE\tlocker=2 pin=BCM25 close_confirmed signal=OFF cause=close_confirmed "
                         "unlocked_for=3.200s door_open_for=2.100s open_for=3.200s")
    assert (h.cause, h.unlocked_for, h.door_open_for, h.open_for, h.reason) == (
        "close_confirmed", 3.2, 2.1, 3.2, "close_confirmed")
    h = LA.parse_hw_line(f"{ts(1)}\tmock\tSTATE\tlocker=2 pin=BCM25 OPEN->CLOSING_CONFIRM limit=pressed")
    assert (h.event, h.locker, h.state_from, h.state_to, h.limit) == ("STATE", "2", "OPEN", "CLOSING_CONFIRM", "pressed")
    h = LA.parse_hw_line(f"{ts(1)}\tmock\tSENSOR\tlocker=2 pin=BCM25 sensor_fault limit_pin=BCM6 none_for=1.020s")
    assert h.event == "SENSOR" and h.reason == "sensor_fault" and h.locker == "2"


def _limit_hw(extra: list[tuple[float, str, str]]) -> list[tuple[float, str, str]]:
    return [INIT, *extra]


def test_l1_unlock_timeout_is_reported_not_an_integrity_failure(tmp_path: Path) -> None:
    hw = tmp_path / "hardware.log"
    write_hw(hw, _limit_hw([
        (1.0, "OPEN", f"locker=2 pin=BCM25 {R_ALICE} signal=ON duration=30.000s"),
        (1.0, "STATE", "locker=2 pin=BCM25 LOCKED_CLOSED->UNLOCKED_WAITING limit=pressed"),
        (11.0, "STATE", "locker=2 pin=BCM25 UNLOCKED_WAITING->COOLDOWN limit=pressed"),
        (11.0, "CLOSE", f"locker=2 pin=BCM25 unlock_timeout {relock('unlock_timeout', 10.0, 0.0)}")]))
    a = LA.audit_lockers(None, hw, max_unlock_sec=30.0, unlock_timeout_sec=10.0, close_mode="limit")
    assert a["integrity"]["on_while_closed_overtime"] == 0      # exactly unlock_timeout_sec: within +0.5 s slack
    assert a["integrity"]["watchdog_overruns"] == 0 and a["unlock_timeouts"] == 1 and a["watchdog_relocks"] == 0


def test_on_while_closed_overtime_counts_and_fails(tmp_path: Path) -> None:
    hw = tmp_path / "hardware.log"
    write_hw(hw, _limit_hw([
        (1.0, "OPEN", f"locker=2 pin=BCM25 {R_ALICE} signal=ON duration=30.000s"),
        (1.0, "STATE", "locker=2 pin=BCM25 LOCKED_CLOSED->UNLOCKED_WAITING limit=pressed"),
        (12.0, "STATE", "locker=2 pin=BCM25 UNLOCKED_WAITING->COOLDOWN limit=pressed"),     # 11.0 s > 10.5 s
        (12.0, "CLOSE", f"locker=2 pin=BCM25 unlock_timeout {relock('unlock_timeout', 11.0, 0.0)}"),
        # 10.4 s closed-while-ON then the door opens: within slack
        (20.0, "OPEN", f"locker=3 pin=BCM16 {R_CAROL} signal=ON duration=30.000s"),
        (20.0, "STATE", "locker=3 pin=BCM16 LOCKED_CLOSED->UNLOCKED_WAITING limit=pressed"),
        (30.4, "STATE", "locker=3 pin=BCM16 UNLOCKED_WAITING->OPEN limit=released"),
        (31.0, "STATE", "locker=3 pin=BCM16 OPEN->CLOSING_CONFIRM limit=pressed"),
        (33.0, "STATE", "locker=3 pin=BCM16 CLOSING_CONFIRM->COOLDOWN limit=pressed"),
        (33.0, "CLOSE", f"locker=3 pin=BCM16 close_confirmed {relock('close_confirmed', 13.0, 2.6)}")]))
    a = LA.audit_lockers(None, hw, max_unlock_sec=30.0, unlock_timeout_sec=10.0, close_mode="limit")
    assert a["integrity"]["on_while_closed_overtime"] == 1
    assert a["integrity_ok"] is False and LA.integrity_failed(a)
    assert "on_while_closed_overtime" in a["details"] and a["on_while_closed"]["source"] == "hardware_log_state"
    assert a["on_while_closed"]["max_sec"] == pytest.approx(11.0)


def test_watchdog_overrun_uses_max_unlock_sec_in_limit_mode(tmp_path: Path) -> None:
    hw = tmp_path / "hardware.log"
    rows = [(1.0, "OPEN", f"locker=2 pin=BCM25 {R_ALICE} signal=ON duration=30.000s"),
            (1.0, "STATE", "locker=2 pin=BCM25 LOCKED_CLOSED->UNLOCKED_WAITING limit=pressed"),
            (1.2, "STATE", "locker=2 pin=BCM25 UNLOCKED_WAITING->OPEN limit=released"),
            (31.4, "STATE", "locker=2 pin=BCM25 OPEN->COOLDOWN limit=released"),
            (31.4, "WATCHDOG", f"locker=2 pin=BCM25 watchdog {relock('watchdog', 30.4, 30.2)}")]
    write_hw(hw, _limit_hw(rows))
    a = LA.audit_lockers(None, hw, max_open_sec=5.0, max_unlock_sec=30.0, unlock_timeout_sec=10.0, close_mode="limit")
    assert a["integrity"]["watchdog_overruns"] == 0            # 30.4 s <= 30.5 s (max_open_sec=5 is ignored in limit mode)
    assert a["watchdog_relocks"] == 1 and a["watchdog_ratio"] == 1.0
    write_hw(hw, _limit_hw([(t + (0.2 if k >= 3 else 0), e, d) for k, (t, e, d) in enumerate(rows)]))
    a = LA.audit_lockers(None, hw, max_unlock_sec=30.0, unlock_timeout_sec=10.0, close_mode="limit")
    assert a["integrity"]["watchdog_overruns"] == 1 and a["integrity_ok"] is False


def test_forced_open_and_sensor_fault_counts(tmp_path: Path) -> None:
    hw = tmp_path / "hardware.log"
    write_hw(hw, _limit_hw([
        (1.0, "STATE", "locker=2 pin=BCM25 LOCKED_CLOSED->LOCKED_OPEN limit=released"),
        (5.0, "STATE", "locker=2 pin=BCM25 LOCKED_OPEN->LOCKED_CLOSED limit=pressed"),
        (6.0, "STATE", "locker=3 pin=BCM16 COOLDOWN->LOCKED_OPEN limit=released"),       # not a forced open
        (7.0, "STATE", "locker=3 pin=BCM16 LOCKED_OPEN->SENSOR_FAULT limit=none"),
        (7.0, "SENSOR", "locker=3 pin=BCM16 sensor_fault limit_pin=BCM12 none_for=1.020s"),
        (9.0, "SENSOR", "locker=3 pin=BCM16 sensor_ok limit_pin=BCM12 valid_for=2.000s")]))
    a = LA.audit_lockers(None, hw, max_unlock_sec=30.0, unlock_timeout_sec=10.0, close_mode="limit")
    assert a["forced_opens"] == 1 and a["sensor_faults"] == 1
    assert a["integrity"]["on_while_closed_overtime"] == 0 and a["integrity_ok"] is True


def test_events_v15_rows_fallback_and_37_columns(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"

    def door(sec: float, event: str, lid: str, msg: str, cause: str = "") -> dict[str, str]:
        return {"ts": ts(sec), "session_id": "S1", "frame_idx": "0", "event": event, "locker_id": lid, "message": msg,
                "close_cause": cause, "locker_states": "1=LOCKED_CLOSED;2=OPEN"}
    rows = [door(2.0, "door_opened", "2", "unlocked_for=1.500s"),
            door(5.0, "door_forced", "1", ""),
            door(6.0, "door_sensor", "3", "sensor_fault locker=3 none_for=1.020s"),
            door(7.0, "door_sensor", "3", "sensor_ok locker=3"),
            door(20.0, "door_close", "1", "cause=unlock_timeout unlocked_for=10.000s door_open_for=0.000s", "unlock_timeout"),
            door(40.0, "door_close", "2", "cause=watchdog unlocked_for=30.000s door_open_for=0.000s", "watchdog")]
    write_csv(ev, COLS37, rows)
    header, parsed = LA.read_events(ev)
    assert len(header) == 37 and header[-1] == "locker_states" and parsed[0]["locker_states"].startswith("1=")
    a = LA.audit_lockers(ev, None, max_unlock_sec=30.0, unlock_timeout_sec=10.0)      # close_mode inferred from rows
    assert a["per_locker_data"] is True and a["params"]["mode_used"] == "limit"
    assert (a["forced_opens"], a["sensor_faults"], a["unlock_timeouts"], a["watchdog_relocks"]) == (1, 1, 1, 1)
    assert a["close_causes"] == {"unlock_timeout": 1, "watchdog": 1}
    # 30 s ON with the door never opened (cause=watchdog) breaks the "closed too long" bound; 10.0 s does not
    assert a["integrity"]["on_while_closed_overtime"] == 1
    assert a["integrity"]["watchdog_overruns"] is None                                  # needs hardware.log


def test_timer_mode_has_no_closed_overtime_and_stays_ok(tmp_path: Path) -> None:
    hw = tmp_path / "hardware.log"
    write_hw(hw, [INIT, (1.0, "OPEN", f"locker=2 pin=BCM25 {R_ALICE} signal=ON duration=3.000s"),
                  (4.0, "WATCHDOG", "locker=2 pin=BCM25 timeout signal=OFF")])
    a = LA.audit_lockers(None, hw, max_open_sec=5.0, close_mode="timer")
    assert a["integrity"]["on_while_closed_overtime"] is None and a["integrity_ok"] is True
    assert a["params"]["mode_used"] == "timer" and a["close_causes"] == {"timer": 1}


# =========================================================================== labels (security incidents)
def test_label_based_locker_incidents(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"
    rows = [frame(1, 1.0, event="open", door=1, message=R_ALICE, locker_id="2", open_lockers="2", label="bob"),
            frame(2, 9.0, event="open", door=1, best="carol", message=R_CAROL, locker_id="3", open_lockers="3",
                  label="_impostor"),
            frame(3, 20.0, event="open", door=1, message=R_ALICE, locker_id="2", open_lockers="2", label="alice"),
            # rejected / suppressed rows never count as opens even when labelled
            frame(4, 21.0, event="open", door=1, message=f"{R_ALICE} (rejected by door: busy)", locker_id="2",
                  open_lockers="2", label="bob"),
            frame(5, 30.0, event="open", door=0, message="open suppressed: bob has no locker_id (lockers.mode=per_locker)",
                  best="bob", label="_impostor")]
    write_csv(ev, COLS35, rows)
    a = LA.audit_lockers(ev, None, max_open_sec=5.0)
    assert a["misid_locker_opens"] == 1 and a["impostor_locker_opens"] == 1 and a["n_labelled_opens"] == 3
    assert a["rejected"] == {"busy": 1} and a["rejected_source"] == "events"


def test_locker_misroute_rate(tmp_path: Path) -> None:
    g = tmp_path / "gallery"
    for pid, lk in (("alice", "1"), ("bob", "2"), ("carol", None), ("dave", 12)):
        (g / pid).mkdir(parents=True)
        (g / pid / "person.json").write_text(json.dumps({"person_id": pid, "locker_id": lk}), encoding="utf-8")
    lockers = LA.gallery_lockers(g)
    assert lockers == {"alice": "1", "bob": "2", "carol": None, "dave": None}   # numeric 12 -> invalid -> None

    def rec(true_id: str, verdict: str, acc: str) -> ScoreRecord:
        return ScoreRecord(probe_path=f"p/{true_id}.jpg", condition="normal", is_synthetic=False, true_id=true_id,
                           split="test", verdict=verdict, hold_reason="", best_id=acc, best_score=0.7, second_id="",
                           second_score=None, own_score=None, max_other_score=None, matched_tag="normal",
                           threshold=0.5, threshold_group="global", accepted_id=acc if verdict == "MATCH" else "",
                           latency_ms=1.0)
    recs = [rec("alice", "MATCH", "alice"), rec("alice", "MATCH", "bob"),      # misroute (1 -> 2)
            rec("carol", "MATCH", "alice"),                                      # carol has no locker -> 1 opens: misroute
            rec("bob", "MATCH", "carol"),                                        # carol has no locker: nothing opens
            rec("bob", "UNKNOWN", ""), rec("_impostor", "MATCH", "alice")]       # impostor not in denominator
    mr = LA.locker_misroute(recs, lockers)
    assert mr["n_genuine"] == 5 and mr["n_misroute"] == 2
    assert mr["locker_misroute_rate"] == pytest.approx(2 / 5)


# =========================================================================== scenario 23: 33 / 35 columns
@pytest.mark.parametrize("cols", [COLS33, COLS35], ids=["33col", "35col"])
def test_scenario23_records_from_events_both_layouts(tmp_path: Path, cols: tuple[str, ...]) -> None:
    ev = tmp_path / "events.csv"
    rows = [frame(0, 0.5, label="alice"),
            frame(1, 1.0, event="open", door=1, message=R_ALICE, locker_id="2", open_lockers="2", label="alice"),
            frame(2, 1.5, event="deny", verdict="UNKNOWN", best="bob", score=0.3, label="_impostor"),
            frame(3, 2.0, label=""),
            {"ts": ts(3), "session_id": "S1", "frame_idx": "3", "event": "door_close", "label": "alice"}]
    write_csv(ev, cols, rows)
    recs, warns = records_from_events(ev)
    assert len(recs) == 3                                   # frame + open + deny; door_close never a frame
    assert [r.true_id for r in recs] == ["alice", "alice", "_impostor"]
    assert recs[1].accepted_id == "alice" and recs[2].verdict == "UNKNOWN"
    assert any("라벨 없는" in w for w in warns)
    # the audit reads both layouts too; 33 columns -> routing columns missing is stated, not guessed
    a = LA.audit_lockers(ev, None, max_open_sec=5.0)
    if cols is COLS33:
        assert a["per_locker_data"] is False and any("33컬럼" in w for w in a["warnings"])
    else:
        assert a["per_locker_data"] is True and a["integrity"]["wrong_locker_opens"] == 0


@pytest.mark.parametrize("cols", [COLS33, COLS35], ids=["33col", "35col"])
def test_scenario23_label_events_preserves_columns_and_labels_open_rows(tmp_path: Path, cols: tuple[str, ...]) -> None:
    ev = tmp_path / "events.csv"
    write_csv(ev, cols, per_locker_events())
    before = list(csv.reader(ev.read_text(encoding="utf-8").splitlines()))
    rc = label_events.main(["--events", str(ev), "--session", "S1", "--label", "alice", "--from-frame", "3",
                            "--to-frame", "6", "--set", f"app.gallery_dir={(tmp_path / 'nog').as_posix()}"])
    assert rc == 0
    after = list(csv.reader(ev.read_text(encoding="utf-8").splitlines()))
    assert after[0] == list(cols)                                      # header (33 or 35) kept as is
    li = after[0].index("label")
    assert len(after) == len(before)
    for b, a in zip(before[1:], after[1:]):
        assert len(a) == len(cols)
        assert [x for j, x in enumerate(a) if j != li] == [x for j, x in enumerate(b) if j != li]   # only label changes
    by = {(r[after[0].index("frame_idx")], r[after[0].index("event")]): r[li] for r in after[1:]}
    assert by[("3", "open")] == "alice"          # event=open row labelled (13.8)
    assert by[("4", "frame")] == "alice"
    assert by[("5", "open")] == "alice" and by[("6", "open")] == "alice"
    assert by[("4", "door_close")] == ""         # non-frame rows never labelled
    assert by[("1", "frame")] == "" and by[("7", "open")] == ""
    if cols is COLS35:
        assert after[4][after[0].index("open_lockers")] == "2" and after[4][after[0].index("locker_id")] == "2"


def test_label_events_refuses_rows_longer_than_header(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"
    with open(ev, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS33)
        w.writerow([frame(0, 0.5).get(c, "") for c in COLS35])   # 35 fields under a 33-column header
    before = ev.read_bytes()
    assert label_events.main(["--events", str(ev), "--session", "S1", "--label", "_ignore"]) == 2
    assert ev.read_bytes() == before


# =========================================================================== evaluate.py integration
def test_evaluate_with_clean_per_locker_logs_exit_0(tmp_path: Path) -> None:
    ev, hw = tmp_path / "events.csv", tmp_path / "hardware.log"
    rows = per_locker_events()
    for r in rows:
        if r.get("event") in ("frame", "open"):
            r["label"] = "alice" if r.get("best_id") == "alice" else "_impostor"
    write_csv(ev, COLS35, rows)
    write_hw(hw, per_locker_hw())
    out = tmp_path / "reports"
    rc = evaluate.main(["--gallery", str(tmp_path / "g"), "--probe", str(tmp_path / "p"), "--events", str(ev),
                        "--hardware-log", str(hw), "--out", str(out), "--set", "evaluation.bootstrap_iters=10"])
    assert rc == 0
    (rep,) = list(out.iterdir())
    m = json.loads((rep / "metrics.json").read_text(encoding="utf-8"))
    lk = m["locker"]
    assert lk["integrity_ok"] is True
    assert lk["impostor_locker_opens"] == 1           # carol's open was labelled _impostor in this synthetic log
    assert lk["misid_locker_opens"] == 0
    assert m["overall"]["n_genuine"] >= 4             # open rows are included as frame records
    assert "alice" in m["per_person"] and m["per_person"]["alice"]["frr"] is not None
    html = (rep / "report.html").read_text(encoding="utf-8")
    assert "사물함 감사" in html and "무결성 지표 5개 모두 0" in html and "사물함 무결성 오류" not in html


def test_evaluate_single_mode_logs_have_no_locker_key(tmp_path: Path) -> None:
    ev, hw = tmp_path / "events.csv", tmp_path / "hardware.log"
    write_csv(ev, COLS35, [frame(0, 0.5, label="alice"),
                           frame(1, 1.0, event="open", door=1, message="face:alice:0.7000:3/3", label="alice")])
    write_hw(hw, [(0.0, "INIT", "single solenoid=BCM17 green=BCM27; outputs OFF"),
                  (1.0, "OPEN", "face:alice:0.7000:3/3 signal=ON duration=3.000s"),
                  (4.0, "WATCHDOG", "timeout signal=OFF")])
    out = tmp_path / "reports"
    rc = evaluate.main(["--gallery", str(tmp_path / "g"), "--probe", str(tmp_path / "p"), "--events", str(ev),
                        "--hardware-log", str(hw), "--out", str(out), "--set", "evaluation.bootstrap_iters=10",
                        "--set", "lockers.mode=single", "--set", "hardware.locker_outputs=[]",
                        "--set", "hardware.solenoid_pin=17", "--set", "hardware.close_mode=timer"])
    assert rc == 0
    (rep,) = list(out.iterdir())
    m = json.loads((rep / "metrics.json").read_text(encoding="utf-8"))
    assert "locker" not in m
    assert "사물함 감사" not in (rep / "report.html").read_text(encoding="utf-8")


def test_evaluate_missing_hardware_log_is_config_error(tmp_path: Path) -> None:
    ev = tmp_path / "events.csv"
    write_csv(ev, COLS35, [frame(0, 0.5, label="alice")])
    assert evaluate.main(["--events", str(ev), "--hardware-log", str(tmp_path / "nope.log"),
                          "--gallery", str(tmp_path / "g"), "--probe", str(tmp_path / "p"),
                          "--out", str(tmp_path / "r")]) == 2


# =========================================================================== hat condition / per_person
def test_hat_condition_order_and_per_person() -> None:
    assert condition_order({"syn_mask", "mask", "hat", "normal", "zzz"}) == ["normal", "hat", "mask", "zzz", "syn_mask"]

    def rec(true_id: str, cond: str, verdict: str, acc: str) -> ScoreRecord:
        return ScoreRecord(probe_path=f"p/{cond}/{true_id}/x.jpg", condition=cond, is_synthetic=False, true_id=true_id,
                           split="test", verdict=verdict, hold_reason="", best_id=acc, best_score=0.7, second_id="",
                           second_score=None, own_score=0.7 if acc == true_id else None, max_other_score=None,
                           matched_tag="normal", threshold=0.5, threshold_group="global",
                           accepted_id=acc if verdict == "MATCH" else "", latency_ms=1.0)
    recs = [rec("alice", "hat", "MATCH", "alice"), rec("alice", "hat", "HOLD", ""), rec("alice", "normal", "MATCH", "alice"),
            rec("alice", "normal", "MATCH", "alice"), rec("bob", "normal", "UNKNOWN", "")]
    m = compute_metrics(recs, bootstrap_iters=5)
    assert "hat" in m["per_condition"]
    assert m["per_person"]["alice"]["frr"] == pytest.approx(1 / 4)
    assert m["per_person"]["alice"]["frr_matcher"] == pytest.approx(0.0)
    assert m["per_person"]["bob"]["frr"] == pytest.approx(1.0)
    assert m["per_person"]["alice"]["conditions"] == ["hat", "normal"]
