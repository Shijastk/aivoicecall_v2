import asyncio
import base64
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from shuo.services.tts_pocket import (
    PocketTTSService,
    _PocketRuntime,
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


class _LoadedPocketModel:
    sample_rate = 24_000

    def get_state_for_audio_prompt(self, voice_source):
        return {"voice": voice_source}


async def _noop_done():
    pass


def _install_fake_pocket_module(monkeypatch, load_model):
    module = types.ModuleType("pocket_tts")

    class FakeTTSModel:
        @classmethod
        def load_model(cls):
            return load_model()

    module.TTSModel = FakeTTSModel
    monkeypatch.setitem(sys.modules, "pocket_tts", module)
    monkeypatch.setattr(
        "shuo.services.tts_pocket.pocket_tts_available",
        lambda: True,
    )


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


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, "1"), ("0", "0")],
)
def test_pocket_hf_cache_mode_defaults_offline_but_respects_explicit_override(
    monkeypatch,
    configured,
    expected,
):
    if configured is None:
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    else:
        monkeypatch.setenv("HF_HUB_OFFLINE", configured)

    observed = []

    def load_model():
        observed.append(__import__("os").environ.get("HF_HUB_OFFLINE"))
        return _LoadedPocketModel()

    _install_fake_pocket_module(monkeypatch, load_model)
    runtime = _PocketRuntime()
    runtime.ensure_loaded("alba")

    assert observed == [expected]
    assert runtime.sample_rate == 24_000


def test_pocket_cache_only_load_failure_explains_one_time_online_preload(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    def load_model():
        raise RuntimeError("cache miss")

    _install_fake_pocket_module(monkeypatch, load_model)
    runtime = _PocketRuntime()

    with pytest.raises(RuntimeError, match="authorized online preload"):
        runtime.ensure_loaded("alba")

    assert __import__("os").environ["HF_HUB_OFFLINE"] == "1"


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
