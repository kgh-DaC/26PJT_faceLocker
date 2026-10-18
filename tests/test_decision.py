"""VoteDecider (N-of-M temporal voting) rules, INTERFACES section 3.9."""

from __future__ import annotations

import itertools

import pytest

from conftest import FakeClock, make_result
from facedoor.config import DecisionSection
from facedoor.datatypes import DecisionState, FrameVerdict, HoldReason
from facedoor.decision import VoteDecider

M, U, H, N = FrameVerdict.MATCH, FrameVerdict.UNKNOWN, FrameVerdict.HOLD, FrameVerdict.NO_FACE


def make(clock: FakeClock, **kw) -> VoteDecider:
    return VoteDecider(DecisionSection(**kw), clock=clock)


def feed(d: VoteDecider, clock: FakeClock, seq, dt: float = 0.1):
    outs = []
    for item in seq:
        v, p = item if isinstance(item, tuple) else (item, None)
        hold = HoldReason.BLUR if v is H else None
        outs.append(d.update(make_result(v, p, hold=hold)))
        clock.advance(dt)
    return outs


def test_three_of_five_opens_exactly_once(clock):
    d = make(clock)
    outs = feed(d, clock, [(M, "alice"), (M, "alice"), (M, "alice")])
    assert [o.should_open for o in outs] == [False, False, True]
    assert outs[1].state is DecisionState.VERIFYING and outs[1].votes == 2
    o = outs[2]
    assert o.state is DecisionState.OPEN and o.person_id == "alice" and o.votes == 3 and o.window == 3
    assert o.best_score == pytest.approx(0.8)


def test_rearm_ignores_inputs_then_rearms(clock):
    d = make(clock, rearm_sec=5.0)
    feed(d, clock, [(M, "alice")] * 3)
    outs = feed(d, clock, [(M, "alice")] * 10, dt=0.3)  # 3.0 s < rearm
    assert all(o.state is DecisionState.REARM and not o.should_open for o in outs)
    clock.advance(2.5)
    outs = feed(d, clock, [(M, "alice")] * 3)
    assert outs[-1].should_open


def test_hold_and_no_face_occupy_slots_without_reset(clock):
    d = make(clock)
    outs = feed(d, clock, [(M, "a"), H, (M, "a"), N, (M, "a")])
    assert outs[-1].should_open and outs[-1].votes == 3 and outs[-1].window == 5


def test_slots_push_old_votes_out_of_window(clock):
    d = make(clock)
    outs = feed(d, clock, [(M, "a"), (M, "a"), H, H, H, (M, "a")])
    # window = last 5 slots: the first MATCH fell out -> 2 votes, no open
    assert not any(o.should_open for o in outs)
    assert outs[-1].votes == 2 and outs[-1].window == 5


def test_unknown_resets_window(clock):
    d = make(clock)
    outs = feed(d, clock, [(M, "a"), (M, "a"), U, (M, "a"), (M, "a")])
    assert not any(o.should_open for o in outs)
    assert outs[-1].votes == 2


def test_other_person_resets_window(clock):
    d = make(clock)
    outs = feed(d, clock, [(M, "a"), (M, "a"), (M, "b"), (M, "a"), (M, "a")])
    assert not any(o.should_open for o in outs)
    assert outs[2].person_id == "b" and outs[2].votes == 1


def test_window_timeout_drops_stale_votes(clock):
    d = make(clock, window_timeout_sec=2.0)
    feed(d, clock, [(M, "a"), (M, "a")])
    clock.advance(3.0)
    o = d.update(make_result(M, "a"))
    assert not o.should_open and o.votes == 1


def test_deny_after_consecutive_unknown_with_cooldown(clock):
    d = make(clock, deny_after_unknown=3, deny_cooldown_sec=2.0)
    outs = feed(d, clock, [U, U, U])
    assert [o.should_deny for o in outs] == [False, False, True]
    assert outs[-1].state is DecisionState.DENIED and not outs[-1].should_open
    outs = feed(d, clock, [U, U, U], dt=0.1)  # within cooldown -> no second deny
    assert not any(o.should_deny for o in outs)
    clock.advance(2.0)
    outs = feed(d, clock, [U, U, U])
    assert any(o.should_deny for o in outs)


def test_hold_state_reported(clock):
    d = make(clock)
    o = feed(d, clock, [H])[0]
    assert o.state is DecisionState.HOLD and not o.should_open


@pytest.mark.parametrize("seq", list(itertools.product([U, H, N], repeat=5)))
def test_never_opens_without_match(seq):
    clock = FakeClock()
    d = make(clock)
    assert not any(o.should_open for o in feed(d, clock, list(seq) * 3))


def test_reset_clears_state(clock):
    d = make(clock)
    feed(d, clock, [(M, "a"), (M, "a")])
    d.reset()
    o = d.update(make_result(M, "a"))
    assert o.votes == 1 and d.state is DecisionState.VERIFYING
