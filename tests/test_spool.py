"""
Tests for the off-loop write spool.

This module is the reason Phase 8 is allowed to write to disk from the call
process at all, so what is being defended is narrow and absolute:

1. **`submit` never touches the disk and never blocks.** It is called from
   HTTP handlers on the same event loop as the media socket, where decision 24
   turns a blocking write into permanent stream delay rather than one late
   frame. `TestItNeverBlocksTheLoop`.
2. **Writes land in submission order.** Phase 8 writes several revisions of
   one call record; out-of-order landing would let `pending` overwrite
   `completed` in the file, and the fold in `call_history` reads file order as
   truth. `TestOrdering`.
3. **It cannot cost a call.** A failing job, a full queue and a dead event
   loop all degrade to a counter. `TestItCannotCostACall`.
"""

import asyncio
import time

import pytest

from shuo.spool import Spool


@pytest.fixture
def spool():
    return Spool(max_pending=8, name="test")


# =============================================================================
# THE ROUND TRIP
# =============================================================================

class TestItRuns:
    @pytest.mark.asyncio
    async def test_a_submitted_job_runs(self, spool):
        seen = []
        spool.submit(seen.append, "written")
        await spool.drain()

        assert seen == ["written"]
        assert spool.written == 1

    @pytest.mark.asyncio
    async def test_arguments_are_passed_through(self, spool):
        seen = {}
        spool.submit(seen.__setitem__, "id", "att-1")
        await spool.drain()

        assert seen == {"id": "att-1"}

    def test_no_running_loop_runs_it_inline(self, spool):
        """
        A synchronous caller has no event loop to protect, so deferring the
        write would be all cost and no benefit -- and would defer it onto a
        loop that may never exist. This is what makes the module usable from a
        plain test and from a CLI path.
        """
        seen = []
        spool.submit(seen.append, "inline")

        assert seen == ["inline"], "a job submitted outside a loop must run now"

    @pytest.mark.asyncio
    async def test_draining_an_empty_spool_is_free(self, spool):
        assert await spool.drain() is True


# =============================================================================
# ORDERING
# =============================================================================

class TestOrdering:
    @pytest.mark.asyncio
    async def test_writes_land_in_submission_order(self, spool):
        """
        🔴 The reason there is one worker rather than a `to_thread` per job.

        A call writes `pending`, then `ringing`, then `completed` into an
        append-only file whose order *is* the history. Concurrent workers
        would let the fold read a stale status as the newest one.
        """
        landed = []

        def slow(value):
            # Uneven work, so a concurrent implementation would reorder.
            time.sleep(0.01 if value == 1 else 0)
            landed.append(value)

        for value in range(6):
            spool.submit(slow, value)
        await spool.drain()

        assert landed == [0, 1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_a_failing_job_does_not_stop_the_ones_behind_it(self, spool):
        landed = []

        def boom():
            raise RuntimeError("disk on fire")

        spool.submit(boom)
        spool.submit(landed.append, "after")
        await spool.drain()

        assert landed == ["after"]
        assert spool.failed == 1


# =============================================================================
# IT NEVER BLOCKS THE LOOP
# =============================================================================

class TestItNeverBlocksTheLoop:
    @pytest.mark.asyncio
    async def test_submit_returns_before_the_job_runs(self, spool):
        """
        🔴 The whole point. `submit` is a `put_nowait`; the work happens on a
        worker thread afterwards, so the caller's next line runs immediately.
        """
        started = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocking():
            loop.call_soon_threadsafe(started.set)
            # Hold the worker thread until the test lets go.
            while not release.is_set():
                time.sleep(0.001)

        spool.submit(blocking)
        # If submit had waited, this line would be unreachable until release.
        assert not release.is_set()

        await asyncio.wait_for(started.wait(), 2.0)
        release.set()
        await spool.drain()

    @pytest.mark.asyncio
    async def test_the_loop_keeps_running_while_a_write_is_in_flight(self, spool):
        """
        The property that matters to a live call: another task -- the player,
        in production -- must keep getting scheduled while a write is on the
        disk.
        """
        release = asyncio.Event()
        ticks = 0

        async def other_work():
            nonlocal ticks
            while not release.is_set():
                ticks += 1
                await asyncio.sleep(0.001)

        def blocking():
            time.sleep(0.05)

        task = asyncio.create_task(other_work())
        spool.submit(blocking)
        await spool.drain()
        release.set()
        await task

        assert ticks > 5, (
            f"the event loop only ran {ticks} times during a 50ms write — it "
            f"was blocked"
        )


# =============================================================================
# IT CANNOT COST A CALL
# =============================================================================

class TestItCannotCostACall:
    @pytest.mark.asyncio
    async def test_a_raising_job_does_not_propagate(self, spool):
        def boom():
            raise OSError("no space left on device")

        spool.submit(boom)  # must not raise
        await spool.drain()

        assert spool.failed == 1
        assert spool.written == 0

    def test_a_raising_job_does_not_propagate_inline_either(self, spool):
        def boom():
            raise OSError("no space left on device")

        spool.submit(boom)  # must not raise
        assert spool.failed == 1

    @pytest.mark.asyncio
    async def test_a_full_queue_drops_the_oldest_and_keeps_going(self):
        """
        🔴 Bounded, because an unbounded queue turns a stuck disk into
        unbounded heap on the *audio* process.

        The oldest is dropped rather than the newest: revisions arrive
        stub-first and transcript-last, so if something must be lost it should
        be the stub.
        """
        spool = Spool(max_pending=2, name="tiny")
        release = asyncio.Event()
        landed = []

        def blocking():
            while not release.is_set():
                time.sleep(0.001)

        # Occupies the worker, so everything after it queues.
        spool.submit(blocking)
        await asyncio.sleep(0.01)

        for value in range(6):
            spool.submit(landed.append, value)

        release.set()
        await spool.drain()

        assert spool.dropped >= 1, "a full queue must drop rather than grow"
        assert landed, "the newest writes must survive"
        assert landed[-1] == 5, "the newest write must never be the one dropped"

    @pytest.mark.asyncio
    async def test_a_drain_that_times_out_reports_rather_than_hangs(self):
        spool = Spool(name="slow")
        release = asyncio.Event()

        def blocking():
            while not release.is_set():
                time.sleep(0.001)

        spool.submit(blocking)
        spool.submit(lambda: None)

        assert await spool.drain(timeout=0.05) is False
        release.set()
        await spool.drain()

    @pytest.mark.asyncio
    async def test_it_adopts_a_new_event_loop(self):
        """
        A queue bound to a closed loop accepts jobs that can never run --
        silently, which is the worst way to lose a row. The suite creates a
        fresh loop per test, and so does `main.py`'s CLI path.
        """
        spool = Spool(name="rebound")
        landed = []

        # Stand in for a previous loop that has since gone away.
        spool._loop = object()
        spool._queue = asyncio.Queue()

        spool.submit(landed.append, "on the new loop")
        await spool.drain()

        assert landed == ["on the new loop"]


# =============================================================================
# SHUTDOWN
# =============================================================================

class TestShutdown:
    @pytest.mark.asyncio
    async def test_close_flushes_what_is_queued(self, spool):
        """
        Without this, a restart between a call ending and its row reaching the
        disk loses the row -- most likely on the call somebody was watching,
        because a deploy usually follows it.
        """
        landed = []
        for value in range(5):
            spool.submit(landed.append, value)

        await spool.close()

        assert landed == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_stats_report_what_was_lost(self, spool):
        spool.submit(lambda: None)
        await spool.drain()

        stats = spool.stats()
        assert stats["submitted"] == 1
        assert stats["written"] == 1
        assert stats["dropped"] == 0
        assert stats["failed"] == 0
        assert stats["pending"] == 0
