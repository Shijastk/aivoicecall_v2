"""
Turn completion -- the second half of Bug B.

`on_done` used to fire when the player *dispatched* its last frame. The
caller had not heard it yet: the carrier still held the pre-roll and the
handset its de-jitter buffer. The carrier's `playedStream` is the signal
that they have, so it is the one that ends a turn now.

The whole difficulty is rules.md V18: that ack is conditional and **may
never arrive**. So every test here is really one of two questions --
does the ack end the turn, and does the turn still end without it.

Two layers:

  * `_TurnCompletion` directly, for the emit-exactly-once invariant.
  * `run_conversation` end to end, because the gate is only correct if
    the five call sites that arm and void it are wired right. Those tests
    set the grace window far higher than the test's own patience, so a
    deleted `ack()` cannot be rescued by the fallback timer -- if the
    wiring breaks, they fail rather than pass slowly.
"""

import json
import time
import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from fake_vobiz import VobizProtocol

from shuo.conversation import _TurnCompletion
from shuo.types import AgentTurnDoneEvent


AUTH_ID = "MA-test-auth-id"
AUTH_TOKEN = "test-auth-token"
PUBLIC_URL = "https://shuo.test"


def drain(queue: asyncio.Queue) -> list:
    """Everything currently on the queue, without blocking."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def until(predicate, timeout=2.0, interval=0.01) -> bool:
    """Poll until true or the timeout expires, yielding to the loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# =============================================================================
# THE GATE ITSELF
# =============================================================================

class TestTurnCompletion:
    @pytest.mark.asyncio
    async def test_ack_ends_the_turn(self):
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=5.0)

        c.arm("turn-1")
        assert q.empty(), "the turn ended before the carrier acknowledged it"

        assert c.ack("turn-1") is True
        assert drain(q) == [AgentTurnDoneEvent()]
        assert c.pending is None

        await c.close()

    @pytest.mark.asyncio
    async def test_ack_for_another_turn_is_ignored(self):
        """
        A late ack for an abandoned turn must never end the turn now
        running -- that would cut a live answer off mid-sentence.
        """
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=5.0)

        c.arm("turn-2")
        assert c.ack("turn-1") is False
        assert q.empty()
        assert c.pending == "turn-2"

        await c.close()

    @pytest.mark.asyncio
    async def test_turn_ends_without_an_ack(self):
        """rules.md V18: `playedStream` may never come. Nothing may block."""
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=0.05)

        c.arm("turn-1")
        assert await until(lambda: not q.empty())
        assert drain(q) == [AgentTurnDoneEvent()]
        assert c.pending is None

        await c.close()

    @pytest.mark.asyncio
    async def test_a_late_ack_does_not_end_the_turn_twice(self):
        """
        The grace window already ended it. A second AgentTurnDoneEvent
        would land during the *next* turn and end that one early.
        """
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=0.05)

        c.arm("turn-1")
        assert await until(lambda: not q.empty())
        drain(q)

        assert c.ack("turn-1") is False
        assert q.empty()

        await c.close()

    @pytest.mark.asyncio
    async def test_void_drops_the_wait_without_ending_a_turn(self):
        """
        Barge-in, reconnect, hangup. The state machine has already left
        RESPONDING, so emitting here would be a no-op at best and would
        truncate the next turn at worst.
        """
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=0.05)

        c.arm("turn-1")
        c.void("barge-in")
        assert c.pending is None

        await asyncio.sleep(0.12)  # past the grace window
        assert q.empty(), "a voided checkpoint still ended a turn"

        await c.close()

    @pytest.mark.asyncio
    async def test_rearming_cancels_the_previous_wait(self):
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=0.05)

        c.arm("turn-1")
        c.arm("turn-2")

        assert await until(lambda: not q.empty())
        await asyncio.sleep(0.1)
        assert drain(q) == [AgentTurnDoneEvent()], "the superseded timer also fired"

        await c.close()

    @pytest.mark.asyncio
    async def test_zero_grace_restores_the_dispatch_time_guess(self):
        """SHUO_CHECKPOINT_GRACE_MS=0 is the escape hatch back to Phase 1."""
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=0.0)

        c.arm("turn-1")
        assert drain(q) == [AgentTurnDoneEvent()]

        await c.close()

    @pytest.mark.asyncio
    async def test_unnamed_checkpoint_ends_immediately(self):
        """A player built without a checkpoint has no ack to wait for."""
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=5.0)

        c.arm(None)
        assert drain(q) == [AgentTurnDoneEvent()]

        await c.close()

    @pytest.mark.asyncio
    async def test_close_leaves_nothing_running(self):
        q: asyncio.Queue = asyncio.Queue()
        c = _TurnCompletion(q, grace_seconds=0.05)

        c.arm("turn-1")
        timer = c._timer
        assert timer is not None

        await c.close()
        # Not just "emits nothing" -- the task itself must be gone, or it
        # outlives run_conversation and asyncio complains at teardown.
        assert timer.done(), "the grace timer outlived the call"

        await asyncio.sleep(0.12)
        assert q.empty(), "a closed gate still ended a turn"

    @pytest.mark.asyncio
    async def test_timeout_is_reported_for_the_trace(self):
        """
        Open question 8 turns on whether Vobiz sends `playedStream` at
        all. The trace has to be able to answer that on its own.
        """
        q: asyncio.Queue = asyncio.Queue()
        seen = []
        c = _TurnCompletion(q, grace_seconds=0.05, on_timeout=seen.append)

        c.arm("turn-3")
        assert await until(lambda: bool(seen))
        assert seen == ["turn-3"]

        await c.close()


# =============================================================================
# THE LOOP WIRING
# =============================================================================

class StubFlux:
    """Stands in for Deepgram Flux, and lets the test drive turn events."""

    instances = []

    def __init__(self, on_end_of_turn=None, on_start_of_turn=None, on_interim=None):
        self.on_end_of_turn = on_end_of_turn
        self.on_start_of_turn = on_start_of_turn
        # W3: interim text goes to the operator's panel and nowhere near the
        # state machine. Accepted here so the stub keeps matching the real
        # constructor.
        self.on_interim = on_interim
        self.stopped = False
        StubFlux.instances.append(self)

    async def start(self):
        pass

    async def send(self, audio_bytes):
        pass

    async def stop(self):
        self.stopped = True


class StubTTSPool:
    def __init__(self, *a, **kw):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass


class StubAgent:
    """
    Stands in for the LLM -> TTS -> Player pipeline.

    Playback completion is a **separate, test-driven step**, not something
    `start_turn` does on the way out. That gap is the entire subject of
    these tests: a real turn runs for tens of seconds before its last
    frame is dispatched, so turn N+1 can be well underway while turn N's
    checkpoint is still unacknowledged. A stub that armed the gate inside
    `start_turn` would close that window and hide every stale-ack bug --
    it did, until mutation testing said so.

    Checkpoint names follow the player's `turn-<n>` convention.
    """

    instances = []

    def __init__(
        self,
        session,
        on_done,
        tts_pool,
        tracer,
        persona_id="default",
        settings=None,
        recorder=None,
    ):
        self._on_done = on_done
        self.settings = settings
        self.started = []
        self.cancelled = 0
        self.playing = None
        StubAgent.instances.append(self)

    async def start_turn(self, transcript):
        self.started.append(transcript)
        self.playing = f"turn-{len(self.started)}"

    def finish_playback(self):
        """The player dispatched its last frame -- the gate arms here."""
        checkpoint, self.playing = self.playing, None
        self._on_done(checkpoint)

    async def cancel_turn(self):
        self.cancelled += 1
        self.playing = None

    async def cleanup(self):
        pass


class DrivenWebSocket:
    """A carrier socket the test feeds one frame at a time, then hangs up."""

    def __init__(self):
        self._frames: asyncio.Queue = asyncio.Queue()
        self.sent = []

    def push(self, frame):
        self._frames.put_nowait(frame)

    def hangup(self):
        self._frames.put_nowait(None)

    async def receive_text(self):
        frame = await self._frames.get()
        if frame is None:
            raise WebSocketDisconnect(code=1000)
        return json.dumps(frame)

    async def send_text(self, text):
        self.sent.append(json.loads(text))


@pytest.fixture
def loop_env(monkeypatch):
    monkeypatch.setenv("CARRIER", "vobiz")
    monkeypatch.setenv("VOBIZ_AUTH_ID", AUTH_ID)
    monkeypatch.setenv("VOBIZ_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("VOBIZ_PHONE_NUMBER", "+911234567890")
    monkeypatch.setenv("PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("RECORD_CALLS", "false")

    import shuo.conversation as conv
    from shuo.carrier import reset_carrier_cache

    reset_carrier_cache()
    StubFlux.instances.clear()
    StubAgent.instances.clear()
    monkeypatch.setattr(conv, "FluxService", StubFlux)
    monkeypatch.setattr(conv, "TTSPool", StubTTSPool)
    monkeypatch.setattr(conv, "Agent", StubAgent)

    yield conv
    reset_carrier_cache()


class _Call:
    """One `run_conversation` under test, driven from the outside."""

    def __init__(self, conv):
        from shuo.carrier import get_carrier
        from shuo.types import CallContext, CallDirection

        self.ws = DrivenWebSocket()
        ctx = CallContext(call_id="c1", direction=CallDirection.OUTBOUND,
                          persona_id="candidate", carrier="vobiz")
        self.task = asyncio.create_task(
            conv.run_conversation(self.ws, ctx, get_carrier("vobiz"))
        )

    async def start_stream(self):
        self.ws.push(VobizProtocol.start("s1", "c1"))
        assert await until(lambda: bool(StubAgent.instances)), "agent was never built"
        return StubAgent.instances[-1]

    async def drain_loop(self):
        """
        Let the loop chew through what it already has.

        The ack and the AgentTurnDoneEvent it queues are two hops with no
        blocking await between them, so a handful of ticks is plenty.
        """
        for _ in range(10):
            await asyncio.sleep(0.005)

    async def finish(self):
        self.ws.hangup()
        await asyncio.wait_for(self.task, timeout=5.0)


class TestLoopWiring:
    """
    Grace is 5s in these tests -- far longer than the test will wait. The
    fallback timer therefore cannot mask a broken ack path: only the
    carrier's acknowledgement can end a turn here.
    """

    @pytest.mark.asyncio
    async def test_the_carrier_ack_is_what_ends_the_turn(self, loop_env, monkeypatch):
        monkeypatch.setenv("SHUO_CHECKPOINT_GRACE_MS", "5000")

        call = _Call(loop_env)
        agent = await call.start_stream()
        flux = StubFlux.instances[-1]

        await flux.on_end_of_turn("tell me about yourself")
        assert await until(lambda: len(agent.started) == 1)

        agent.finish_playback()
        await call.drain_loop()

        # Still RESPONDING: the caller has not heard the answer yet, so a
        # second end-of-turn is dropped by the state machine.
        await flux.on_end_of_turn("and your notice period?")
        await call.drain_loop()
        assert len(agent.started) == 1, "the turn ended before the carrier acked it"

        call.ws.push(VobizProtocol.played_stream("turn-1"))
        await call.drain_loop()

        await flux.on_end_of_turn("and your notice period?")
        assert await until(lambda: len(agent.started) == 2), (
            "the ack did not end the turn"
        )

        await call.finish()

    @pytest.mark.asyncio
    async def test_an_ack_we_are_not_waiting_on_changes_nothing(self, loop_env, monkeypatch):
        monkeypatch.setenv("SHUO_CHECKPOINT_GRACE_MS", "5000")

        call = _Call(loop_env)
        agent = await call.start_stream()
        flux = StubFlux.instances[-1]

        await flux.on_end_of_turn("first")
        assert await until(lambda: len(agent.started) == 1)
        agent.finish_playback()
        await call.drain_loop()

        call.ws.push(VobizProtocol.played_stream("turn-9"))
        await call.drain_loop()

        await flux.on_end_of_turn("second")
        await call.drain_loop()
        assert len(agent.started) == 1, "an unrelated ack ended the turn"

        await call.finish()

    @pytest.mark.asyncio
    async def test_a_barge_in_voids_the_checkpoint(self, loop_env, monkeypatch):
        """
        The hazard the `void` calls exist for: turn 1 is abandoned, turn 2
        starts, and turn 1's ack finally shows up. It must not end turn 2.
        """
        monkeypatch.setenv("SHUO_CHECKPOINT_GRACE_MS", "5000")

        call = _Call(loop_env)
        agent = await call.start_stream()
        flux = StubFlux.instances[-1]

        await flux.on_end_of_turn("a long answer")
        assert await until(lambda: len(agent.started) == 1)

        # Playback dispatched, ack still outstanding -- the 250ms window
        # a real barge-in can land in.
        agent.finish_playback()
        await call.drain_loop()

        await flux.on_start_of_turn()          # caller interrupts
        await call.drain_loop()
        assert agent.cancelled == 1

        # Turn 2 is now running and has NOT reached playback, so nothing
        # has superseded turn 1's checkpoint. Only the void has.
        await flux.on_end_of_turn("second question")
        assert await until(lambda: len(agent.started) == 2)

        call.ws.push(VobizProtocol.played_stream("turn-1"))
        await call.drain_loop()

        await flux.on_end_of_turn("third question")
        await call.drain_loop()
        assert len(agent.started) == 2, "a stale ack ended the turn that was running"

        await call.finish()

    @pytest.mark.asyncio
    async def test_the_call_survives_a_carrier_that_never_acks(self, loop_env, monkeypatch):
        """
        rules.md V18, end to end. If the ack never comes the conversation
        must keep going -- otherwise one silent carrier deadlocks the call
        after a single turn.
        """
        monkeypatch.setenv("SHUO_CHECKPOINT_GRACE_MS", "60")

        call = _Call(loop_env)
        agent = await call.start_stream()
        flux = StubFlux.instances[-1]

        await flux.on_end_of_turn("first")
        assert await until(lambda: len(agent.started) == 1)
        agent.finish_playback()

        await asyncio.sleep(0.15)  # past the grace window, with no ack sent
        await flux.on_end_of_turn("second")
        assert await until(lambda: len(agent.started) == 2), (
            "the call deadlocked waiting for an ack that never came"
        )

        await call.finish()
