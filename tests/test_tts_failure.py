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
import time
from types import SimpleNamespace

import pytest

from shuo.agent import Agent
from shuo.services.tts import TTSService
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


@pytest.fixture
def delayed_warmup(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    instances = []

    class DelayedTTS(DeadTTS):
        def __init__(self, on_audio, on_done, voice_id):
            super().__init__()
            self.voice_id = voice_id
            self.start_error = None
            instances.append(self)

        async def start(self):
            started.set()
            await release.wait()
            if self.start_error:
                raise self.start_error

    monkeypatch.setattr("shuo.services.tts_pool.TTSService", DelayedTTS)
    return started, release, instances


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
    async def test_idle_origin_excludes_handshake_but_includes_init_send(self, monkeypatch):
        clock = [100.0]
        fake_time = SimpleNamespace(monotonic=lambda: clock[0])
        monkeypatch.setattr("shuo.services.tts.time", fake_time)
        monkeypatch.setattr("shuo.services.tts_pool.time", fake_time)
        monkeypatch.setenv("ELEVENLABS_API_KEY", "")

        class Socket:
            closed = False

            async def send(self, message):
                # Initialization starts at 104 and completes at 106.
                clock[0] += 2

            async def recv(self):
                await asyncio.Event().wait()

            async def close(self):
                self.closed = True

        socket = Socket()

        async def connect(url):
            clock[0] += 4  # four-second handshake, no provider text sent yet
            return socket

        monkeypatch.setattr("shuo.services.tts.websockets.connect", connect)
        pool = TTSPool()
        await pool.start()
        try:
            await pool.wait_ready(timeout=1)
            entry = pool._ready[0]
            assert isinstance(entry.tts, TTSService)
            assert entry.created_at == entry.tts.warm_idle_started_at == 104.0
            assert clock[0] == 106.0
            clock[0] = 118.0  # 18s after handshake began, only 14s idle
            await pool._evict_stale()
            assert pool.available == 1
            clock[0] = 119.0  # 15s since init send began, not since it finished
            await pool._evict_stale()
            assert pool.available == 0
            assert socket.closed
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_first_checkout_joins_initial_warmup(self, delayed_warmup):
        started, release, instances = delayed_warmup
        pool = TTSPool(voice_id="chosen-voice")
        await pool.start()
        checkout = asyncio.create_task(pool.get(None, None))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0)
            assert not checkout.done()
            assert len(instances) == 1, "first turn opened a duplicate cold socket"
            release.set()
            got = await asyncio.wait_for(checkout, 1)
            assert got is instances[0]
            assert got.voice_id == "chosen-voice"
            await got.cancel()
        finally:
            checkout.cancel()
            await asyncio.gather(checkout, return_exceptions=True)
            await pool.stop()
        assert all(tts.cancelled for tts in instances)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["cancel", "timeout", "stop", "failure"])
    async def test_readiness_waiter_cleanup(self, delayed_warmup, outcome):
        started, release, instances = delayed_warmup
        pool = TTSPool()
        await pool.start()
        waiter = asyncio.create_task(pool.wait_ready(timeout=0.05 if outcome == "timeout" else 1))
        try:
            await asyncio.wait_for(started.wait(), 1)
            if outcome == "cancel":
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert not instances[0].cancelled
                release.set()
                await pool.wait_ready()
                assert pool.available == 1
            elif outcome == "timeout":
                with pytest.raises(asyncio.TimeoutError):
                    await waiter
                assert not instances[0].cancelled
            elif outcome == "stop":
                await pool.stop()
                with pytest.raises(RuntimeError, match="not warming"):
                    await waiter
            else:
                raise_error = RuntimeError("connect refused")
                instances[0].start_error = raise_error
                release.set()
                with pytest.raises(asyncio.TimeoutError, match="readiness") as exc:
                    await waiter
                assert exc.value.__cause__ is raise_error
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await pool.stop()
        assert all(tts.cancelled for tts in instances)

    @pytest.mark.asyncio
    async def test_readiness_retries_initial_failure_then_first_turn_reuses(self, monkeypatch):
        failed = asyncio.Event()
        instances = []

        class RetryTTS(DeadTTS):
            def __init__(self, **kwargs):
                super().__init__()
                instances.append(self)

            async def start(self):
                if len(instances) == 1:
                    failed.set()
                    raise RuntimeError("temporary preconnect failure")

        monkeypatch.setattr("shuo.services.tts_pool.TTSService", RetryTTS)
        pool = TTSPool()
        await pool.start()
        checkout = asyncio.create_task(pool.get(None, None))
        try:
            await asyncio.wait_for(failed.wait(), 1)
            await asyncio.sleep(0)
            assert not checkout.done()
            assert instances[0].cancelled
            got = await asyncio.wait_for(checkout, 3)
            assert got is instances[1]
            assert pool._fill_error is None
            await got.cancel()
        finally:
            checkout.cancel()
            await asyncio.gather(checkout, return_exceptions=True)
            await pool.stop()
        assert all(tts.cancelled for tts in instances)

    @pytest.mark.asyncio
    async def test_readiness_timeout_retains_most_recent_retry_error(self, monkeypatch):
        errors = []
        instances = []

        class FailedTTS(DeadTTS):
            def __init__(self, **kwargs):
                super().__init__()
                instances.append(self)

            async def start(self):
                error = RuntimeError(f"attempt {len(errors) + 1}")
                errors.append(error)
                raise error

        monkeypatch.setattr("shuo.services.tts_pool.TTSService", FailedTTS)
        pool = TTSPool()
        await pool.start()
        try:
            with pytest.raises(asyncio.TimeoutError) as exc:
                await pool.wait_ready(timeout=1.2)
            assert len(errors) >= 2
            assert exc.value.__cause__ is errors[-1]
        finally:
            await pool.stop()
        assert all(tts.cancelled for tts in instances)

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

    @pytest.mark.asyncio
    async def test_healthy_connection_beyond_eight_seconds_below_safe_max_is_reused(self):
        pool = TTSPool(pool_size=1, ttl=8.0, voice_id="pool-voice")
        alive = DeadTTS(active=True)
        pool._ready = [_Entry(tts=alive, created_at=time.monotonic() - 11)]

        async def on_audio(audio):
            pass

        async def on_done():
            pass

        try:
            got = await pool.get(on_audio=on_audio, on_done=on_done)
            assert got is alive
            assert got._on_audio is on_audio
            assert got._on_done is on_done
            assert not alive.cancelled
            assert pool.available == 0
        finally:
            await pool.stop()
            await alive.cancel()  # ownership passed to the caller

    @pytest.mark.asyncio
    @pytest.mark.parametrize("condition", ["healthy", "dead", "over_age"])
    async def test_periodic_maintenance_retains_safe_or_refills_unusable(
        self, monkeypatch, condition
    ):
        pool = TTSPool(pool_size=1, health_check_interval=0.01, voice_id="pool-voice")
        alive = DeadTTS(active=True)
        entry = _Entry(tts=alive, created_at=time.monotonic() - 11)
        pool._ready = [entry]
        created = []
        maintained = asyncio.Event()
        first_pass = asyncio.Event()
        original_evict = pool._evict_stale
        passes = 0

        class FreshTTS(DeadTTS):
            def __init__(self, on_audio, on_done, voice_id):
                super().__init__()
                self.voice_id = voice_id
                created.append(self)

            async def start(self):
                pass

        async def observe_maintenance():
            nonlocal passes
            await original_evict()
            passes += 1
            if passes == 1:
                first_pass.set()
            if passes == 3:
                maintained.set()

        monkeypatch.setattr("shuo.services.tts_pool.TTSService", FreshTTS)
        monkeypatch.setattr(pool, "_evict_stale", observe_maintenance)
        await pool.start()
        fill_task = pool._fill_task
        try:
            await asyncio.wait_for(first_pass.wait(), 1)
            # No checkout or fill signal: maintenance must discover the change.
            if condition == "dead":
                alive.is_active = False
            elif condition == "over_age":
                entry.created_at = time.monotonic() - 16
            await asyncio.wait_for(maintained.wait(), 1)
            assert pool.available == 1
            if condition == "healthy":
                assert pool._ready[0].tts is alive
                assert not alive.cancelled
                assert created == []
            else:
                assert alive.cancelled
                assert len(created) == 1
                assert pool._ready[0].tts is created[0]
                assert created[0].voice_id == "pool-voice"
        finally:
            await pool.stop()
            await pool.stop()
        assert fill_task.done()
        assert pool._fill_task is None
        assert pool.available == 0
        assert alive.cancelled
        assert all(tts.cancelled for tts in created)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("age", [15.0, 16.0, 19.819])
    async def test_at_or_beyond_safe_max_is_never_dispensed(self, monkeypatch, age):
        pool = TTSPool(1, 8.0, "pool-voice")  # legacy positional callers still work
        expired = DeadTTS()
        fresh = DeadTTS()
        monkeypatch.setattr("shuo.services.tts_pool.time.monotonic", lambda: 100.0)
        pool._ready = [_Entry(expired, 100.0 - age), _Entry(fresh, 100.0)]
        try:
            assert await pool.get(None, None) is fresh
            assert expired.cancelled
            assert expired._on_audio is None
            assert not fresh.cancelled
        finally:
            await pool.stop()
            await fresh.cancel()

    @pytest.mark.asyncio
    async def test_idle_deadline_wakes_before_long_health_interval(self, monkeypatch):
        replaced = asyncio.Event()
        old = DeadTTS()
        fresh = DeadTTS()

        async def start():
            replaced.set()

        fresh.start = start
        monkeypatch.setattr("shuo.services.tts_pool.TTSService", lambda **kw: fresh)
        pool = TTSPool(max_idle_age=0.05, health_check_interval=60)
        pool._ready = [_Entry(old, time.monotonic())]
        await pool.start()
        try:
            await asyncio.wait_for(replaced.wait(), 1)
            assert old.cancelled
            assert pool._ready[0].tts is fresh
        finally:
            await pool.stop()
        assert fresh.cancelled

    @pytest.mark.parametrize("name", ["max_idle_age", "health_check_interval"])
    @pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
    def test_idle_policy_requires_finite_positive_limits(self, name, value):
        with pytest.raises(ValueError, match=name):
            TTSPool(**{name: value})

    @pytest.mark.asyncio
    async def test_stop_cleans_connection_during_preconnect(self, monkeypatch):
        started = asyncio.Event()
        pending = DeadTTS()

        async def start():
            started.set()
            await asyncio.Event().wait()

        pending.start = start
        monkeypatch.setattr("shuo.services.tts_pool.TTSService", lambda **kw: pending)
        pool = TTSPool()
        await pool.start()
        fill_task = pool._fill_task
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            await pool.stop()
        assert pending.cancelled
        assert fill_task.done()
        assert pool.available == 0

    @pytest.mark.asyncio
    async def test_checkout_during_eviction_is_not_reinserted(self):
        closing = asyncio.Event()
        release = asyncio.Event()

        class SlowDeadTTS(DeadTTS):
            async def cancel(self):
                closing.set()
                await release.wait()
                await super().cancel()

        dead = SlowDeadTTS(active=False)
        alive = DeadTTS()
        pool = TTSPool()
        pool._ready = [_Entry(dead, time.monotonic()), _Entry(alive, time.monotonic())]
        maintenance = asyncio.create_task(pool._evict_stale())
        try:
            await asyncio.wait_for(closing.wait(), 1)
            assert await pool.get(None, None) is alive
            alive.is_active = False  # caller may close its socket immediately
            release.set()
            await asyncio.wait_for(maintenance, 1)
            assert pool.available == 0
            assert dead.cancelled
            assert not alive.cancelled
        finally:
            release.set()
            await maintenance
            await pool.stop()
            await alive.cancel()

    @pytest.mark.asyncio
    async def test_stop_finishes_cleanup_of_detached_dead_entry(self):
        closing = asyncio.Event()

        class InterruptedCloseTTS(DeadTTS):
            attempts = 0

            async def cancel(self):
                self.attempts += 1
                if self.attempts == 1:
                    closing.set()
                    await asyncio.Event().wait()
                await super().cancel()

        dead = InterruptedCloseTTS(active=False)
        pool = TTSPool()
        pool._ready = [_Entry(dead, time.monotonic())]
        await pool.start()
        fill_task = pool._fill_task
        try:
            await asyncio.wait_for(closing.wait(), 1)
            assert pool.available == 0
        finally:
            await asyncio.wait_for(pool.stop(), 1)
        assert dead.cancelled
        assert fill_task.done()
        assert pool._fill_task is None
