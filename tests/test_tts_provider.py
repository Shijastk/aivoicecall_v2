import asyncio
import io
import wave

import pytest

from shuo.services.tts_espeak import EspeakTTSService, wav_to_mulaw_8k
from shuo.services.tts_provider import (
    FallbackTTSService,
    build_tts_service,
    espeak_requested,
    tts_required_env_vars,
    validate_tts_provider_config,
)


class FakePrimary:
    instances = []

    def __init__(self, on_audio, on_done, voice_id=None):
        self._on_audio = on_audio
        self._on_done = on_done
        self.voice_id = voice_id
        self.is_active = False
        self.fatal_error = None
        self.warm_idle_started_at = None
        self.sent = []
        self.flushes = 0
        self.cancelled = 0
        self.start_error = None
        FakePrimary.instances.append(self)

    def bind(self, on_audio, on_done):
        self._on_audio = on_audio
        self._on_done = on_done

    async def start(self):
        if self.start_error:
            raise self.start_error
        self.is_active = True
        self.warm_idle_started_at = 1.0

    async def send(self, text):
        self.sent.append(text)

    async def flush(self):
        self.flushes += 1

    async def cancel(self):
        self.cancelled += 1
        self.is_active = False

    async def emit_audio(self, audio="primary-audio"):
        await self._on_audio(audio)

    async def finish(self, fatal_error=None):
        self.fatal_error = fatal_error
        self.is_active = False
        await self._on_done()


class FakeEspeak:
    instances = []

    def __init__(self, on_audio, on_done, voice_id=None):
        self._on_audio = on_audio
        self._on_done = on_done
        self.voice_id = voice_id
        self.is_active = False
        self.fatal_error = None
        self.warm_idle_started_at = None
        self.sent = []
        self.flushes = 0
        self.cancelled = 0
        FakeEspeak.instances.append(self)

    def bind(self, on_audio, on_done):
        self._on_audio = on_audio
        self._on_done = on_done

    async def start(self):
        self.is_active = True
        self.warm_idle_started_at = 2.0

    async def send(self, text):
        self.sent.append(text)
        await self._on_audio(f"fallback:{text}")

    async def flush(self):
        self.flushes += 1
        self.is_active = False
        await self._on_done()

    async def cancel(self):
        self.cancelled += 1
        self.is_active = False


@pytest.fixture(autouse=True)
def clean_tts_env(monkeypatch):
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    monkeypatch.delenv("TTS_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    FakePrimary.instances.clear()
    FakeEspeak.instances.clear()


def test_default_provider_preserves_elevenlabs(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    service = build_tts_service(
        lambda audio: asyncio.sleep(0),
        lambda: asyncio.sleep(0),
        voice_id="voice-1",
        elevenlabs_cls=FakePrimary,
        espeak_cls=FakeEspeak,
    )

    assert isinstance(service, FakePrimary)
    assert service.voice_id == "voice-1"
    assert FakeEspeak.instances == []


def test_direct_espeak_never_constructs_elevenlabs(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "espeak")

    service = build_tts_service(
        lambda audio: asyncio.sleep(0),
        lambda: asyncio.sleep(0),
        voice_id="ignored",
        elevenlabs_cls=FakePrimary,
        espeak_cls=FakeEspeak,
    )

    assert isinstance(service, FakeEspeak)
    assert FakePrimary.instances == []
    assert tts_required_env_vars() == ()
    assert espeak_requested()


def test_configured_fallback_without_key_bypasses_elevenlabs(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("TTS_FALLBACK_PROVIDER", "espeak")

    service = build_tts_service(
        lambda audio: asyncio.sleep(0),
        lambda: asyncio.sleep(0),
        voice_id="voice-1",
        elevenlabs_cls=FakePrimary,
        espeak_cls=FakeEspeak,
    )

    assert isinstance(service, FakeEspeak)
    assert FakePrimary.instances == []
    assert tts_required_env_vars() == ()


def test_elevenlabs_without_fallback_still_requires_key(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "elevenlabs")
    assert tts_required_env_vars() == ("ELEVENLABS_API_KEY",)


@pytest.mark.asyncio
async def test_zero_audio_primary_failure_replays_only_unheard_text_to_espeak():
    heard = []
    done = []

    async def on_audio(audio):
        heard.append(audio)

    async def on_done():
        done.append(True)

    service = FallbackTTSService(
        on_audio,
        on_done,
        voice_id="voice-1",
        primary_cls=FakePrimary,
        fallback_cls=FakeEspeak,
    )
    await service.start()

    await service.send("Hello ")
    await service.send("world.")
    await service.flush()
    await service._primary.finish("quota_exceeded")

    assert service._fallback.sent == ["Hello world."]
    assert heard == ["fallback:Hello world."]
    assert service._fallback.flushes == 1
    assert done == [True]


@pytest.mark.asyncio
async def test_primary_audio_disables_same_turn_replay():
    heard = []
    done = []

    async def on_audio(audio):
        heard.append(audio)

    async def on_done():
        done.append(True)

    service = FallbackTTSService(
        on_audio,
        on_done,
        voice_id="voice-1",
        primary_cls=FakePrimary,
        fallback_cls=FakeEspeak,
    )
    await service.start()

    await service.send("This text starts on ElevenLabs.")
    await service._primary.emit_audio()
    await service._primary.finish("provider_closed")

    assert heard == ["primary-audio"]
    assert service._fallback.sent == []
    assert service._fallback.cancelled == 1
    assert done == [True]


@pytest.mark.asyncio
async def test_primary_start_failure_uses_already_validated_fallback():
    heard = []

    async def on_audio(audio):
        heard.append(audio)

    async def on_done():
        pass

    class StartFailPrimary(FakePrimary):
        async def start(self):
            self.is_active = True
            raise RuntimeError("provider unavailable")

    service = FallbackTTSService(
        on_audio,
        on_done,
        voice_id="voice-1",
        primary_cls=StartFailPrimary,
        fallback_cls=FakeEspeak,
    )

    await service.start()
    await service.send("Fallback works.")

    assert service._using_fallback
    assert heard == ["fallback:Fallback works."]
    assert service._primary.cancelled == 1


def test_invalid_provider_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "unknown")
    assert "Unsupported TTS_PROVIDER" in validate_tts_provider_config()


def test_wav_conversion_emits_exact_mulaw_8k_geometry():
    # 20 ms of mono PCM16 at 8 kHz -> exactly 160 mu-law bytes.
    pcm = b"\x00\x00" * 160
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8_000)
        writer.writeframes(pcm)

    mulaw = wav_to_mulaw_8k(buf.getvalue())

    assert len(mulaw) == 160


@pytest.mark.asyncio
async def test_espeak_missing_binary_fails_before_turn(monkeypatch):
    monkeypatch.setattr("shuo.services.tts_espeak.find_espeak_executable", lambda: None)

    service = EspeakTTSService(
        lambda audio: asyncio.sleep(0),
        lambda: asyncio.sleep(0),
    )

    with pytest.raises(RuntimeError, match="no 'espeak-ng'"):
        await service.start()
