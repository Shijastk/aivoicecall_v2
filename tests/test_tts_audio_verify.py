import pytest

from shuo.bluetooth.tts_audio_verify import (
    OutboundAudioTranscriptCapture,
)


def test_tts_audio_capture_finalizes_checkpoint_and_clear_boundaries():
    capture = OutboundAudioTranscriptCapture(
        max_segment_bytes=1000,
        max_segments=4,
    )

    capture.on_dispatched_audio(b"A" * 160)
    capture.on_checkpoint("turn-1")
    capture.on_dispatched_audio(b"B" * 320)
    capture.on_clear()

    assert capture.segment_count == 2
    assert capture._segments[0].checkpoint == "turn-1"
    assert capture._segments[0].cancelled is False
    assert bytes(capture._segments[0].audio) == b"A" * 160
    assert capture._segments[1].checkpoint == ""
    assert capture._segments[1].cancelled is True
    assert bytes(capture._segments[1].audio) == b"B" * 320


def test_tts_audio_capture_is_bounded_and_marks_truncation():
    capture = OutboundAudioTranscriptCapture(
        max_segment_bytes=160,
        max_segments=2,
    )

    capture.on_dispatched_audio(b"A" * 320)
    capture.on_checkpoint("turn-1")

    assert capture.segment_count == 1
    segment = capture._segments[0]
    assert len(segment.audio) == 160
    assert segment.truncated is True


@pytest.mark.asyncio
async def test_tts_audio_capture_requires_explicit_local_runtime(tmp_path):
    capture = OutboundAudioTranscriptCapture()
    capture.on_dispatched_audio(b"A" * 160)
    capture.on_checkpoint("turn-1")

    with pytest.raises(
        RuntimeError,
        match="SHUO_LOCAL_STT_PYTHON",
    ):
        await capture.transcribe_and_save(
            "test",
            worker_python=str(tmp_path / "missing-python"),
            model_dir=str(tmp_path),
        )
