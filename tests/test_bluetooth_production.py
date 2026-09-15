import asyncio
from dataclasses import dataclass

import pytest

from shuo.bluetooth.production import (
    BluetoothProductionDeps,
    run_production_bluetooth_conversation,
)
from shuo.runtime_config import CallSettings
from shuo.services.tts_pool import TTSPool


class FakeTracer:
    def __init__(self):
        self.saved = []

    def save(self, call_id):
        self.saved.append(call_id)


class FakePool:
    instances = []

    def __init__(self, pool_size, ttl, voice_id):
        self.pool_size = pool_size
        self.ttl = ttl
        self.voice_id = voice_id
        self.started = 0
        self.stopped = 0
        FakePool.instances.append(self)

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def wait_ready(self):
        assert self.started == 1


class FakeFlux:
    instances = []

    def __init__(self, on_end_of_turn, on_start_of_turn, on_interim):
        self.on_end_of_turn = on_end_of_turn
        self.on_start_of_turn = on_start_of_turn
        self.on_interim = on_interim
        FakeFlux.instances.append(self)


class EagerFakeFlux(FakeFlux):
    def __init__(
        self,
        on_end_of_turn,
        on_start_of_turn,
        on_interim,
        eager_eot_threshold,
    ):
        super().__init__(on_end_of_turn, on_start_of_turn, on_interim)
        self.eager_eot_threshold = eager_eot_threshold


class FakeAgent:
    instances = []

    def __init__(
        self,
        session,
        on_done,
        tts_pool,
        tracer,
        persona_id,
        settings,
    ):
        self.session = session
        self.on_done = on_done
        self.tts_pool = tts_pool
        self.tracer = tracer
        self.persona_id = persona_id
        self.settings = settings
        FakeAgent.instances.append(self)


class DummySession:
    pass


def settings():
    return CallSettings(
        system_prompt="test prompt",
        voice_id="voice-123",
        prompt_source="test",
        voice_source="test",
        rules_chars=0,
        knowledge_chars=0,
    )


@pytest.mark.asyncio
async def test_production_wiring_starts_pool_only_when_agent_is_built_and_stops_it():
    FakePool.instances.clear()
    FakeAgent.instances.clear()
    FakeFlux.instances.clear()
    tracer = FakeTracer()
    captured = {}

    async def fake_runner(
        session,
        *,
        flux_factory,
        agent_factory,
        stream_id,
        call_id,
    ):
        captured["flux"] = flux_factory(
            lambda text: asyncio.sleep(0),
            lambda: asyncio.sleep(0),
            lambda text: asyncio.sleep(0),
        )
        assert FakePool.instances[0].started == 0

        agent = await agent_factory(object(), lambda checkpoint: None)
        captured["agent"] = agent
        assert FakePool.instances[0].started == 1

    deps = BluetoothProductionDeps(
        flux_cls=FakeFlux,
        tts_pool_cls=FakePool,
        agent_cls=FakeAgent,
        tracer_factory=lambda: tracer,
        settings_loader=settings,
        conversation_runner=fake_runner,
    )

    await run_production_bluetooth_conversation(
        DummySession(),
        persona_id="candidate",
        call_id="bt-test",
        deps=deps,
    )

    pool = FakePool.instances[0]
    agent = FakeAgent.instances[0]

    assert pool.voice_id == "voice-123"
    assert pool.started == 1
    assert pool.stopped == 1
    assert agent.tts_pool is pool
    assert agent.persona_id == "candidate"
    assert agent.settings.system_prompt == "test prompt"
    assert tracer.saved == ["bt-test"]


@pytest.mark.asyncio
async def test_eager_threshold_is_forwarded_only_on_explicit_measurement_path():
    FakePool.instances.clear()
    FakeAgent.instances.clear()
    FakeFlux.instances.clear()
    captured = {}

    async def fake_runner(
        session,
        *,
        flux_factory,
        agent_factory,
        stream_id,
        call_id,
    ):
        captured["flux"] = flux_factory(
            lambda text: asyncio.sleep(0),
            lambda: asyncio.sleep(0),
            lambda text: asyncio.sleep(0),
        )

    deps = BluetoothProductionDeps(
        flux_cls=EagerFakeFlux,
        tts_pool_cls=FakePool,
        agent_cls=FakeAgent,
        tracer_factory=FakeTracer,
        settings_loader=settings,
        conversation_runner=fake_runner,
    )

    await run_production_bluetooth_conversation(
        DummySession(),
        eager_eot_threshold=0.4,
        deps=deps,
    )

    assert captured["flux"].eager_eot_threshold == 0.4
    assert FakePool.instances[0].started == 0


@pytest.mark.asyncio
async def test_runner_failure_before_agent_creation_does_not_start_tts_pool():
    FakePool.instances.clear()
    tracer = FakeTracer()

    async def failing_runner(*args, **kwargs):
        raise RuntimeError("bluetooth start failed")

    deps = BluetoothProductionDeps(
        flux_cls=FakeFlux,
        tts_pool_cls=FakePool,
        agent_cls=FakeAgent,
        tracer_factory=lambda: tracer,
        settings_loader=settings,
        conversation_runner=failing_runner,
    )

    with pytest.raises(RuntimeError, match="bluetooth start failed"):
        await run_production_bluetooth_conversation(
            DummySession(),
            call_id="bt-fail",
            deps=deps,
        )

    pool = FakePool.instances[0]
    assert pool.started == 0
    assert pool.stopped == 0
    assert tracer.saved == ["bt-fail"]


@pytest.mark.asyncio
async def test_explicit_settings_snapshot_skips_settings_loader():
    FakePool.instances.clear()
    calls = {"settings": 0}

    def forbidden_loader():
        calls["settings"] += 1
        raise AssertionError("settings loader must not run")

    async def fake_runner(
        session,
        *,
        flux_factory,
        agent_factory,
        stream_id,
        call_id,
    ):
        await agent_factory(object(), lambda checkpoint: None)

    deps = BluetoothProductionDeps(
        flux_cls=FakeFlux,
        tts_pool_cls=FakePool,
        agent_cls=FakeAgent,
        tracer_factory=FakeTracer,
        settings_loader=forbidden_loader,
        conversation_runner=fake_runner,
    )

    await run_production_bluetooth_conversation(
        DummySession(),
        settings=settings(),
        deps=deps,
    )

    assert calls["settings"] == 0
    assert FakePool.instances[0].voice_id == "voice-123"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "cancel", "retry"])
async def test_agent_creation_waits_for_real_pool_readiness(monkeypatch, outcome):
    entered = asyncio.Event()
    release = asyncio.Event()
    instances = []
    built = []
    tracer = FakeTracer()

    class DelayedTTS:
        is_active = True
        fatal_error = None

        def __init__(self, on_audio, on_done, voice_id):
            self.voice_id = voice_id
            self.cancelled = False
            instances.append(self)

        async def start(self):
            entered.set()
            await release.wait()
            if outcome == "retry" and len(instances) == 1:
                raise RuntimeError("test provider unavailable")

        def bind(self, on_audio, on_done):
            pass

        async def cancel(self):
            self.cancelled = True

    class FirstTurnAgent(FakeAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            built.append(self)

    async def runner(session, *, agent_factory, **kwargs):
        agent = await agent_factory(object(), lambda checkpoint: None)
        # Same first TTS acquisition used by Agent.start_turn().
        got = await agent.tts_pool.get(None, None)
        assert got is instances[1 if outcome == "retry" else 0]
        assert got.voice_id == "voice-123"
        await got.cancel()

    monkeypatch.setattr("shuo.services.tts_pool.TTSService", DelayedTTS)
    deps = BluetoothProductionDeps(
        tts_pool_cls=TTSPool,
        agent_cls=FirstTurnAgent,
        tracer_factory=lambda: tracer,
        settings_loader=settings,
        conversation_runner=runner,
    )
    task = asyncio.create_task(run_production_bluetooth_conversation(
        DummySession(), call_id="startup-test", deps=deps,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert built == []
        assert len(instances) == 1
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await asyncio.wait_for(task, 3)
            assert len(built) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert all(tts.cancelled for tts in instances)
    assert tracer.saved == ["startup-test"]
