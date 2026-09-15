import asyncio
import time
from types import SimpleNamespace

import pytest

from shuo.bluetooth.speculative import (
    AsyncCapacityGate,
    SpeculativeTurnCoordinator,
)
from shuo.services.llm import ShadowLLMProbe


class BlockingProbe:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def first_token_at(self, transcript: str) -> float:
        self.calls.append(transcript)
        self.started.set()
        await self.release.wait()
        return time.perf_counter()


@pytest.mark.asyncio
async def test_shadow_first_token_before_final_is_observed_but_never_spoken():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    probe.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    coordinator.on_final("hello")

    assert coordinator.observations[-1].outcome == "ready_before_final"
    assert coordinator.observations[-1].first_token_before_final is True
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_turn_resumed_cancels_and_discards_shadow_generation():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    coordinator.on_resumed()
    await asyncio.sleep(0)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "resumed"
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_final_before_first_token_cancels_shadow():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    coordinator.on_final("hello")
    await asyncio.sleep(0)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "not_ready_by_final"
    assert coordinator.observations[-1].first_token_before_final is False
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_transcript_mismatch_never_keeps_shadow():
    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
    )

    coordinator.on_eager("hello")
    await probe.started.wait()
    coordinator.on_final("hello changed")
    await asyncio.sleep(0)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "transcript_mismatch"
    assert coordinator.observations[-1].transcript_match is False
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_capacity_pressure_skips_shadow_without_failing_call_path():
    gate = AsyncCapacityGate(1, max_wait_seconds=0.005)
    assert await gate.acquire() is True

    probe = BlockingProbe()
    coordinator = SpeculativeTurnCoordinator(probe=probe, capacity_gate=gate)
    coordinator.on_eager("hello")
    await asyncio.sleep(0.02)

    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "capacity_skip"

    gate.release()
    await coordinator.cleanup()


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, stream):
        self.stream = stream
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.stream


class FakeClient:
    def __init__(self, stream):
        self.chat = SimpleNamespace(completions=FakeCompletions(stream))


@pytest.mark.asyncio
async def test_shadow_llm_uses_history_snapshot_without_mutating_it(monkeypatch):
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    before = [dict(item) for item in history]
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="first"))]
        )
    ]
    stream = FakeStream(chunks)
    client = FakeClient(stream)
    monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.6-27b")

    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: history,
        client=client,
    )
    first_token_at = await probe.first_token_at("new question")

    assert isinstance(first_token_at, float)
    assert history == before
    assert client.chat.completions.kwargs["messages"] == [
        {"role": "system", "content": "system"},
        *before,
        {"role": "user", "content": "new question"},
    ]
    assert client.chat.completions.kwargs["extra_body"] == {
        "reasoning_effort": "none"
    }
    assert stream.closed is True


def early_coordinator(probe, gate=None):
    return SpeculativeTurnCoordinator(
        probe=probe, capacity_gate=gate or AsyncCapacityGate(1),
        early_transcripts=True,
    )


def stable_update(coordinator, text="tell me about yourself", at=10.0):
    coordinator.on_interim(text, observed_at=at)
    coordinator.on_interim(text, observed_at=at + 0.25)


async def drain(coordinator):
    tasks = tuple(coordinator._tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("span,admitted", [(0.099, False), (0.1, True), (0.111629, True)])
async def test_early_minimum_span_requires_a_confirming_update(span, admitted):
    coordinator = early_coordinator(BlockingProbe())
    coordinator.on_interim("synthetic four word question", observed_at=0)
    await asyncio.sleep(0)
    assert coordinator.active_generation_id is None
    coordinator.on_interim("synthetic four word question", observed_at=span)
    assert (coordinator.active_generation_id is not None) is admitted
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_live_turn_five_repeat_admits_before_eager_after_resume():
    # Sanitized receive-time replay, relative to 12:39:55.000. Text is synthetic.
    # Initial repeat is already too late to beat eager, even with the new rule.
    probe = BlockingProbe()
    coordinator = early_coordinator(probe)
    coordinator.on_start()
    coordinator.on_interim("synthetic four word question", observed_at=0.038)
    coordinator.on_interim("synthetic five word question here", observed_at=0.076)
    coordinator.on_eager("synthetic five word question here", observed_at=0.164)
    await probe.started.wait()
    coordinator.on_interim("synthetic five word question here", observed_at=0.201447)
    assert coordinator._candidate.trigger == "eager"
    coordinator.on_resumed(observed_at=1.205)
    await drain(coordinator)

    text = "synthetic six word question after resume"
    coordinator.on_interim(text, observed_at=1.480)
    coordinator.on_interim(text, observed_at=1.591629)
    assert coordinator._candidate.trigger == "interim"
    assert coordinator._candidate.trigger_at < 1.748  # observed next eager
    assert coordinator._attempts == 2
    generation = coordinator.active_generation_id
    coordinator.on_eager(text, observed_at=1.650)  # matching eager race variant
    assert coordinator.active_generation_id == generation
    assert coordinator._attempts == 2

    # Actual run changed text ~118ms after the repeat; the earlier start is risky.
    changed = "synthetic six word question now corrected"
    coordinator.on_interim(changed, observed_at=1.710)
    assert coordinator.active_generation_id is None
    await drain(coordinator)
    coordinator.on_eager(changed, observed_at=1.748)
    coordinator.on_final(changed, observed_at=1.7482)
    assert coordinator.observations[-1].outcome == "discarded_by_final"
    assert coordinator.observations[-1].transcript_match is False
    assert coordinator.observations[-1].first_token_before_final is False
    assert coordinator._generation_id == 2
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_early_repeat_wins_matching_eager_without_extra_request():
    probe = BlockingProbe()
    coordinator = early_coordinator(probe)
    text = "synthetic four word question"
    coordinator.on_interim(text, observed_at=0)
    coordinator.on_interim(text, observed_at=0.111629)
    await probe.started.wait()
    generation = coordinator.active_generation_id
    coordinator.on_eager(text, observed_at=0.15)
    coordinator.on_interim(text, observed_at=0.2)
    assert coordinator.active_generation_id == generation
    assert coordinator._candidate.trigger == "interim"
    assert coordinator._attempts == 1
    assert probe.calls == [text]
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_live_turn_six_cooldown_is_shared_and_survives_start(caplog):
    caplog.set_level("INFO", logger="shuo.bluetooth.speculation")
    coordinator = early_coordinator(BlockingProbe())
    coordinator.on_eager("synthetic", observed_at=0.556)
    coordinator.on_resumed(observed_at=0.807)
    await drain(coordinator)
    coordinator.on_interim("synthetic three words", observed_at=1.069)
    coordinator.on_interim("synthetic three words", observed_at=1.299892)
    assert "reason=cooldown" in caplog.messages[-1]
    coordinator.on_eager("synthetic three words", observed_at=1.335)
    assert "reason=cooldown" in caplog.messages[-1]
    coordinator.on_start()
    coordinator.on_interim("synthetic three words", observed_at=1.34)
    coordinator.on_interim("synthetic three words", observed_at=1.45)
    assert "reason=cooldown" in caplog.messages[-1]
    coordinator.on_interim("synthetic three words", observed_at=1.557)
    assert coordinator._candidate.trigger == "interim"
    assert coordinator._generation_id == 2
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_repeated_tiny_mutations_cannot_restart_inside_cooldown_or_budget():
    coordinator = early_coordinator(BlockingProbe())
    for index in range(12):
        text = "synthetic four word question" + "." * index
        start = index * 0.2
        coordinator.on_interim(text, observed_at=start)
        await drain(coordinator)  # previous cancelled stream must finish closing
        coordinator.on_interim(text, observed_at=start + 0.111629)
    assert coordinator._generation_id == 2
    assert coordinator._attempts == 2
    await coordinator.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["final_mismatch", "eager_mismatch", "resume"])
async def test_ready_early_token_cannot_survive_invalidating_boundary(boundary):
    class ReadyProbe:
        async def first_token_at(self, transcript):
            return 0.15

    coordinator = early_coordinator(ReadyProbe())
    text = "synthetic four word question"
    coordinator.on_interim(text, observed_at=0)
    coordinator.on_interim(text, observed_at=0.111629)
    await drain(coordinator)
    if boundary == "resume":
        coordinator.on_resumed(observed_at=0.2)
        final = text  # even equal final text cannot revive resumed work
    else:
        final = text + "."
        if boundary == "eager_mismatch":
            coordinator.on_eager(final, observed_at=0.2)
    coordinator.on_final(final, observed_at=0.409)
    observation = coordinator.observations[-1]
    assert observation.speculative_lead_ms == pytest.approx(259)
    assert observation.first_token_before_final is False
    assert observation.outcome == (
        "transcript_mismatch" if boundary == "final_mismatch" else "discarded_by_final"
    )
    assert not any(o.outcome == "ready_before_final" for o in coordinator.observations)
    await coordinator.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,reason,repetitions,span,eligible,trigger", [
    ("changed", "transcript_changed", 0, "0.000", True, None),
    ("short", "too_few_words", 1, "250.000", False, None),
    ("empty", "too_few_words", 1, "250.000", False, None),
    ("fast", "stable_span_too_short", 1, "50.000", True, None),
    ("eager_first", "active_candidate", 1, "250.000", True, "eager"),
    ("stable", "admitted", 1, "250.000", True, "interim"),
])
async def test_admission_diagnostics_distinguish_no_early_hypotheses(
    caplog, scenario, reason, repetitions, span, eligible, trigger,
):
    caplog.set_level("INFO", logger="shuo.bluetooth.speculation")
    coordinator = early_coordinator(BlockingProbe())
    text = "synthetic private phrase" if scenario != "short" else "synthetic phrase"
    if scenario == "empty":
        text = ""
    coordinator.on_start()
    coordinator.on_interim(text, observed_at=10)
    if scenario == "eager_first":
        coordinator.on_eager(text, observed_at=10.125)
    coordinator.on_interim(
        text + " extension" if scenario == "changed" else text,
        observed_at=10.05 if scenario == "fast" else 10.25,
    )
    row = caplog.messages[-1]
    assert "event=update update_count=2" in row
    assert f"reason={reason}" in row
    assert f"same_transcript_repetitions={repetitions}" in row
    assert f"stable_span_ms={span}" in row
    assert f"word_count_eligible={eligible}" in row
    assert f"eager_arrived_first={scenario == 'eager_first'}" in row
    assert "synthetic" not in caplog.text
    if trigger:
        assert coordinator._candidate.trigger == trigger
    else:
        assert coordinator.active_generation_id is None
    coordinator.on_final(text, observed_at=10.5)
    summary = next(m for m in reversed(caplog.messages) if "event=final" in m)
    assert "update_count=2" in summary
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_admission_diagnostics_report_bounds_and_resets(caplog):
    caplog.set_level("INFO", logger="shuo.bluetooth.speculation")
    coordinator = early_coordinator(BlockingProbe())
    stable_update(coordinator)
    coordinator.on_resumed()
    # A cancelled task still exists until its cancellation is processed.
    stable_update(coordinator, at=10.5)
    assert "reason=prior_task_pending" in caplog.messages[-1]
    await drain(coordinator)
    coordinator.on_interim("tell me about yourself", observed_at=10.875)
    assert "reason=cooldown" in caplog.messages[-1]
    coordinator.on_interim("tell me about yourself", observed_at=12)
    coordinator.on_resumed()
    await drain(coordinator)
    stable_update(coordinator, at=14)
    assert "reason=attempt_budget" in caplog.messages[-1]
    coordinator.on_final("tell me about yourself", observed_at=15)
    coordinator.on_interim("tell me about yourself", observed_at=16)
    assert "reason=turn_closed" in caplog.messages[-1]
    coordinator.on_start()
    coordinator.on_final("", observed_at=17)
    assert "update_count=0" in next(m for m in reversed(caplog.messages) if "event=final" in m)
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_early_stability_admission_and_content_free_timing(caplog):
    class TimedProbe:
        async def first_token_at(self, transcript):
            return 10.75

    coordinator = early_coordinator(TimedProbe())
    with caplog.at_level("INFO", logger="shuo.bluetooth.speculation"):
        coordinator.on_interim("tell me about yourself", observed_at=10)
        coordinator.on_interim("tell me about yourself", observed_at=10.09)
        assert coordinator.active_generation_id is None
        coordinator.on_interim("tell me about yourself", observed_at=10.25)
        await drain(coordinator)
        generation = coordinator.active_generation_id
        coordinator.on_eager("tell me about yourself", observed_at=10.8)
        assert coordinator.active_generation_id == generation
        coordinator.on_final("tell me about yourself", observed_at=11)
    observation = coordinator.observations[-1]
    assert observation.outcome == "ready_before_final"
    assert observation.trigger == "interim"
    assert observation.trigger_to_first_token_ms == 500
    assert observation.trigger_to_final_ms == 750
    assert observation.speculative_lead_ms == 250
    assert observation.eager_to_final_ms is None
    assert "tell me about yourself" not in caplog.text
    await coordinator.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["tell me about yourself please", "what is your experience", ""])
async def test_early_extension_replacement_or_empty_invalidates_immediately(replacement):
    probe = BlockingProbe()
    coordinator = early_coordinator(probe)
    stable_update(coordinator)
    await probe.started.wait()
    coordinator.on_interim(replacement, observed_at=10.3)
    assert coordinator.active_generation_id is None
    assert coordinator.observations[-1].outcome == "replaced"
    await drain(coordinator)
    coordinator.on_interim(replacement, observed_at=10.6)
    assert probe.calls == ["tell me about yourself"]  # cooldown
    coordinator.on_interim(replacement, observed_at=11.3)
    if replacement:
        assert coordinator.active_generation_id == 2
    else:
        assert coordinator.active_generation_id is None
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_resumed_requires_fresh_stability_and_keeps_attempt_budget():
    probe = BlockingProbe()
    coordinator = early_coordinator(probe)
    stable_update(coordinator)
    await probe.started.wait()
    coordinator.on_resumed()
    await drain(coordinator)
    assert coordinator.observations[-1].outcome == "resumed"
    coordinator.on_interim("tell me about yourself", observed_at=12)
    assert coordinator.active_generation_id is None
    coordinator.on_interim("tell me about yourself", observed_at=12.25)
    assert coordinator.active_generation_id == 2
    coordinator.on_resumed()
    await drain(coordinator)
    stable_update(coordinator, at=14)
    coordinator.on_eager("tell me about yourself", observed_at=15)
    assert coordinator.active_generation_id is None
    coordinator.on_final("tell me about yourself", observed_at=16)
    assert coordinator.observations[-1].outcome == "discarded_by_final"
    assert coordinator.observations[-1].transcript_match is True
    assert coordinator.observations[-1].first_token_before_final is False
    coordinator.on_start()
    stable_update(coordinator, at=17)
    assert coordinator.active_generation_id == 3
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_stale_completion_and_slow_close_never_overlap_or_become_eligible():
    class SlowCancelProbe(BlockingProbe):
        async def first_token_at(self, transcript):
            self.calls.append(transcript)
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()  # simulate asynchronous stream close
                return 10.75  # late completion from cancelled generation

    probe = SlowCancelProbe()
    coordinator = early_coordinator(probe, AsyncCapacityGate(3))
    stable_update(coordinator)
    await probe.started.wait()
    coordinator.on_resumed()
    stable_update(coordinator, "what is your experience", at=12)
    assert coordinator.active_generation_id is None
    assert len(probe.calls) == 1
    probe.release.set()
    await drain(coordinator)
    coordinator.on_final("tell me about yourself", observed_at=13)
    assert coordinator.observations[-1].outcome == "discarded_by_final"
    assert coordinator.observations[-1].trigger_to_first_token_ms is None
    assert not any(o.outcome == "ready_before_final" for o in coordinator.observations)
    await coordinator.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [True, False])
async def test_early_final_match_and_mismatch_cancel_unfinished_work(matches):
    probe = BlockingProbe()
    coordinator = early_coordinator(probe)
    stable_update(coordinator)
    await probe.started.wait()
    coordinator.on_final("tell me about yourself" if matches else "a changed final")
    await drain(coordinator)
    observation = coordinator.observations[-1]
    assert observation.outcome == ("not_ready_by_final" if matches else "transcript_mismatch")
    assert observation.transcript_match is matches
    stable_update(coordinator, at=20)  # Updates after final cannot start a new turn
    coordinator.on_eager("another eager", observed_at=22)
    assert len(probe.calls) == 1
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_admission_budget_bounds_capacity_retries_and_eager_fallback():
    gate = AsyncCapacityGate(1, max_wait_seconds=0.001)
    assert await gate.acquire()
    probe = BlockingProbe()
    coordinator = early_coordinator(probe, gate)
    stable_update(coordinator)
    await drain(coordinator)
    assert coordinator.observations[-1].outcome == "capacity_skip"
    stable_update(coordinator, at=12)
    await drain(coordinator)
    stable_update(coordinator, at=14)
    coordinator.on_eager("eager fallback", observed_at=16)
    assert len(coordinator.observations) == 2
    assert probe.calls == []
    gate.release()
    coordinator.on_final("tell me about yourself")
    coordinator.on_start()
    coordinator.on_eager("short turn", observed_at=20)
    await probe.started.wait()
    assert probe.calls == ["short turn"]
    await coordinator.cleanup()
    assert await gate.acquire()
    gate.release()


@pytest.mark.asyncio
async def test_small_mutations_short_and_oversized_updates_do_not_start():
    probe = BlockingProbe()
    coordinator = early_coordinator(probe)
    for i, text in enumerate(["", "one", "two words", "word " * 501]):
        stable_update(coordinator, text, at=10 + i)
    for i in range(20):
        coordinator.on_interim("word " * (i + 3), observed_at=20 + i * 0.1)
    assert coordinator.active_generation_id is None
    await coordinator.cleanup()
    stable_update(coordinator, at=40)
    coordinator.on_eager("after cleanup")
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_cleanup_waits_for_cancel_close_without_recancelling_it():
    closing = asyncio.Event()
    release_close = asyncio.Event()

    class ClosingProbe(BlockingProbe):
        async def first_token_at(self, transcript):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release_close.wait()

    probe = ClosingProbe()
    coordinator = early_coordinator(probe)
    stable_update(coordinator)
    await probe.started.wait()
    coordinator.on_resumed()
    await closing.wait()
    cleanup = asyncio.create_task(coordinator.cleanup())
    await asyncio.sleep(0)
    assert not cleanup.done()
    release_close.set()
    await cleanup
    await coordinator.cleanup()
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_probe_error_falls_back_and_does_not_log_exception_content(caplog):
    class ErrorProbe:
        async def first_token_at(self, transcript):
            raise RuntimeError("private provider content")

    coordinator = early_coordinator(ErrorProbe())
    with caplog.at_level("INFO", logger="shuo.bluetooth.speculation"):
        stable_update(coordinator)
        await drain(coordinator)
        coordinator.on_final("tell me about yourself")
    assert coordinator.observations[0].outcome == "probe_error:RuntimeError"
    assert coordinator.observations[-1].outcome == "discarded_by_final"
    assert "private provider content" not in caplog.text
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_shadow_timeout_releases_capacity(monkeypatch):
    monkeypatch.setattr("shuo.bluetooth.speculative.PROBE_TIMEOUT_SECONDS", 0.001)
    gate = AsyncCapacityGate(1)
    coordinator = early_coordinator(BlockingProbe(), gate)
    stable_update(coordinator)
    await drain(coordinator)
    assert coordinator.observations[-1].outcome == "probe_error:TimeoutError"
    assert await gate.acquire()
    gate.release()
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_real_shadow_provider_cancellation_closes_stream_and_preserves_history():
    started = asyncio.Event()

    class WaitingStream(FakeStream):
        async def __anext__(self):
            started.set()
            await asyncio.Event().wait()

    stream = WaitingStream([])
    history = [{"role": "assistant", "content": "committed answer"}]
    before = [dict(item) for item in history]
    probe = ShadowLLMProbe(system_prompt="system", history_provider=lambda: history, client=FakeClient(stream))
    coordinator = early_coordinator(probe)
    stable_update(coordinator)
    await started.wait()
    coordinator.on_resumed()
    await coordinator.cleanup()
    assert stream.closed
    assert history == before
