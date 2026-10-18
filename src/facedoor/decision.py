"""N-of-M temporal voting (owner: vision-engineer). Contract: INTERFACES section 3.9.

should_open is True only from MATCH votes, exactly once per OPEN decision; then REARM ignores input for rearm_sec.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from facedoor.config import DecisionSection
from facedoor.datatypes import DecisionOutput, DecisionState, FrameVerdict, RecognitionResult


@dataclass(slots=True)
class _Entry:
    ts: float
    verdict: FrameVerdict
    person_id: str | None
    score: float | None


class VoteDecider:
    def __init__(self, cfg: DecisionSection, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.clock = clock
        self._window: deque[_Entry] = deque()
        self._state = DecisionState.IDLE
        self._rearm_until: float | None = None
        self._unknown_streak = 0
        self._last_deny: float | None = None
        self._last_open_person: str | None = None

    @property
    def state(self) -> DecisionState:
        return self._state

    def reset(self) -> None:
        self._window.clear()
        self._state = DecisionState.IDLE
        self._rearm_until = None
        self._unknown_streak = 0
        self._last_deny = None

    def _out(self, state: DecisionState, reason: str, should_open: bool = False, should_deny: bool = False,
             person: str | None = None, votes: int = 0, window: int | None = None,
             best: float | None = None) -> DecisionOutput:
        self._state = state
        return DecisionOutput(state, should_open, should_deny, person, votes,
                              len(self._window) if window is None else window, best, reason)

    def _tally(self) -> tuple[str | None, int, float | None]:
        counts: dict[str, int] = {}
        best: dict[str, float] = {}
        last_seen: dict[str, int] = {}
        for i, e in enumerate(self._window):
            if e.verdict is FrameVerdict.MATCH and e.person_id is not None:
                counts[e.person_id] = counts.get(e.person_id, 0) + 1
                best[e.person_id] = max(best.get(e.person_id, -2.0), e.score if e.score is not None else -2.0)
                last_seen[e.person_id] = i
        if not counts:
            return None, 0, None
        p = max(counts, key=lambda k: (counts[k], last_seen[k]))
        return p, counts[p], best[p]

    def update(self, result: RecognitionResult, now: float | None = None) -> DecisionOutput:
        cfg = self.cfg
        now = self.clock() if now is None else float(now)

        # rule 4: REARM ignores every input
        if self._rearm_until is not None:
            if now < self._rearm_until:
                return self._out(DecisionState.REARM, f"rearm {self._rearm_until - now:.1f}s",
                                 person=self._last_open_person, window=0)
            self._rearm_until = None

        # rule 1: drop stale entries
        while self._window and now - self._window[0].ts > cfg.window_timeout_sec:
            self._window.popleft()

        v = result.verdict
        pid = result.matched_id
        score = result.match.best.score if (result.match is not None and result.match.best is not None) else None

        if v is FrameVerdict.UNKNOWN:
            self._unknown_streak += 1
            if cfg.reset_on_unknown:
                self._window.clear()
            else:
                self._window.append(_Entry(now, v, None, score))
            if self._unknown_streak >= cfg.deny_after_unknown and (
                    self._last_deny is None or now - self._last_deny >= cfg.deny_cooldown_sec):
                self._last_deny = now
                self._unknown_streak = 0
                self._window.clear()
                return self._out(DecisionState.DENIED, f"{cfg.deny_after_unknown} consecutive UNKNOWN",
                                 should_deny=True)
        elif v is FrameVerdict.MATCH and pid is not None:
            self._unknown_streak = 0
            if cfg.reset_on_other_person and any(
                    e.verdict is FrameVerdict.MATCH and e.person_id != pid for e in self._window):
                self._window.clear()
            self._window.append(_Entry(now, v, pid, score))
        else:  # HOLD / NO_FACE occupy a slot only (rule 5)
            if v is FrameVerdict.NO_FACE:
                self._unknown_streak = 0
            self._window.append(_Entry(now, v, None, None))

        while len(self._window) > cfg.window_frames:
            self._window.popleft()

        person, votes, best = self._tally()
        window_len = len(self._window)
        if person is not None and votes >= cfg.min_votes:
            self._window.clear()
            self._rearm_until = now + cfg.rearm_sec
            self._last_open_person = person
            return self._out(DecisionState.OPEN, f"{votes}/{window_len} votes for {person}", should_open=True,
                             person=person, votes=votes, window=window_len, best=best)
        if person is not None:
            return self._out(DecisionState.VERIFYING, f"{votes}/{window_len} votes for {person} "
                             f"(need {cfg.min_votes})", person=person, votes=votes, best=best)
        if v is FrameVerdict.HOLD:
            reason = result.hold_reason.value if result.hold_reason is not None else "hold"
            return self._out(DecisionState.HOLD, f"hold: {reason}")
        if v is FrameVerdict.UNKNOWN:
            return self._out(DecisionState.IDLE, f"unknown x{self._unknown_streak}")
        return self._out(DecisionState.IDLE, "idle")
