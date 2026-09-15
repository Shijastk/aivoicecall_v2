from __future__ import annotations

import pytest

from shuo.benchmark.conversation import (
    BenchmarkReport,
    Metric,
    analyze_bluetooth_log,
    compare_reports,
    run_offline_benchmark,
)


@pytest.mark.asyncio
async def test_offline_benchmark_exercises_barge_in_and_replacement_answer():
    report = await run_offline_benchmark()
    checks = {item.name: item for item in report.checks}

    assert checks["conversation_runner_completed_scenario"].passed is True
    assert checks["barge_in_cancelled_exactly_one_normal_turn"].passed is True
    assert checks["playback_clear_observed"].passed is True
    assert checks["replacement_answer_started_once"].passed is True
    assert checks["replacement_answer_reached_playback_dispatch"].passed is True
    assert checks["semantic_answer_quality"].status == "NOT_MEASURED"

    metrics = {item.name: item for item in report.metrics}
    assert metrics["eot_to_agent_start"].count == 2
    assert metrics["barge_start_to_agent_cancel_return"].count == 1
    assert metrics["barge_start_to_playback_clear"].count == 1
    assert all(value >= 0 for metric in report.metrics for value in metric.samples_ms)


@pytest.mark.asyncio
async def test_provider_benchmark_requires_explicit_network_permission():
    from shuo.benchmark.conversation import run_provider_benchmark

    with pytest.raises(PermissionError, match="opt-in"):
        await run_provider_benchmark(allow_provider_network=False)


def test_log_analyzer_never_turns_missing_boundaries_into_zero():
    report = analyze_bluetooth_log("13:00:00.000 │ unrelated")
    metrics = {item.name: item for item in report.metrics}

    assert metrics["flux_eot_to_agent_start"].samples_ms == ()
    assert metrics["flux_eot_to_agent_start"].median_ms is None
    assert metrics["caller_heard_first_audio"].status == "NOT_MEASURED"
    assert metrics["caller_heard_first_audio"].samples_ms == ()


def test_log_analyzer_extracts_only_explicit_lifecycle_fields():
    text = "\n".join(
        [
            "13:00:00.000 │ BTLifecycle: event=FluxStartOfTurnEvent phase_before=RESPONDING phase_after=LISTENING normal_turn=2 actions=ResetAgentTurnAction queue_depth=0",
            "13:00:00.001 │ BTLifecycle: event=AgentCancel_returned normal_turn=2 elapsed_ms=0.5",
            "13:00:00.100 │ BTLatency: Flux EndOfTurn -> Agent start 0.2ms",
            "13:00:00.500 │   Agent: ⏱  LLM first token  +400ms",
            "13:00:00.750 │   Agent: ⏱  TTS first audio  +650ms  (TTS latency 250ms) turn=3",
            "13:00:02.000 │   Agent: ⏱  Playback dispatched  +1900ms total turn=3",
            "13:00:02.010 │ BTLifecycle: event=PlaybackFirstWrite_returned pcm_bytes=640",
            "13:00:02.020 │ BTLifecycle: event=PlaybackClear_returned elapsed_ms=0.0",
            "13:00:02.030 │ BTShadow: generation=4 outcome=ready_before_final chars=20 eager_to_final_ms=500.0 shadow_ttft_ms=350.0 ready_before_final=True transcript_match=True trigger=eager trigger_to_final_ms=500.0 trigger_to_first_token_ms=350.0 speculative_lead_ms=150.0",
        ]
    )
    report = analyze_bluetooth_log(text)
    metrics = {item.name: item for item in report.metrics}

    assert metrics["agent_cancel_elapsed"].samples_ms == (0.5,)
    assert metrics["flux_eot_to_agent_start"].samples_ms == (0.2,)
    assert metrics["agent_start_to_llm_first_token"].samples_ms == (400.0,)
    assert metrics["llm_first_token_to_tts_first_audio"].samples_ms == (250.0,)
    assert metrics["agent_start_to_tts_first_audio"].samples_ms == (650.0,)
    assert metrics["agent_start_to_local_playback_dispatch_complete"].samples_ms == (1900.0,)
    assert metrics["barge_start_log_to_cancel_return_log"].samples_ms == (1.0,)
    assert metrics["shadow_useful_ready_before_final_lead"].samples_ms == (150.0,)



def test_log_analyzer_requires_clear_only_when_player_was_actively_playing():
    text = "\n".join(
        [
            # First barge-in lands before the player ever starts. No clear is expected.
            "13:00:00.000 │ BTLifecycle: event=FluxStartOfTurnEvent phase_before=RESPONDING phase_after=LISTENING normal_turn=3 actions=ResetAgentTurnAction queue_depth=0",
            "13:00:00.001 │ BTLifecycle: event=AgentCancel_begin normal_turn=3",
            "13:00:00.002 │   Agent: Lifecycle: turn=3 cancel_stage=llm_begin",
            "13:00:00.003 │   Agent: Lifecycle: turn=3 cancel_stage=llm_returned",
            "13:00:00.004 │   Agent: Lifecycle: turn=3 cancel_stage=tts_begin",
            "13:00:00.005 │   Agent: Lifecycle: turn=3 cancel_stage=tts_returned",
            "13:00:00.006 │ BTLifecycle: event=AgentCancel_returned normal_turn=3 elapsed_ms=5.0",
            # Second barge-in has an active player. This one must clear.
            "13:00:01.000 │ BTLifecycle: event=FluxStartOfTurnEvent phase_before=RESPONDING phase_after=LISTENING normal_turn=4 actions=ResetAgentTurnAction queue_depth=0",
            "13:00:01.001 │ BTLifecycle: event=AgentCancel_begin normal_turn=4",
            "13:00:01.002 │   Agent: Lifecycle: turn=4 cancel_stage=llm_begin",
            "13:00:01.003 │   Agent: Lifecycle: turn=4 cancel_stage=llm_returned",
            "13:00:01.004 │   Agent: Lifecycle: turn=4 cancel_stage=tts_begin",
            "13:00:01.005 │   Agent: Lifecycle: turn=4 cancel_stage=tts_returned",
            "13:00:01.006 │   Agent: Lifecycle: turn=4 cancel_stage=player_begin dispatched_bytes=160",
            "13:00:01.007 │ BTLifecycle: event=PlaybackClear_begin dispatched_chunks=1",
            "13:00:01.008 │ BTLifecycle: event=PlaybackClear_returned elapsed_ms=0.0",
            "13:00:01.009 │   Agent: Lifecycle: turn=4 cancel_stage=player_returned",
            "13:00:01.010 │ BTLifecycle: event=AgentCancel_returned normal_turn=4 elapsed_ms=9.0",
        ]
    )

    report = analyze_bluetooth_log(text)
    checks = {item.name: item for item in report.checks}
    check = checks["playback_clear_for_active_player_cancels"]

    assert check.passed is True
    assert "active_player_cancels=1" in check.note
    assert "required_clears_missing=0" in check.note
    assert "cancels_before_player_started=1" in check.note

def test_compare_refuses_different_provenance():
    before = BenchmarkReport(
        mode="a",
        metrics=[Metric("x", (10.0,), "PROVEN", "scope-a", "s", "e", "clock")],
    )
    after = BenchmarkReport(
        mode="b",
        metrics=[Metric("x", (9.0,), "PROVEN", "scope-b", "s", "e", "clock")],
    )

    report = compare_reports(before, after)
    assert report.raw["comparisons"] == []
    assert report.checks[0].status == "NOT_COMPARABLE"


def test_compare_reports_observed_delta_without_causal_claim():
    before = BenchmarkReport(
        mode="a",
        metrics=[Metric("x", (10.0, 12.0), "PROVEN", "scope", "s", "e", "clock")],
    )
    after = BenchmarkReport(
        mode="a",
        metrics=[Metric("x", (8.0, 10.0), "PROVEN", "scope", "s", "e", "clock")],
    )

    report = compare_reports(before, after)
    row = report.raw["comparisons"][0]
    assert row["delta_median_ms"] == -2.0
    assert row["interpretation"] == "OBSERVED_DELTA_ONLY_NO_CAUSAL_OR_SIGNIFICANCE_CLAIM"


def test_flux_fixture_word_error_rate_is_deterministic():
    from shuo.benchmark.flux_pipeline import word_error_rate

    assert word_error_rate("What is the benchmark codeword?", "what is the benchmark codeword") == 0.0
    assert word_error_rate("one two three four", "one two four") == 0.25


def test_flux_fixture_loader_verifies_hash_and_audio_contract(tmp_path):
    import hashlib
    import json

    from shuo.benchmark.flux_pipeline import FIXTURE_SCHEMA, load_flux_fixture

    audio = b"\x00\x00" * 320
    audio_path = tmp_path / "fixture.s16le"
    manifest_path = tmp_path / "fixture.json"
    audio_path.write_bytes(audio)
    manifest_path.write_text(
        json.dumps(
            {
                "schema": FIXTURE_SCHEMA,
                "source_text": "What is the benchmark codeword?",
                "sha256": hashlib.sha256(audio).hexdigest(),
                "bytes": len(audio),
                "sample_rate": 16000,
                "sample_width_bytes": 2,
                "channels": 1,
                "encoding": "s16le",
                "generator": "test",
                "voice_id": "test-voice",
            }
        ),
        encoding="utf-8",
    )

    loaded, manifest = load_flux_fixture(audio_path, manifest_path)
    assert loaded == audio
    assert manifest.bytes == len(audio)
    assert manifest.duration_ms == 20.0

    audio_path.write_bytes(audio + b"\x00\x00")
    with pytest.raises(ValueError, match="byte count mismatch"):
        load_flux_fixture(audio_path, manifest_path)


def test_flux_fixture_split_preserves_every_byte():
    from shuo.benchmark.flux_pipeline import _split_pcm_frames

    audio = (b"\x01\x02" * 1000)
    frames = _split_pcm_frames(audio)
    assert b"".join(frames) == audio
    assert all(len(frame) <= 640 for frame in frames)
    assert all(len(frame) % 2 == 0 for frame in frames)


@pytest.mark.asyncio
async def test_flux_fixture_generation_requires_explicit_network_permission(tmp_path):
    from shuo.benchmark.flux_pipeline import prepare_flux_fixture

    with pytest.raises(PermissionError, match="opt-in"):
        await prepare_flux_fixture(
            audio_path=tmp_path / "fixture.s16le",
            manifest_path=tmp_path / "fixture.json",
            allow_provider_network=False,
        )


@pytest.mark.asyncio
async def test_flux_pipeline_requires_explicit_network_permission(tmp_path):
    from shuo.benchmark.flux_pipeline import run_flux_pipeline_benchmark

    with pytest.raises(PermissionError, match="opt-in"):
        await run_flux_pipeline_benchmark(
            audio_path=tmp_path / "missing.s16le",
            manifest_path=tmp_path / "missing.json",
            allow_provider_network=False,
        )
