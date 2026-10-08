import asyncio
import base64
import json

import pytest

from shuo.services.tts_sarvam import (
    SARVAM_CLONE_WS_URL,
    SarvamVoiceCloneTTSService,
)


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True

    def feed(self, payload):
        self.incoming.put_nowait(json.dumps(payload))


@pytest.fixture
def sarvam_env(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")
    monkeypatch.setenv("SARVAM_VOICE_ID", "svc-test-voice")
    monkeypatch.setenv("SARVAM_TTS_LANGUAGE_CODE", "ml-IN")
    monkeypatch.delenv("SARVAM_TTS_PACE", raising=False)
    monkeypatch.delenv("SARVAM_TTS_MIN_BUFFER_SIZE", raising=False)
    monkeypatch.delenv("SARVAM_TTS_MAX_CHUNK_LENGTH", raising=False)


@pytest.mark.asyncio
async def test_sarvam_streams_native_mulaw_8k_without_transcoding(
    sarvam_env,
):
    socket = FakeSocket()
    connect_calls = []
    heard = []
    done = asyncio.Event()

    async def connect(url, **kwargs):
        connect_calls.append((url, kwargs))
        return socket

    async def on_audio(audio):
        heard.append(base64.b64decode(audio))

    async def on_done():
        done.set()

    service = SarvamVoiceCloneTTSService(
        on_audio,
        on_done,
        connect=connect,
    )
    await service.start()

    assert connect_calls == [
        (
            SARVAM_CLONE_WS_URL,
            {"subprotocols": ["api-subscription-key.test-key"]},
        )
    ]
    assert socket.sent[0] == {
        "type": "config",
        "data": {
            "target_language_code": "ml-IN",
            "voice_id": "svc-test-voice",
            "output_audio_codec": "mulaw",
            "speech_sample_rate": 8000,
            "pace": 1.0,
            "min_buffer_size": 30,
            "max_chunk_length": 200,
        },
    }

    await service.send("ഹലോ ")
    await service.send("client")
    await service.flush()

    assert socket.sent[1:] == [
        {"type": "text", "data": {"text": "ഹലോ "}},
        {"type": "text", "data": {"text": "client"}},
        {"type": "flush"},
    ]

    encoded = base64.b64encode(b"mulaw-bytes").decode("ascii")
    socket.feed(
        {
            "type": "audio",
            "data": {
                "content_type": "audio/basic",
                "audio": encoded,
            },
        }
    )
    socket.feed(
        {
            "type": "event",
            "data": {"event_type": "final"},
        }
    )

    await asyncio.wait_for(done.wait(), timeout=1)
    assert heard == [b"mulaw-bytes"]
    assert socket.closed
    assert not service.is_active
    assert service.fatal_error is None


@pytest.mark.asyncio
async def test_sarvam_provider_error_finishes_turn_without_hanging(
    sarvam_env,
):
    socket = FakeSocket()
    done = asyncio.Event()

    async def connect(_url, **_kwargs):
        return socket

    async def on_done():
        done.set()

    service = SarvamVoiceCloneTTSService(
        lambda _audio: asyncio.sleep(0),
        on_done,
        connect=connect,
    )
    await service.start()

    socket.feed(
        {
            "type": "error",
            "data": {
                "code": "bad_config",
                "message": "provider rejected config",
            },
        }
    )

    await asyncio.wait_for(done.wait(), timeout=1)
    assert "provider rejected config" in service.fatal_error
    assert not service.is_active
    assert socket.closed


@pytest.mark.asyncio
async def test_sarvam_invalid_audio_is_fatal_and_completes(
    sarvam_env,
):
    socket = FakeSocket()
    done = asyncio.Event()

    async def connect(_url, **_kwargs):
        return socket

    async def on_done():
        done.set()

    service = SarvamVoiceCloneTTSService(
        lambda _audio: asyncio.sleep(0),
        on_done,
        connect=connect,
    )
    await service.start()
    socket.feed(
        {
            "type": "audio",
            "data": {"audio": "not valid base64%%%"},
        }
    )

    await asyncio.wait_for(done.wait(), timeout=1)
    assert "invalid base64 audio" in service.fatal_error
    assert socket.closed


@pytest.mark.asyncio
async def test_sarvam_missing_voice_fails_before_network(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")
    monkeypatch.delenv("SARVAM_VOICE_ID", raising=False)
    connected = False

    async def connect(_url, **_kwargs):
        nonlocal connected
        connected = True
        return FakeSocket()

    service = SarvamVoiceCloneTTSService(
        lambda _audio: asyncio.sleep(0),
        lambda: asyncio.sleep(0),
        connect=connect,
    )

    with pytest.raises(RuntimeError, match="SARVAM_VOICE_ID"):
        await service.start()
    assert not connected


def test_sarvam_tuning_limits_fail_fast(sarvam_env, monkeypatch):
    monkeypatch.setenv("SARVAM_TTS_MIN_BUFFER_SIZE", "29")
    with pytest.raises(RuntimeError, match="SARVAM_TTS_MIN_BUFFER_SIZE"):
        SarvamVoiceCloneTTSService(
            lambda _audio: asyncio.sleep(0),
            lambda: asyncio.sleep(0),
        )
