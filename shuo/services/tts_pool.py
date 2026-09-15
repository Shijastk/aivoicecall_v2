"""
ElevenLabs TTS connection pool.

Pre-connects WebSocket connections and manages their lifecycle.
Inactive connections and sockets past the safe provider idle lifetime are evicted.
The pool auto-refills after a connection is dispensed.

Usage:
    pool = TTSPool(pool_size=1, ttl=8.0, voice_id="JBFqnCBsd6RMkjVDRZzb")
    await pool.start()
    await pool.wait_ready()  # optional startup barrier before accepting turns

    tts = await pool.get(on_audio=..., on_done=...)
    # tts is ready to use immediately (if warm) or after a fresh connect

    await pool.stop()
"""

import asyncio
import math
import time
from typing import Optional, Callable, Awaitable, List
from dataclasses import dataclass

from .tts import TTSService
from ..log import ServiceLogger

log = ServiceLogger("TTSPool")

# Owner-observed ElevenLabs input timeout: 20s. Leave roughly 5s for the
# first real text to arrive after checkout; this margin needs live validation.
DEFAULT_MAX_IDLE_AGE = 15.0


# No-op callbacks for pre-connected (idle) services
async def _noop_audio(_audio: str) -> None:
    pass


async def _noop_done() -> None:
    pass


@dataclass
class _Entry:
    """A pooled socket timed from its provider initialization send boundary."""
    tts: TTSService
    created_at: float  # time.monotonic()


class TTSPool:
    """
    Connection pool for ElevenLabs TTS WebSockets.

    - Pre-connects `pool_size` connections at startup
    - Dispenses warm connections via get() with callback rebinding
    - Evicts inactive or over-age connections on checkout and during maintenance
    - Auto-refills in the background after dispensing or eviction

    **The pool is scoped to one voice**, which is why `voice_id` is set here
    and not on `get()`. ElevenLabs binds the voice into the `stream-input`
    URL at connect time, so a warm connection is already committed to a
    voice before any turn asks for one -- a per-turn voice would throw away
    the pre-connection that is the point of the pool. One call is one voice,
    so the pool is built per call.

    `max_idle_age` bounds warm reuse below the observed provider input timeout.
    Idle time starts before sending initialization, excluding the handshake but
    conservatively including send time. Injected services without that timestamp
    retain the conservative pre-start origin.
    `health_check_interval` controls liveness polling; expiry deadlines can wake
    maintenance sooner. Legacy `ttl` is only a compatibility fallback for that
    interval (`ttl / 2`); it does not control the safe maximum idle age.
    """

    def __init__(
        self,
        pool_size: int = 1,
        ttl: float = 8.0,
        voice_id: Optional[str] = None,
        *,
        max_idle_age: float = DEFAULT_MAX_IDLE_AGE,
        health_check_interval: Optional[float] = None,
    ):
        interval = ttl / 2 if health_check_interval is None else health_check_interval
        for name, value in (("max_idle_age", max_idle_age),
                            ("health_check_interval", interval)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self._pool_size = pool_size
        self._max_idle_age = max_idle_age
        self._health_check_interval = interval
        self._voice_id = voice_id

        self._ready: List[_Entry] = []
        self._running = False
        self._fill_event = asyncio.Event()
        self._fill_task: Optional[asyncio.Task] = None
        self._ready_changed = asyncio.Event()
        self._initial_warmup = False
        self._fill_error: Optional[Exception] = None

    @property
    def available(self) -> int:
        """Number of warm connections ready to dispense."""
        return len(self._ready)

    async def start(self) -> None:
        """Begin background preconnection; use wait_ready() for a startup barrier."""
        if self._running:
            return

        self._running = True
        self._initial_warmup = self._pool_size > 0
        self._fill_error = None
        self._ready_changed.clear()
        self._fill_task = asyncio.create_task(self._fill_loop())

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Await a usable warm socket without borrowing it or cancelling warmup.

        Allow preconnection retries until the timeout; report the latest error
        as its cause. Stop still fails immediately.
        The pool owner remains responsible for stop(), including on cancellation.
        Readiness isn't a reservation: get() still checks liveness and idle age.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")

        async def wait():
            while True:
                self._ready_changed.clear()
                if not self._running or self._pool_size <= 0:
                    raise RuntimeError("TTS pool is not warming connections")
                if any(
                    entry.tts.is_active
                    and time.monotonic() - entry.created_at < self._max_idle_age
                    for entry in self._ready
                ):
                    return
                self._trigger_fill()
                await self._ready_changed.wait()

        try:
            await asyncio.wait_for(wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise asyncio.TimeoutError("Timed out waiting for TTS pool readiness") from (
                self._fill_error if self._fill_error is not None else exc
            )

    async def get(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
    ) -> TTSService:
        """
        Get a connected TTS service with the given callbacks.

        Returns a warm connection if active and below the safe idle maximum,
        otherwise blocks to create a fresh one.
        """
        if self._initial_warmup:
            # Share the pool-owned initial connection, never race it with a
            # second cold handshake. Cancellation only cancels this waiter.
            await self.wait_ready()
        # Try to grab a warm, active connection
        while self._ready:
            entry = self._ready.pop(0)
            age = time.monotonic() - entry.created_at

            if not entry.tts.is_active:
                # A pooled socket can die where it sits. ElevenLabs closes
                # `stream-input` on its own inactivity timeout, and a
                # refused generation or a network blip closes it sooner --
                # none of which age can see. Dispensing the corpse is worse
                # than having nothing warm: `send()` and `flush()` both
                # return silently once `_running` is False, so the turn
                # feeds an entire LLM response into a closed socket, gets
                # no audio and no error, and hangs until the caller barges
                # in. Liveness and the provider idle deadline are separate
                # checks: a locally active socket may be near input timeout.
                reason = entry.tts.fatal_error or "socket closed while pooled"
                log.error(f"Discarded dead connection -- {reason}")
                await entry.tts.cancel()
                continue

            if age >= self._max_idle_age:
                log.info(f"Discarded over-age connection (idle {int(age * 1000)}ms)")
                await entry.tts.cancel()
                continue

            entry.tts.bind(on_audio, on_done)
            age_ms = int(age * 1000)
            log.info(f"Dispensed warm connection (idle {age_ms}ms)")
            self._trigger_fill()
            return entry.tts

        # No warm connections available -- create fresh (blocking)
        log.info("Pool empty, connecting fresh...")
        # The cold path takes the pool's voice too. Missing it here is the
        # silent version of this bug: warm turns use the operator's voice and
        # the occasional cold one does not, which reads as ElevenLabs being
        # inconsistent rather than as a wiring error.
        tts = TTSService(on_audio=on_audio, on_done=on_done, voice_id=self._voice_id)
        await tts.start()
        self._trigger_fill()
        return tts

    async def stop(self) -> None:
        """Shut down pool and clean up all connections."""
        self._running = False
        self._ready_changed.set()
        self._fill_event.set()  # unblock fill loop

        if self._fill_task:
            self._fill_task.cancel()
            try:
                await self._fill_task
            except asyncio.CancelledError:
                pass
            self._fill_task = None

        for entry in self._ready:
            await entry.tts.cancel()
        self._ready.clear()

    def _trigger_fill(self) -> None:
        """Signal the fill loop to check pool levels."""
        self._fill_event.set()

    async def _fill_loop(self) -> None:
        """Background loop that keeps the pool at target size."""
        try:
            while self._running:
                # Evict inactive entries and sockets at their safe idle limit
                await self._evict_stale()

                # Fill to target
                while self._running and len(self._ready) < self._pool_size:
                    tts = TTSService(
                        on_audio=_noop_audio,
                        on_done=_noop_done,
                        voice_id=self._voice_id,
                    )
                    created_at = time.monotonic()
                    try:
                        try:
                            await tts.start()
                        except BaseException:
                            # start() may already own a socket when it fails or
                            # stop() cancels the fill task. It isn't in _ready yet.
                            await tts.cancel()
                            raise
                        idle_started_at = getattr(tts, "warm_idle_started_at", None)
                        if idle_started_at is not None:
                            created_at = idle_started_at
                        self._ready.append(
                            _Entry(tts=tts, created_at=created_at)
                        )
                        self._initial_warmup = False
                        self._fill_error = None
                        self._ready_changed.set()
                        log.info(
                            f"🔥 Warm connection ready "
                            f"({len(self._ready)}/{self._pool_size})"
                        )
                    except Exception as e:
                        self._fill_error = e
                        self._ready_changed.set()
                        log.error("Pre-connect failed", e)
                        await asyncio.sleep(1.0)  # back off

                # Wait for signal (dispensed/evicted) or periodic refresh
                self._fill_event.clear()
                timeout = self._health_check_interval
                if self._ready:
                    remaining = min(
                        entry.created_at + self._max_idle_age - time.monotonic()
                        for entry in self._ready
                    )
                    timeout = min(timeout, max(0.0, remaining))
                try:
                    await asyncio.wait_for(
                        self._fill_event.wait(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    pass  # periodic liveness check

        except asyncio.CancelledError:
            pass
        finally:
            self._ready_changed.set()

    async def _evict_stale(self) -> None:
        """Remove inactive sockets and those at the safe provider idle limit."""
        for entry in list(self._ready):
            if entry not in self._ready:
                continue  # transferred to a caller while another entry closed
            if not entry.tts.is_active:
                # Same liveness rule as `get()`, applied proactively. Left
                # in `_ready` a dead entry still counts toward `_pool_size`,
                # so the fill loop would not replace it and the next turn
                # would discard it and then block on a cold connect. Drop it
                # here and the refill happens in the idle time instead.
                reason = entry.tts.fatal_error or "socket closed while pooled"
                log.error(f"Evicted dead connection -- {reason}")
            elif time.monotonic() - entry.created_at >= self._max_idle_age:
                age_ms = int((time.monotonic() - entry.created_at) * 1000)
                log.info(f"Evicted over-age connection (idle {age_ms}ms)")
            else:
                continue
            # Remove before yielding: get() may dispense another entry
            # during cleanup. Never replace _ready with an old snapshot.
            self._ready.remove(entry)
            try:
                await entry.tts.cancel()
            except asyncio.CancelledError:
                # stop() must finish cleanup of this detached entry too.
                await entry.tts.cancel()
                raise
