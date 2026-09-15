import asyncio
from types import SimpleNamespace

import pytest

from shuo.bluetooth.conversation import run_bluetooth_conversation
from shuo.bluetooth.production import run_production_bluetooth_conversation
from shuo.bluetooth.speculative import AsyncCapacityGate, SpeculativeTurnCoordinator
from shuo.services.llm import LLMService, ShadowLLMProbe


def chunk(text=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
    )


class CountingStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.reads = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        self.reads += 1
        return self.chunks.pop(0)

    async def close(self):
        self.closed = True


class BlockingStream(CountingStream):
    def __init__(self, chunks):
        super().__init__(chunks)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def __anext__(self):
        if self.reads == 0:
            self.entered.set()
            await self.release.wait()
        return await super().__anext__()


class FakeCompletions:
    def __init__(self, stream):
        self.stream = stream
        self.calls = 0
        self.kwargs = None

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return self.stream


class FakeClient:
    def __init__(self, stream):
        self.completions = FakeCompletions(stream)
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.mark.asyncio
async def test_prepared_stream_prefetches_only_first_content_token_then_reuses_rest():
    history = [{"role": "user", "content": "old"}]
    stream = CountingStream([chunk(None), chunk("first"), chunk(" second")])
    client = FakeClient(stream)
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: history,
        client=client,
    )

    prepared = probe.create_prepared("question")
    prepared.start()
    assert isinstance(await prepared.wait_first_token(), float)
    assert client.completions.calls == 1
    assert stream.reads == 2
    assert stream.closed is False

    tokens = []

    async def collect(token):
        tokens.append(token)

    await prepared.consume(collect)
    assert tokens == ["first", " second"]
    assert client.completions.calls == 1
    assert stream.reads == 3
    assert stream.closed is True
    assert history == [{"role": "user", "content": "old"}]


@pytest.mark.asyncio
async def test_llm_service_commits_prepared_stream_with_normal_history_semantics(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")
    tokens = []
    done = asyncio.Event()

    async def on_token(token):
        tokens.append(token)

    async def on_done():
        done.set()

    service = LLMService(on_token=on_token, on_done=on_done, system_prompt="system")
    service._history = [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
    ]
    stream = CountingStream([chunk("new"), chunk(" answer")])
    client = FakeClient(stream)
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: service.history,
        client=client,
    )
    prepared = probe.create_prepared("final question")
    prepared.start()
    await prepared.wait_first_token()

    assert await service.start_prepared("final question", prepared) is True
    await asyncio.wait_for(done.wait(), 1)
    if service._task is not None:
        await service._task
    assert client.completions.calls == 1
    assert tokens == ["new", " answer"]
    assert service.history == [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
        {"role": "user", "content": "final question"},
        {"role": "assistant", "content": "new answer"},
    ]


@pytest.mark.asyncio
async def test_prepared_history_mismatch_fails_closed_without_history_mutation(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")

    async def noop(*args):
        return None

    service = LLMService(on_token=noop, on_done=noop, system_prompt="system")
    service._history = [{"role": "user", "content": "old"}]
    stream = CountingStream([chunk("draft")])
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: service.history,
        client=FakeClient(stream),
    )
    prepared = probe.create_prepared("question")
    prepared.start()
    await prepared.wait_first_token()
    service._history.append({"role": "assistant", "content": "changed"})
    before = service.history

    assert await service.start_prepared("question", prepared) is False
    assert service.history == before
    assert stream.closed is True


@pytest.mark.asyncio
async def test_prepared_cancellation_matches_normal_partial_history_semantics(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")
    got_first = asyncio.Event()
    release_second = asyncio.Event()

    class TwoStageStream:
        def __init__(self):
            self.index = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return chunk("first")
            if self.index == 2:
                await release_second.wait()
                return chunk(" second")
            raise StopAsyncIteration

        async def close(self):
            self.closed = True

    async def on_token(token):
        if token == "first":
            got_first.set()

    async def on_done():
        return None

    service = LLMService(on_token=on_token, on_done=on_done, system_prompt="system")
    stream = TwoStageStream()
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: service.history,
        client=FakeClient(stream),
    )
    prepared = probe.create_prepared("question")
    prepared.start()
    await prepared.wait_first_token()
    assert await service.start_prepared("question", prepared) is True
    await got_first.wait()
    await service.cancel()

    assert service.history == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "first..."},
    ]
    assert stream.closed is True


@pytest.mark.asyncio
async def test_coordinator_promotes_only_matching_first_token_ready_stream_once():
    stream = CountingStream([chunk("first"), chunk(" rest")])
    client = FakeClient(stream)
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: [],
        client=client,
    )
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
        prepared_reuse=True,
    )
    coordinator.on_eager("hello", observed_at=10.0)
    await asyncio.sleep(0)
    candidate = coordinator._candidate
    assert candidate is not None
    await candidate.prepared.wait_first_token()
    await asyncio.sleep(0)

    coordinator.on_final(
        "hello", observed_at=candidate.prepared.first_token_at + 0.001
    )
    assert coordinator.observations[-1].outcome == "promoted_ready_before_final"
    prepared = coordinator.take_committed("hello")
    assert prepared is not None
    assert coordinator.take_committed("hello") is None

    tokens = []

    async def collect(token):
        tokens.append(token)

    await prepared.consume(collect)
    assert tokens == ["first", " rest"]
    assert client.completions.calls == 1
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_final_before_first_token_cancels_prepared_and_falls_back():
    stream = BlockingStream([chunk("draft")])
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: [],
        client=FakeClient(stream),
    )
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
        prepared_reuse=True,
    )
    coordinator.on_eager("hello", observed_at=1.0)
    await asyncio.sleep(0)
    await asyncio.wait_for(stream.entered.wait(), 1)
    coordinator.on_final("hello", observed_at=1.2)
    await asyncio.sleep(0)

    assert coordinator.take_committed("hello") is None
    assert coordinator.observations[-1].outcome == "not_ready_by_final"
    await coordinator.cleanup()
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["mismatch", "resume"])
async def test_invalidated_prepared_stream_is_never_promoted(boundary):
    stream = CountingStream([chunk("draft"), chunk(" rest")])
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: [],
        client=FakeClient(stream),
    )
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
        prepared_reuse=True,
    )
    coordinator.on_eager("hello", observed_at=1.0)
    await asyncio.sleep(0)
    await coordinator._candidate.prepared.wait_first_token()
    await asyncio.sleep(0)

    if boundary == "resume":
        coordinator.on_resumed(observed_at=1.1)
        await asyncio.sleep(0)
        coordinator.on_final("hello", observed_at=1.2)
    else:
        coordinator.on_final("hello changed", observed_at=1.2)
    await asyncio.sleep(0)

    assert coordinator.take_committed("hello") is None
    assert coordinator.take_committed("hello changed") is None
    await coordinator.cleanup()
    assert stream.closed is True


class FakeSession:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.block = asyncio.Event()

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def read(self):
        await self.block.wait()
        raise RuntimeError("ended")

    async def write(self, audio):
        return None

    async def clear(self):
        return None


@pytest.mark.asyncio
async def test_conversation_routes_committed_response_to_agent_once():
    session = FakeSession()
    holder = {}
    ready = asyncio.Event()
    prepared = SimpleNamespace(cancel=lambda: None)

    class Flux:
        def __init__(self, eot, sot, interim, eager, resumed):
            self.eot = eot

        async def start(self):
            ready.set()

        async def stop(self):
            return None

        async def send(self, audio):
            return None

    class Agent:
        history = []

        def __init__(self, done):
            self.done = done
            self.calls = []

        async def start_turn(self, transcript, prepared_response=None):
            self.calls.append((transcript, prepared_response))
            self.done(None)

        async def cancel_turn(self):
            return None

        async def cleanup(self):
            return None

    class Speculator:
        def __init__(self):
            self.taken = False

        def on_start(self): pass
        def on_interim(self, *args, **kwargs): pass
        def on_eager(self, *args, **kwargs): pass
        def on_resumed(self, *args, **kwargs): pass
        def on_final(self, transcript, **kwargs): self.final = transcript
        def take_committed(self, transcript):
            if self.taken:
                return None
            self.taken = True
            return prepared
        async def cleanup(self): pass

    def flux_factory(eot, sot, interim, eager, resumed):
        holder["flux"] = Flux(eot, sot, interim, eager, resumed)
        return holder["flux"]

    def agent_factory(outbound, done):
        holder["agent"] = Agent(done)
        return holder["agent"]

    def speculation_factory(agent):
        return Speculator()

    task = asyncio.create_task(run_bluetooth_conversation(
        session,
        flux_factory=flux_factory,
        agent_factory=agent_factory,
        speculation_factory=speculation_factory,
    ))
    try:
        await ready.wait()
        await holder["flux"].eot("question")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert holder["agent"].calls == [("question", prepared)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_production_rejects_prepared_reuse_without_shadow_before_resources():
    with pytest.raises(ValueError, match="requires shadow_speculation"):
        await run_production_bluetooth_conversation(
            object(),
            prepared_response_reuse=True,
        )
