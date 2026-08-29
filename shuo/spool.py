"""
Durable writes from the call process, without touching its event loop.

This module exists because of one sentence in decision 32: the player emits a
160-byte frame every 20ms against an accumulating monotonic deadline, and
decision 24 removed its ability to claw lateness back, so a blocking write on
that loop does not cost a frame -- it costs permanent stream delay for the
rest of the call. The config API is a separate *process* to keep operator
saves away from it.

But Phase 8 needs the call process itself to write to disk **while a call is
running**: a pending row the moment a call is placed, a `ringing` row when the
carrier says so, a terminal row at teardown. :3041 cannot do it -- it never
sees an inbound call, and it never sees a call placed by `curl` from the
runbook. So the write has to happen here, and it has to be free on the loop.

    submit(fn, *args)  ->  queue.put_nowait      O(1), never blocks, never raises
    one worker task    ->  await asyncio.to_thread(fn, *args)

Four properties, and they are the whole design:

- **The producer never touches the filesystem.** `submit` is a bounded
  `put_nowait` and a counter -- the same cost class as `call_monitor`'s
  `deque.append`, which is the standard every hot-path publisher in this
  codebase is already held to. All the actual I/O happens on a worker thread,
  where the loop is not waiting for it.
- **One worker, so writes land in submission order.** This is not tidiness.
  Phase 8 writes several revisions of the *same* call record, and a fan-out of
  independent `to_thread` calls could land `completed` before `ringing` and
  leave the fold in `call_history.load` reading the wrong one as newest.
- **Bounded, drop-oldest, counted.** An unbounded queue turns a stuck disk
  into unbounded heap on the audio process. Dropping the *oldest* is
  deliberate: the newest revision of a call row is the one that carries the
  transcript, so if something has to be lost it should be the stub, not the
  call.
- **It never raises and it never propagates.** A failed write costs a row in a
  table. Same posture as `call_history.append` and `call_monitor._emit`: a
  call log that can end a call is worse than no call log.

**No running loop means nothing to protect**, so `submit` runs the job inline
in that case. That is what makes this safe to call from a plain synchronous
test, and from a CLI path that has no server loop of its own.

Deliberately generic: it takes a callable, so nothing about the call log or
recordings leaks in here, and `call_history` keeps depending on nothing but
`json`, `pathlib` and the logger -- which is what lets :3041 import it across
the process seam (decision 44).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional, Tuple

from .log import get_logger

logger = get_logger("shuo.spool")


# =============================================================================
# LIMITS
# =============================================================================

# Jobs allowed to queue before the oldest is dropped. A call contributes about
# six revisions plus, later, its recording chunks, so this is dozens of calls
# deep -- and it is reached at all only if the disk has stopped answering, at
# which point the right behaviour is to bound the damage rather than to hope.
MAX_PENDING = 256

# How long teardown waits for queued writes to land before giving up. Long
# enough for a slow disk, short enough that it cannot hold a shutdown open.
DRAIN_TIMEOUT_SECONDS = 5.0


Job = Tuple[Callable[..., Any], tuple, dict]


class Spool:
    """
    A single-consumer queue of file writes, drained on a worker thread.

    One instance per process (`SPOOL` below). Not thread-safe and does not
    need to be: every producer runs on the call process's one event loop, and
    `submit` contains no `await`, so no two submissions can interleave.
    """

    def __init__(self, *, max_pending: int = MAX_PENDING, name: str = "call-writes"):
        self._name = name
        self._max_pending = max_pending
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        # Which loop `_queue` and `_worker` belong to. A test suite creates a
        # fresh loop per test, and a queue bound to a closed one accepts jobs
        # that can never run -- silently, which is the worst way to lose a row.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.submitted = 0
        self.written = 0
        self.dropped = 0
        self.failed = 0
        self._warned = False

    # ── Write path (hot) ────────────────────────────────────────────

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """
        Queue one write. Never blocks, never raises, never touches the disk.

        Safe to call from anywhere on the call process's event loop, including
        from an HTTP handler that shares it with the media socket.
        """
        self.submitted += 1
        job: Job = (fn, args, kwargs)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop to protect -- a synchronous test, or a CLI path. Doing
            # it now is both correct and simpler than deferring it to a loop
            # that may never exist.
            self._execute(job)
            return

        try:
            queue = self._ensure_worker(loop)
            try:
                queue.put_nowait(job)
            except asyncio.QueueFull:
                self._drop_oldest(queue)
                queue.put_nowait(job)
        except Exception as exc:  # pragma: no cover - defensive
            self._warn(exc)

    def _drop_oldest(self, queue: asyncio.Queue) -> None:
        """
        Make room by discarding the oldest queued write.

        The oldest is the right one to lose: revisions arrive stub-first and
        transcript-last, so the newest is always the one that carries the most.
        """
        try:
            queue.get_nowait()
            queue.task_done()
            self.dropped += 1
        except asyncio.QueueEmpty:  # pragma: no cover - defensive
            return

        if self.dropped == 1:
            logger.warning(
                f"The {self._name} spool is full ({self._max_pending} pending) "
                f"— dropping the oldest write. The disk is not keeping up; "
                f"calls are unaffected but the call log will have gaps."
            )

    def _ensure_worker(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        """Start the consumer, or adopt a new event loop."""
        if self._loop is not loop or self._worker is None or self._worker.done():
            if self._loop is not loop and self._queue is not None:
                # The old queue belongs to a loop that is gone. Anything still
                # in it could never have run.
                pending = self._queue.qsize()
                if pending:
                    self.dropped += pending
                    logger.debug(
                        f"Dropped {pending} write(s) queued on a closed event loop"
                    )
            self._queue = asyncio.Queue(maxsize=self._max_pending)
            self._worker = loop.create_task(self._work(self._queue))
            self._loop = loop
        return self._queue

    # ── Drain path (worker) ─────────────────────────────────────────

    async def _work(self, queue: asyncio.Queue) -> None:
        """Run queued jobs one at a time, off the loop, in order."""
        while True:
            job = await queue.get()
            try:
                await asyncio.to_thread(self._execute, job)
            except asyncio.CancelledError:
                queue.task_done()
                raise
            except Exception as exc:  # pragma: no cover - _execute swallows
                self._warn(exc)
                queue.task_done()
            else:
                queue.task_done()

    def _execute(self, job: Job) -> None:
        """
        Run one job, swallowing whatever it does.

        Its callers already never raise (`call_history.append` swallows its
        own I/O errors), so reaching the `except` here means a programming
        error rather than a full disk -- which is exactly when a spool that
        died quietly would be hardest to diagnose.
        """
        fn, args, kwargs = job
        try:
            fn(*args, **kwargs)
            self.written += 1
        except Exception as exc:
            self.failed += 1
            self._warn(exc)

    def _warn(self, exc: Exception) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning(
            f"A queued write failed ({exc!r}). Calls are unaffected; the call "
            f"log may be missing a row. Further failures are counted, not logged."
        )

    # ── Shutdown ────────────────────────────────────────────────────

    async def drain(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> bool:
        """
        Wait for queued writes to land. True if the queue emptied in time.

        An `await`, not a block: the loop keeps pacing frames for every other
        live call while this waits. That is what makes it safe to call from a
        call's teardown, where it turns "the row will be written eventually"
        into "the row is on disk before the call is considered over".
        """
        queue = self._queue
        if queue is None or queue.qsize() == 0:
            return True

        try:
            await asyncio.wait_for(asyncio.shield(queue.join()), timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"{queue.qsize()} queued write(s) did not finish within "
                f"{timeout:g}s. They may still land; the call is unaffected."
            )
            return False

    async def close(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> None:
        """Drain, then stop the worker. For process shutdown."""
        await self.drain(timeout)

        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        self._queue = None
        self._loop = None

    # ── Observability ───────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        """
        Counters for `/health`.

        `dropped` and `failed` are the two that matter: both mean the call log
        is lying by omission, and neither is visible any other way.
        """
        return {
            "submitted": self.submitted,
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
            "pending": self._queue.qsize() if self._queue is not None else 0,
        }

    def reset(self) -> None:
        """Forget everything. For tests, and for nothing else."""
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
        self._queue = None
        self._loop = None
        self.submitted = self.written = self.dropped = self.failed = 0
        self._warned = False


# One per process. Every durable write made from the call process goes through
# it; `shuo/config_api.py` has no use for it and does not import it.
SPOOL = Spool()
