from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from shuo.bluetooth.codec import BluetoothOutboundCodec
from shuo.bluetooth.conversation import run_bluetooth_conversation
from shuo.runtime_config import CallSettings
from shuo.services.tts_pocket import PocketTTSService, pocket_tts_available
from shuo.services.tts_pool import TTSPool
from shuo.services.tts_provider import (
    tts_fallback_provider_name,
    tts_provider_name,
    tts_required_env_vars,
    validate_tts_provider_config,
)

from .conversation import (
    BenchmarkReport,
    CheckResult,
    Metric,
    _Capture,
    _InstrumentedAgent,
    _NullTracer,
    _Recorder,
    _base_metadata,
    _ms_between,
    _ns,
    _present,
    _wait_until,
)
from .flux_pipeline import _MeasuredFlux, _RealtimeReplaySession


HUMAN_SIM_EOT_THRESHOLD = 0.8
DEFAULT_THINKING_PAUSE_MS = 650
MAX_SCENARIO_SECONDS = 300.0

HUMAN_SIM_SYSTEM_PROMPT = (
    "You are running a deterministic synthetic spoken-conversation benchmark. "
    "Speak naturally and briefly unless the caller explicitly asks for a detailed explanation. "
    "The benchmark codeword is ORBIT-7. If asked for it, include ORBIT-7. "
    "Remember facts the caller states during this conversation and answer later follow-ups from that history. "
    "If asked for a benchmark fact that has not been provided, say exactly: I don't have that detail. "
    "When explicitly asked for a detailed six-sentence explanation, give at least six short spoken sentences "
    "so an interruption can occur while speech is still being produced. "
    "Do not use markdown and do not claim to be an AI or language model."
)


@dataclass(frozen=True)
class _HumanSimResult:
    name: str
    passed: Optional[bool]
    note: str = ""


async def _pocket_text_to_pcm(text: str, *, timeout_seconds: float) -> bytes:
    """Synthesize one caller utterance fully in memory.

    Pocket emits the repository's normal mu-law/8k provider contract. The
    existing Bluetooth outbound codec converts that to the validated isolated
    S16LE/16k boundary used by the replay session. No audio file is written.
    """

    if not pocket_tts_available():
        raise RuntimeError(
            "human-sim requires the optional Pocket TTS package; "
            "install requirements-pocket-tts.txt first"
        )

    chunks: list[bytes] = []
    done = asyncio.Event()

    async def on_audio(audio_base64: str) -> None:
        chunks.append(base64.b64decode(audio_base64))

    async def on_done() -> None:
        done.set()

    tts = PocketTTSService(on_audio=on_audio, on_done=on_done)
    try:
        await asyncio.wait_for(tts.start(), timeout=timeout_seconds)
        await asyncio.wait_for(tts.send(text), timeout=timeout_seconds)
        await asyncio.wait_for(tts.flush(), timeout=timeout_seconds)
        await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        if tts.fatal_error:
            raise RuntimeError(tts.fatal_error)
    finally:
        await tts.cancel()

    mulaw = b"".join(chunks)
    if not mulaw:
        raise RuntimeError("Pocket caller synthesis returned no audio")

    codec = BluetoothOutboundCodec()
    pcm = codec.feed(mulaw) + codec.finish()
    if not pcm:
        raise RuntimeError("Pocket caller synthesis produced no S16LE16k audio")
    return pcm


async def _prepare_stimuli(
    texts: dict[str, str],
    *,
    timeout_seconds: float,
) -> dict[str, bytes]:
    prepared: dict[str, bytes] = {}
    for name, text in texts.items():
        prepared[name] = await _pocket_text_to_pcm(
            text,
            timeout_seconds=timeout_seconds,
        )
    return prepared


async def run_human_sim_benchmark(
    *,
    allow_provider_network: bool,
    repo_root: Optional[Path] = None,
    timeout_seconds: float = 45.0,
    thinking_pause_ms: int = DEFAULT_THINKING_PAUSE_MS,
    max_scenario_seconds: float = MAX_SCENARIO_SECONDS,
) -> BenchmarkReport:
    """Run a multi-turn human-like synthetic caller through real Flux/Groq/Pocket.

    This is intentionally *not* a Bluetooth-device or cellular benchmark.
    Caller speech is pre-synthesized in memory with Pocket, replayed at the
    validated 20 ms S16LE/16k Bluetooth boundary cadence, and then traverses the
    real inbound codec -> Deepgram Flux -> SHUO state machine -> Groq -> Pocket
    -> AudioPlayer path.

    It exercises a deliberate mid-question pause, two independent barge-ins and
    short-session continuity without storing raw audio. It cannot truthfully
    measure PipeWire/HFP/cellular/handset or caller-heard latency.
    """

    if not allow_provider_network:
        raise PermissionError(
            "human-sim contacts Deepgram and Groq. "
            "Pass --allow-provider-network explicitly."
        )
    if not 100 <= thinking_pause_ms <= 1500:
        raise ValueError("thinking_pause_ms must be between 100 and 1500")
    if not 30.0 <= max_scenario_seconds <= MAX_SCENARIO_SECONDS:
        raise ValueError(
            f"max_scenario_seconds must be between 30 and {int(MAX_SCENARIO_SECONDS)}"
        )

    provider_error = validate_tts_provider_config()
    if provider_error:
        raise ValueError(provider_error)
    if tts_provider_name() != "pocket" or tts_fallback_provider_name():
        raise ValueError(
            "human-sim is locked to TTS_PROVIDER=pocket and an empty "
            "TTS_FALLBACK_PROVIDER so the measured agent TTS path is unambiguous"
        )

    missing = [
        name
        for name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", *tts_required_env_vars())
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    stimuli_text = {
        "codeword": "What is the benchmark codeword? Keep the answer short.",
        "continuity_seed": (
            "Please remember this for later in our conversation. "
            "My benchmark fruit is mango. Reply briefly."
        ),
        "normal_one": "In one short answer, what does a car brake do?",
        "pause_a": "I want to ask you something about driving",
        "pause_b": "what does a clutch do in a manual car?",
        "normal_two": "Give me one short difference between petrol and diesel engines.",
        "barge_one_setup": (
            "Give me a detailed six-sentence spoken explanation of how a petrol engine works."
        ),
        "barge_one": "Stop there. What benchmark fruit did I tell you earlier?",
        "unknown": "What is the benchmark launch city?",
        "barge_two_setup": (
            "Give me a detailed six-sentence spoken explanation of how a manual gearbox works."
        ),
        "barge_two": "Interrupting you. What is the benchmark codeword?",
        "continuity_final": "One last check. What benchmark fruit did I tell you earlier?",
    }

    # Pre-synthesize before the measured conversation. Caller generation must
    # not contend with the agent's Pocket inference lock and distort turn data.
    stimuli = await _prepare_stimuli(
        stimuli_text,
        timeout_seconds=timeout_seconds,
    )

    capture = _Capture()
    recorder = _Recorder(capture)
    session = _RealtimeReplaySession()
    holder: dict[str, Any] = {}
    ready = asyncio.Event()
    settings = CallSettings(
        system_prompt=HUMAN_SIM_SYSTEM_PROMPT,
        voice_id=CallSettings.builtin().voice_id,
        prompt_source="benchmark-human-sim",
        voice_source="pocket-local",
        rules_chars=0,
        knowledge_chars=0,
    )
    pool = TTSPool(pool_size=1, ttl=8.0, voice_id=settings.voice_id)
    pool_started = False

    def flux_factory(eot, sot, interim):
        flux = _MeasuredFlux(
            eot,
            sot,
            interim,
            source_session=session,
            eot_threshold=HUMAN_SIM_EOT_THRESHOLD,
        )
        holder["flux"] = flux
        return flux

    async def agent_factory(outbound, done):
        nonlocal pool_started
        if not pool_started:
            await pool.start()
            pool_started = True
            await pool.wait_ready(timeout=min(timeout_seconds, 15.0))
        agent = _InstrumentedAgent(
            session=outbound,
            on_done=done,
            tts_pool=pool,
            tracer=_NullTracer(),
            recorder=recorder,
            settings=settings,
            benchmark_capture=capture,
        )
        holder["agent"] = agent
        ready.set()
        return agent

    scenario_results: list[_HumanSimResult] = []
    barge_latencies: list[float] = []
    barge_clear_latencies: list[float] = []
    barge_late_local_writes: list[int] = []
    scenario_started_ns: Optional[int] = None
    scenario_ended_ns: Optional[int] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task] = None

    async def wait_for_response(
        capture_index: int,
        *,
        timeout: float = timeout_seconds,
    ) -> None:
        await _wait_until(
            lambda: (
                len(capture.turns) > capture_index
                and capture.turns[capture_index].playback_dispatch_ns is not None
                and not holder["agent"].is_turn_active
            ),
            timeout,
        )

    async def normal_turn(name: str) -> int:
        flux: _MeasuredFlux = holder["flux"]
        before_flux = len(flux.turns)
        before_capture = len(capture.turns)
        await session.play_fixture(stimuli[name], timeout_seconds=timeout_seconds)
        await flux.wait_for_turn(before_flux + 1, timeout_seconds=timeout_seconds)
        await wait_for_response(before_capture)
        return before_capture

    async def barge_turn(setup_name: str, barge_name: str) -> tuple[int, int, int]:
        flux: _MeasuredFlux = holder["flux"]
        before_flux = len(flux.turns)
        before_capture = len(capture.turns)
        before_writes = len(session.write_times_ns)
        before_clears = len(session.clear_times_ns)

        await session.play_fixture(stimuli[setup_name], timeout_seconds=timeout_seconds)
        await flux.wait_for_turn(before_flux + 1, timeout_seconds=timeout_seconds)
        await _wait_until(
            lambda: (
                len(capture.turns) > before_capture
                and capture.turns[before_capture].first_audio_ns is not None
                and len(session.write_times_ns) > before_writes
                and holder["agent"].is_turn_active
            ),
            timeout_seconds,
        )

        # Synthetic caller begins while the agent is already emitting audio.
        await session.play_fixture(stimuli[barge_name], timeout_seconds=timeout_seconds)
        replacement_flux = await flux.wait_for_turn(
            before_flux + 2,
            timeout_seconds=timeout_seconds,
        )
        await _wait_until(
            lambda: (
                capture.turns[before_capture].cancel_return_ns is not None
                and len(session.clear_times_ns) > before_clears
            ),
            timeout_seconds,
        )
        await wait_for_response(before_capture + 1)

        cancel_ns = capture.turns[before_capture].cancel_return_ns
        start_ns = replacement_flux.start_of_turn_ns
        clear_ns = session.clear_times_ns[before_clears]
        if cancel_ns is not None and start_ns is not None:
            value = _ms_between(start_ns, cancel_ns)
            if value is not None:
                barge_latencies.append(value)
        if start_ns is not None:
            value = _ms_between(start_ns, clear_ns)
            if value is not None:
                barge_clear_latencies.append(value)

        replacement_first_audio_ns = capture.turns[before_capture + 1].first_audio_ns
        late_local_writes = 0
        if replacement_first_audio_ns is not None:
            late_local_writes = sum(
                1
                for write_ns in session.write_times_ns
                if clear_ns < write_ns < replacement_first_audio_ns
            )
        barge_late_local_writes.append(late_local_writes)

        return before_capture, before_capture + 1, late_local_writes

    try:
        task = asyncio.create_task(
            run_bluetooth_conversation(
                session,
                flux_factory=flux_factory,
                agent_factory=agent_factory,
                call_id="benchmark-human-sim",
            )
        )
        await asyncio.wait_for(ready.wait(), timeout_seconds)
        scenario_started_ns = _ns()

        # Establish several ordinary turns before interruption tests.
        await normal_turn("codeword")
        await asyncio.sleep(0.35)
        await normal_turn("continuity_seed")
        await asyncio.sleep(0.55)
        await normal_turn("normal_one")
        await asyncio.sleep(0.45)

        # Thinking-pause turn. The pass criterion is deliberately narrow:
        # no real Flux final EndOfTurn may arrive during the configured pause.
        flux: _MeasuredFlux = holder["flux"]
        before_pause_flux = len(flux.turns)
        before_pause_capture = len(capture.turns)
        await session.play_fixture(stimuli["pause_a"], timeout_seconds=timeout_seconds)
        pause_started_ns = _ns()
        await asyncio.sleep(thinking_pause_ms / 1000.0)
        premature_eot = len(flux.turns) > before_pause_flux
        pause_ended_ns = _ns()

        scenario_results.append(
            _HumanSimResult(
                "thinking_pause_no_premature_eot",
                not premature_eot,
                (
                    f"pause_ms={(pause_ended_ns - pause_started_ns) / 1_000_000.0:.1f}; "
                    f"eot_threshold={HUMAN_SIM_EOT_THRESHOLD}"
                ),
            )
        )

        if premature_eot:
            # Preserve evidence and recover deterministically rather than
            # pretending the two halves remained one caller turn.
            await wait_for_response(before_pause_capture)
            await normal_turn("pause_b")
        else:
            await session.play_fixture(stimuli["pause_b"], timeout_seconds=timeout_seconds)
            await flux.wait_for_turn(before_pause_flux + 1, timeout_seconds=timeout_seconds)
            await wait_for_response(before_pause_capture)

        await asyncio.sleep(0.45)
        await normal_turn("normal_two")
        await asyncio.sleep(0.45)

        first_cancel, first_replacement, first_late_writes = await barge_turn(
            "barge_one_setup",
            "barge_one",
        )
        first_cancelled = capture.turns[first_cancel].cancel_return_ns is not None
        first_cleared = len(barge_clear_latencies) >= 1
        scenario_results.append(
            _HumanSimResult(
                "barge_in_1_cancel_and_clear",
                first_cancelled and first_cleared,
                "real Deepgram StartOfTurn while SHUO was responding",
            )
        )
        scenario_results.append(
            _HumanSimResult(
                "barge_in_1_no_late_local_audio_before_replacement",
                first_late_writes == 0,
                f"writes_after_clear_before_replacement_first_audio={first_late_writes}",
            )
        )
        first_answer = capture.turns[first_replacement].response_text
        scenario_results.append(
            _HumanSimResult(
                "continuity_after_barge_in",
                "mango" in first_answer.casefold(),
                "replacement answer must recall caller-provided fruit=mango",
            )
        )

        await asyncio.sleep(0.55)
        unknown_index = await normal_turn("unknown")
        unknown_answer = capture.turns[unknown_index].response_text.casefold()
        scenario_results.append(
            _HumanSimResult(
                "unknown_fact_grounded_fallback",
                "i don't have that detail" in unknown_answer,
                "synthetic prompt supplies no benchmark launch city",
            )
        )

        await asyncio.sleep(0.45)
        second_cancel, second_replacement, second_late_writes = await barge_turn(
            "barge_two_setup",
            "barge_two",
        )
        second_cancelled = capture.turns[second_cancel].cancel_return_ns is not None
        second_cleared = len(barge_clear_latencies) >= 2
        scenario_results.append(
            _HumanSimResult(
                "barge_in_2_cancel_and_clear",
                second_cancelled and second_cleared,
                "second independent real Deepgram StartOfTurn during response",
            )
        )
        scenario_results.append(
            _HumanSimResult(
                "barge_in_2_no_late_local_audio_before_replacement",
                second_late_writes == 0,
                f"writes_after_clear_before_replacement_first_audio={second_late_writes}",
            )
        )
        second_answer = capture.turns[second_replacement].response_text.casefold()
        scenario_results.append(
            _HumanSimResult(
                "replacement_codeword_grounding",
                "orbit-7" in second_answer,
                "replacement answer must contain benchmark codeword",
            )
        )

        await asyncio.sleep(0.5)
        continuity_index = await normal_turn("continuity_final")
        continuity_answer = capture.turns[continuity_index].response_text.casefold()
        scenario_results.append(
            _HumanSimResult(
                "late_short_session_continuity",
                "mango" in continuity_answer,
                "later follow-up must remain connected to the earlier caller fact",
            )
        )

        scenario_ended_ns = _ns()
        elapsed = _ms_between(scenario_started_ns, scenario_ended_ns)
        if elapsed is not None:
            scenario_results.append(
                _HumanSimResult(
                    "scenario_within_phase5_five_minute_cap",
                    elapsed <= max_scenario_seconds * 1000.0,
                    f"elapsed_ms={elapsed:.1f}; configured_cap_s={max_scenario_seconds:.1f}",
                )
            )

        scenario_results.append(
            _HumanSimResult(
                "at_least_ten_agent_turns_exercised",
                len(capture.turns) >= 10,
                f"agent_turns={len(capture.turns)}",
            )
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if error is None:
                    error = f"teardown {type(exc).__name__}: {exc}"
        if pool_started:
            try:
                await pool.stop()
            except Exception as exc:
                if error is None:
                    error = f"pool stop {type(exc).__name__}: {exc}"

    scenario_duration_ms = _ms_between(scenario_started_ns, scenario_ended_ns)

    checks = [
        CheckResult(
            name="human_sim_scenario_completed",
            passed=error is None,
            status="PROVEN_PROVIDER_PIPELINE_NO_DEVICE" if error is None else "FAILED",
            note=error or "real Deepgram + Groq + Pocket; no PipeWire/Bluetooth/cellular",
        )
    ]
    for result in scenario_results:
        checks.append(
            CheckResult(
                name=result.name,
                passed=result.passed if error is None else None,
                status=(
                    "PROVEN_PROVIDER_PIPELINE_NO_DEVICE"
                    if error is None
                    else "UNKNOWN"
                ),
                note=result.note,
            )
        )

    checks.extend(
        [
            CheckResult(
                name="real_bluetooth_hfp_used",
                passed=None,
                status="NOT_MEASURED",
                note="Replay session is in memory; no PipeWire/BlueZ device is opened.",
            ),
            CheckResult(
                name="caller_heard_audio",
                passed=None,
                status="NOT_MEASURED",
                note=(
                    "No handset playback acknowledgement/external caller clock exists "
                    "in human-sim mode."
                ),
            ),
            CheckResult(
                name="real_cellular_echo_or_self_loop",
                passed=None,
                status="NOT_MEASURED",
                note=(
                    "Synthetic outbound audio is structurally not fed back into the "
                    "replay source; this cannot qualify real phone/network echo."
                ),
            ),
        ]
    )

    status = "PROVEN_PROVIDER_PIPELINE_NO_DEVICE"
    flux_turns = holder["flux"].turns if holder.get("flux") else []
    paired_turns = min(len(flux_turns), len(capture.turns))
    scope = (
        "Pocket-generated synthetic caller S16LE16k at 20ms cadence -> isolated "
        "Bluetooth inbound codec -> real Deepgram Flux eot_threshold=0.8 -> "
        "real Groq -> configured Pocket TTSPool -> AudioPlayer -> in-memory "
        "Bluetooth outbound adapter; no PipeWire/BlueZ/cellular/handset"
    )
    metrics = [
        Metric(
            name="flux_eot_to_agent_start",
            samples_ms=_present(
                _ms_between(
                    flux_turns[i].end_of_turn_ns,
                    capture.turns[i].agent_start_enter_ns,
                )
                for i in range(paired_turns)
            ),
            status=status if paired_turns else "NOT_MEASURED",
            scope=scope,
            start_boundary="Deepgram Flux EndOfTurn callback receipt",
            end_boundary="Agent.start_turn entry",
        ),
        Metric(
            name="agent_start_to_llm_first_token",
            samples_ms=_present(
                _ms_between(turn.agent_start_enter_ns, turn.first_token_ns)
                for turn in capture.turns
            ),
            status=status if capture.turns else "NOT_MEASURED",
            scope=scope,
            start_boundary="Agent.start_turn entry",
            end_boundary="Agent._on_llm_token first callback",
        ),
        Metric(
            name="llm_first_token_to_tts_first_audio",
            samples_ms=_present(
                _ms_between(turn.first_token_ns, turn.first_audio_ns)
                for turn in capture.turns
            ),
            status=status if capture.turns else "NOT_MEASURED",
            scope=scope,
            start_boundary="Agent._on_llm_token first callback",
            end_boundary="Agent._on_tts_audio first callback",
        ),
        Metric(
            name="agent_start_to_tts_first_audio",
            samples_ms=_present(
                _ms_between(turn.agent_start_enter_ns, turn.first_audio_ns)
                for turn in capture.turns
            ),
            status=status if capture.turns else "NOT_MEASURED",
            scope=scope,
            start_boundary="Agent.start_turn entry",
            end_boundary="Agent._on_tts_audio first callback",
        ),
        Metric(
            name="barge_start_to_agent_cancel_return",
            samples_ms=_present(barge_latencies),
            status=status if barge_latencies else "NOT_MEASURED",
            scope=scope,
            start_boundary="Deepgram Flux StartOfTurn callback receipt",
            end_boundary="Agent.cancel_turn return",
        ),
        Metric(
            name="barge_start_to_playback_clear",
            samples_ms=_present(barge_clear_latencies),
            status=status if barge_clear_latencies else "NOT_MEASURED",
            scope=scope,
            start_boundary="Deepgram Flux StartOfTurn callback receipt",
            end_boundary="in-memory outbound clear() entry",
        ),
        Metric(
            name="scenario_duration",
            samples_ms=_present([scenario_duration_ms]),
            status=status if scenario_duration_ms is not None else "NOT_MEASURED",
            scope=scope,
            start_boundary="human-sim first scenario action after readiness",
            end_boundary="human-sim final scored response",
        ),
        Metric(
            name="caller_heard_first_audio",
            samples_ms=(),
            status="NOT_MEASURED",
            scope="requires real remote endpoint/handset or separately approved external clock",
            start_boundary="caller stops speaking at remote endpoint",
            end_boundary="remote caller hears first reply sample",
            clock="unavailable",
        ),
        Metric(
            name="bluetooth_hfp_transport_delay",
            samples_ms=(),
            status="NOT_MEASURED",
            scope="requires actual PipeWire/BlueZ HFP transport and a valid receive boundary",
            start_boundary="local Bluetooth write/read boundary",
            end_boundary="phone/remote receive boundary",
            clock="unavailable",
        ),
    ]

    combined_answers = "\n".join(
        turn.response_text for turn in capture.turns if turn.response_text
    ).casefold()
    checks.append(
        CheckResult(
            name="no_ai_identity_break_in_scored_answers",
            passed=(
                "as an ai" not in combined_answers
                and "ai language model" not in combined_answers
            )
            if combined_answers
            else None,
            status=status if combined_answers else "NOT_MEASURED",
        )
    )

    return BenchmarkReport(
        mode="human-sim",
        metrics=metrics,
        checks=checks,
        metadata={
            **_base_metadata(repo_root),
            "network_used": True,
            "providers": {
                "stt_turn_detection": "Deepgram Flux",
                "llm": "Groq",
                "agent_tts": "Pocket",
                "caller_stimulus": "Pocket (pre-synthesized in memory)",
            },
            "eot_threshold": HUMAN_SIM_EOT_THRESHOLD,
            "thinking_pause_ms": thinking_pause_ms,
            "max_scenario_seconds": max_scenario_seconds,
            "deepgram_used": True,
            "bluetooth_hardware_used": False,
            "cellular_used": False,
            "raw_audio_written": False,
            "agent_turns": len(capture.turns),
            "flux_final_turns": len(holder["flux"].turns) if holder.get("flux") else 0,
        },
        limitations=[
            "This is a deterministic synthetic human-like caller, not a real human.",
            "It exercises real Deepgram Flux, Groq, Pocket, SHUO state transitions, streaming and cancellation.",
            "It does not open PipeWire/BlueZ, so Bluetooth HFP transport delay is NOT_MEASURED.",
            "It does not traverse a cellular network or remote handset, so caller-heard/mouth-to-ear latency is NOT_MEASURED.",
            "Real phone/network echo and acoustic fallback are NOT_MEASURED.",
            "No raw caller or agent audio is written to disk.",
            "The live Phase 5 runbook remains authoritative for hardware/cellular acceptance and manual answer/hangup.",
        ],
        raw={
            "turns": [asdict(turn) for turn in capture.turns],
            "scenario_results": [asdict(item) for item in scenario_results],
            "session_write_count": len(session.writes),
            "session_clear_count": len(session.clear_times_ns),
            "barge_late_local_writes": list(barge_late_local_writes),
            "source_read_count": session.read_count,
            "source_max_lateness_ms": session.max_source_lateness_ms,
            "error": error,
        },
    )
