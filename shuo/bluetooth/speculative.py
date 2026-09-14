from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from ..log import get_logger


log = get_logger("shuo.bluetooth.speculation")


class ShadowProbe(Protocol):
    async def first_token_at(self, transcript: str) -> float: ...


@dataclass(frozen=True)
class ShadowObservation:
    generation_id: int
    outcome: str
    transcript_chars: int
    eager_to_final_ms: Optional[float] = None
    shadow_ttft_ms: Optional[float] = None
    first_token_before_final: Optional[bool] = None
    transcript_match: Optional[bool] = None


@dataclass
class _Candidate:
    generation_id: int
    transcript: str
    eager_at: float
    task: Optional[asyncio.Task[None]] = None
    started_at: Optional[float] = None
    first_token_at: Optional[float] = None


class AsyncCapacityGate:
    """Bound shadow work without blocking the realtime conversation path."""

    def __init__(self, limit: int, *, max_wait_seconds: float = 0.025):
        if limit < 1:
            raise ValueError("speculation capacity limit must be >= 1")
        if max_wait_seconds < 0:
            raise ValueError("max_wait_seconds must be >= 0")
        self._semaphore = asyncio.Semaphore(limit)
        self._max_wait_seconds = max_wait_seconds

    async def acquire(self) -> bool:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._max_wait_seconds,
            )
        except TimeoutError:
            return False
        return True

    def release(self) -> None:
        self._semaphore.release()


class SpeculativeTurnCoordinator:
    """Phase-4B shadow-only speculative LLM coordinator.

    It never speaks, never writes conversation history and never owns TTS.
    The provider-layer probe returns only the time of the first content token;
    the token text itself is discarded inside the probe.
    """

    def __init__(self, *, probe: ShadowProbe, capacity_gate: AsyncCapacityGate):
        self._probe = probe
        self._capacity_gate = capacity_gate
        self._generation_id = 0
        self._candidate: Optional[_Candidate] = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._observations: list[ShadowObservation] = []
        self._closed = False

    @property
    def observations(self) -> tuple[ShadowObservation, ...]:
        return tuple(self._observations)

    @property
    def active_generation_id(self) -> Optional[int]:
        candidate = self._candidate
        return candidate.generation_id if candidate is not None else None

    def on_eager(self, transcript: str, *, observed_at: Optional[float] = None) -> None:
        if self._closed:
            return
        transcript = transcript.strip()
        if not transcript:
            return

        if self._candidate is not None:
            self._cancel_current("replaced")

        self._generation_id += 1
        candidate = _Candidate(
            generation_id=self._generation_id,
            transcript=transcript,
            eager_at=time.perf_counter() if observed_at is None else observed_at,
        )
        self._candidate = candidate
        task = asyncio.create_task(self._run_probe(candidate))
        candidate.task = task
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

        log.info(
            "BTShadow: eager generation=%d transcript_chars=%d",
            candidate.generation_id,
            len(candidate.transcript),
        )

    def on_resumed(self, *, observed_at: Optional[float] = None) -> None:
        if self._candidate is not None:
            self._cancel_current("resumed")

    def on_final(self, transcript: str, *, observed_at: Optional[float] = None) -> None:
        candidate = self._candidate
        if candidate is None:
            return

        final_at = time.perf_counter() if observed_at is None else observed_at
        final_transcript = transcript.strip()
        transcript_match = final_transcript == candidate.transcript
        eager_to_final_ms = max(0.0, (final_at - candidate.eager_at) * 1000.0)

        if not transcript_match:
            self._record(
                candidate,
                outcome="transcript_mismatch",
                eager_to_final_ms=eager_to_final_ms,
                transcript_match=False,
            )
            self._cancel_task(candidate)
            self._candidate = None
            return

        if candidate.first_token_at is not None:
            shadow_ttft_ms = None
            if candidate.started_at is not None:
                shadow_ttft_ms = max(
                    0.0,
                    (candidate.first_token_at - candidate.started_at) * 1000.0,
                )
            self._record(
                candidate,
                outcome="ready_before_final",
                eager_to_final_ms=eager_to_final_ms,
                shadow_ttft_ms=shadow_ttft_ms,
                first_token_before_final=True,
                transcript_match=True,
            )
            self._candidate = None
            return

        # Shadow-only Phase 4B stops unfinished work at final EOT so it cannot
        # compete with the ordinary final-EOT Agent request after finality.
        self._record(
            candidate,
            outcome="not_ready_by_final",
            eager_to_final_ms=eager_to_final_ms,
            first_token_before_final=False,
            transcript_match=True,
        )
        self._cancel_task(candidate)
        self._candidate = None

    async def cleanup(self) -> None:
        self._closed = True
        if self._candidate is not None:
            self._cancel_task(self._candidate)
            self._candidate = None

        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_probe(self, candidate: _Candidate) -> None:
        acquired = False
        try:
            acquired = await self._capacity_gate.acquire()
            if not acquired:
                if self._candidate is candidate:
                    self._record(candidate, outcome="capacity_skip")
                    self._candidate = None
                return

            if self._candidate is not candidate or self._closed:
                return

            candidate.started_at = time.perf_counter()
            candidate.first_token_at = await self._probe.first_token_at(
                candidate.transcript
            )

            if self._candidate is candidate:
                ttft_ms = max(
                    0.0,
                    (candidate.first_token_at - candidate.started_at) * 1000.0,
                )
                log.info(
                    "BTShadow: first token ready generation=%d ttft=%.1fms",
                    candidate.generation_id,
                    ttft_ms,
                )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._candidate is candidate:
                self._record(candidate, outcome=f"probe_error:{type(exc).__name__}")
                self._candidate = None
        finally:
            if acquired:
                self._capacity_gate.release()

    def _cancel_task(self, candidate: _Candidate) -> None:
        task = candidate.task
        if task is not None and not task.done():
            task.cancel()

    def _cancel_current(self, outcome: str) -> None:
        candidate = self._candidate
        if candidate is None:
            return
        shadow_ttft_ms = None
        if candidate.started_at is not None and candidate.first_token_at is not None:
            shadow_ttft_ms = max(
                0.0,
                (candidate.first_token_at - candidate.started_at) * 1000.0,
            )
        self._record(
            candidate,
            outcome=outcome,
            shadow_ttft_ms=shadow_ttft_ms,
        )
        self._cancel_task(candidate)
        self._candidate = None

    def _record(
        self,
        candidate: _Candidate,
        *,
        outcome: str,
        eager_to_final_ms: Optional[float] = None,
        shadow_ttft_ms: Optional[float] = None,
        first_token_before_final: Optional[bool] = None,
        transcript_match: Optional[bool] = None,
    ) -> None:
        observation = ShadowObservation(
            generation_id=candidate.generation_id,
            outcome=outcome,
            transcript_chars=len(candidate.transcript),
            eager_to_final_ms=eager_to_final_ms,
            shadow_ttft_ms=shadow_ttft_ms,
            first_token_before_final=first_token_before_final,
            transcript_match=transcript_match,
        )
        self._observations.append(observation)
        log.info(
            "BTShadow: generation=%d outcome=%s chars=%d eager_to_final_ms=%s "
            "shadow_ttft_ms=%s ready_before_final=%s transcript_match=%s",
            observation.generation_id,
            observation.outcome,
            observation.transcript_chars,
            (
                f"{observation.eager_to_final_ms:.1f}"
                if observation.eager_to_final_ms is not None
                else "-"
            ),
            (
                f"{observation.shadow_ttft_ms:.1f}"
                if observation.shadow_ttft_ms is not None
                else "-"
            ),
            observation.first_token_before_final,
            observation.transcript_match,
        )

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass
