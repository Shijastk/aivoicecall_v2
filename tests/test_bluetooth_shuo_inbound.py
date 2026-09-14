import pytest

from shuo.bluetooth.codec import AudioContractError
from shuo.bluetooth.shuo_inbound import BluetoothInboundEvents
from shuo.state import process_event
from shuo.types import AppState, FeedFluxAction, MediaEvent


class FakePhase3Capture:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read_calls = 0
        self.write_calls = []
        self.clear_calls = 0

    async def read(self) -> bytes:
        self.read_calls += 1
        if not self.chunks:
            raise RuntimeError("fake capture exhausted")
        return self.chunks.pop(0)

    # These exist only to prove the inbound bridge never touches them.
    async def write(self, audio: bytes) -> None:
        self.write_calls.append(audio)

    async def clear(self) -> None:
        self.clear_calls += 1


@pytest.mark.asyncio
async def test_inbound_bridge_returns_only_inbound_media_event():
    # 20ms S16LE/16k mono silence = 320 samples = 640 bytes.
    session = FakePhase3Capture([b"\x00\x00" * 320])
    bridge = BluetoothInboundEvents(session)

    event = await bridge.read_event()

    assert isinstance(event, MediaEvent)
    assert event.track == "inbound"
    assert event.audio_bytes
    assert session.write_calls == []
    assert session.clear_calls == 0


@pytest.mark.asyncio
async def test_inbound_media_event_reuses_existing_state_machine_feed_action():
    session = FakePhase3Capture([b"\x00\x00" * 320])
    bridge = BluetoothInboundEvents(session)

    event = await bridge.read_event()
    state, actions = process_event(AppState(), event)

    assert state == AppState()
    assert actions == [FeedFluxAction(audio_bytes=event.audio_bytes)]


@pytest.mark.asyncio
async def test_fragmented_s16le_sample_boundary_is_preserved():
    # First read is one byte: codec must retain it and read again rather than
    # invent/drop a sample. Together the two reads form 320 valid samples.
    full = b"\x01\x00" * 320
    session = FakePhase3Capture([full[:1], full[1:]])
    bridge = BluetoothInboundEvents(session)

    event = await bridge.read_event()

    assert event.audio_bytes
    assert session.read_calls == 2


@pytest.mark.asyncio
async def test_inbound_conversion_state_is_continuous_between_reads():
    session = FakePhase3Capture([
        b"\x00\x00" * 320,
        b"\x00\x00" * 320,
    ])
    bridge = BluetoothInboundEvents(session)

    first = await bridge.read_event()
    second = await bridge.read_event()

    # 16k -> 8k produces the existing documented startup boundary once;
    # continuity means the second read is not reset to a fresh converter.
    assert len(second.audio_bytes) >= len(first.audio_bytes)
    assert session.read_calls == 2


def test_finish_rejects_incomplete_s16le_sample():
    session = FakePhase3Capture([])
    bridge = BluetoothInboundEvents(session)

    # Feed one dangling byte directly into the owned codec via a deliberately
    # fragmented fake read path in the next test would block waiting for more.
    # Here we use the injectable codec contract explicitly.
    from shuo.bluetooth.codec import BluetoothInboundCodec

    codec = BluetoothInboundCodec()
    codec.feed(b"\x00")
    bridge = BluetoothInboundEvents(session, codec=codec)

    with pytest.raises(AudioContractError):
        bridge.finish()

    assert bridge.closed is True


@pytest.mark.asyncio
async def test_reset_starts_a_fresh_inbound_stream():
    session = FakePhase3Capture([
        b"\x00\x00" * 320,
        b"\x00\x00" * 320,
    ])
    bridge = BluetoothInboundEvents(session)

    first = await bridge.read_event()
    bridge.finish()
    assert bridge.closed is True

    bridge.reset()
    second = await bridge.read_event()

    assert bridge.closed is False
    # Both are fresh converter starts after reset.
    assert len(first.audio_bytes) == len(second.audio_bytes)


@pytest.mark.asyncio
async def test_closed_bridge_refuses_further_capture_reads():
    session = FakePhase3Capture([b"\x00\x00" * 320])
    bridge = BluetoothInboundEvents(session)
    bridge.finish()

    with pytest.raises(RuntimeError, match="closed"):
        await bridge.read_event()

    assert session.read_calls == 0
