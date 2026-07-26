"""
Tests for the live-call monitor -- W3's read side.

Three things are being defended here, in descending order of how badly they
would hurt if they broke:

1. **The monitor cannot cost a call.** It never raises, it never blocks, and
   publishing stays cheap enough to sit inside a loop that pages a 160-byte
   frame every 20ms. `TestItCannotCostACall` and `TestHotPathCost`.
2. **The state machine does not know it exists.** No new events, no new
   actions, no import. `TestPurityIsUnaffected`.
3. **The cursor is honest.** A panel that polls must not replay the
   transcript, must not silently skip it, and must say so when the ring
   buffer dropped something. `TestTheCursor`.
"""

import asyncio
import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from shuo.call_monitor import (
    CONNECTING,
    ENDED,
    LISTENING,
    SPEAKING,
    CallMonitor,
    CallRecorder,
)


@pytest.fixture
def call_env(monkeypatch):
    """
    Enough environment to run `run_conversation` without touching a vendor.

    A local copy of test_integration's `app_env` rather than a shared
    fixture: this file drives the loop directly and never builds the HTTP
    app, so importing that one would pull in more than it needs.
    """
    from tests.test_integration import StubFlux, StubTTSPool

    monkeypatch.setenv("CARRIER", "vobiz")
    monkeypatch.setenv("VOBIZ_AUTH_ID", "MA_TEST")
    monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("VOBIZ_PHONE_NUMBER", "+911234567890")
    monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")
    monkeypatch.setenv("PERSONA", "candidate")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
    monkeypatch.setenv("RECORD_CALLS", "false")

    import shuo.conversation as conv
    from shuo.carrier import reset_carrier_cache

    reset_carrier_cache()
    StubFlux.instances.clear()
    StubTTSPool.instances.clear()
    monkeypatch.setattr(conv, "FluxService", StubFlux)
    monkeypatch.setattr(conv, "TTSPool", StubTTSPool)

    yield
    reset_carrier_cache()


@pytest.fixture
def monitor():
    return CallMonitor()


@pytest.fixture
def recorder(monitor):
    return monitor.begin(persona="candidate", direction="outbound")


def kinds(snapshot):
    return [event["kind"] for event in snapshot["events"]]


def texts(snapshot, kind):
    return [e.get("text") for e in snapshot["events"] if e["kind"] == kind]


# =============================================================================
# THE BASICS
# =============================================================================

class TestACall:
    def test_a_new_call_is_connecting_and_live(self, monitor, recorder):
        snapshot = monitor.snapshot()

        assert snapshot["state"] == CONNECTING
        assert snapshot["live"] is True
        assert snapshot["persona"] == "candidate"
        assert snapshot["direction"] == "outbound"
        assert snapshot["callId"] == ""

    def test_the_carrier_call_id_arrives_later(self, monitor, recorder):
        """
        Empty until the `start` frame lands, and that is not a defect -- it
        is why hangup keys on the monitor rather than on what `originate`
        returned.
        """
        assert monitor.snapshot()["callId"] == ""

        recorder.identify("MZ8a3b1f")

        assert monitor.snapshot()["callId"] == "MZ8a3b1f"
        assert "connected" in kinds(monitor.snapshot())

    def test_identifying_twice_does_not_emit_twice(self, monitor, recorder):
        """A reconnect replays `start` on the same call (decision 19)."""
        recorder.identify("MZ8a3b1f")
        recorder.identify("MZ8a3b1f")

        assert kinds(monitor.snapshot()).count("connected") == 1

    def test_the_config_provenance_line_is_published(self, monitor, recorder):
        recorder.configured("prompt=operator (812 chars)  voice=George [JBF…]")

        snapshot = monitor.snapshot()
        assert snapshot["config"].startswith("prompt=operator")
        assert "config" in kinds(snapshot)

    def test_phase_maps_onto_the_panel_states(self, monitor, recorder):
        recorder.phase(LISTENING)
        assert monitor.snapshot()["state"] == LISTENING

        recorder.phase(SPEAKING)
        assert monitor.snapshot()["state"] == SPEAKING

    def test_a_repeated_phase_is_not_an_event(self, monitor, recorder):
        recorder.phase(LISTENING)
        recorder.phase(LISTENING)
        recorder.phase(LISTENING)

        assert monitor.snapshot()["state"] == LISTENING

    def test_ending_the_call_stops_it_being_live(self, monitor, recorder):
        recorder.phase(SPEAKING)
        recorder.ended("the call ended")

        snapshot = monitor.snapshot()
        assert snapshot["state"] == ENDED
        assert snapshot["live"] is False
        assert snapshot["endedReason"] == "the call ended"

    def test_a_late_phase_change_cannot_resurrect_a_finished_call(
        self, monitor, recorder
    ):
        """
        Teardown is not instantaneous and the loop keeps draining events
        through it. A stray transition must not put the panel back on air.
        """
        recorder.ended("the call ended")
        recorder.phase(LISTENING)

        assert monitor.snapshot()["state"] == ENDED


# =============================================================================
# TRANSCRIPT
# =============================================================================

class TestTheTranscript:
    def test_both_sides_of_the_conversation_appear(self, monitor, recorder):
        recorder.caller_said("So, tell me about yourself.")
        recorder.agent_said("Sure — I'm a backend engineer, ji.", turn=1)

        snapshot = monitor.snapshot()
        assert texts(snapshot, "caller") == ["So, tell me about yourself."]
        assert texts(snapshot, "agent") == ["Sure — I'm a backend engineer, ji."]

    def test_interim_text_replaces_and_never_appends(self, monitor, recorder):
        """
        🔴 The reason interim text is a field and not an event.

        Flux emits `Update` many times a second. Appending each one would
        flush a real transcript out of the ring buffer within seconds -- the
        panel would show live typing and no history, which is the opposite of
        what it is for (judging whether the twin contradicted itself three
        turns ago).
        """
        for partial in ("So", "So tell", "So tell me", "So tell me about"):
            recorder.caller_partial(partial)

        snapshot = monitor.snapshot()
        assert snapshot["partial"] == "So tell me about"
        assert snapshot["events"] == []

    def test_a_settled_transcript_clears_the_partial(self, monitor, recorder):
        recorder.caller_partial("So tell me abou")
        recorder.caller_said("So, tell me about yourself.")

        snapshot = monitor.snapshot()
        assert snapshot["partial"] == ""
        assert texts(snapshot, "caller") == ["So, tell me about yourself."]

    def test_the_partial_is_cleared_when_the_call_ends(self, monitor, recorder):
        recorder.caller_partial("half a sentence")
        recorder.ended("the call ended")

        assert monitor.snapshot()["partial"] == ""

    def test_an_interrupted_answer_is_flagged_not_hidden(self, monitor, recorder):
        """
        A caller line with no reply under it reads as the twin having failed
        to answer. Barge-in is a *success* of the pipeline, so it has to be
        distinguishable from a failure.
        """
        recorder.agent_said("Well, I would say that my great—", turn=2,
                            interrupted=True)

        event = monitor.snapshot()["events"][-1]
        assert event["kind"] == "agent"
        assert event["interrupted"] is True

    def test_empty_text_is_not_published(self, monitor, recorder):
        recorder.caller_said("   ")
        recorder.agent_said("")
        recorder.note("")

        assert monitor.snapshot()["events"] == []

    def test_runaway_text_is_clipped(self, monitor, recorder):
        recorder.agent_said("x" * 50_000)

        assert len(monitor.snapshot()["events"][-1]["text"]) < 5_000


class TestInstrumentation:
    def test_latency_milestones_reach_the_panel(self, monitor, recorder):
        recorder.timing("llm_first_token", 312, turn=1)
        recorder.timing("tts_first_audio", 498, turn=1)
        recorder.timing("playback_dispatched", 8_210, turn=1)

        marks = {
            e["name"]: e["ms"]
            for e in monitor.snapshot()["events"]
            if e["kind"] == "timing"
        }
        assert marks == {
            "llm_first_token": 312,
            "tts_first_audio": 498,
            "playback_dispatched": 8_210,
        }

    def test_a_silent_turn_says_so(self, monitor, recorder):
        """
        Decision 29's failure is not audible -- it is silence. Without a line
        in the panel, a vendor entitlement problem is indistinguishable from
        the twin having nothing to say.
        """
        recorder.note("TTS produced no audio for this turn — the caller heard silence.")

        assert texts(monitor.snapshot(), "note")


# =============================================================================
# THE CURSOR
# =============================================================================

class TestTheCursor:
    def test_a_steady_state_poll_carries_nothing(self, monitor, recorder):
        recorder.caller_said("Hello?")

        first = monitor.snapshot()
        assert first["events"]

        second = monitor.snapshot(since=first["nextSeq"])
        assert second["events"] == []
        assert second["nextSeq"] == first["nextSeq"]

    def test_only_events_after_the_cursor_come_back(self, monitor, recorder):
        recorder.caller_said("one")
        cursor = monitor.snapshot()["nextSeq"]
        recorder.caller_said("two")

        assert texts(monitor.snapshot(since=cursor), "caller") == ["two"]

    def test_the_sequence_is_global_so_a_second_call_does_not_replay(self, monitor):
        """
        A per-call sequence would reset to zero on call two, and a panel
        holding cursor 40 would either replay everything or skip it all.
        """
        first = monitor.begin(persona="candidate", direction="outbound")
        first.caller_said("call one")
        first.ended("done")

        cursor = monitor.snapshot()["nextSeq"]

        second = monitor.begin(persona="candidate", direction="inbound")
        second.caller_said("call two")

        snapshot = monitor.snapshot(since=cursor)
        assert texts(snapshot, "caller") == ["call two"]

    def test_dropped_events_are_reported_not_hidden(self):
        monitor = CallMonitor(max_events=5)
        recorder = monitor.begin(persona="candidate", direction="outbound")

        recorder.caller_said("first")
        cursor = monitor.snapshot()["nextSeq"]
        for i in range(20):
            recorder.caller_said(f"line {i}")

        snapshot = monitor.snapshot(since=cursor)
        assert snapshot["missed"] > 0, "a hole in the transcript must be admitted"

    def test_nothing_is_missed_when_nothing_was_dropped(self, monitor, recorder):
        recorder.caller_said("one")
        cursor = monitor.snapshot()["nextSeq"]
        recorder.caller_said("two")

        assert monitor.snapshot(since=cursor)["missed"] == 0


class TestSelectingACall:
    def test_the_latest_call_is_the_default(self, monitor):
        monitor.begin(persona="candidate", direction="outbound")
        second = monitor.begin(persona="receptionist", direction="inbound")
        second.identify("second")

        assert monitor.snapshot()["persona"] == "receptionist"

    def test_a_call_can_be_selected_by_the_carriers_id(self, monitor):
        first = monitor.begin(persona="candidate", direction="outbound")
        first.identify("MZ-one")
        first.caller_said("from call one")
        monitor.begin(persona="receptionist", direction="inbound")

        snapshot = monitor.snapshot(call_ref="MZ-one")
        assert snapshot["persona"] == "candidate"
        assert texts(snapshot, "caller") == ["from call one"]

    def test_events_from_another_call_do_not_leak_in(self, monitor):
        first = monitor.begin(persona="candidate", direction="outbound")
        first.caller_said("call one line")
        second = monitor.begin(persona="candidate", direction="inbound")
        second.caller_said("call two line")

        assert texts(monitor.snapshot(), "caller") == ["call two line"]

    def test_old_calls_are_evicted(self, monitor):
        for _ in range(10):
            monitor.begin(persona="candidate", direction="outbound")

        # Bounded, so a long-running server cannot accumulate calls.
        assert len(monitor._calls) <= 3

    def test_an_empty_monitor_answers_rather_than_failing(self, monitor):
        snapshot = monitor.snapshot()

        assert snapshot["live"] is False
        assert snapshot["state"] is None
        assert snapshot["events"] == []

    def test_current_is_the_live_call_only(self, monitor):
        recorder = monitor.begin(persona="candidate", direction="outbound")
        assert monitor.current() is not None

        recorder.ended("done")
        assert monitor.current() is None


# =============================================================================
# IT CANNOT COST A CALL
# =============================================================================

class TestItCannotCostACall:
    def test_a_disabled_recorder_no_ops(self):
        """
        What `Agent` falls back to when it is built outside a call loop. Every
        publish site is unconditional, so this has to absorb all of them.
        """
        recorder = CallRecorder.disabled()

        recorder.identify("x")
        recorder.configured("x")
        recorder.phase(SPEAKING)
        recorder.caller_partial("x")
        recorder.caller_said("x")
        recorder.agent_said("x", turn=1)
        recorder.timing("t", 1, turn=1)
        recorder.turn_started(1)
        recorder.note("x")
        recorder.ended("x")

        assert recorder.call_id == ""

    def test_a_broken_buffer_does_not_raise_into_the_call_loop(
        self, monitor, recorder, caplog
    ):
        """
        The contract is "never raises", so it is worth proving rather than
        asserting in a docstring. A monitor that can throw is a monitor that
        can hang up on a caller.
        """
        class Exploding:
            def append(self, _):
                raise RuntimeError("buffer is gone")

            def __iter__(self):
                return iter(())

            def __len__(self):
                return 0

            def __bool__(self):
                return False

        monitor._events = Exploding()

        recorder.caller_said("this must not raise")
        recorder.agent_said("nor this", turn=1)

        # And the read path still answers.
        assert monitor.snapshot()["events"] == []

    def test_it_warns_once_and_not_on_every_turn(self, monitor, recorder, caplog):
        class Exploding:
            def append(self, _):
                raise RuntimeError("buffer is gone")

            def __iter__(self):
                return iter(())

            def __len__(self):
                return 0

            def __bool__(self):
                return False

        monitor._events = Exploding()

        with caplog.at_level("WARNING"):
            for i in range(50):
                recorder.caller_said(f"line {i}")

        warnings = [r for r in caplog.records if "monitor" in r.getMessage()]
        assert len(warnings) == 1

    def test_no_publish_method_is_a_coroutine(self):
        """
        Every one of these is called from the call loop, and several from
        synchronous callbacks that cannot await. An `async def` here would be
        a coroutine nobody awaits -- silently doing nothing, with a warning
        buried in the log.
        """
        recorder = CallRecorder.disabled()
        for name in (
            "identify", "configured", "phase", "caller_partial", "caller_said",
            "agent_said", "timing", "turn_started", "note", "ended",
        ):
            method = getattr(recorder, name)
            assert not asyncio.iscoroutinefunction(method), f"{name} must be sync"


class TestHotPathCost:
    def test_publishing_is_cheap_enough_for_the_call_loop(self, monitor, recorder):
        """
        The budget this has to fit inside: the player emits a frame every
        20ms and decision 24 removed its ability to claw back lateness, so a
        stall is permanent stream delay rather than one late frame.

        The ceiling is deliberately loose -- this is a smoke alarm for
        somebody adding disk I/O or JSON encoding to `_append`, not a
        microbenchmark. 10,000 publishes is ~2,000 turns' worth.
        """
        start = time.perf_counter()
        for i in range(10_000):
            recorder.caller_said(f"line {i}")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 250, (
            f"10,000 publishes took {elapsed_ms:.0f}ms — something in the "
            f"monitor's write path is no longer a deque append"
        )

    def test_interim_text_is_cheaper_still(self, monitor, recorder):
        """It runs several times a second for the whole call."""
        start = time.perf_counter()
        for i in range(10_000):
            recorder.caller_partial(f"partial {i}")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 150

    def test_the_buffer_cannot_grow_without_bound(self, monitor, recorder):
        for i in range(5_000):
            recorder.caller_said(f"line {i}")

        assert len(monitor._events) <= 200


# =============================================================================
# THE STATE MACHINE NEVER LEARNS IT EXISTS
# =============================================================================

class TestPurityIsUnaffected:
    def test_the_state_machine_does_not_import_the_monitor(self):
        """
        CLAUDE.md rule 1. Observability enters at the dispatch boundary, the
        same way `Tracer` and `_TurnCompletion` do -- never as a side effect
        inside `process_event`.
        """
        import ast
        import inspect

        import shuo.state as state_module

        tree = ast.parse(inspect.getsource(state_module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert not any("call_monitor" in name for name in imported)

    def test_no_new_events_or_actions_were_added(self):
        """
        The panel is fed from observation, not from traffic through the
        machine. If interim text had become an Event, this is where it would
        show up -- and acting on interim text is how you answer half a
        question.
        """
        import shuo.types as types

        assert not hasattr(types, "InterimTranscriptEvent")
        assert not hasattr(types, "MonitorAction")

    def test_the_monitor_imports_nothing_that_does_io(self):
        import ast
        import inspect

        import shuo.call_monitor as monitor_module

        tree = ast.parse(inspect.getsource(monitor_module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert imported <= {
            "__future__", "time", "collections", "dataclasses", "datetime",
            "typing", ".log", "log",
        }, f"unexpected import in call_monitor: {imported}"


# =============================================================================
# END TO END THROUGH THE REAL LOOP
# =============================================================================

class TestItSeesARealCall:
    """
    Driven through `run_conversation` rather than by calling the recorder,
    because the thing that breaks is the *wiring*, not the buffer. A monitor
    with perfect unit tests and one unwired publish site shows the operator a
    blank panel during a live call.
    """

    @pytest.mark.asyncio
    async def test_a_call_reaches_the_panel(self, call_env, monkeypatch):
        import shuo.conversation as conv
        from shuo.call_monitor import MONITOR
        from shuo.carrier import get_carrier
        from shuo.types import CallContext, CallDirection

        from tests.test_integration import (
            MULAW_SILENCE_FRAME,
            StubFlux,
            VobizProtocol,
        )

        class DrainingWebSocket:
            """
            Like test_integration's `ClosingWebSocket`, but it yields to the
            event loop between frames and settles before disconnecting.

            That is not cosmetic. The plain version returns every frame
            without ever awaiting, so the reader task drains the whole script
            -- disconnect included -- before the loop processes frame one.
            The `StreamStop` then lands *ahead* of the turn that the last
            media frame set in motion, and the loop exits before taking it.
            A real carrier delivers frames 20ms apart.
            """

            def __init__(self, frames):
                self._frames = list(frames)
                self.sent = []

            async def receive_text(self):
                if self._frames:
                    await asyncio.sleep(0)
                    return json.dumps(self._frames.pop(0))
                await asyncio.sleep(0.1)
                raise WebSocketDisconnect(code=1006)

            async def send_text(self, text):
                self.sent.append(json.loads(text))

        class SpeakingFlux(StubFlux):
            """Types a partial, then settles it into a turn."""

            async def send(self, audio_bytes):
                await super().send(audio_bytes)
                if len(self.fed) == 1:
                    await self.on_interim("so tell me")
                    await self.on_end_of_turn("So, tell me about yourself.")

        class QuietAgent:
            def __init__(self, **kwargs):
                self.recorder = kwargs.get("recorder")

            async def start_turn(self, transcript):
                self.recorder.agent_said("Sure, ji.", turn=1)
                self.recorder.timing("llm_first_token", 300, turn=1)

            async def cancel_turn(self):
                pass

            async def cleanup(self):
                pass

        MONITOR.reset()
        monkeypatch.setattr(conv, "FluxService", SpeakingFlux)
        monkeypatch.setattr(conv, "Agent", QuietAgent)

        ws = DrainingWebSocket([
            VobizProtocol.start("s1", "MZ-live"),
            VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1),
        ])
        ctx = CallContext(call_id="", direction=CallDirection.OUTBOUND,
                          persona_id="candidate", carrier="vobiz")

        await asyncio.wait_for(
            conv.run_conversation(ws, ctx, get_carrier("vobiz")), timeout=5.0
        )

        snapshot = MONITOR.snapshot()

        assert snapshot["callId"] == "MZ-live", "the carrier's call id never landed"
        assert snapshot["persona"] == "candidate"
        assert snapshot["config"], "the W2 provenance line never reached the panel"
        assert texts(snapshot, "caller") == ["So, tell me about yourself."]
        assert texts(snapshot, "agent") == ["Sure, ji."]
        assert "timing" in kinds(snapshot)
        assert snapshot["state"] == ENDED
        assert snapshot["live"] is False

        # The interim hook is wired. Its text is gone by now -- the settled
        # transcript cleared it -- so what is checked is the wiring, which is
        # the part that silently does not exist if the callback is dropped.
        assert StubFlux.instances[-1].on_interim is not None, (
            "Flux's interim callback is not wired, so the panel will never "
            "show the caller typing"
        )

    @pytest.mark.asyncio
    async def test_the_token_loop_actually_accumulates(self):
        """
        Drives the real `_on_llm_token`, because the two tests below set
        `_response` themselves and would both pass with the accumulation
        deleted -- the panel would then show a caller line and no reply, on
        every turn, with nothing failing anywhere.
        """
        from shuo.agent import Agent
        from shuo.call_monitor import CallMonitor

        class StubTTS:
            def __init__(self):
                self.sent = []

            async def send(self, token):
                self.sent.append(token)

        monitor = CallMonitor()
        recorder = monitor.begin(persona="candidate", direction="outbound")

        agent = Agent.__new__(Agent)
        agent._recorder = recorder
        agent._tracer = None
        agent._response = []
        agent._turn = 1
        agent._active = True
        agent._got_first_token = True  # skip the tracer marks
        agent._t0 = time.monotonic()
        agent._tts = StubTTS()

        for token in ("Haan", " ji", ", ", "I did."):
            await agent._on_llm_token(token)

        agent._publish_response(interrupted=False)

        assert texts(monitor.snapshot(), "agent") == ["Haan ji, I did."]
        # And the streaming chain is untouched: every token still went to TTS
        # on its way past (CLAUDE.md rule 2).
        assert agent._tts.sent == ["Haan", " ji", ", ", "I did."]

    @pytest.mark.asyncio
    async def test_the_agents_own_text_is_joined_from_its_tokens(self):
        """
        The token loop only appends to a list; the join happens once, when
        the turn ends. Proven against the real `Agent` because that is where
        the accumulation lives.
        """
        from shuo.agent import Agent
        from shuo.call_monitor import CallMonitor
        from shuo.tracer import Tracer

        monitor = CallMonitor()
        recorder = monitor.begin(persona="candidate", direction="outbound")

        agent = Agent.__new__(Agent)  # no vendor clients
        agent._recorder = recorder
        agent._response = ["Ha", "an", " ji", ", I ", "did."]
        agent._turn = 4

        agent._publish_response(interrupted=False)

        assert texts(monitor.snapshot(), "agent") == ["Haan ji, I did."]

    @pytest.mark.asyncio
    async def test_an_answer_is_published_once_even_if_both_paths_run(self):
        """
        `cancel_turn` can follow `_end_turn`. The buffer is cleared on
        publish so the panel cannot show the same answer twice.
        """
        from shuo.agent import Agent
        from shuo.call_monitor import CallMonitor

        monitor = CallMonitor()
        recorder = monitor.begin(persona="candidate", direction="outbound")

        agent = Agent.__new__(Agent)
        agent._recorder = recorder
        agent._response = ["once"]
        agent._turn = 1

        agent._publish_response(interrupted=False)
        agent._publish_response(interrupted=True)

        assert texts(monitor.snapshot(), "agent") == ["once"]
