"""Synthetic lifecycle replay: real Flux parser, coordinator, Agent and player.

Only provider and hardware boundaries are replaced. No call content or sockets.
"""
import asyncio
import base64

import pytest

from shuo.agent import Agent
from shuo.bluetooth.conversation import run_bluetooth_conversation
from shuo.bluetooth.speculative import AsyncCapacityGate, SpeculativeTurnCoordinator
from shuo.services.flux import FluxService
from shuo.tracer import Tracer
from tests.test_bluetooth_conversation import FakeSession


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_shadow", [False, True])
async def test_speaking_barge_in_restarts_once_and_dispatches_new_answer(monkeypatch, caplog, stale_shadow):
    caplog.set_level("INFO")
    holder = {}
    shadow_started = asyncio.Event()
    shadow_cancelled = asyncio.Event()
    release_shadow = asyncio.Event()
    ready = asyncio.Event()
    session = FakeSession(block_when_empty=True)

    class LLM:
        def __init__(self, on_token, on_done, **kwargs):
            self.on_token, self.on_done = on_token, on_done
            self.history = []
            self.task = None

        async def start(self, text):
            self.history.append({"role": "user", "content": text})

            async def generate():
                await self.on_token("synthetic answer")
                await self.on_done()

            self.task = asyncio.create_task(generate())

        async def cancel(self):
            if self.task is not None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    monkeypatch.setattr("shuo.agent.LLMService", LLM)

    class TTS:
        def __init__(self, on_audio, on_done, size):
            self.on_audio, self.on_done, self.size = on_audio, on_done, size
            self.cancelled = False
            self.tokens = []

        async def send(self, token):
            self.tokens.append(token)
            await self.on_audio(base64.b64encode(b"\xfe" * self.size).decode())

        async def flush(self):
            await self.on_done()

        async def cancel(self):
            self.cancelled = True

    class Pool:
        def __init__(self):
            self.services = []

        async def get(self, on_audio, on_done):
            # Long old answer is still draining when the caller interrupts.
            service = TTS(on_audio, on_done, 80000 if not self.services else 480)
            self.services.append(service)
            return service

    pool = Pool()

    class OfflineFlux(FluxService):
        async def start(self):
            pass

        async def stop(self):
            pass

    class Probe:
        async def first_token_at(self, text):
            shadow_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                shadow_cancelled.set()
                # Model a provider that continues closing after cancellation.
                await release_shadow.wait()
                return 1.0

    def flux_factory(eot, sot, interim, eager, resumed):
        holder["flux"] = OfflineFlux(
            eot, sot, interim, eager, resumed, eager_eot_threshold=0.3,
            include_empty_interims=True, diagnose_updates=True,
        )
        return holder["flux"]

    def agent_factory(outbound, done):
        holder["agent"] = Agent(outbound, done, pool, Tracer())
        return holder["agent"]

    def speculation_factory(agent):
        holder["shadow"] = SpeculativeTurnCoordinator(
            probe=Probe(), capacity_gate=AsyncCapacityGate(1), early_transcripts=True,
        )
        ready.set()
        return holder["shadow"]

    task = asyncio.create_task(run_bluetooth_conversation(
        session, flux_factory=flux_factory, agent_factory=agent_factory,
        speculation_factory=speculation_factory,
    ))

    async def emit(event, text=""):
        await holder["flux"]._on_message({"type": "TurnInfo", "event": event, "transcript": text})

    try:
        await asyncio.wait_for(ready.wait(), 2)
        await emit("StartOfTurn")
        if stale_shadow:
            await emit("EagerEndOfTurn", "synthetic initial question")
            await asyncio.wait_for(shadow_started.wait(), 2)
        await emit("EndOfTurn", "synthetic initial question")
        await eventually(lambda: bool(session.writes))
        agent = holder["agent"]
        old_player = agent._player
        assert old_player.is_playing
        if stale_shadow:
            await asyncio.wait_for(shadow_cancelled.wait(), 2)
            assert holder["shadow"]._tasks

        await emit("StartOfTurn")
        await eventually(lambda: session.clears == 1)
        old_frames = old_player.frames_sent
        assert not old_player.is_playing
        assert pool.services[0].cancelled
        assert not agent.is_turn_active

        # No speculative generation or repeated correction can restart normal
        # Agent work; only this utterance's final EOT can do that.
        for _ in range(3):
            await emit("TurnResumed")
            await emit("Update", "synthetic corrected question")
        assert len(pool.services) == 1
        assert holder["shadow"]._turn_open
        assert old_player.frames_sent == old_frames

        await emit("EndOfTurn", "synthetic corrected question")
        await emit("EndOfTurn", "synthetic corrected question")  # duplicate while responding
        await eventually(lambda: len(pool.services) == 2 and not agent.is_turn_active)
        await asyncio.sleep(0)  # consume dispatch completion
        assert old_player.frames_sent == old_frames
        assert pool.services[1].tokens == ["synthetic answer"]
        assert len(session.writes) > old_frames
        assert len(agent.history) == 2
        assert "event=AgentStart_begin normal_turn=2" in caplog.text
        assert "TTS first audio" in caplog.text
        assert "event=PlaybackDispatch_complete" in caplog.text
        assert "event=AgentCancel_returned" in caplog.text
        assert "cancel_stage=tts_returned" in caplog.text
        if stale_shadow:
            # The normal answer finished while cancelled shadow work still
            # held its capacity slot. Late success must remain discarded.
            assert holder["shadow"]._tasks
            release_shadow.set()
            await eventually(lambda: not holder["shadow"]._tasks)
            assert len(agent.history) == 2

        # A later real start reopens shadow admission after final closed it.
        await emit("StartOfTurn")
        await emit("TurnResumed")
        await emit("Update", "synthetic third question")
        assert holder["shadow"]._turn_open
        await emit("EndOfTurn", "synthetic third question")
        await eventually(lambda: len(pool.services) == 3 and not agent.is_turn_active)
        assert len(agent.history) == 3
        assert "synthetic" not in caplog.text
    finally:
        release_shadow.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert session.stopped == 1
    assert not holder["shadow"]._tasks
