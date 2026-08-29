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

**The archive is not the ring buffer.** Alongside the 200-event ring, each
call accumulates its own transcript and milestone lists, and `record()` hands
them to `call_history.append` once the call is over. They are separate
because the ring is bounded: a 40-turn call has already evicted its own
opening by the time it ends, so there is no whole call left in there to save.
Both are `list.append` on the same guarded write site, so the hot path is
unchanged -- and nothing here writes to disk. The single write is in
`conversation.py`'s teardown, after the call.

**One ring per call, one sequence for the process** (W5c). The events live on
`_Call`, so a flood on one call cannot evict another's transcript -- the thing
a single shared ring got wrong the moment two calls overlapped. `_seq` stays
*global* on purpose, and the two facts are not in tension: the deque decides
which events a call keeps, the sequence decides what a panel has already
seen. A per-call sequence would reset to zero on the next call, and a panel
holding cursor 40 would either replay the whole buffer or skip it entirely.

Not thread-safe, and does not need to be: every publisher and the single
reader all run on the one event loop of the call process, and no method here
awaits, so none of them can interleave.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from .call_status import (
    CALL_COMPLETED,
    CALL_FAILED,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    MISSED,
    NO_ANSWER,
)
from .log import get_logger

logger = get_logger("shuo.call_monitor")


# =============================================================================
# LIMITS
# =============================================================================

# A turn contributes ~5 events (caller, agent, three timing marks), so this
# holds roughly 40 turns -- longer than any test call, and bounded so a
# forgotten panel cannot grow the audio process's heap.
#
# 🔴 **Per call, not per process** (W5c). It was one shared ring until Phase 8,
# and that made two concurrent calls evict each other's transcripts: a busy
# receptionist line would flush an interview's opening turns out of the buffer
# from the other side of the process. Bounded heap is still the property being
# protected -- it is now `MAX_EVENTS * MAX_CALLS`, which is the same order of
# magnitude and still a ceiling.
MAX_EVENTS = 200

# Calls kept for reading after they end. More than one because a panel
# polling across a hangup should still be able to show the call that just
# finished; not many more, because this is a live view, not a call log.
#
# Raised 3 -> 8 in W5c, when `/calls/active` gave the panel a reason to ask
# about calls in the plural. Eight is a working ceiling on concurrency for a
# single box rather than a measured limit -- if real traffic ever exceeds it,
# the calls that get dropped from the *view* are the oldest, and no call is
# ever affected by being unobserved.
MAX_CALLS = 8

# Per-event text ceiling. The candidate persona sustains 20-45s answers
# (CLAUDE.md §4.4), which is well under this; the cap exists so a runaway
# generation cannot put a megabyte in the buffer.
MAX_TEXT_CHARS = 4000

# Per-call ceilings on the *archive* (see `_Call.transcript`/`milestones`).
# These are separate from MAX_EVENTS because they answer a different
# question: the ring buffer is bounded so a forgotten panel cannot grow the
# heap, and these are bounded so one very long call cannot. A 45-minute
# interview is well inside both.
MAX_ARCHIVED_TURNS = 400
MAX_ARCHIVED_MILESTONES = 600


# The four states `shuo-frontend/components/dashboard/test-call-status.tsx`
# already renders. Named here rather than derived from `Phase` so that the
# mapping is one visible place: the pipeline has two phases, the panel has
# four, and the extra two are the edges of the call rather than states the
# machine knows about.
CONNECTING = "connecting"
LISTENING = "listening"
SPEAKING = "speaking"
ENDED = "ended"


# The three terminal outcomes this module can *derive*, imported from
# `call_status` so there is one vocabulary rather than two that drift.
#
# Three of the seven, and that is right: `pending`, `ringing` and `cancelled`
# describe a call that either has not reached this process yet or never will,
# so nothing here is in a position to observe them. They are written by the
# sites that do know -- `server.py`'s origination and webhook handlers -- and
# folded into the same row by `call_history.merge`.
#
# 🔴 From `call_status`, **not** from `call_history`, and the distinction is
# load-bearing rather than stylistic. `call_history` writes files;
# `test_the_monitor_imports_nothing_that_does_io` exists to stop this module
# acquiring the ability to block on an fsync, and importing a log writer to
# fetch a string constant is exactly how that ability arrives by accident.
# `call_status` imports nothing at all.


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

    `transcript` and `milestones` are the *archive*, and they are held here
    rather than derived from the ring buffer at the end because the ring is
    bounded: a long call evicts its own opening turns before it is over, so by
    teardown there is no longer a whole call in there to save. Both are plain
    `list.append` on the same guarded write site as the ring, so the hot path
    is unchanged.

    `events` is this call's own ring (W5c). Before Phase 8 there was one shared
    across the process, which meant a busy line evicted a quiet one's
    transcript from the other side of the event loop.
    """

    id: str
    persona: str
    direction: str
    started_at: str
    started_monotonic: float

    # The far end and our own number, carried so the call log can answer "who
    # was called" -- which it could not before Phase 8. Known at origination
    # for an outbound call and from the carrier's form for an inbound one;
    # empty here when the socket was opened without either, in which case the
    # fold in `call_history.merge` supplies them from the earlier revision.
    to_number: str = ""
    from_number: str = ""

    call_id: str = ""
    state: str = CONNECTING
    config: str = ""
    partial: str = ""
    ended_reason: str = ""
    # The machine-readable half of `ended_reason`, from `call_status`. Empty
    # while the call is live, and empty on a call ended by a caller that
    # supplied only prose -- `ended_status_code` falls back to the status for
    # both, so a panel branching on it never has to handle "".
    ended_code: str = ""
    turns: int = 0

    ended_at: str = ""
    ended_monotonic: Optional[float] = None

    # This call's ring. `begin` builds it with the monitor's `max_events`; the
    # default is here so a `_Call` constructed directly -- in a test, or by a
    # future caller -- is still bounded rather than unbounded.
    events: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_EVENTS)
    )

    # How much of this call's transcript aged out before anyone read it, and
    # the highest sequence number that went with it. Two ints maintained at the
    # write site so `missed` can be answered without walking anything: see
    # `CallMonitor._missed` for why the global-buffer arithmetic it replaced
    # cannot survive a second concurrent call.
    evicted: int = 0
    evicted_seq: int = 0

    transcript: List[Dict[str, Any]] = field(default_factory=list)
    milestones: List[Dict[str, Any]] = field(default_factory=list)

    def elapsed_ms(self) -> int:
        """
        Wall-clock so far, frozen once the call has ended.

        Without the freeze the duration on a finished call would keep ticking
        for as long as the process stayed up, so a call that ran two minutes
        would be archived as however long ago it happened to be written.
        """
        end = self.ended_monotonic if self.ended_monotonic is not None else time.monotonic()
        return int((end - self.started_monotonic) * 1000)

    @property
    def live(self) -> bool:
        return self.state != ENDED

    # ── The archive ─────────────────────────────────────────────────

    @property
    def status(self) -> str:
        """
        The call's outcome, in the panel's three-way vocabulary.

        Derived rather than tracked, because none of the three is a state the
        pipeline is ever *in* -- they are readings of how it finished:

            failed      the call loop raised, or the carrier's `start` frame
                        never arrived, so there was never a call to have
            missed      it connected and nobody said anything -- neither side
                        produced a single transcript line
            completed   anything else

        "Never identified" lands in `failed` rather than `missed` on purpose:
        a socket that opened and produced no `start` is a transport failure,
        and calling it "missed" would blame the person who did not answer a
        phone that never rang.
        """
        if "failed" in self.ended_reason:
            return FAILED
        if not self.call_id:
            return FAILED
        if not self.transcript:
            return MISSED
        return COMPLETED

    @property
    def lifecycle_status(self) -> str:
        """
        This call in the log's seven-state vocabulary, not the panel's four.

        `in_progress` while the socket is open, and the derived terminal
        outcome once it is not. It exists so that :3041's `/v1/calls/active`
        can union a live summary with a row read off disk *without* the route
        handler translating between two vocabularies -- which is where two
        vocabularies quietly become three.

        The four panel states (`connecting`/`listening`/`speaking`/`ended`)
        travel alongside it as `state`, because they are a different question:
        this says how the attempt is going, that says what the twin is doing
        this second.
        """
        return IN_PROGRESS if self.live else self.status

    @property
    def ended_status_code(self) -> str:
        """
        Why this call ended, as a code a panel can branch on. `""` while live.

        Prefers what the call loop said, because it is the only thing that
        knows the difference between "the caller hung up" and "the loop
        raised". Falls back to the code implied by the derived status, so a
        caller that passed prose and no code still yields something switchable
        rather than an empty string the panel has to special-case.
        """
        if self.live:
            return ""
        if self.ended_code:
            return self.ended_code
        status = self.status
        if status == FAILED:
            return CALL_FAILED
        if status == MISSED:
            return NO_ANSWER
        return CALL_COMPLETED

    def to_summary(self) -> Dict[str, Any]:
        """
        One row of `/calls/active` -- enough to list the call, not to read it.

        Deliberately small and deliberately not the transcript: the panel polls
        this at 1Hz for every call at once, and the events for the *one* call
        an operator has open come from `snapshot` instead. Eight of these is a
        couple of hundred bytes.
        """
        return {
            "id": self.id,
            "callId": self.call_id,
            "direction": self.direction,
            "persona": self.persona,
            "to": self.to_number,
            "from": self.from_number,
            "state": self.state,
            "live": self.live,
            "status": self.lifecycle_status,
            "startedAt": self.started_at,
            "durationMs": self.elapsed_ms(),
            "turns": self.turns,
            "endedReason": self.ended_reason,
            "endedCode": self.ended_status_code,
            "endedAt": self.ended_at,
        }

    def to_record(self) -> Dict[str, Any]:
        """
        This call as one durable row, in the panel's camelCase.

        Same convention as the config document (`config_store/store.py` writes
        `by_alias`): the file is a literal record of what the panel will be
        shown, so "why does the table say that" is answerable with `cat`.
        """
        return {
            "id": self.id,
            "callId": self.call_id,
            "startedAt": self.started_at,
            "endedAt": self.ended_at or _now_iso(),
            "durationSeconds": round(self.elapsed_ms() / 1000),
            "direction": self.direction,
            "persona": self.persona,
            "status": self.status,
            "endedReason": self.ended_reason,
            "endedCode": self.ended_status_code,
            # Empty unless this process was told them. `merge` treats an empty
            # string as "this write had nothing to say", so a blank here
            # cannot overwrite the number the origination stub recorded.
            "to": self.to_number,
            "from": self.from_number,
            # W2's provenance line, archived with the call it applied to. The
            # whole point of it is answering "what was this call running
            # with", and that question outlives the call by rather a lot.
            "config": self.config,
            "turns": self.turns,
            "transcript": list(self.transcript),
            "milestones": list(self.milestones),
        }


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

    @property
    def id(self) -> str:
        """
        Our id for this call -- the attempt id, where there was one.

        The key every durable revision of this call is written under, so the
        call loop can address the history row without reaching into `_Call`.
        """
        return self._call.id if self._call else ""

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

    def ended(self, reason: str = "", code: str = "") -> None:
        """
        The call is over. `reason` is prose; `code` is what a panel branches on.

        `code` is optional so every existing caller -- and every test -- keeps
        working unchanged; `_Call.ended_status_code` derives one from the
        status when it is omitted. Supplying it is still better, because the
        status cannot distinguish a caller who hung up from a loop that raised
        once both have produced a transcript.
        """
        if not self._call:
            return
        self._call.state = ENDED
        self._call.ended_reason = reason
        self._call.ended_code = code
        self._call.partial = ""
        # Stop the duration clock here rather than at archive time. The two
        # are microseconds apart today, but they are separated by the whole of
        # teardown -- closing the TTS pool, draining Deepgram, saving the trace
        # -- and a call log that reports a two-minute call as two minutes and
        # four seconds is quietly wrong in a way nobody would think to check.
        self._call.ended_monotonic = time.monotonic()
        self._call.ended_at = _now_iso()
        self._emit("ended", text=reason)

    def record(self) -> Optional[Dict[str, Any]]:
        """
        This call as a durable row, for `call_history.append`.

        `None` when there is nothing to archive -- a disabled recorder, which
        is what an `Agent` built outside a call loop holds. The caller in
        `conversation.py` checks for it rather than this returning an empty
        dict, because writing a row for a call that never happened is worse
        than writing nothing.

        Pure: it allocates a dict and touches no I/O, so it is safe to call
        from teardown before the disk write it feeds.
        """
        return self._call.to_record() if self._call else None

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
    A bounded ring buffer per call, for the last few calls.

    One instance per process (`MONITOR` below). `begin` opens a call and
    returns the recorder the call loop publishes through; `snapshot` reads one
    call in full and `summaries` reads all of them shallowly -- the two read
    paths the HTTP handlers serve.
    """

    def __init__(self, *, max_events: int = MAX_EVENTS, max_calls: int = MAX_CALLS):
        self._calls: "OrderedDict[str, _Call]" = OrderedDict()
        self._max_events = max_events
        # Held on the instance rather than read from the module constant at the
        # eviction site, which is what it used to do -- `CallMonitor(max_calls=1)`
        # was silently ignored, so the bound could only ever be tested at
        # whatever the default happened to be.
        self._max_calls = max_calls
        self._seq = 0
        self._counter = 0
        self._warned = False

    # ── Write path (hot) ────────────────────────────────────────────

    def begin(
        self,
        *,
        persona: str,
        direction: str,
        attempt: str = "",
        to_number: str = "",
        from_number: str = "",
    ) -> CallRecorder:
        """
        Open a call. Called once, when the media socket is accepted.

        `attempt` is the id minted before the phone rang -- in `trigger_call`
        for an outbound call, in `/answer` for an inbound one -- and adopting
        it as this call's `id` is what makes the live view, the history row and
        (in W5b) the recording file all key on one string. Falling back to
        `call-<n>` keeps a socket opened without one working, which is what a
        direct `/ws` connection in a test does.

        It cannot change mid-call, which is the property the panel's cursor
        depends on: a field that changes value silently resets the cursor and
        replays the transcript.

        Returns a recorder even on failure -- a disabled one -- because the
        call loop is going to publish through it either way and must not
        have to check.
        """
        try:
            self._counter += 1
            call = _Call(
                id=attempt or f"call-{self._counter}",
                persona=persona,
                direction=direction,
                to_number=to_number,
                from_number=from_number,
                started_at=_now_iso(),
                started_monotonic=time.monotonic(),
                events=deque(maxlen=self._max_events),
            )
            self._calls[call.id] = call
            while len(self._calls) > self._max_calls:
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

        The eviction bookkeeping is one `len` compare and, on the rare tick
        where the ring is actually full, one index -- both O(1). It has to
        happen *here* because `deque(maxlen=...)` drops its head silently, and
        a transcript with an unadmitted hole in it is the one failure the panel
        cannot recover from (see `_missed`).
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

            ring = call.events
            # `getattr` rather than `.maxlen`: the never-raises tests swap in a
            # buffer that is only an `append`, and this must not be the reason
            # the write path fails.
            maxlen = getattr(ring, "maxlen", None)
            if maxlen is not None and len(ring) >= maxlen:
                call.evicted += 1
                call.evicted_seq = ring[0]["seq"]

            ring.append(event)
            self._archive(call, kind, event)
        except Exception as exc:  # pragma: no cover - defensive
            self._warn(exc)

    @staticmethod
    def _archive(call: _Call, kind: str, event: Dict[str, Any]) -> None:
        """
        Keep the parts of this event that outlive the ring buffer.

        Inside `_append`'s `try` deliberately: this is the same write, on the
        same hot path, and it must fail the same way -- once, silently, with
        the call unaffected. Two appends and a comparison; no allocation
        beyond the dict, and nothing that can block.

        Only two kinds are kept. `config`, `connected` and `ended` are already
        fields on the call, and `note`/`partial` are live-view affordances
        with nothing to say a day later.
        """
        if kind in ("caller", "agent"):
            if len(call.transcript) < MAX_ARCHIVED_TURNS:
                call.transcript.append(
                    {
                        "speaker": "caller" if kind == "caller" else "agent",
                        "text": event.get("text", ""),
                        "atMs": event["atMs"],
                        # Present only where it means something. A caller line
                        # is never "interrupted" in this sense -- barge-in is
                        # something the caller does, not something done to
                        # them -- and a false there would read as a claim.
                        **(
                            {"interrupted": True}
                            if kind == "agent" and event.get("interrupted")
                            else {}
                        ),
                    }
                )
        elif kind == "timing":
            if len(call.milestones) < MAX_ARCHIVED_MILESTONES:
                call.milestones.append(
                    {
                        "name": event.get("name", ""),
                        "ms": event.get("ms", 0),
                        "turn": event.get("turn", 0),
                    }
                )

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

    def find(self, call_ref: str) -> Optional[_Call]:
        """
        One call by our id or the carrier's. `None` if this process never saw it.

        The public form of `_select`, for the handlers that act on a *named*
        call rather than on whichever one is current -- `/calls/current/hangup`
        with an `expect`, now that eight concurrent calls make "the current
        one" an ambiguous thing to hang up.
        """
        return self._select(call_ref) if call_ref else None

    def summaries(self) -> List[Dict[str, Any]]:
        """
        Every call in the buffer, newest first. The read path for `/calls/active`.

        Newest first because that is the order the panel lists them in, and
        because a live call is almost always the newest -- an operator opening
        the panel mid-call should not have to scroll past yesterday's.

        A comprehension over at most `MAX_CALLS` dataclasses, off the hot path,
        inside the HTTP handler. Ended calls are included: a panel polling
        across a hangup has to be able to see the call that just finished, and
        `live` and `status` say which is which.
        """
        return [call.to_summary() for call in reversed(self._calls.values())]

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

        Reads *this call's* ring (W5c), so the work is bounded by one call's
        events rather than by every call in the process -- and so a busy line
        can no longer make a quiet one's transcript disappear.
        """
        call = self._select(call_ref)

        if call is None:
            # This process has never seen the call -- which is the *ordinary*
            # state for one that is still ringing, or that was declined, since
            # neither ever opens a media socket. The lifecycle fields are
            # present and empty rather than absent, so a caller can tell "no
            # opinion" from "not answered": :3041 fills them in from the call
            # log, which is the only place that call exists.
            return {
                "id": None,
                "callId": "",
                "live": False,
                "state": None,
                "status": "",
                "endedReason": "",
                "endedCode": "",
                "endedAt": "",
                "events": [],
                "nextSeq": self._seq,
                "missed": 0,
            }

        # No `event["id"] == call.id` filter any more: the ring belongs to the
        # call, so there is nothing in it that could have come from another one.
        events = [event for event in call.events if event["seq"] > since]

        return {
            "id": call.id,
            "callId": call.call_id,
            "live": call.live,
            "state": call.state,
            # The seven-state lifecycle value, so a panel holding a snapshot and
            # a row from `/v1/calls/active` is not comparing two vocabularies.
            "status": call.lifecycle_status,
            "persona": call.persona,
            "direction": call.direction,
            "startedAt": call.started_at,
            "durationMs": call.elapsed_ms(),
            "config": call.config,
            "partial": call.partial,
            "turns": call.turns,
            "endedReason": call.ended_reason,
            # The two fields a panel needs to leave its ringing screen. `live`
            # above says the socket is gone; these say why, and `endedAt` gives
            # the transition a time rather than only a poll to have noticed it.
            "endedCode": call.ended_status_code,
            "endedAt": call.ended_at,
            "events": events,
            "nextSeq": self._seq,
            "missed": self._missed(call, since),
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

    @staticmethod
    def _missed(call: _Call, since: int) -> int:
        """
        How many of *this call's* events aged out before the panel read them.

        A transcript with a silent hole in it is worse than one that admits the
        hole -- the whole point of the panel is judging whether the twin
        contradicted itself three turns ago.

        Per call, and it had to become per call in W5c. The old form was
        `oldest_seq_in_the_buffer - since - 1`, which is only a count of lost
        events while the sequence numbers in the buffer are *contiguous*. With
        one ring per call they are not: call A's ring skips every seq that
        belonged to call B, so that arithmetic would have reported the whole of
        B's traffic as holes in A's transcript. Every concurrent call would
        have accused the others of losing its events.

        Answered from two ints maintained at the write site instead. It can
        still over-report -- it knows how many events this call evicted and the
        highest sequence among them, not how many of those were already below
        a particular cursor -- so a panel whose cursor is behind the eviction
        point is told the total. That is the same safe direction the previous
        form erred in, and a steady 1Hz poller sits well ahead of it and reads
        zero.
        """
        if since <= 0 or not call.evicted:
            return 0
        if since >= call.evicted_seq:
            # Everything this call dropped was already below the cursor: the
            # panel had read it before it aged out.
            return 0
        return call.evicted

    # ── Testing seam ────────────────────────────────────────────────

    def reset(self) -> None:
        """Drop everything. For tests, and for nothing else."""
        self._calls.clear()
        self._seq = 0
        self._counter = 0
        self._warned = False


# One per process. The call loop publishes into it; `GET /calls/live` and
# `GET /calls/active` read it. Deliberately module-level rather than threaded
# through `CallContext`: the HTTP handler has no path to the call loop's
# locals.
MONITOR = CallMonitor()
