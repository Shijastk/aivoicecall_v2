import asyncio

import pytest

from shuo.services.tts_provider import FallbackTTSService, _MAX_REPLAY_CHARS


class PrimaryProbe:
    instances = []

    def __init__(self, on_audio, on_done, voice_id=None):
        self._on_audio = on_audio
        self._on_done = on_done
        self.voice_id = voice_id
        self.is_active = False
        self.fatal_error = None
        self.warm_idle_started_at = None
        self.sent = []
        self.cancelled = 0
        PrimaryProbe.instances.append(self)

    def bind(self, on_audio, on_done):
        self._on_audio = on_audio
        self._on_done = on_done

    async def start(self):
        self.is_active = True

    async def send(self, text):
        # The wrapper must call this during each send(), not wait for the
        # response or flush boundary. A4 protects this streaming seam.
        self.sent.append(text)

    async def flush(self):
        pass

    async def cancel(self):
        self.cancelled += 1
        self.is_active = False


class FallbackProbe:
    instances = []

    def __init__(self, on_audio, on_done, voice_id=None):
        self._on_audio = on_audio
        self._on_done = on_done
        self.is_active = False
        self.fatal_error = None
        self.warm_idle_started_at = None
        self.sent = []
        FallbackProbe.instances.append(self)

    def bind(self, on_audio, on_done):
        self._on_audio = on_audio
        self._on_done = on_done

    async def start(self):
        self.is_active = True

    async def send(self, text):
        self.sent.append(text)

    async def flush(self):
        await self._on_done()

    async def cancel(self):
        self.is_active = False


@pytest.fixture(autouse=True)
def clear_instances():
    PrimaryProbe.instances.clear()
    FallbackProbe.instances.clear()


async def _noop_audio(_audio):
    pass


async def _noop_done():
    pass


@pytest.mark.asyncio
async def test_pre_audio_shadow_never_gates_primary_streaming():
    service = FallbackTTSService(
        _noop_audio,
        _noop_done,
        voice_id="voice-1",
        primary_cls=PrimaryProbe,
        fallback_cls=FallbackProbe,
    )
    await service.start()

    await service.send("first ")
    assert service._primary.sent == ["first "]
    assert service._replay_text == "first "

    await service.send("second")
    assert service._primary.sent == ["first ", "second"]
    assert service._replay_text == "first second"
    assert service._fallback.sent == []

    await service.cancel()


@pytest.mark.asyncio
async def test_shadow_window_is_bounded_and_overflow_does_not_hold_primary_text():
    service = FallbackTTSService(
        _noop_audio,
        _noop_done,
        voice_id="voice-1",
        primary_cls=PrimaryProbe,
        fallback_cls=FallbackProbe,
    )
    await service.start()

    prefix = "a" * _MAX_REPLAY_CHARS
    overflow = "b"

    await service.send(prefix)
    assert service._primary.sent == [prefix]
    assert len(service._replay_text) == _MAX_REPLAY_CHARS

    await service.send(overflow)

    # Overflow disables same-turn replay instead of retaining unbounded text,
    # but the primary streaming path still receives the token immediately.
    assert service._primary.sent == [prefix, overflow]
    assert service._replay_text == ""
    assert service._replay_disabled is True
    assert service._fallback.sent == []

    await service.cancel()
