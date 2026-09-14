from __future__ import annotations

from typing import Optional

from ..types import MediaEvent
from .codec import BluetoothInboundCodec
from .phase3_session import Phase3AiOnlySession


class BluetoothInboundEvents:
    """Phase-3 Bluetooth capture -> SHUO inbound MediaEvent bridge.

    This object owns exactly one inbound codec instance for exactly one
    Bluetooth media session. It never accepts outbound/TTS audio and it never
    writes to the Phase-3 playback endpoint.

    The returned MediaEvent is always marked ``track="inbound"`` so the
    existing pure state machine applies the same caller-only STT rule used by
    carrier media.
    """

    def __init__(
        self,
        session: Phase3AiOnlySession,
        *,
        codec: Optional[BluetoothInboundCodec] = None,
    ) -> None:
        self._session = session
        self._codec = codec or BluetoothInboundCodec()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def read_event(self) -> MediaEvent:
        """Read until at least one complete converted SHUO audio byte exists.

        PipeWire reads are arbitrary byte chunks and may split a 16-bit sample.
        ``BluetoothInboundCodec`` retains such a trailing byte internally.
        Empty conversion output is therefore not an end-of-stream signal.
        """
        if self._closed:
            raise RuntimeError("Bluetooth inbound bridge is closed")

        while True:
            pcm = await self._session.read()
            mulaw = self._codec.feed(pcm)
            if mulaw:
                return MediaEvent(audio_bytes=mulaw, track="inbound")

    def finish(self) -> bytes:
        """Validate codec tail at stream end and close this bridge.

        Returns any codec-produced final bytes. The current codec contract
        returns ``b""``; an incomplete S16LE sample raises AudioContractError.
        """
        if self._closed:
            return b""

        try:
            return self._codec.finish()
        finally:
            self._closed = True

    def reset(self) -> None:
        """Reset only inbound conversion state for a new Bluetooth stream."""
        self._codec.reset()
        self._closed = False
