"""
What the twin is doing *right now* -- the read-only half of W3.

`shuo/config_api.py` (:3041) cannot see a call. It is a separate process by
design (decision 32), and merging the two to give the panel a transcript
would undo the exact thing that split protects. So the call server keeps a
small in-memory record of the live call and serves it over
`GET /calls/live`; :3041 proxies that to the operator's split-pane panel.

Three rules, and they are the whole design:

- **Publishing is a `deque.append` and nothing else.** No disk, no lock, no
  `await`, no JSON. Every publish site is on the audio process's event loop,
  which paces a 160-byte frame every 20ms against an accumulating deadline
  -- and decision 24 removed the player's ability to claw lateness back, so
  a stall there is not one late frame, it is permanent stream delay for the
  rest of the call. Serialization happens in the *read* path, inside the
  HTTP handler, where a slow poll costs the poller and nobody else.
- **It never raises.** A monitor that can end a call is worse than no
  monitor at all. Every publish goes through `_emit`, which swallows and
  logs once. Same posture as `runtime_config.load_call_settings`.
- **It observes at the dispatch boundary; the state machine never learns it
  exists.** No new events, no new actions, `process_event` stays
  `(State, Event) -> (State, [Action])` with zero I/O (CLAUDE.md rule 1).
  This is the same category of thing as `Tracer` and `_TurnCompletion`.

Two consequences worth stating, because both were choices:

**Interim caller text is not an event.** Deepgram Flux emits `Update` many
times a second while someone is speaking; appending each one would flush the
entire transcript out of a 200-entry ring buffer in a couple of seconds. It
is held as a single `partial` field, replaced in place and cleared when the
final transcript lands. That is also what the panel wants to render: a
greyed in-progress line under a settled transcript.

**The agent's own text arrives once per turn, not once per token.** The
token loop in `agent.py` is the streaming chain that is the entire latency
advantage, and a read-only preview does not get to put anything in it beyond
a `list.append`. The joined string is published when the turn ends.

Not thread-safe, and does not need to be: every publisher and the single
reader all run on the one event loop of the call process, and no method here
awaits, so none of them can interleave.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional

from .log import get_logger

logger = get_logger("shuo.call_monitor")


# =============================================================================
# LIMITS
# =============================================================================

# A turn contributes ~5 events (caller, agent, three timing marks), so this
# holds roughly 40 turns -- longer than any test call, and bounded so a
# forgotten panel cannot grow the audio process's heap.
MAX_EVENTS = 200

# Calls kept for reading after they end. More than one because a panel
# polling across a hangup should still be able to show the call that just
# finished; not many more, because this is a live view, not a call log.
MAX_CALLS = 3

# Per-event text ceiling. The candidate persona sustains 20-45s answers
# (CLAUDE.md §4.4), which is well under this; the cap exists so a runaway
# generation cannot put a megabyte in the buffer.
MAX_TEXT_CHARS = 4000


# The four states `shuo-frontend/components/dashboard/test-call-status.tsx`
# already renders. Named here rather than derived from `Phase` so that the
# mapping is one visible place: the pipeline has two phases, the panel has
# four, and the extra two are the edges of the call rather than states the
# machine knows about.
CONNECTING = "connecting"
LISTENING = "listening"
SPEAKING = "speaking"
ENDED = "ended"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _clip(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + "…"


# =============================================================================
# ONE CALL
# =============================================================================

@dataclass
class _Call:
    """
    The mutable summary of a single call.

    `id` is ours and is stable from the moment the socket is accepted;
    `call_id` is the carrier's and is empty until the `start` frame arrives.
    The panel keys its cursor on `id` for exactly that reason -- a field that
    changes value mid-call is a field that silently resets the cursor and
    replays the transcript.
    """

    id: str
    persona: str
    direction: str
    started_at: str
    started_monotonic: float

    call_id: str = ""
    state: str = CONNECTING
    config: str = ""
    partial: str = ""
    ended_reason: str = ""
    turns: int = 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    @property
    def live(self) -> bool:
        return self.state != ENDED


class CallRecorder:
    """
    One call's handle on the monitor.

    Held by `run_conversation` and handed to `Agent`, the same way `Tracer`
    is. Every method is fire-and-forget: nothing returns a value the caller
    is expected to check, and nothing raises.

    A recorder with no monitor behind it (`CallRecorder.disabled()`) no-ops
    on every call. That is what `Agent` falls back to when it is constructed
    outside a live call -- in the tests, and in any future caller -- so no
    site has to guard with `if self._recorder:`.
    """

    __slots__ = ("_monitor", "_call")

    def __init__(self, monitor: Optional["CallMonitor"], call: Optional[_Call]):
        self._monitor = monitor
        self._call = call

    @classmethod
    def disabled(cls) -> "CallRecorder":
        return cls(None, None)

    @property
    def call_id(self) -> str:
        return self._call.call_id if self._call else ""

    # ── Lifecycle ───────────────────────────────────────────────────

    def identify(self, call_id: str) -> None:
        """
        The carrier's call id, learned from the `start` frame.

        Not the value `originate` returned: [server.py](server.py)'s
        `/hangup` handler already records that `request_uuid` is not reliably
        the same as `CallUUID`, and hanging up is the one operation where
        being wrong about which call this is matters.
        """
        if self._call and call_id and not self._call.call_id:
            self._call.call_id = call_id
            self._emit("connected", callId=call_id)

    def configured(self, description: str) -> None:
        """
        What this call resolved from the config store (W2's provenance line).

        The single most useful thing in the panel: it answers "did my save
        apply?" *on the call*, rather than by inference from how the twin
        sounded.
        """
        if self._call:
            self._call.config = description
        self._emit("config", text=description)

    def phase(self, state: str) -> None:
        """Pipeline phase -> panel state. Idempotent; repeats emit nothing."""
        if not self._call or self._call.state == state:
            return
        if not self._call.live:
            # A late phase change after the call ended must not resurrect it.
            return
        self._call.state = state

    def ended(self, reason: str = "") -> None:
        if not self._call:
            return
        self._call.state = ENDED
        self._call.ended_reason = reason
        self._call.partial = ""
        self._emit("ended", text=reason)

    # ── Transcript ──────────────────────────────────────────────────

    def caller_partial(self, text: str) -> None:
        """
        In-progress caller text. Replaced in place, never appended.

        See the module docstring: Flux emits these many times a second and
        appending them would evict the settled transcript within seconds.
        """
        if self._call and self._call.live:
            self._call.partial = _clip(text)

    def caller_said(self, text: str) -> None:
        """The settled caller transcript for a turn."""
        if self._call:
            self._call.partial = ""
        text = _clip(text)
        if text:
            self._emit("caller", text=text)

    def agent_said(self, text: str, *, turn: int = 0, interrupted: bool = False) -> None:
        """
        The twin's answer, joined and published once the turn is over.

        `interrupted` marks a turn the caller barged in on. The text is the
        whole generation, not the part that was actually heard -- truncating
        to what the caller heard is Bug A, and it is not solved yet. The flag
        is there so the panel does not present an unheard sentence as
        something the twin said.
        """
        text = _clip(text)
        if text:
            self._emit("agent", text=text, turn=turn, interrupted=interrupted)

    # ── Instrumentation ─────────────────────────────────────────────

    def timing(self, name: str, ms: int, *, turn: int = 0) -> None:
        """
        A latency milestone, in milliseconds from the top of the turn.

        These are the numbers a real test call exists to produce, so they go
        to the panel rather than only to the trace file: the operator sees
        first-token and first-audio latency while the call is still running.
        """
        self._emit("timing", name=name, ms=int(ms), turn=turn)

    def turn_started(self, turn: int) -> None:
        if self._call:
            self._call.turns = max(self._call.turns, turn)

    def note(self, text: str) -> None:
        """
        Something the operator should see that is not speech.

        A vendor refusing to synthesise is the case this exists for: per
        decision 29 that failure is not an error the caller can hear, it is
        *silence*, and without a line in the panel it looks like the twin
        simply had nothing to say.
        """
        text = _clip(text)
        if text:
            self._emit("note", text=text)

    # ── Internals ───────────────────────────────────────────────────

    def _emit(self, kind: str, **fields: Any) -> None:
        if self._monitor is None or self._call is None:
            return
        self._monitor._append(self._call, kind, fields)


# =============================================================================
# THE MONITOR
# =============================================================================

class CallMonitor:
    """
    A bounded ring buffer of what happened on the last few calls.

    One instance per process (`MONITOR` below). `begin` opens a call and
    returns the recorder the call loop publishes through; `snapshot` is the
    read path the HTTP handler serves.
    """

    def __init__(self, *, max_events: int = MAX_EVENTS, max_calls: int = MAX_CALLS):
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max_events)
        self._calls: "OrderedDict[str, _Call]" = OrderedDict()
        self._seq = 0
        self._counter = 0
        self._warned = False

    # ── Write path (hot) ────────────────────────────────────────────

    def begin(self, *, persona: str, direction: str) -> CallRecorder:
        """
        Open a call. Called once, when the media socket is accepted.

        Returns a recorder even on failure -- a disabled one -- because the
        call loop is going to publish through it either way and must not
        have to check.
        """
        try:
            self._counter += 1
            call = _Call(
                id=f"call-{self._counter}",
                persona=persona,
                direction=direction,
                started_at=_now_iso(),
                started_monotonic=time.monotonic(),
            )
            self._calls[call.id] = call
            while len(self._calls) > MAX_CALLS:
                self._calls.popitem(last=False)
            return CallRecorder(self, call)
        except Exception as exc:  # pragma: no cover - defensive
            self._warn(exc)
            return CallRecorder.disabled()

    def _append(self, call: _Call, kind: str, fields: Dict[str, Any]) -> None:
        """
        The one guarded write site. Must stay O(1) and allocation-light.

        `try` costs nothing on the non-raising path in CPython, and the
        alternative -- an unguarded append in the middle of the call loop --
        trades a free branch for the ability to drop a call.
        """
        try:
            self._seq += 1
            event = {
                "seq": self._seq,
                "id": call.id,
                "atMs": call.elapsed_ms(),
                "kind": kind,
            }
            event.update(fields)
            self._events.append(event)
        except Exception as exc:  # pragma: no cover - defensive
            self._warn(exc)

    def _warn(self, exc: Exception) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning(
            f"Call monitor failed to record an event ({exc!r}). The panel will "
            f"be incomplete; the call is unaffected."
        )

    # ── Read path (off the hot path) ────────────────────────────────

    def current(self) -> Optional[_Call]:
        """The call still in progress, if there is one."""
        for call in reversed(self._calls.values()):
            if call.live:
                return call
        return None

    def latest(self) -> Optional[_Call]:
        """The most recently opened call, live or not."""
        return next(reversed(self._calls.values()), None)

    def snapshot(self, *, since: int = 0, call_ref: Optional[str] = None) -> Dict[str, Any]:
        """
        Everything the panel needs for one poll.

        `since` is the `nextSeq` from the previous poll, so a steady-state
        poll carries nothing at all. The sequence is global across calls
        rather than per-call, which is what makes the cursor safe to keep
        across a call boundary: a number that resets would replay the whole
        buffer the first time a second call started.

        `call_ref` matches either our `id` or the carrier's `callId`, so a
        panel that has only ever seen one of them can still ask for it.
        """
        call = self._select(call_ref)

        if call is None:
            return {
                "id": None,
                "callId": "",
                "live": False,
                "state": None,
                "events": [],
                "nextSeq": self._seq,
                "missed": 0,
            }

        events = [
            event
            for event in self._events
            if event["seq"] > since and event["id"] == call.id
        ]

        return {
            "id": call.id,
            "callId": call.call_id,
            "live": call.live,
            "state": call.state,
            "persona": call.persona,
            "direction": call.direction,
            "startedAt": call.started_at,
            "durationMs": call.elapsed_ms(),
            "config": call.config,
            "partial": call.partial,
            "turns": call.turns,
            "endedReason": call.ended_reason,
            "events": events,
            "nextSeq": self._seq,
            "missed": self._missed(since),
        }

    def _select(self, call_ref: Optional[str]) -> Optional[_Call]:
        if call_ref:
            found = self._calls.get(call_ref)
            if found is not None:
                return found
            for call in reversed(self._calls.values()):
                if call.call_id and call.call_id == call_ref:
                    return call
            return None
        return self.latest()

    def _missed(self, since: int) -> int:
        """
        How many events aged out of the buffer before the panel read them.

        A transcript with a silent hole in it is worse than one that admits
        the hole -- the whole point of the panel is judging whether the twin
        contradicted itself three turns ago. Counted against the unfiltered
        stream, so it can over-report across a call boundary; that direction
        is the safe one.
        """
        if since <= 0 or not self._events:
            return 0
        oldest = self._events[0]["seq"]
        return max(0, oldest - since - 1)

    # ── Testing seam ────────────────────────────────────────────────

    def reset(self) -> None:
        """Drop everything. For tests, and for nothing else."""
        self._events.clear()
        self._calls.clear()
        self._seq = 0
        self._counter = 0
        self._warned = False


# One per process. The call loop publishes into it; `GET /calls/live` reads
# it. Deliberately module-level rather than threaded through `CallContext`:
# the HTTP handler has no path to the call loop's locals.
MONITOR = CallMonitor()
