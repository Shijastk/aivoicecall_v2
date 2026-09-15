import asyncio

import pytest

from shuo.bluetooth.conversation import run_bluetooth_conversation


class FakeSession:
    def __init__(self, chunks=None, *, fail_start=False, block_when_empty=False):
        self.chunks = list(chunks or [])
        self.fail_start = fail_start
        self.block_when_empty = block_when_empty
        self.started = 0
        self.stopped = 0
        self.writes = []
        self.clears = 0
        self._empty_wait = asyncio.Event()

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("start failed")

    async def stop(self):
        self.stopped += 1

    async def read(self):
        if self.chunks:
            return self.chunks.pop(0)
        if self.block_when_empty:
            # Model a healthy live capture stream with no bytes available yet.
            # Cancellation of the orchestrator cancels this await cleanly.
            await self._empty_wait.wait()
            raise AssertionError("unreachable")
        raise RuntimeError("capture ended")

    async def write(self, audio):
        self.writes.append(audio)

    async def clear(self):
        self.clears += 1


class FakeFlux:
    def __init__(self, on_eot, on_sot, on_interim):
        self.on_eot = on_eot
        self.on_sot = on_sot
        self.on_interim = on_interim
        self.started = 0
        self.stopped = 0
        self.sent = []

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def send(self, audio):
        self.sent.append(audio)


class FakeAgent:
    def __init__(self, outbound, on_done):
        self.outbound = outbound
        self.on_done = on_done
        self.started = []
        self.cancelled = 0
        self.cleaned = 0

    async def start_turn(self, transcript):
        self.started.append(transcript)

    async def cancel_turn(self):
        self.cancelled += 1
        await self.outbound.clear_audio()

    async def cleanup(self):
        self.cleaned += 1


@pytest.mark.asyncio
async def test_capture_audio_reaches_flux_only_as_converted_mulaw():
    session = FakeSession([b"\x00\x00" * 320])
    holder = {}

    def flux_factory(eot, sot, interim):
        holder["flux"] = FakeFlux(eot, sot, interim)
        return holder["flux"]

    def agent_factory(outbound, done):
        holder["agent"] = FakeAgent(outbound, done)
        return holder["agent"]

    await run_bluetooth_conversation(
        session,
        flux_factory=flux_factory,
        agent_factory=agent_factory,
    )

    assert session.started == 1
    assert session.stopped == 1
    assert holder["flux"].started == 1
    assert holder["flux"].stopped == 1
    assert len(holder["flux"].sent) == 1
    assert holder["flux"].sent[0]
    assert session.writes == []


@pytest.mark.asyncio
async def test_flux_end_of_turn_starts_agent_and_dispatch_completion_is_local():
    session = FakeSession([], block_when_empty=True)
    holder = {}
    ready = asyncio.Event()

    class DrivingFlux(FakeFlux):
        async def start(self):
            await super().start()
            ready.set()

    def flux_factory(eot, sot, interim):
        holder["flux"] = DrivingFlux(eot, sot, interim)
        return holder["flux"]

    class CompletingAgent(FakeAgent):
        async def start_turn(self, transcript):
            await super().start_turn(transcript)
            self.on_done("turn-1")

    def agent_factory(outbound, done):
        holder["agent"] = CompletingAgent(outbound, done)
        return holder["agent"]

    task = asyncio.create_task(
        run_bluetooth_conversation(
            session,
            flux_factory=flux_factory,
            agent_factory=agent_factory,
        )
    )
    await ready.wait()
    await holder["flux"].on_eot("hello")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert holder["agent"].started == ["hello"]

    # End the fake capture/session.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.stopped == 1


@pytest.mark.asyncio
async def test_barge_in_cancels_agent_and_clears_only_bluetooth_playback_queue():
    session = FakeSession([], block_when_empty=True)
    holder = {}
    ready = asyncio.Event()

    class DrivingFlux(FakeFlux):
        async def start(self):
            await super().start()
            ready.set()

    def flux_factory(eot, sot, interim):
        holder["flux"] = DrivingFlux(eot, sot, interim)
        return holder["flux"]

    def agent_factory(outbound, done):
        holder["agent"] = FakeAgent(outbound, done)
        return holder["agent"]

    task = asyncio.create_task(
        run_bluetooth_conversation(
            session,
            flux_factory=flux_factory,
            agent_factory=agent_factory,
        )
    )
    await ready.wait()

    await holder["flux"].on_eot("question")
    await asyncio.sleep(0)
    await holder["flux"].on_sot()
    await asyncio.sleep(0)

    assert holder["agent"].started == ["question"]
    assert holder["agent"].cancelled == 1
    assert session.clears == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_start_failure_does_not_attempt_stop_on_never_started_session():
    session = FakeSession(fail_start=True)

    def flux_factory(eot, sot, interim):
        raise AssertionError("Flux must not be built after session start failure")

    def agent_factory(outbound, done):
        raise AssertionError("Agent must not be built after session start failure")

    with pytest.raises(RuntimeError, match="start failed"):
        await run_bluetooth_conversation(
            session,
            flux_factory=flux_factory,
            agent_factory=agent_factory,
        )

    assert session.started == 1
    assert session.stopped == 0


@pytest.mark.asyncio
async def test_flux_start_failure_still_stops_started_bluetooth_session():
    session = FakeSession([])
    holder = {}

    class FailingFlux(FakeFlux):
        async def start(self):
            raise RuntimeError("flux start failed")

    def flux_factory(eot, sot, interim):
        holder["flux"] = FailingFlux(eot, sot, interim)
        return holder["flux"]

    def agent_factory(outbound, done):
        raise AssertionError("Agent must not be built after Flux start failure")

    with pytest.raises(RuntimeError, match="flux start failed"):
        await run_bluetooth_conversation(
            session,
            flux_factory=flux_factory,
            agent_factory=agent_factory,
        )

    assert session.stopped == 1
    assert holder["flux"].stopped == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity_available", [True, False])
async def test_real_flux_updates_are_shadow_only_and_final_always_uses_agent(monkeypatch, capacity_available, caplog):
    from types import SimpleNamespace
    from shuo.services.flux import FluxService
    from shuo.bluetooth.speculative import AsyncCapacityGate, SpeculativeTurnCoordinator

    now = [10.0]
    caplog.set_level("INFO")
    monkeypatch.setattr("shuo.bluetooth.conversation.time", SimpleNamespace(perf_counter=lambda: now[0]))
    ready = asyncio.Event()
    holder = {}
    session = FakeSession([], block_when_empty=True)
    gate = AsyncCapacityGate(1, max_wait_seconds=0.001)
    if not capacity_available:
        assert await gate.acquire()

    class OfflineFlux(FluxService):
        async def start(self):
            pass

        async def stop(self):
            pass

    class Probe:
        async def first_token_at(self, transcript):
            return 10.5

    def flux_factory(eot, sot, interim, eager, resumed):
        holder["flux"] = OfflineFlux(
            eot, sot, interim, eager, resumed,
            eager_eot_threshold=0.3, include_empty_interims=True,
            diagnose_updates=True,
        )
        return holder["flux"]

    def agent_factory(outbound, done):
        holder["agent"] = FakeAgent(outbound, done)
        return holder["agent"]

    def speculation_factory(agent):
        holder["shadow"] = SpeculativeTurnCoordinator(
            probe=Probe(), capacity_gate=gate, early_transcripts=True,
        )
        ready.set()
        return holder["shadow"]

    task = asyncio.create_task(run_bluetooth_conversation(
        session, flux_factory=flux_factory, agent_factory=agent_factory,
        speculation_factory=speculation_factory,
    ))
    try:
        await ready.wait()
        async def emit(event, text=""):
            await holder["flux"]._on_message({"type": "TurnInfo", "event": event, "transcript": text})

        await emit("StartOfTurn")
        await emit("Update", "tell me about yourself")
        now[0] = 10.25
        await emit("Update", "tell me about yourself")
        await asyncio.gather(*tuple(holder["shadow"]._tasks))
        assert "BTShadowFlux: event=Update update_count=2 callback_present=True" in caplog.text
        assert "event=update update_count=2" in caplog.text
        assert "BTShadowFlux: event=Update_callback_returned update_count=2" in caplog.text
        assert holder["agent"].started == []
        assert holder["agent"].cancelled == 0
        assert session.writes == []
        now[0] = 11.0
        await emit("EndOfTurn", "tell me about yourself")
        await asyncio.sleep(0)
        assert holder["agent"].started == ["tell me about yourself"]
        assert holder["agent"].cancelled == 0
        expected = "ready_before_final" if capacity_available else "discarded_by_final"
        assert holder["shadow"].observations[-1].outcome == expected
        await emit("TurnResumed")
        assert holder["agent"].cancelled == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if not capacity_available:
            gate.release()
    assert not holder["shadow"]._tasks
    assert holder["agent"].cleaned == 1
