import asyncio
from types import SimpleNamespace

import pytest

from shuo.bluetooth.conversation import run_bluetooth_conversation
from shuo.bluetooth.production import BluetoothProductionDeps, run_production_bluetooth_conversation
from shuo.runtime_config import CallSettings
from shuo.services.llm import LLMService, ShadowLLMProbe
from shuo.services.phrase_buffer import BoundedPhraseBuffer
from shuo.services.player import AudioPlayer


def _chunk(text=None, usage=None):
    choices = [] if text is None else [SimpleNamespace(delta=SimpleNamespace(content=text))]
    return SimpleNamespace(choices=choices, usage=usage)


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


class CaptureClient:
    def __init__(self, stream):
        self.stream = stream
        self.kwargs = None
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.stream


def test_phrase_buffer_preserves_exact_text_and_is_bounded():
    buffer = BoundedPhraseBuffer(32)
    tokens = [
        "Hello", ", this is a bounded phrase. ",
        "The next sentence is intentionally longer than the cap", " and ends now!"
    ]
    emitted = []
    for token in tokens:
        emitted.extend(buffer.feed(token))
    tail = buffer.flush()
    if tail:
        emitted.append(tail)

    assert "".join(emitted) == "".join(tokens)
    assert all(len(chunk) <= 32 for chunk in emitted)
    assert len(emitted) < len("".join(tokens))


def test_phrase_buffer_rejects_invalid_capacity():
    with pytest.raises(ValueError):
        BoundedPhraseBuffer(0)


@pytest.mark.asyncio
async def test_llm_context_budget_keeps_system_and_latest_user_but_not_canonical_history(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")
    done = asyncio.Event()

    async def on_token(_token):
        pass

    async def on_done():
        done.set()

    usage = SimpleNamespace(
        prompt_tokens=9, completion_tokens=1, total_tokens=10,
        queue_time=0.010, prompt_time=0.020, completion_time=0.030, total_time=0.050,
        prompt_tokens_details=SimpleNamespace(cached_tokens=2),
    )
    client = CaptureClient(FakeStream([_chunk("ok"), _chunk(usage=usage)]))
    service = LLMService(
        on_token=on_token, on_done=on_done, system_prompt="DIGITAL TWIN FACTS",
        history_max_chars=24, capture_provider_timing=True,
    )
    service._client = client
    service._history = [
        {"role": "user", "content": "old question that is deliberately long"},
        {"role": "assistant", "content": "old answer that is deliberately long"},
    ]

    await service.start("latest question")
    await asyncio.wait_for(done.wait(), 1)

    sent = client.kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "DIGITAL TWIN FACTS"}
    assert sent[-1] == {"role": "user", "content": "latest question"}
    assert all("old question" not in message["content"] for message in sent)
    assert client.kwargs["stream_options"] == {"include_usage": True}
    # Canonical history is retained locally; only the provider-visible prompt is bounded.
    assert service.history[0]["content"].startswith("old question")
    assert service.history[-1] == {"role": "assistant", "content": "ok"}


@pytest.mark.asyncio
async def test_llm_warmup_is_static_history_neutral_and_callback_free(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")
    callbacks = []
    stream = FakeStream([_chunk("pong")])
    client = CaptureClient(stream)

    async def on_token(token):
        callbacks.append(("token", token))

    async def on_done():
        callbacks.append(("done", None))

    service = LLMService(
        on_token=on_token,
        on_done=on_done,
        system_prompt="PRIVATE DIGITAL TWIN FACTS",
    )
    service._client = client
    service._history = [
        {"role": "user", "content": "existing private user turn"},
        {"role": "assistant", "content": "existing private assistant turn"},
    ]
    before = service.history

    assert await service.warmup(timeout_seconds=1.0) is True
    assert client.kwargs["messages"] == [{"role": "user", "content": "ping"}]
    assert client.kwargs["stream"] is True
    assert client.kwargs["max_tokens"] == 1
    assert client.kwargs["temperature"] == 0.7
    assert service.history == before
    assert callbacks == []
    assert stream.closed is True

    first_kwargs = client.kwargs
    assert await service.warmup(timeout_seconds=1.0) is True
    assert client.kwargs is first_kwargs


@pytest.mark.asyncio
async def test_llm_warmup_failure_is_fail_open_and_history_neutral(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")

    class FailingClient:
        async def create(self, **kwargs):
            raise RuntimeError("provider unavailable")

        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )

    async def noop(*_args):
        return None

    service = LLMService(on_token=noop, on_done=noop, system_prompt="facts")
    service._client = FailingClient()
    service._history = [{"role": "user", "content": "kept"}]
    before = service.history

    assert await service.warmup(timeout_seconds=1.0) is False
    assert service.history == before
    assert service._running is False


@pytest.mark.asyncio
async def test_shadow_probe_uses_same_budget_and_usage_opt_in(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")
    stream = FakeStream([_chunk("first")])
    client = CaptureClient(stream)
    history = [
        {"role": "user", "content": "older user message that is too large"},
        {"role": "assistant", "content": "older assistant message that is too large"},
    ]
    probe = ShadowLLMProbe(
        system_prompt="FACTS", history_provider=lambda: history, client=client,
        history_max_chars=16, capture_provider_timing=True,
    )
    prepared = probe.create_prepared("latest")
    prepared.start()
    await prepared.wait_first_token()
    try:
        assert client.kwargs["messages"] == [
            {"role": "system", "content": "FACTS"},
            {"role": "user", "content": "latest"},
        ]
        assert client.kwargs["stream_options"] == {"include_usage": True}
    finally:
        await prepared.cancel()
    assert stream.closed is True


class BlockingSession:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self._wait = asyncio.Event()

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def read(self):
        await self._wait.wait()
        raise AssertionError("unreachable")

    async def write(self, _audio):
        pass

    async def clear(self):
        pass


class ParallelFlux:
    def __init__(self, *callbacks, flux_entered, agent_entered):
        self.flux_entered = flux_entered
        self.agent_entered = agent_entered
        self.stopped = 0

    async def start(self):
        self.flux_entered.set()
        await self.agent_entered.wait()

    async def stop(self):
        self.stopped += 1

    async def send(self, _audio):
        pass


class MinimalAgent:
    def __init__(self):
        self.cleaned = 0

    @property
    def history(self):
        return []

    async def start_turn(self, _transcript, prepared_response=None):
        pass

    async def cancel_turn(self):
        pass

    async def cleanup(self):
        self.cleaned += 1


@pytest.mark.asyncio
async def test_parallel_startup_allows_independent_flux_and_agent_warmups():
    session = BlockingSession()
    flux_entered = asyncio.Event()
    agent_entered = asyncio.Event()
    ready = asyncio.Event()
    holder = {}

    def flux_factory(*callbacks):
        holder["flux"] = ParallelFlux(
            *callbacks, flux_entered=flux_entered, agent_entered=agent_entered
        )
        return holder["flux"]

    async def agent_factory(_outbound, _done):
        agent_entered.set()
        await flux_entered.wait()
        holder["agent"] = MinimalAgent()
        ready.set()
        return holder["agent"]

    task = asyncio.create_task(run_bluetooth_conversation(
        session, flux_factory=flux_factory, agent_factory=agent_factory,
        parallel_service_startup=True,
    ))
    await asyncio.wait_for(ready.wait(), 1)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.started == 1
    assert session.stopped == 1
    assert holder["flux"].stopped == 1
    assert holder["agent"].cleaned == 1


@pytest.mark.asyncio
async def test_parallel_startup_cancels_sibling_on_failure():
    session = BlockingSession()
    agent_cancelled = asyncio.Event()

    class FailingFlux:
        async def start(self):
            raise RuntimeError("flux boom")
        async def stop(self):
            pass
        async def send(self, _audio):
            pass

    def flux_factory(*_callbacks):
        return FailingFlux()

    async def agent_factory(_outbound, _done):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            agent_cancelled.set()
            raise

    with pytest.raises(RuntimeError, match="flux boom"):
        await run_bluetooth_conversation(
            session, flux_factory=flux_factory, agent_factory=agent_factory,
            parallel_service_startup=True,
        )

    assert agent_cancelled.is_set()
    assert session.stopped == 1


class PlayerSession:
    async def play_audio(self, _audio):
        pass
    async def checkpoint(self, _name):
        pass
    async def clear_audio(self):
        pass


def test_player_preroll_is_limited_to_rules_c5_values():
    player = AudioPlayer(PlayerSession(), preroll_frames=2)
    assert player.preroll_frames == 2
    with pytest.raises(ValueError):
        AudioPlayer(PlayerSession(), preroll_frames=1)
    with pytest.raises(ValueError):
        AudioPlayer(PlayerSession(), preroll_frames=4)


class CapturePool:
    def __init__(self, pool_size, ttl, voice_id):
        self.started = False
    async def start(self):
        self.started = True
    async def wait_ready(self):
        assert self.started
    async def stop(self):
        pass


class CaptureAgent:
    captured = None
    warmup_calls = 0

    def __init__(self, **kwargs):
        CaptureAgent.captured = kwargs
        self.history = []

    async def warmup_llm(self):
        CaptureAgent.warmup_calls += 1
        return True


class CaptureProbe:
    captured = None
    def __init__(self, **kwargs):
        CaptureProbe.captured = kwargs


class CaptureTracer:
    def save(self, _call_id):
        pass


def _settings():
    return CallSettings(
        system_prompt="facts", voice_id="voice", prompt_source="test",
        voice_source="test", rules_chars=0, knowledge_chars=0,
    )


@pytest.mark.asyncio
async def test_phase4d_options_are_explicitly_wired_only_on_bluetooth_path():
    captured = {}
    CaptureAgent.warmup_calls = 0

    async def runner(session, *, agent_factory, speculation_factory, **kwargs):
        captured.update(kwargs)
        agent = await agent_factory(object(), lambda _checkpoint: None)
        speculation_factory(agent)

    await run_production_bluetooth_conversation(
        object(), settings=_settings(), eager_eot_threshold=0.3,
        shadow_speculation=True, tts_phrase_chars=48, llm_history_max_chars=4096,
        llm_provider_timing=True, llm_warmup=True, parallel_startup=True,
        player_preroll_frames=2,
        deps=BluetoothProductionDeps(
            tts_pool_cls=CapturePool, agent_cls=CaptureAgent,
            shadow_probe_cls=CaptureProbe, tracer_factory=CaptureTracer,
            conversation_runner=runner,
        ),
    )

    assert captured["parallel_service_startup"] is True
    assert CaptureAgent.captured["tts_phrase_chars"] == 48
    assert CaptureAgent.captured["llm_history_max_chars"] == 4096
    assert CaptureAgent.captured["llm_provider_timing"] is True
    assert CaptureAgent.warmup_calls == 1
    assert CaptureAgent.captured["player_preroll_frames"] == 2
    assert CaptureProbe.captured["history_max_chars"] == 4096
    assert CaptureProbe.captured["capture_provider_timing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"tts_phrase_chars": 23}, "tts_phrase_chars"),
        ({"tts_phrase_chars": 161}, "tts_phrase_chars"),
        ({"llm_history_max_chars": 0}, "llm_history_max_chars"),
        ({"player_preroll_frames": 1}, "player_preroll_frames"),
    ],
)
async def test_phase4d_invalid_options_fail_before_resources(kwargs, message):
    with pytest.raises(ValueError, match=message):
        await run_production_bluetooth_conversation(object(), **kwargs)
