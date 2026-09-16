"""Provider-aware TTS connection/service pool.

ElevenLabs remains the default production provider. An explicitly selected local
provider or fallback is constructed through the provider router while preserving
the existing warm-pool lifecycle and callback contract.
"""

import asyncio
import math
import time
from typing import Any, Optional, Callable, Awaitable, List
from dataclasses import dataclass

# Keep this symbol as the injectable ElevenLabs seam. Existing regression tests
# monkeypatch shuo.services.tts_pool.TTSService directly.
from .tts import TTSService
from .tts_provider import build_tts_service
from ..log import ServiceLogger

log = ServiceLogger("TTSPool")

# Owner-observed ElevenLabs input timeout: 20s. Leave roughly 5s for the
# first real text to arrive after checkout; this margin needs live validation.
# Local eSpeak services do not need this bound, but applying it is harmless:
# their "warm" state owns no speech process and is cheap to recreate.
DEFAULT_MAX_IDLE_AGE = 15.0


# No-op callbacks for pre-connected (idle) services
async def _noop_audio(_audio: str) -> None:
    pass


async def _noop_done() -> None:
    pass


@dataclass
class _Entry:
    """A pooled service timed from its provider readiness boundary."""
    tts: Any
    created_at: float  # time.monotonic()


class TTSPool:
    """Warm TTS service pool with provider-neutral checkout semantics.

    - Prepares `pool_size` services at startup
    - Dispenses ready services via get() with callback rebinding
    - Evicts inactive or over-age services on checkout and maintenance
    - Auto-refills in the background after dispensing or eviction

    ElevenLabs remains the default, so without TTS_PROVIDER /
    TTS_FALLBACK_PROVIDER this class behaves exactly as the previous
    ElevenLabs-only pool. The pool still carries one configured voice id;
    providers that do not use that id may ignore it.

    `max_idle_age` remains the safety bound required by the ElevenLabs warm
    socket. Injected/local services expose the same readiness timestamp so the
    lifecycle machinery stays provider-neutral.
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
        """Number of warm services ready to dispense."""
        return len(self._ready)

    def _create_service(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
    ):
        return build_tts_service(
            on_audio,
            on_done,
            voice_id=self._voice_id,
            # Resolve the module symbol at call time so the long-standing
            # monkeypatch seam used by tests remains intact.
            elevenlabs_cls=TTSService,
        )

    async def start(self) -> None:
        """Begin background preparation; use wait_ready() for a startup barrier."""
        if self._running:
            return

        self._running = True
        self._initial_warmup = self._pool_size > 0
        self._fill_error = None
        self._ready_changed.clear()
        self._fill_task = asyncio.create_task(self._fill_loop())

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Await a usable warm service without borrowing it or cancelling warmup.

        Allow preparation retries until the timeout; report the latest error as
        its cause. Stop still fails immediately. The pool owner remains
        responsible for stop(), including on cancellation. Readiness is not a
        reservation: get() still checks liveness and age.
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
    ):
        """Get a ready TTS service with the given callbacks."""
        if self._initial_warmup:
            # Share the pool-owned initial service, never race it with a second
            # cold preparation. Cancellation only cancels this waiter.
            await self.wait_ready()

        while self._ready:
            entry = self._ready.pop(0)
            age = time.monotonic() - entry.created_at

            if not entry.tts.is_active:
                reason = entry.tts.fatal_error or "service closed while pooled"
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

        # No warm service available -- create fresh (blocking). The provider
        # router preserves the pool voice for ElevenLabs and ignores it for
        # local eSpeak.
        log.info("Pool empty, connecting fresh...")
        tts = self._create_service(on_audio, on_done)
        await tts.start()
        self._trigger_fill()
        return tts

    async def stop(self) -> None:
        """Shut down pool and clean up all ready services."""
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
                await self._evict_stale()

                while self._running and len(self._ready) < self._pool_size:
                    tts = self._create_service(_noop_audio, _noop_done)
                    created_at = time.monotonic()
                    try:
                        try:
                            await tts.start()
                        except BaseException:
                            # start() may already own resources when it fails or
                            # stop() cancels the fill task. It is not in _ready yet.
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
        """Remove inactive services and those at the safe idle limit."""
        for entry in list(self._ready):
            if entry not in self._ready:
                continue  # transferred to a caller while another entry closed

            if not entry.tts.is_active:
                reason = entry.tts.fatal_error or "service closed while pooled"
                log.error(f"Evicted dead connection -- {reason}")
            elif time.monotonic() - entry.created_at >= self._max_idle_age:
                age_ms = int((time.monotonic() - entry.created_at) * 1000)
                log.info(f"Evicted over-age connection (idle {age_ms}ms)")
            else:
                continue

            # Remove before yielding: get() may dispense another entry during
            # cleanup. Never replace _ready with an old snapshot.
            self._ready.remove(entry)
            try:
                await entry.tts.cancel()
            except asyncio.CancelledError:
                # stop() must finish cleanup of this detached entry too.
                await entry.tts.cancel()
                raise
