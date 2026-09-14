import asyncio
import time
from types import SimpleNamespace

import pytest

from shuo.bluetooth.speculative import (
    AsyncCapacityGate,
    SpeculativeTurnCoordinator,
)
from shuo.services.llm import ShadowLLMProbe


class BlockingProbe:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def first_token_at(self, transcript: str) -> float:
        self.calls.append(transcript)
        self.started.set()
        await self.release.wait()
        return time.perf_counter()


@pytest.mark.asyncio
async def test_shadow_first_token_before_final_is_observed_but_never_spoken():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    probe.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    coordinator.on_final("hello")

    assert coordinator.observations[-1].outcome == "ready_before_final"
    assert coordinator.observations[-1].first_token_before_final is True
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_turn_resumed_cancels_and_discards_shadow_generation():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    coordinator.on_resumed()
    await asyncio.sleep(0)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "resumed"
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_final_before_first_token_cancels_shadow():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    coordinator.on_final("hello")
    await asyncio.sleep(0)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "not_ready_by_final"
    assert coordinator.observations[-1].first_token_before_final is False
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_transcript_mismatch_never_keeps_shadow():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    coordinator.on_final("hello changed")
    await asyncio.sleep(0)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "transcript_mismatch"
    assert coordinator.observations[-1].transcript_match is False
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_capacity_pressure_skips_shadow_without_failing_call_path():
    gate = AsyncCapacityGate(1, max_wait_seconds=0.005)
    assert await gate.acquire() is True

    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(probe=probe, capacity_gate=gate)
    coordinator.on_eager("hello")
    await asyncio.sleep(0.02)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "capacity_skip"

    gate.release()
    await coordinator.cleanup()


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, stream):
        self.stream = stream
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.stream


class FakeClient:
    def __init__(self, stream):
        self.chat = SimpleNamespace(completions=FakeCompletions(stream))


@pytest.mark.asyncio
async def test_shadow_llm_uses_history_snapshot_without_mutating_it(monkeypatch):
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    before = [dict(item) for item in history]
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="first"))]
        )
    ]
    stream = FakeStream(chunks)
    client = FakeClient(stream)
    monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.6-27b")

    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: history,
        client=client,
    )
    first_token_at = await probe.first_token_at("new question")

    assert isinstance(first_token_at, float)
    assert history == before
    assert client.chat.completions.kwargs["messages"] == [
        {"role": "system", "content": "system"},
        *before,
        {"role": "user", "content": "new question"},
    ]
    assert client.chat.completions.kwargs["extra_body"] == {
        "reasoning_effort": "none"
    }
    assert stream.closed is True
