import base64
import sys
from types import SimpleNamespace

import pytest

from shuo.services.local_malayalam_speech import (
    LocalMalayalamSpeechError,
    LocalMalayalamSpeechService,
)
from shuo.services.local_malayalam_speech_worker import (
    FRAME_BYTES,
    SpeechTurnBuffer,
    format_end_frame,
    format_verify_mulaw_frame,
    format_verify_pcm_frame,
    transcribe_mulaw8,
)


class Recorder:
    def __init__(self):
        self.starts = 0
        self.ends = []

    async def start(self):
        self.starts += 1

    async def end(self, text):
        self.ends.append(text)


@pytest.mark.asyncio
async def test_worker_start_event_maps_to_turn_start():
    rec = Recorder()

    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
    )

    await service._handle_worker_line(
        "START"
    )

    assert rec.starts == 1
    assert rec.ends == []


@pytest.mark.asyncio
async def test_worker_end_event_maps_to_malayalam_transcript():
    rec = Recorder()

    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
    )

    text = "നാളെ മീറ്റിംഗ് റീഷെഡ്യൂൾ ചെയ്യണം"

    encoded = base64.b64encode(
        text.encode("utf-8")
    ).decode("ascii")

    await service._handle_worker_line(
        f"END\t{encoded}"
    )

    assert rec.ends == [text]


@pytest.mark.asyncio
async def test_empty_transcript_is_forwarded_to_state_machine():
    rec = Recorder()

    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
    )

    encoded = base64.b64encode(
        b""
    ).decode("ascii")

    await service._handle_worker_line(
        f"END\t{encoded}"
    )

    assert rec.ends == [""]


@pytest.mark.asyncio
async def test_invalid_worker_message_fails_closed():
    rec = Recorder()

    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
    )

    with pytest.raises(
        LocalMalayalamSpeechError,
        match="Unexpected",
    ):
        await service._handle_worker_line(
            "UNKNOWN"
        )


def test_missing_worker_python_fails_before_process_start(
    tmp_path,
):
    rec = Recorder()

    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
        worker_python=str(
            tmp_path / "missing-python"
        ),
        model_dir=str(tmp_path),
    )

    with pytest.raises(
        LocalMalayalamSpeechError,
        match="Python does not exist",
    ):
        service._validate_runtime()


def test_runtime_requires_explicit_worker_configuration(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv(
        "SHUO_LOCAL_STT_PYTHON",
        raising=False,
    )

    service = LocalMalayalamSpeechService(
        Recorder().end,
        Recorder().start,
        model_dir=str(tmp_path),
    )

    with pytest.raises(
        LocalMalayalamSpeechError,
        match="SHUO_LOCAL_STT_PYTHON",
    ):
        service._validate_runtime()


def test_runtime_requires_explicit_model_configuration(
    monkeypatch,
):
    monkeypatch.delenv(
        "SHUO_MALAYALAM_STT_MODEL_DIR",
        raising=False,
    )

    service = LocalMalayalamSpeechService(
        Recorder().end,
        Recorder().start,
        worker_python=sys.executable,
    )

    with pytest.raises(
        LocalMalayalamSpeechError,
        match="SHUO_MALAYALAM_STT_MODEL_DIR",
    ):
        service._validate_runtime()



def _pcm_frame():
    return b"\x01\x00" * (FRAME_BYTES // 2)


def test_turn_buffer_merges_resume_before_conversational_commit():
    turn = SpeechTurnBuffer(
        preroll_frames=2,
        commit_silence_ms=96,
        start_qualify_ms=32,
        start_min_rms=0,
    )
    frame = _pcm_frame()
    max_bytes = FRAME_BYTES * 100

    assert turn.feed(
        frame,
        None,
        max_speech_bytes=max_bytes,
    ) == (None, None)

    # Acoustic start is tentative. The next frame qualifies the
    # conversational START instead of cancelling on the first VAD edge.
    assert turn.feed(
        frame,
        {"start": 0},
        max_speech_bytes=max_bytes,
    ) == (None, None)

    assert turn.feed(
        frame,
        None,
        max_speech_bytes=max_bytes,
    ) == ("start", None)

    assert turn.feed(
        frame,
        {"end": 0},
        max_speech_bytes=max_bytes,
    ) == (None, None)

    assert turn.feed(
        frame,
        None,
        max_speech_bytes=max_bytes,
    ) == (None, None)

    # Speech resumes inside the commit window. This is the key regression:
    # the same conversational turn continues and no second START is emitted.
    assert turn.feed(
        frame,
        {"start": 0},
        max_speech_bytes=max_bytes,
    ) == (None, None)

    assert turn.feed(
        frame,
        {"end": 0},
        max_speech_bytes=max_bytes,
    ) == (None, None)

    assert turn.feed(
        frame,
        None,
        max_speech_bytes=max_bytes,
    ) == (None, None)
    assert turn.feed(
        frame,
        None,
        max_speech_bytes=max_bytes,
    ) == (None, None)

    action, payload = turn.feed(
        frame,
        None,
        max_speech_bytes=max_bytes,
    )

    assert action == "commit"
    reason, speech = payload
    assert reason == "vad_silence"
    assert len(speech) == FRAME_BYTES * 9


def test_turn_buffer_forced_max_speech_commit_is_immediate():
    turn = SpeechTurnBuffer(
        preroll_frames=1,
        commit_silence_ms=320,
        start_qualify_ms=32,
        start_min_rms=0,
    )
    frame = _pcm_frame()

    assert turn.feed(
        frame,
        {"start": 0},
        max_speech_bytes=FRAME_BYTES * 2,
    ) == (None, None)

    action, payload = turn.feed(
        frame,
        None,
        max_speech_bytes=FRAME_BYTES * 2,
    )

    assert action == "commit"
    reason, speech = payload
    assert reason == "max_speech"
    assert len(speech) == FRAME_BYTES * 2


def test_turn_buffer_requires_duration_and_energy_before_start():
    loud = b"\xd0\x07" * (FRAME_BYTES // 2)  # sample value 2000
    quiet = b"\x01\x00" * (FRAME_BYTES // 2)
    max_bytes = FRAME_BYTES * 100

    turn = SpeechTurnBuffer(
        preroll_frames=1,
        commit_silence_ms=96,
        start_qualify_ms=96,
        start_min_rms=500,
    )

    assert turn.feed(
        quiet,
        {"start": 0},
        max_speech_bytes=max_bytes,
    ) == (None, None)
    assert turn.feed(
        quiet,
        None,
        max_speech_bytes=max_bytes,
    ) == (None, None)
    assert turn.feed(
        quiet,
        None,
        max_speech_bytes=max_bytes,
    ) == (None, None)

    # A rolling 96 ms window becomes energetic enough only after real speech.
    assert turn.feed(
        loud,
        None,
        max_speech_bytes=max_bytes,
    ) == ("start", None)
    assert turn.start_emitted is True
    assert turn.best_start_rms >= 500


def test_turn_buffer_commits_low_energy_segment_without_qualifying_start():
    quiet = b"\x01\x00" * (FRAME_BYTES // 2)
    max_bytes = FRAME_BYTES * 100
    turn = SpeechTurnBuffer(
        preroll_frames=1,
        commit_silence_ms=64,
        start_qualify_ms=64,
        start_min_rms=500,
    )

    assert turn.feed(
        quiet,
        {"start": 0},
        max_speech_bytes=max_bytes,
    ) == (None, None)
    assert turn.feed(
        quiet,
        {"end": 0},
        max_speech_bytes=max_bytes,
    ) == (None, None)
    assert turn.feed(
        quiet,
        None,
        max_speech_bytes=max_bytes,
    ) == (None, None)

    action, payload = turn.feed(
        quiet,
        None,
        max_speech_bytes=max_bytes,
    )
    assert action == "commit"
    assert payload[0] == "vad_silence"
    assert turn.start_emitted is False
    assert turn.best_start_rms < 500


@pytest.mark.asyncio
async def test_worker_end_metadata_is_content_free_and_transcript_still_forwards():
    rec = Recorder()
    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
    )
    text = "നാളെ മീറ്റിങ് ഉണ്ട്"
    encoded = base64.b64encode(
        text.encode("utf-8")
    ).decode("ascii")

    await service._handle_worker_line(
        "END\t"
        + encoded
        + "\tbuffered_audio_ms=1824"
        + "\tasr_ms=91.4"
        + "\tpeak=12000"
        + "\trms=850"
        + "\treason=vad_silence"
    )

    assert rec.ends == [text]


@pytest.mark.asyncio
async def test_worker_end_rejects_unknown_end_reason():
    rec = Recorder()
    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
    )
    encoded = base64.b64encode(b"hello").decode("ascii")

    with pytest.raises(
        LocalMalayalamSpeechError,
        match="invalid end reason",
    ):
        await service._handle_worker_line(
            "END\t"
            + encoded
            + "\tbuffered_audio_ms=320"
            + "\tasr_ms=20.0"
            + "\tpeak=10"
            + "\trms=2"
            + "\treason=unknown"
        )


def test_commit_silence_must_be_positive():
    with pytest.raises(
        ValueError,
        match="commit_silence_ms",
    ):
        LocalMalayalamSpeechService(
            Recorder().end,
            Recorder().start,
            commit_silence_ms=0,
        )



def test_worker_end_frame_builder_executes_runtime_metadata_path():
    speech = b"\x01\x00" * 1600
    text = "നാളെ മീറ്റിങ് ഉണ്ട്"

    frame = format_end_frame(
        text,
        speech,
        asr_ms=87.25,
        reason="vad_silence",
    )

    parts = frame.split("\t")
    assert parts[0] == "END"
    assert base64.b64decode(parts[1]).decode("utf-8") == text
    metadata = dict(field.split("=", 1) for field in parts[2:])
    assert metadata["buffered_audio_ms"] == "100"
    assert int(metadata["peak"]) >= 0
    assert float(metadata["rms"]) >= 0
    assert metadata["asr_ms"] == "87.2"
    assert metadata["reason"] == "vad_silence"
    assert metadata["start_qualified"] == "1"
    assert metadata["start_rms"] == "0"
    assert int(metadata["peak"]) >= 0
    assert float(metadata["rms"]) >= 0



def test_tts_verify_frame_builder_executes_runtime_protocol_path():
    mulaw = b"\xff" * 800
    text = "ഇത് ടെസ്റ്റ് ശബ്ദമാണ്"

    frame = format_verify_mulaw_frame(
        "7",
        text,
        mulaw,
    )

    parts = frame.split("\t")
    assert parts[0] == "VERIFY"
    assert parts[1] == "7"
    assert base64.b64decode(parts[2]).decode("utf-8") == text
    metadata = dict(field.split("=", 1) for field in parts[3:])
    assert metadata["buffered_audio_ms"] == "100"
    assert int(metadata["peak"]) >= 0
    assert float(metadata["rms"]) >= 0
    assert float(metadata["near_full_scale_ratio"]) >= 0


def test_tts_verify_mulaw_path_calls_asr_at_16khz():
    class FakeModel:
        def __init__(self):
            self.sample_rate = None
            self.samples = None

        def recognize(self, samples, *, sample_rate):
            self.sample_rate = sample_rate
            self.samples = samples
            return "വെരിഫൈ"

    model = FakeModel()
    result = transcribe_mulaw8(
        model,
        b"\xff" * 800,
    )

    assert result == "വെരിഫൈ"
    assert model.sample_rate == 16000
    assert len(model.samples) > 800



def test_tts_verify_pcm_frame_reports_post_codec_metrics():
    pcm = b"\x00\x00" * 1600
    text = "പോസ്റ്റ് കോഡെക്"

    frame = format_verify_pcm_frame(
        "8",
        text,
        pcm,
    )

    parts = frame.split("\t")
    assert parts[0] == "VERIFY"
    assert parts[1] == "8"
    assert base64.b64decode(parts[2]).decode("utf-8") == text
    metadata = dict(field.split("=", 1) for field in parts[3:])
    assert metadata["buffered_audio_ms"] == "100"
    assert metadata["peak"] == "0"
    assert metadata["rms"] == "0.0"
    assert metadata["near_full_scale_ratio"] == "0.00000000"


@pytest.mark.asyncio
async def test_unqualified_worker_end_is_not_promoted_to_agent_turn():
    rec = Recorder()
    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
    )
    text = "ആ"
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")

    await service._handle_worker_line(
        "END\t"
        + encoded
        + "\tbuffered_audio_ms=832"
        + "\tasr_ms=30.0"
        + "\tpeak=428"
        + "\trms=45"
        + "\treason=vad_silence"
        + "\tstart_qualified=0"
        + "\tstart_rms=45"
    )

    assert rec.starts == 0
    assert rec.ends == []


@pytest.mark.asyncio
async def test_post_end_guard_suppresses_residual_start_and_matching_end(monkeypatch):
    rec = Recorder()
    now = [10.0]
    monkeypatch.setattr(
        "shuo.services.local_malayalam_speech.time",
        SimpleNamespace(perf_counter=lambda: now[0]),
    )
    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
        post_end_start_guard_ms=200,
    )

    first = base64.b64encode("ഹലോ".encode("utf-8")).decode("ascii")
    await service._handle_worker_line(
        "END\t"
        + first
        + "\tbuffered_audio_ms=1184"
        + "\tasr_ms=80.0"
        + "\tpeak=17000"
        + "\trms=2500"
        + "\treason=vad_silence"
        + "\tstart_qualified=1"
        + "\tstart_rms=2200"
    )
    assert rec.ends == ["ഹലോ"]

    now[0] = 10.050
    await service._handle_worker_line("START")
    assert rec.starts == 0

    now[0] = 10.300
    residual = base64.b64encode("ആ".encode("utf-8")).decode("ascii")
    await service._handle_worker_line(
        "END\t"
        + residual
        + "\tbuffered_audio_ms=900"
        + "\tasr_ms=40.0"
        + "\tpeak=18000"
        + "\trms=1200"
        + "\treason=vad_silence"
        + "\tstart_qualified=1"
        + "\tstart_rms=1100"
    )

    assert rec.ends == ["ഹലോ"]


@pytest.mark.asyncio
async def test_start_after_post_end_guard_is_forwarded(monkeypatch):
    rec = Recorder()
    now = [20.0]
    monkeypatch.setattr(
        "shuo.services.local_malayalam_speech.time",
        SimpleNamespace(perf_counter=lambda: now[0]),
    )
    service = LocalMalayalamSpeechService(
        rec.end,
        rec.start,
        post_end_start_guard_ms=200,
    )

    service._last_worker_end_at = 20.0
    now[0] = 20.250
    await service._handle_worker_line("START")

    assert rec.starts == 1
