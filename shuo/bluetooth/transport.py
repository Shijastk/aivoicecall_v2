from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class QueueOverflowError(RuntimeError):
    pass


class QueueClosedError(RuntimeError):
    pass


class OverflowPolicy(str, Enum):
    DROP_OLDEST = "drop_oldest"
    REJECT_NEW = "reject_new"


@dataclass(frozen=True)
class QueueStats:
    accepted: int
    dropped_oldest: int
    rejected_new: int


class BoundedAudioQueue:
    """
    Bounded realtime queue with an explicit overflow policy.

    There is deliberately no default capacity and no default overflow policy.
    Phase 2 must not silently invent production latency budgets.
    """

    def __init__(
        self,
        *,
        max_frames: int,
        overflow_policy: OverflowPolicy,
    ):
        if max_frames <= 0:
            raise ValueError("max_frames must be positive")

        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_frames)
        self._overflow_policy = overflow_policy
        self._closed = False

        self._accepted = 0
        self._dropped_oldest = 0
        self._rejected_new = 0

    @property
    def max_frames(self) -> int:
        return self._queue.maxsize

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> QueueStats:
        return QueueStats(
            accepted=self._accepted,
            dropped_oldest=self._dropped_oldest,
            rejected_new=self._rejected_new,
        )

    def put_nowait(self, frame: bytes) -> bool:
        if self._closed:
            raise QueueClosedError("audio queue is closed")

        if not frame:
            raise ValueError("empty audio frame is not allowed")

        if not self._queue.full():
            self._queue.put_nowait(frame)
            self._accepted += 1
            return True

        if self._overflow_policy is OverflowPolicy.REJECT_NEW:
            self._rejected_new += 1
            return False

        if self._overflow_policy is OverflowPolicy.DROP_OLDEST:
            self._queue.get_nowait()
            self._dropped_oldest += 1

            self._queue.put_nowait(frame)
            self._accepted += 1
            return True

        raise QueueOverflowError(
            f"unknown overflow policy: {self._overflow_policy!r}"
        )

    async def get(self) -> bytes:
        if self._closed and self._queue.empty():
            raise QueueClosedError("audio queue is closed")

        return await self._queue.get()

    def get_nowait(self) -> bytes:
        if self._closed and self._queue.empty():
            raise QueueClosedError("audio queue is closed")

        return self._queue.get_nowait()

    def clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        self.clear()


class CaptureEndpoint(Protocol):
    """
    Produces caller/downlink audio only.

    Implementations must never expose generated/TTS uplink audio here.
    """

    async def start(self) -> None:
        ...

    async def read(self) -> bytes:
        ...

    async def stop(self) -> None:
        ...


class PlaybackEndpoint(Protocol):
    """Consumes generated audio destined only for the phone uplink."""

    async def start(self) -> None:
        ...

    async def write(self, audio: bytes) -> None:
        ...

    async def clear(self) -> None:
        ...

    async def stop(self) -> None:
        ...


@dataclass
class DuplexQueues:
    """
    Structurally separate directions.

    inbound:
        caller -> future STT boundary

    outbound:
        future SHUO/TTS -> phone uplink
    """

    inbound: BoundedAudioQueue
    outbound: BoundedAudioQueue

    def __post_init__(self) -> None:
        if self.inbound is self.outbound:
            raise ValueError(
                "inbound and outbound must be different queue instances"
            )

    def close(self) -> None:
        self.inbound.close()
        self.outbound.close()