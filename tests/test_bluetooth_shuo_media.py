import base64

import pytest

from shuo.bluetooth.shuo_media import BluetoothOutboundMedia


class FakePhase3Session:
    def __init__(self):
        self.writes = []
        self.clear_calls = 0

    async def write(self, audio: bytes) -> None:
        self.writes.append(audio)

    async def clear(self) -> None:
        self.clear_calls += 1


@pytest.mark.asyncio
async def test_bluetooth_outbound_converts_shuo_mulaw_to_s16le_16k():
    session = FakePhase3Session()
    media = BluetoothOutboundMedia(session)

    # One SHUO 20ms frame: 160 bytes of 8kHz mu-law silence.
    payload = base64.b64encode(b"\xff" * 160).decode("ascii")

    await media.play_audio(payload)

    assert len(session.writes) == 1
    # Python 3.12 audioop.ratecv has the already-documented one-sample
    # interpolation startup boundary: first output is 638 bytes, then 640.
    assert len(session.writes[0]) == 638


@pytest.mark.asyncio
async def test_bluetooth_outbound_keeps_streaming_state_between_chunks():
    session = FakePhase3Session()
    media = BluetoothOutboundMedia(session)
    payload = base64.b64encode(b"\xff" * 160).decode("ascii")

    await media.play_audio(payload)
    await media.play_audio(payload)

    assert [len(chunk) for chunk in session.writes] == [638, 640]


@pytest.mark.asyncio
async def test_clear_only_calls_phase3_clear_and_resets_outbound_codec():
    session = FakePhase3Session()
    media = BluetoothOutboundMedia(session)
    payload = base64.b64encode(b"\xff" * 160).decode("ascii")

    await media.play_audio(payload)
    await media.clear_audio()
    await media.play_audio(payload)

    assert session.clear_calls == 1
    # Reset means the next independent utterance starts at the known 638-byte
    # interpolation boundary again.
    assert [len(chunk) for chunk in session.writes] == [638, 638]


@pytest.mark.asyncio
async def test_checkpoint_is_local_dispatch_bookkeeping_not_playback_ack():
    session = FakePhase3Session()
    media = BluetoothOutboundMedia(session)

    await media.checkpoint("turn-7")

    assert media.last_checkpoint == "turn-7"
    assert session.writes == []
    assert session.clear_calls == 0


def test_bluetooth_outbound_adapter_exposes_no_capture_method():
    media = BluetoothOutboundMedia(FakePhase3Session())

    assert not hasattr(media, "read")
