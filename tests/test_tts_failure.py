"""
A TTS turn that produces no audio must END, not hang.

Regression cover for the live-call failure of 2026-07-22: ElevenLabs
refused the configured voice (`payment_required`, close 1008), the agent
emitted zero audio, and the turn never completed. The state machine sat in
RESPONDING until the caller gave up and barged in -- a pipeline fault
wearing a conversation fault's clothes.

Two independent defects kept it invisible, and both are covered here:

  * `Agent._on_tts_done` only marked the player done. The player is started
    by `send_chunk`, so with zero chunks there is no playback loop, hence no
    `_on_playback_done`, hence no `on_done`, hence no AgentTurnDoneEvent.
  * `TTSPool.get` dispensed on age alone, so a connection that died while
    pooled was handed to a turn that then fed an entire LLM response into a
    closed socket. `send()` and `flush()` both return silently when
    `_running` is False, so that produced no audio and no error either.

Nothing here touches the network.
"""

import asyncio

import pytest

from shuo.agent import Agent
from shuo.services.tts_pool import TTSPool, _Entry
from shuo.tracer import Tracer


class FakeSession:
    """Minimal CarrierSession stand-in that records calls."""

    def __init__(self):
        self.played = []
        self.cleared = 0
        self.checkpoints = []

    async def play_audio(self, payload_b64: str) -> None:
        self.played.append(payload_b64)

    async def clear_audio(self) -> None:
        self.cleared += 1

    async def checkpoint(self, name: str) -> None:
        self.checkpoints.append(name)


class DeadTTS:
    """
    A TTS connection that accepts text and returns nothing.

    Models every zero-audio failure identically, because from the agent's
    side they are identical: a refused generation, a socket that closed
    while pooled, a vendor outage. Only `_on_done` ever fires.
    """

    def __init__(self, active=True, fatal_error=None):
        self.is_active = active
        self.fatal_error = fatal_error
        self.sent = []
        self.flushed = False
        self.cancelled = False
        self._on_audio = None
        self._on_done = None

    def bind(self, on_audio, on_done):
        self._on_audio = on_audio
        self._on_done = on_done

    async def send(self, text):
        self.sent.append(text)

    async def flush(self):
        self.flushed = True

    async def cancel(self):
        self.cancelled = True

    async def finish_without_audio(self):
        """What `_receive_loop`'s finally does after a refusal."""
        await self._on_done()


class StubPool:
    """Hands out one prepared TTS connection."""

    def __init__(self, tts):
        self._tts = tts

    async def get(self, on_audio, on_done):
        self._tts.bind(on_audio, on_done)
        return self._tts


class StubLLM:
    """Replaces LLMService so no Groq call is made."""

    def __init__(self, *a, **kw):
        self.history = []
        self.cancelled = False

    async def start(self, user_message):
        self.history.append({"role": "user", "content": user_message})

    async def cancel(self):
        self.cancelled = True


@pytest.fixture
def agent_parts(monkeypatch):
    """An Agent wired to a dead TTS, with the LLM stubbed out."""
    monkeypatch.setattr("shuo.agent.LLMService", StubLLM)

    tts = DeadTTS()
    session = FakeSession()
    done = []

    agent = Agent(
        session=session,
        on_done=done.append,
        tts_pool=StubPool(tts),
        tracer=Tracer(),
    )
    return agent, tts, session, done


# ── The agent must not hang ──────────────────────────────────────────

class TestZeroAudioTurnEnds:

    @pytest.mark.asyncio
    async def test_turn_completes_when_tts_produces_no_audio(self, agent_parts):
        agent, tts, session, done = agent_parts

        await agent.start_turn("Hello?")
        assert agent.is_turn_active

        # The refusal path: TTS closes having emitted nothing.
        await tts.finish_without_audio()

        assert not agent.is_turn_active, "turn hung -- this is the live-call bug"
        assert len(done) == 1, "on_done must fire exactly once"

    @pytest.mark.asyncio
    async def test_no_checkpoint_is_armed_for_a_silent_turn(self, agent_parts):
        agent, tts, session, done = agent_parts

        await agent.start_turn("Hello?")
        await tts.finish_without_audio()

        # Not one frame was dispatched, so there is nothing for the carrier
        # to acknowledge. Arming would burn the grace window for no reason.
        assert done == [None]
        assert session.checkpoints == []
        assert session.played == []

    @pytest.mark.asyncio
    async def test_a_second_done_callback_does_not_end_a_new_turn(self, agent_parts):
        """
        ElevenLabs can deliver `isFinal` and *then* close, which calls
        `_on_done` twice. The second must not reach through into whatever
        turn is running by then.
        """
        agent, tts, session, done = agent_parts

        await agent.start_turn("Hello?")
        await tts.finish_without_audio()
        assert len(done) == 1

        await agent.start_turn("Second question?")
        await tts.finish_without_audio()   # late duplicate + the new turn's own

        assert len(done) == 2, "a duplicate _on_done must not double-complete"

    @pytest.mark.asyncio
    async def test_turn_with_audio_still_completes_through_the_player(self, agent_parts):
        """The healthy path must still end via playback, with a checkpoint."""
        agent, tts, session, done = agent_parts

        await agent.start_turn("Hello?")

        import base64
        # Two whole 20ms frames so the player has something to dispatch.
        await tts._on_audio(base64.b64encode(b"\x55" * 320).decode())
        await tts.finish_without_audio()

        for _ in range(100):
            if done:
                break
            await asyncio.sleep(0.01)

        assert done == ["turn-1"], "healthy turns must still carry a checkpoint"
        assert session.played, "frames must reach the carrier"
        assert session.checkpoints == ["turn-1"]


# ── The pool must not dispense corpses ───────────────────────────────

class TestPoolLiveness:

    @pytest.mark.asyncio
    async def test_dead_connection_is_never_dispensed(self):
        pool = TTSPool(pool_size=1, ttl=8.0)
        dead = DeadTTS(active=False, fatal_error="payment_required: ...")
        alive = DeadTTS(active=True)

        import time
        now = time.monotonic()
        pool._ready = [
            _Entry(tts=dead, created_at=now),    # young, but dead
            _Entry(tts=alive, created_at=now),
        ]

        got = await pool.get(on_audio=None, on_done=None)

        assert got is alive, "age is not liveness -- a closed socket must be skipped"
        assert dead.cancelled

    @pytest.mark.asyncio
    async def test_dead_connection_is_evicted_so_the_pool_refills(self):
        pool = TTSPool(pool_size=1, ttl=8.0)
        dead = DeadTTS(active=False)

        import time
        pool._ready = [_Entry(tts=dead, created_at=time.monotonic())]

        await pool._evict_stale()

        # Left in place it would still count toward pool_size, so the fill
        # loop would not replace it and the next turn would block on a cold
        # connect after discarding it.
        assert pool._ready == []
        assert dead.cancelled

    @pytest.mark.asyncio
    async def test_live_connection_survives_eviction(self):
        pool = TTSPool(pool_size=1, ttl=8.0)
        alive = DeadTTS(active=True)

        import time
        pool._ready = [_Entry(tts=alive, created_at=time.monotonic())]

        await pool._evict_stale()

        assert len(pool._ready) == 1
        assert not alive.cancelled
