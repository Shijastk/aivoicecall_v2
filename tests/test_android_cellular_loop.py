from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from shuo.benchmark.android_cellular_loop import (
    AndroidCellularLoopError,
    AndroidCellularLoopReport,
    LoopCheck,
    LoopMetric,
    _RxTurnMonitor,
    _contains_mango,
    _contains_orbit_seven,
    play_pcm_realtime,
    run_android_cellular_closed_loop,
    synthesize_pocket_pcm,
)
from shuo.benchmark.android_cellular_tx import AndroidBuildTools


class FakePocketService:
    def __init__(
        self,
        on_audio,
        on_done,
        voice_id=None,
        *,
        voice_source=None,
        **_kwargs,
    ):
        self.on_audio = on_audio
        self.on_done = on_done
        self.fatal_error = None
        self.voice_source = voice_source

    async def start(self):
        return None

    async def send(self, text):
        assert text
        await self.on_audio(
            base64.b64encode(b"\xff" * 160).decode("ascii")
        )

    async def flush(self):
        await self.on_done()

    async def cancel(self):
        return None


@pytest.mark.asyncio
async def test_pocket_caller_synthesis_stays_in_existing_provider_and_tx_boundaries():
    pcm = await synthesize_pocket_pcm(
        "synthetic caller",
        pocket_cls=FakePocketService,
    )

    assert pcm
    assert len(pcm) % 2 == 0


class CollectingTx:
    def __init__(self):
        self.chunks = []

    async def write_pcm16(self, data):
        self.chunks.append(bytes(data))


@pytest.mark.asyncio
async def test_realtime_tx_pacer_uses_pcm16_chunks():
    bridge = CollectingTx()
    pcm = b"\x01\x02" * 320

    start_ns, end_ns = await play_pcm_realtime(bridge, pcm)

    assert end_ns >= start_ns
    assert b"".join(bridge.chunks) == pcm
    assert all(len(chunk) % 2 == 0 for chunk in bridge.chunks)


@pytest.mark.asyncio
async def test_rx_turn_monitor_orders_start_and_final_without_serializing_content():
    monitor = _RxTurnMonitor()

    start_before = monitor.start_count
    end_before = monitor.end_count

    await monitor.on_start()
    start = await monitor.wait_for_start(
        start_before,
        timeout_seconds=0.1,
    )

    await monitor.on_end("private synthetic transcript")
    end = await monitor.wait_for_end_after(
        start.at_ns,
        end_before,
        timeout_seconds=0.1,
    )

    assert end.transcript == "private synthetic transcript"
    assert monitor.ended_after(start.at_ns, end_before)


def test_continuity_matchers_are_narrow_and_case_insensitive():
    assert _contains_mango("You told me MANGO.")
    assert not _contains_mango("You told me apple.")

    assert _contains_orbit_seven("The codeword is Orbit Seven.")
    assert _contains_orbit_seven("ORBIT 7")
    assert not _contains_orbit_seven("The codeword is seven.")


def test_report_never_contains_response_transcripts():
    report = AndroidCellularLoopReport(
        checks=[LoopCheck("continuity", True, "boolean only")],
        metrics=[LoopMetric("local_ms", 12.5, "ms")],
        metadata={"raw_audio_written": False},
        limitations=["response text remains in memory only"],
    )

    payload = report.to_json()
    decoded = json.loads(payload)

    assert decoded["passed"] is True
    assert "transcript" not in payload.casefold()
    assert "audio" in payload.casefold()
    assert decoded["metadata"]["raw_audio_written"] is False


@pytest.mark.asyncio
async def test_closed_loop_requires_explicit_provider_network_before_device_work(tmp_path):
    tools = AndroidBuildTools(
        javac="javac",
        android_jar=Path("/unused/android.jar"),
        dx=Path("/unused/dx"),
        adb="adb",
    )

    with pytest.raises(PermissionError, match="allow-provider-network"):
        await run_android_cellular_closed_loop(
            tools=tools,
            repo_root=tmp_path,
            build_dir=tmp_path / "build",
            serial="SERIAL",
            allow_provider_network=False,
        )


@pytest.mark.asyncio
async def test_closed_loop_rejects_pause_that_reaches_final_eot_threshold(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-only")
    tools = AndroidBuildTools(
        javac="javac",
        android_jar=Path("/unused/android.jar"),
        dx=Path("/unused/dx"),
        adb="adb",
    )

    with pytest.raises(ValueError, match="799"):
        await run_android_cellular_closed_loop(
            tools=tools,
            repo_root=tmp_path,
            build_dir=tmp_path / "build",
            serial="SERIAL",
            allow_provider_network=True,
            thinking_pause_ms=800,
        )


def test_closed_loop_latency_output_is_host_correlated_not_caller_heard():
    root = Path(__file__).resolve().parents[1]
    module = (
        root / "shuo" / "benchmark" / "android_cellular_loop.py"
    ).read_text(encoding="utf-8")
    cli = (
        root / "scripts" / "dev" / "18_android_cellular_closed_loop.py"
    ).read_text(encoding="utf-8")

    assert "tx_end_to_observer_start_ms" in module
    assert "MEASURED_HOST_CORRELATED" in module
    assert "response_latency_sample_count" in module
    assert "response_latency_valid_sample_count" in module
    assert "OVERLAP_RESPONSE_STARTED_BEFORE_TX_END" in module
    assert "valid_response_observer_latencies_ms" in module
    assert "response_started_before_tx_end_count" in module
    assert "RESPONSE_LATENCY_KIND=HOST_CORRELATED_OBSERVER" in cli
    assert "RESPONSE_LATENCY_VALID_SAMPLES=" in cli
    assert "STATUS={metric.status}" in cli
    assert "CALLER_HEARD_LATENCY=NOT_MEASURED" in cli


def test_closed_loop_source_has_no_automatic_call_control_or_raw_audio_writer():
    root = Path(__file__).resolve().parents[1]
    module = (
        root / "shuo" / "benchmark" / "android_cellular_loop.py"
    ).read_text(encoding="utf-8")
    cli = (
        root / "scripts" / "dev" / "18_android_cellular_closed_loop.py"
    ).read_text(encoding="utf-8")

    combined = module + "\n" + cli

    assert "VoiceCall Answer" not in combined
    assert "VoiceCall Hangup" not in combined
    assert "dial(" not in combined
    assert "FileOutputStream" not in combined
    assert "wave.open" not in combined
    assert "RAW_AUDIO_PERSISTED=NO" in cli
    assert "CALL_CONTROL=MANUAL" in cli
