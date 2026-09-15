from __future__ import annotations

import base64
import time

from typing import Optional

from ..log import get_logger

from ..media import OutboundMediaSession
from .codec import BluetoothOutboundCodec
from .phase3_session import Phase3AiOnlySession


_log = get_logger("shuo.bluetooth.latency")


class BluetoothOutboundMedia(OutboundMediaSession):
    """SHUO mu-law/8k -> Phase-3 Bluetooth playback adapter.

    The adapter is deliberately outbound-only. It has no capture/read method,
    so generated AI audio cannot be routed back into STT through this object.

    ``checkpoint()`` is dispatch-only bookkeeping. Bluetooth/PipeWire currently
    provides no authoritative handset-played acknowledgement comparable to a
    carrier ``playedStream``/``mark``. A Bluetooth conversation runner must
    therefore complete a turn from local dispatch completion and must not wait
    for, synthesize, or claim a handset playback acknowledgement.
    """

    def __init__(
        self,
        session: Phase3AiOnlySession,
        *,
        codec: Optional[BluetoothOutboundCodec] = None,
    ) -> None:
        self._session = session
        self._codec = codec or BluetoothOutboundCodec()
        self._last_checkpoint: Optional[str] = None
        self._dispatch_chunks = 0

    @property
    def last_checkpoint(self) -> Optional[str]:
        """Last locally dispatched checkpoint name, not a playback ACK."""
        return self._last_checkpoint

    async def play_audio(self, payload_b64: str) -> None:
        mulaw = base64.b64decode(payload_b64)
        if not mulaw:
            return

        pcm = self._codec.feed(mulaw)
        if pcm:
            if self._dispatch_chunks == 0:
                _log.info("BTLifecycle: event=PlaybackFirstWrite_begin")
            await self._session.write(pcm)
            self._dispatch_chunks += 1
            if self._dispatch_chunks == 1:
                _log.info("BTLifecycle: event=PlaybackFirstWrite_returned pcm_bytes=%d", len(pcm))

    async def clear_audio(self) -> None:
        # Phase 3 clear() discards only audio still owned by its bounded local
        # playback queue. Bytes already handed to pw-cat/OS are not falsely
        # claimed as retractable.
        started = time.perf_counter()
        _log.info("BTLifecycle: event=PlaybackClear_begin dispatched_chunks=%d", self._dispatch_chunks)
        await self._session.clear()
        _log.info("BTLifecycle: event=PlaybackClear_returned elapsed_ms=%.1f", (time.perf_counter() - started) * 1000)
        self._dispatch_chunks = 0

        # Barge-in starts a new independent outbound utterance. Reset only the
        # outbound resampler state; inbound capture has its own codec object.
        self._codec.reset()
        self._last_checkpoint = None

    async def checkpoint(self, name: str) -> None:
        # There is intentionally no fabricated PlaybackMarkEvent here.
        self._last_checkpoint = name
        _log.info("BTLifecycle: event=PlaybackDispatch_complete dispatched_chunks=%d", self._dispatch_chunks)
        self._dispatch_chunks = 0

    def reset(self) -> None:
        """Reset adapter-local state between Bluetooth sessions."""
        self._dispatch_chunks = 0
        self._codec.reset()
        self._last_checkpoint = None
