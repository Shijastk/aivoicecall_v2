from __future__ import annotations

import audioop
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AudioContractError(ValueError):
    """Raised when audio does not match an explicitly supported contract."""


class SampleEncoding(str, Enum):
    S16LE = "s16le"
    MULAW = "mulaw"


@dataclass(frozen=True)
class AudioFormat:
    encoding: SampleEncoding
    sample_rate: int
    channels: int

    def validate(self) -> None:
        if self.sample_rate <= 0:
            raise AudioContractError("sample_rate must be positive")
        if self.channels <= 0:
            raise AudioContractError("channels must be positive")


# Validated Phase-1 Bluetooth reference contract.
BLUETOOTH_PCM_FORMAT = AudioFormat(
    encoding=SampleEncoding.S16LE,
    sample_rate=16_000,
    channels=1,
)

# Existing SHUO/carrier contract. Do not change this.
SHUO_MULAW_FORMAT = AudioFormat(
    encoding=SampleEncoding.MULAW,
    sample_rate=8_000,
    channels=1,
)


def validate_bluetooth_format(fmt: AudioFormat) -> None:
    fmt.validate()

    if fmt != BLUETOOTH_PCM_FORMAT:
        raise AudioContractError(
            "unsupported Bluetooth audio contract: "
            f"{fmt.encoding.value}/{fmt.sample_rate}Hz/{fmt.channels}ch; "
            "Phase 2 supports only s16le/16000Hz/mono"
        )


def validate_shuo_format(fmt: AudioFormat) -> None:
    fmt.validate()

    if fmt != SHUO_MULAW_FORMAT:
        raise AudioContractError(
            "unsupported SHUO audio contract: "
            f"{fmt.encoding.value}/{fmt.sample_rate}Hz/{fmt.channels}ch; "
            "expected mulaw/8000Hz/mono"
        )


class BluetoothInboundCodec:
    """
    Bluetooth caller downlink -> SHUO inbound audio.

        S16LE / 16kHz / mono
            ->
        resample to 8kHz linear PCM
            ->
        G.711 mu-law / 8kHz / mono

    Conversion state belongs to one direction of one session.
    Do not share this object across calls or directions.
    """

    def __init__(self) -> None:
        self._rate_state = None
        self._byte_tail = bytearray()

    def feed(self, pcm_s16le_16k: bytes) -> bytes:
        if not pcm_s16le_16k:
            return b""

        # S16LE samples are exactly two bytes. pw/OS reads are not guaranteed
        # to align to sample boundaries, so retain one trailing byte.
        data = bytes(self._byte_tail) + pcm_s16le_16k
        self._byte_tail.clear()

        if len(data) % 2:
            self._byte_tail.extend(data[-1:])
            data = data[:-1]

        if not data:
            return b""

        pcm_8k, self._rate_state = audioop.ratecv(
            data,
            2,       # 16-bit samples
            1,       # mono
            16_000,
            8_000,
            self._rate_state,
        )

        return audioop.lin2ulaw(pcm_8k, 2)

    def finish(self) -> bytes:
        if self._byte_tail:
            raise AudioContractError(
                "Bluetooth PCM stream ended with an incomplete 16-bit sample"
            )

        return b""

    def reset(self) -> None:
        self._rate_state = None
        self._byte_tail.clear()


class BluetoothOutboundCodec:
    """
    SHUO generated audio -> Bluetooth phone uplink.

        G.711 mu-law / 8kHz / mono
            ->
        linear PCM
            ->
        resample to 16kHz
            ->
        S16LE / 16kHz / mono

    Conversion state is separate from inbound state.
    """

    def __init__(self) -> None:
        self._rate_state = None

    def feed(self, mulaw_8k: bytes) -> bytes:
        if not mulaw_8k:
            return b""

        pcm_8k = audioop.ulaw2lin(mulaw_8k, 2)

        pcm_16k, self._rate_state = audioop.ratecv(
            pcm_8k,
            2,
            1,
            8_000,
            16_000,
            self._rate_state,
        )

        return pcm_16k

    def finish(self) -> bytes:
        return b""

    def reset(self) -> None:
        self._rate_state = None


class FrameReframer:
    """
    Reframe arbitrary byte chunks without assuming provider/OS read boundaries.

    This class does not know whether the bytes are PCM or mu-law.
    """

    def __init__(self, frame_bytes: int):
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")

        self._frame_bytes = frame_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        if data:
            self._buffer.extend(data)

        frames: list[bytes] = []

        while len(self._buffer) >= self._frame_bytes:
            frames.append(bytes(self._buffer[: self._frame_bytes]))
            del self._buffer[: self._frame_bytes]

        return frames

    def finish(self) -> bytes:
        remainder = bytes(self._buffer)
        self._buffer.clear()
        return remainder

    def reset(self) -> None:
        self._buffer.clear()