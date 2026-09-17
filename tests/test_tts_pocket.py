import asyncio
import base64
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from shuo.services.tts_pocket import (
    PocketTTSService,
    float_audio_to_mulaw_8k,
)


class FakeRuntime:
    def __init__(self, chunks=3):
        self.sample_rate = 24_000
        self.chunks = chunks
        self.loaded = []
        self.texts = []

    def ensure_loaded(self, voice_source):
        self.loaded.append(voice_source)

    def stream(self, voice_source, text, cancel_event):
        self.texts.append(text)
        for _ in range(self.chunks):
            if cancel_event.is_set():
                return
            yield np.full(2400, 0.1, dtype=np.float32)


class CooperativeBlockingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__(chunks=0)
        self.first_chunk = threading.Event()
        self.stopped = threading.Event()

    def stream(self, voice_source, text, cancel_event):
        self.texts.append(text)
        self.first_chunk.set()
        yield np.full(2400, 0.1, dtype=np.float32)
        while not cancel_event.is_set():
            time.sleep(0.005)
        self.stopped.set()


async def _noop_done():
    pass


def test_float_audio_conversion_has_mulaw_8k_duration_geometry():
    # 20ms at 24 kHz -> 20ms at 8 kHz -> 160 mu-law bytes.
    samples = np.zeros(480, dtype=np.float32)
    mulaw, state = float_audio_to_mulaw_8k(samples, 24_000)
    assert len(mulaw) == 160
    assert state is not None


def test_pocket_profile_installs_audioop_lts_for_python_313_plus():
    profile = (
        Path(__file__).resolve().parents[1] / "requirements-pocket-tts.txt"
    ).read_text(encoding="utf-8")
    assert 'audioop-lts==0.2.2; python_version >= "3.13"' in profile


@pytest.mark.asyncio
async def test_pocket_streams_native_chunks_without_writing_a_full_response():
    heard = []
    done = []
    runtime = FakeRuntime(chunks=3)

    async def on_audio(audio):
        heard.append(base64.b64decode(audio))

    async def on_done():
        done.append(True)

    service = PocketTTSService(
        on_audio,
        on_done,
        runtime=runtime,
        voice_source="test-voice",
    )
    await service.start()

    # More than PHRASE_CHARS forces one bounded phrase before flush; the
    # remainder stays bounded rather than waiting for the complete response.
    text = "This is a deliberately bounded streaming phrase for Pocket TTS."
    await service.send(text)
    assert heard, "first bounded phrase produced no streaming audio"
    assert service.is_active

    await service.flush()

    assert "".join(runtime.texts) == text
    assert len(heard) == len(runtime.texts) * runtime.chunks
    assert all(chunk for chunk in heard)
    assert done == [True]
    assert service.fatal_error is None


@pytest.mark.asyncio
async def test_pocket_cancel_stops_cooperative_inference_and_suppresses_late_audio():
    heard = []
    runtime = CooperativeBlockingRuntime()

    async def on_audio(audio):
        heard.append(base64.b64decode(audio))

    service = PocketTTSService(
        on_audio,
        _noop_done,
        runtime=runtime,
        voice_source="test-voice",
    )
    await service.start()

    send_task = asyncio.create_task(
        service.send("This sentence is long enough to trigger synthesis immediately.")
    )

    for _ in range(100):
        if heard:
            break
        await asyncio.sleep(0.01)
    assert heard

    before_cancel = len(heard)
    await asyncio.wait_for(service.cancel(), timeout=1.5)
    await asyncio.wait_for(send_task, timeout=1.5)

    assert runtime.stopped.wait(0.2)
    assert not service.is_active
    assert len(heard) == before_cancel


@pytest.mark.asyncio
async def test_pocket_missing_optional_package_fails_before_turn(monkeypatch):
    monkeypatch.setattr(
        "shuo.services.tts_pocket.pocket_tts_available",
        lambda: False,
    )
    service = PocketTTSService(
        lambda audio: asyncio.sleep(0),
        _noop_done,
    )

    with pytest.raises(RuntimeError, match="requirements-pocket-tts.txt"):
        await service.start()


def test_pocket_rejects_invalid_sample_rate():
    with pytest.raises(RuntimeError, match="invalid Pocket TTS sample rate"):
        float_audio_to_mulaw_8k(np.zeros(1, dtype=np.float32), 0)
