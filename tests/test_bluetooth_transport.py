import asyncio

import pytest

from shuo.bluetooth.transport import (
    BoundedAudioQueue,
    DuplexQueues,
    OverflowPolicy,
    QueueClosedError,
)


def test_reject_new_policy_is_explicit_and_bounded():
    queue = BoundedAudioQueue(
        max_frames=2,
        overflow_policy=OverflowPolicy.REJECT_NEW,
    )

    assert queue.put_nowait(b"one") is True
    assert queue.put_nowait(b"two") is True
    assert queue.put_nowait(b"three") is False

    assert queue.stats.accepted == 2
    assert queue.stats.rejected_new == 1
    assert queue.stats.dropped_oldest == 0


def test_drop_oldest_keeps_realtime_tail():
    queue = BoundedAudioQueue(
        max_frames=2,
        overflow_policy=OverflowPolicy.DROP_OLDEST,
    )

    queue.put_nowait(b"one")
    queue.put_nowait(b"two")
    queue.put_nowait(b"three")

    assert queue.get_nowait() == b"two"
    assert queue.get_nowait() == b"three"

    assert queue.stats.dropped_oldest == 1


def test_inbound_and_outbound_must_be_different_objects():
    queue = BoundedAudioQueue(
        max_frames=2,
        overflow_policy=OverflowPolicy.REJECT_NEW,
    )

    with pytest.raises(ValueError):
        DuplexQueues(
            inbound=queue,
            outbound=queue,
        )


def test_duplex_directions_do_not_cross():
    inbound = BoundedAudioQueue(
        max_frames=2,
        overflow_policy=OverflowPolicy.REJECT_NEW,
    )

    outbound = BoundedAudioQueue(
        max_frames=2,
        overflow_policy=OverflowPolicy.REJECT_NEW,
    )

    queues = DuplexQueues(
        inbound=inbound,
        outbound=outbound,
    )

    queues.inbound.put_nowait(b"caller")
    queues.outbound.put_nowait(b"agent")

    assert queues.inbound.get_nowait() == b"caller"
    assert queues.outbound.get_nowait() == b"agent"


def test_closed_queue_rejects_audio():
    queue = BoundedAudioQueue(
        max_frames=2,
        overflow_policy=OverflowPolicy.REJECT_NEW,
    )

    queue.close()

    with pytest.raises(QueueClosedError):
        queue.put_nowait(b"audio")