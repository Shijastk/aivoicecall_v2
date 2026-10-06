import base64
import sys

import pytest

from shuo.services.local_malayalam_speech import (
    LocalMalayalamSpeechError,
    LocalMalayalamSpeechService,
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
