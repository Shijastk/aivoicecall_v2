from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Protocol

from ..log import get_logger


log = get_logger("shuo.bluetooth.speculation")

PROBE_TIMEOUT_SECONDS = 2.0
EARLY_STABLE_SPAN_SECONDS = 0.1


class ShadowProbe(Protocol):
    async def first_token_at(self, transcript: str) -> float: ...
    def create_prepared(self, transcript: str): ...


class PreparedResponse(Protocol):
    @property
    def first_token_at(self) -> Optional[float]: ...
    def start(self) -> None: ...
    async def wait_first_token(self) -> float: ...
    async def wait_closed(self) -> None: ...
    async def cancel(self) -> None: ...


@dataclass(frozen=True)
class ShadowObservation:
    generation_id: int
    outcome: str
    transcript_chars: int
    trigger: str = "eager"
    trigger_to_final_ms: Optional[float] = None
    trigger_to_first_token_ms: Optional[float] = None
    speculative_lead_ms: Optional[float] = None
    eager_to_final_ms: Optional[float] = None
    shadow_ttft_ms: Optional[float] = None
    first_token_before_final: Optional[bool] = None
    transcript_match: Optional[bool] = None


@dataclass
class _Candidate:
    generation_id: int
    transcript: str
    trigger_at: float
    trigger: str = "eager"
    task: Optional[asyncio.Task[None]] = None
    started_at: Optional[float] = None
    first_token_at: Optional[float] = None
    prepared: Optional[PreparedResponse] = None
    promoted: bool = False


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
    the token text itself is discarded inside the probe. Early Update admission
    is separately opt-in and does not change final-EOT Agent generation.
    """

    def __init__(
        self, *, probe: ShadowProbe, capacity_gate: AsyncCapacityGate,
        early_transcripts: bool = False,
        prepared_reuse: bool = False,
    ):
        if prepared_reuse and not callable(getattr(probe, "create_prepared", None)):
            raise ValueError("prepared reuse requires a prepared-response probe")
        self._probe = probe
        self._capacity_gate = capacity_gate
        self._generation_id = 0
        self._candidate: Optional[_Candidate] = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._observations: deque[ShadowObservation] = deque(maxlen=256)
        self._early_transcripts = early_transcripts
        self._prepared_reuse = prepared_reuse
        self._committed: Optional[tuple[int, str, PreparedResponse]] = None
        self._stable_text = ""
        self._stable_since = 0.0
        self._attempts = 0
        self._last_trigger_at = float("-inf")
        self._turn_open = True
        self._last_candidate: Optional[_Candidate] = None
        self._closed = False
        self._diagnostic_turn = 0
        self._reset_diagnostics()
        log.info(
            "BTShadowAdmission: configured early_enabled=%s min_stable_span_ms=%.0f",
            early_transcripts, EARLY_STABLE_SPAN_SECONDS * 1000,
        )

    def _reset_diagnostics(self) -> None:
        self._update_count = 0
        self._same_transcript_repetitions = 0
        self._early_admitted = False
        self._eager_arrived_first = False
        self._diagnostic_words = 0
        self._diagnostic_span_ms = 0.0
        self._diagnostic_stable_since: Optional[float] = None

    def _diagnose(self, event: str, reason: str, *, words: Optional[int] = None,
                  stable_span_ms: Optional[float] = None) -> None:
        if self._early_transcripts:
            if words is not None:
                self._diagnostic_words = words
            if stable_span_ms is not None:
                self._diagnostic_span_ms = stable_span_ms
            log.info(
                "BTShadowAdmission: turn=%d event=%s update_count=%d "
                "same_transcript_repetitions=%d stable_span_ms=%.3f "
                "word_count=%d word_count_eligible=%s reason=%s "
                "eager_arrived_first=%s early_admitted=%s attempts=%d",
                self._diagnostic_turn, event, self._update_count,
                self._same_transcript_repetitions, self._diagnostic_span_ms,
                self._diagnostic_words, self._diagnostic_words >= 3,
                reason, self._eager_arrived_first,
                self._early_admitted, self._attempts,
            )

    @property
    def observations(self) -> tuple[ShadowObservation, ...]:
        return tuple(self._observations)

    @property
    def active_generation_id(self) -> Optional[int]:
        candidate = self._candidate
        return candidate.generation_id if candidate is not None else None

    def take_committed(self, transcript: str):
        """Transfer one exact final-matching prepared stream to the Agent."""
        committed = self._committed
        if committed is None:
            return None
        generation_id, committed_transcript, prepared = committed
        self._committed = None
        if transcript.strip() != committed_transcript:
            self._schedule_prepared_cancel(prepared)
            log.info(
                "BTPrepared: generation=%d discarded reason=take_mismatch",
                generation_id,
            )
            return None
        log.info("BTPrepared: generation=%d handed_to_agent", generation_id)
        return prepared

    def on_start(self) -> None:
        self._discard_committed("new_turn")
        self._diagnose("start_boundary", "reset")
        self._cancel_current("new_turn")
        self._reset_turn()
        self._diagnostic_turn += 1
        self._reset_diagnostics()
        self._turn_open = True

    def _reset_turn(self) -> None:
        self._stable_text = ""
        self._stable_since = 0.0
        self._attempts = 0
        self._last_candidate = None

    def on_interim(self, transcript: str, *, observed_at: Optional[float] = None) -> None:
        """One or more identical Update repeats spanning >=100ms, 3+ words,
        <=2000 chars. A confirming callback is required; elapsed time alone
        never admits. Matching eager leaves the earlier candidate intact.

        No timer infers silence. At most two attempts per turn, >=1s apart,
        including eager fallback. These are admission heuristics, not finality.
        """
        if not self._early_transcripts:
            return
        self._update_count += 1
        if self._closed or not self._turn_open:
            self._diagnose("update", "closed" if self._closed else "turn_closed")
            return
        now = time.perf_counter() if observed_at is None else observed_at
        transcript = transcript.strip()
        if self._candidate is not None and transcript != self._candidate.transcript:
            self._cancel_current("replaced")
        if len(transcript) > 2000:
            self._stable_text = ""
            self._same_transcript_repetitions = 0
            self._diagnostic_stable_since = None
            self._diagnose("update", "too_long", words=len(transcript.split()), stable_span_ms=0)
            return
        words = len(transcript.split())
        if transcript != self._stable_text:
            self._stable_text = transcript
            self._stable_since = now
            self._same_transcript_repetitions = 0
            self._diagnostic_stable_since = now
            self._diagnose("update", "transcript_changed", words=words, stable_span_ms=0)
            return
        if self._diagnostic_stable_since is None:
            self._diagnostic_stable_since = now
        else:
            self._same_transcript_repetitions += 1
        span = now - self._stable_since
        reason = "too_few_words" if words < 3 else "stable_span_too_short"
        if words >= 3 and span >= EARLY_STABLE_SPAN_SECONDS:
            reason = self._admit(transcript, now, "interim")
            if reason == "admitted":
                self._early_admitted = True
        self._diagnose("update", reason, words=words,
                       stable_span_ms=(now - self._diagnostic_stable_since) * 1000)

    def on_eager(self, transcript: str, *, observed_at: Optional[float] = None) -> None:
        if self._closed or not self._turn_open:
            return
        self._eager_arrived_first |= not self._early_admitted
        transcript = transcript.strip()
        if self._candidate is not None:
            if transcript == self._candidate.transcript:
                self._diagnose("eager", "matching_candidate")
                return
            self._cancel_current("replaced")
        if transcript:
            reason = self._admit(
                transcript, time.perf_counter() if observed_at is None else observed_at,
                "eager",
            )
            self._diagnose("eager", reason)
        else:
            self._diagnose("eager", "empty_transcript")

    def _admit(self, transcript: str, now: float, trigger: str) -> str:
        if self._candidate is not None:
            return "active_candidate"
        if any(not task.done() for task in self._tasks):
            # Cancelled providers may still be closing. Never overlap requests.
            return "prior_task_pending"
        if self._early_transcripts:
            if self._attempts >= 2:
                return "attempt_budget"
            if now - self._last_trigger_at < 1.0:
                return "cooldown"
            if len(transcript) > 2000:
                return "too_long"
        self._attempts += 1
        self._last_trigger_at = now
        self._generation_id += 1
        candidate = _Candidate(self._generation_id, transcript, now, trigger)
        if self._prepared_reuse:
            try:
                candidate.prepared = self._probe.create_prepared(transcript)
            except Exception as exc:
                self._record(
                    candidate,
                    outcome=f"probe_error:{type(exc).__name__}",
                )
                return "prepare_error"
        self._candidate = self._last_candidate = candidate
        task = asyncio.create_task(
            self._run_prepared(candidate)
            if self._prepared_reuse
            else self._run_probe(candidate)
        )
        candidate.task = task
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        log.info(
            "BTShadow: trigger=%s generation=%d transcript_chars=%d",
            trigger, candidate.generation_id, len(transcript),
        )
        return "admitted"

    def on_resumed(self, *, observed_at: Optional[float] = None) -> None:
        self._discard_committed("resumed")
        self._diagnose("resumed", "stability_reset")
        self._same_transcript_repetitions = 0
        self._diagnostic_stable_since = None
        self._diagnostic_span_ms = 0.0
        self._diagnostic_words = 0
        self._stable_text = ""
        self._stable_since = 0.0
        if self._candidate is not None:
            self._cancel_current("resumed")

    def on_final(self, transcript: str, *, observed_at: Optional[float] = None) -> None:
        self._diagnose("final", "turn_closed")
        candidate = self._candidate or self._last_candidate
        eligible = self._candidate is not None
        self._turn_open = False
        self._reset_turn()
        if candidate is None:
            log.info("BTShadow: outcome=no_candidate_by_final")
            return

        final_at = time.perf_counter() if observed_at is None else observed_at
        final_transcript = transcript.strip()
        transcript_match = final_transcript == candidate.transcript
        eager_to_final_ms = max(0.0, (final_at - candidate.trigger_at) * 1000.0)

        if not eligible:
            self._record(
                candidate, outcome="discarded_by_final",
                eager_to_final_ms=eager_to_final_ms,
                transcript_match=transcript_match, first_token_before_final=False,
            )
            return

        if not transcript_match:
            self._record(
                candidate,
                outcome="transcript_mismatch",
                eager_to_final_ms=eager_to_final_ms,
                transcript_match=False,
                first_token_before_final=False,
            )
            self._cancel_task(candidate)
            self._candidate = None
            return

        if (
            self._prepared_reuse
            and candidate.prepared is not None
            and candidate.first_token_at is not None
            and candidate.first_token_at <= final_at
        ):
            candidate.promoted = True
            self._committed = (
                candidate.generation_id,
                final_transcript,
                candidate.prepared,
            )
            shadow_ttft_ms = None
            if candidate.started_at is not None:
                shadow_ttft_ms = max(
                    0.0,
                    (candidate.first_token_at - candidate.started_at) * 1000.0,
                )
            self._record(
                candidate,
                outcome="promoted_ready_before_final",
                eager_to_final_ms=eager_to_final_ms,
                shadow_ttft_ms=shadow_ttft_ms,
                first_token_before_final=True,
                transcript_match=True,
            )
            self._candidate = None
            return

        if candidate.first_token_at is not None and candidate.first_token_at <= final_at:
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
        self._diagnose("cleanup", "closed")
        self._closed = True
        self._discard_committed("cleanup")
        if self._candidate is not None:
            self._cancel_current("cancelled")

        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._reset_turn()

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
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                first_token_at = await self._probe.first_token_at(candidate.transcript)

            if self._candidate is candidate and not self._closed:
                candidate.first_token_at = first_token_at
                ttft_ms = max(
                    0.0,
                    (candidate.first_token_at - candidate.started_at) * 1000.0,
                )
                log.info(
                    "BTShadow: first token ready generation=%d ttft=%.1fms "
                    "trigger_to_first_token_ms=%.1f",
                    candidate.generation_id,
                    ttft_ms,
                    (first_token_at - candidate.trigger_at) * 1000.0,
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

    async def _run_prepared(self, candidate: _Candidate) -> None:
        acquired = False
        prepared = candidate.prepared
        if prepared is None:
            return
        try:
            acquired = await self._capacity_gate.acquire()
            if not acquired:
                if self._candidate is candidate:
                    self._record(candidate, outcome="capacity_skip")
                    self._candidate = None
                await prepared.cancel()
                return

            if self._candidate is not candidate or self._closed:
                await prepared.cancel()
                return

            candidate.started_at = time.perf_counter()
            prepared.start()
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                candidate.first_token_at = await prepared.wait_first_token()

            if self._candidate is candidate and not self._closed:
                ttft_ms = max(
                    0.0,
                    (candidate.first_token_at - candidate.started_at) * 1000.0,
                )
                log.info(
                    "BTPrepared: first token ready generation=%d ttft=%.1fms "
                    "trigger_to_first_token_ms=%.1f",
                    candidate.generation_id,
                    ttft_ms,
                    (candidate.first_token_at - candidate.trigger_at) * 1000.0,
                )

            # Keep the single provider stream alive but unread until final EOT
            # either commits it to Agent or invalidation closes it.
            await prepared.wait_closed()

        except asyncio.CancelledError:
            if not candidate.promoted:
                await prepared.cancel()
            raise
        except Exception as exc:
            if self._candidate is candidate:
                self._record(candidate, outcome=f"probe_error:{type(exc).__name__}")
                self._candidate = None
            if not candidate.promoted:
                await prepared.cancel()
        finally:
            if acquired:
                self._capacity_gate.release()

    def _schedule_prepared_cancel(self, prepared: PreparedResponse) -> None:
        task = asyncio.create_task(prepared.cancel())
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _discard_committed(self, reason: str) -> None:
        committed = self._committed
        if committed is None:
            return
        generation_id, _transcript, prepared = committed
        self._committed = None
        self._schedule_prepared_cancel(prepared)
        log.info(
            "BTPrepared: generation=%d discarded reason=%s",
            generation_id,
            reason,
        )

    def _cancel_task(self, candidate: _Candidate) -> None:
        task = candidate.task
        if task is not None and not task.done() and not task.cancelling():
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
            trigger=candidate.trigger,
            trigger_to_final_ms=eager_to_final_ms,
            trigger_to_first_token_ms=(
                (candidate.first_token_at - candidate.trigger_at) * 1000.0
                if candidate.first_token_at is not None else None
            ),
            speculative_lead_ms=(
                eager_to_final_ms - (candidate.first_token_at - candidate.trigger_at) * 1000.0
                if eager_to_final_ms is not None and candidate.first_token_at is not None
                else None
            ),
            eager_to_final_ms=eager_to_final_ms if candidate.trigger == "eager" else None,
            shadow_ttft_ms=shadow_ttft_ms,
            first_token_before_final=first_token_before_final,
            transcript_match=transcript_match,
        )
        self._observations.append(observation)
        log.info(
            "BTShadow: generation=%d outcome=%s chars=%d eager_to_final_ms=%s "
            "shadow_ttft_ms=%s ready_before_final=%s transcript_match=%s "
            "trigger=%s trigger_to_final_ms=%s trigger_to_first_token_ms=%s "
            "speculative_lead_ms=%s",
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
            observation.trigger, observation.trigger_to_final_ms,
            observation.trigger_to_first_token_ms, observation.speculative_lead_ms,
        )

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass
