import math
import struct

import pytest

from shuo.bluetooth.codec import (
    AudioContractError,
    AudioFormat,
    BLUETOOTH_PCM_FORMAT,
    SHUO_MULAW_FORMAT,
    BluetoothInboundCodec,
    BluetoothOutboundCodec,
    FrameReframer,
    SampleEncoding,
    validate_bluetooth_format,
    validate_shuo_format,
)


def _tone_16k(duration_ms: int = 100, frequency: float = 440.0) -> bytes:
    samples = int(16_000 * duration_ms / 1000)

    return b"".join(
        struct.pack(
            "<h",
            int(10_000 * math.sin(2 * math.pi * frequency * i / 16_000)),
        )
        for i in range(samples)
    )


def test_contracts_are_exact():
    assert BLUETOOTH_PCM_FORMAT == AudioFormat(
        SampleEncoding.S16LE,
        16_000,
        1,
    )

    assert SHUO_MULAW_FORMAT == AudioFormat(
        SampleEncoding.MULAW,
        8_000,
        1,
    )


def test_rejects_unvalidated_bluetooth_format():
    with pytest.raises(AudioContractError):
        validate_bluetooth_format(
            AudioFormat(
                SampleEncoding.S16LE,
                8_000,
                1,
            )
        )


def test_rejects_non_shuo_contract():
    with pytest.raises(AudioContractError):
        validate_shuo_format(
            AudioFormat(
                SampleEncoding.S16LE,
                8_000,
                1,
            )
        )


def test_20ms_bluetooth_pcm_becomes_160_mulaw_bytes():
    # 16 kHz * 20 ms = 320 samples.
    # S16LE = 2 bytes/sample => 640 input bytes.
    pcm = _tone_16k(duration_ms=20)

    assert len(pcm) == 640

    codec = BluetoothInboundCodec()
    mulaw = codec.feed(pcm)

    assert len(mulaw) == 160


def test_outbound_resampler_is_stateful_and_does_not_accumulate_chunk_drift():
    codec = BluetoothOutboundCodec()

    first = codec.feed(bytes([0xFF]) * 160)
    second = codec.feed(bytes([0xFF]) * 160)
    third = codec.feed(bytes([0xFF]) * 160)

    # audioop.ratecv's first stateful 8k -> 16k conversion emits
    # 319 output samples (638 S16LE bytes). Subsequent 20ms chunks
    # emit the expected 320 samples (640 bytes).
    #
    # Do not pad the converter output merely to force chunk geometry:
    # the Bluetooth boundary must support arbitrary stream chunk sizes.
    assert len(first) == 638
    assert len(second) == 640
    assert len(third) == 640

    # Three 20 ms source frames represent 60 ms.
    # Stateful interpolation is one output sample short at stream start,
    # not progressively drifting on every frame.
    assert len(first + second + third) == 1918


def test_fragmented_inbound_equals_contiguous_conversion():
    pcm = _tone_16k(duration_ms=100)

    contiguous = BluetoothInboundCodec().feed(pcm)

    codec = BluetoothInboundCodec()

    # Deliberately include odd byte boundaries.
    pieces = [
        pcm[:101],
        pcm[101:517],
        pcm[517:1000],
        pcm[1000:],
    ]

    fragmented = b"".join(codec.feed(piece) for piece in pieces)
    codec.finish()

    assert fragmented == contiguous


def test_inbound_and_outbound_state_are_not_shared():
    inbound = BluetoothInboundCodec()
    outbound = BluetoothOutboundCodec()

    caller_pcm = _tone_16k(duration_ms=20)
    caller_mulaw = inbound.feed(caller_pcm)

    first_agent_pcm = outbound.feed(bytes([0xFF]) * 160)
    second_agent_pcm = outbound.feed(bytes([0xFF]) * 160)

    assert len(caller_mulaw) == 160

    # Outbound resampling owns independent 8k -> 16k state.
    assert len(first_agent_pcm) == 638
    assert len(second_agent_pcm) == 640


def test_outbound_resampler_has_no_progressive_duration_drift():
    codec = BluetoothOutboundCodec()

    chunks = [
        codec.feed(bytes([0xFF]) * 160)
        for _ in range(100)
    ]

    total = b"".join(chunks)

    # 100 SHUO frames = exactly 2 seconds of 8 kHz mu-law input.
    #
    # Ideal 16 kHz S16LE output:
    #   16,000 samples/sec * 2 sec * 2 bytes = 64,000 bytes.
    #
    # audioop.ratecv starts one interpolated 16 kHz sample short,
    # therefore the complete stateful stream is 2 bytes short once,
    # not 2 bytes short per frame.
    assert len(total) == 63_998

    # Prove the error is a fixed startup interpolation boundary,
    # not accumulating realtime drift.
    assert 64_000 - len(total) == 2

def test_inbound_rejects_incomplete_s16_sample_at_end():
    codec = BluetoothInboundCodec()
    codec.feed(b"\x00")

    with pytest.raises(AudioContractError):
        codec.finish()


def test_reframer_handles_fragmented_reads():
    reframer = FrameReframer(frame_bytes=160)

    assert reframer.feed(b"a" * 100) == []

    frames = reframer.feed(b"b" * 100)

    assert len(frames) == 1
    assert len(frames[0]) == 160
    assert reframer.buffered_bytes == 40

    remainder = reframer.finish()

    assert len(remainder) == 40
    assert reframer.buffered_bytes == 0

    