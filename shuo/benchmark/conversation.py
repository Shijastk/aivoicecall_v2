from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from unittest.mock import patch

from shuo.agent import Agent
from shuo.bluetooth.conversation import run_bluetooth_conversation
from shuo.runtime_config import CallSettings
from shuo.services.flux import FluxService
from shuo.services.tts_pool import TTSPool
from shuo.services.tts_provider import (
    tts_fallback_provider_name,
    tts_provider_name,
    tts_required_env_vars,
    validate_tts_provider_config,
)


REPORT_SCHEMA = "shuo.conversation-benchmark/v1"
CLOCK = "time.perf_counter_ns"


@dataclass(frozen=True)
class Metric:
    """One measurement with enough provenance to prevent accidental overclaiming."""

    name: str
    samples_ms: tuple[float, ...] = ()
    status: str = "NOT_MEASURED"
    scope: str = ""
    start_boundary: str = ""
    end_boundary: str = ""
    clock: str = CLOCK
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.samples_ms)

    @property
    def median_ms(self) -> Optional[float]:
        if not self.samples_ms:
            return None
        return float(statistics.median(self.samples_ms))

    @property
    def p95_ms(self) -> Optional[float]:
        if not self.samples_ms:
            return None
        values = sorted(self.samples_ms)
        rank = max(1, math.ceil(0.95 * len(values)))
        return float(values[rank - 1])

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["samples_ms"] = list(self.samples_ms)
        out["count"] = self.count
        out["median_ms"] = self.median_ms
        out["p95_ms"] = self.p95_ms
        return out


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: Optional[bool]
    status: str
    note: str = ""


@dataclass
class BenchmarkReport:
    mode: str
    metrics: list[Metric] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def metric(self, name: str) -> Optional[Metric]:
        return next((item for item in self.metrics if item.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "mode": self.mode,
            "metadata": self.metadata,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "checks": [asdict(check) for check in self.checks],
            "limitations": self.limitations,
            "raw": self.raw,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def _ns() -> int:
    return time.perf_counter_ns()


def _ms_between(start_ns: Optional[int], end_ns: Optional[int]) -> Optional[float]:
    if start_ns is None or end_ns is None:
        return None
    return (end_ns - start_ns) / 1_000_000.0


def _present(values: Iterable[Optional[float]]) -> tuple[float, ...]:
    return tuple(float(value) for value in values if value is not None)


def _git_metadata(repo_root: Optional[Path]) -> dict[str, Any]:
    if repo_root is None:
        return {"git_commit": None, "git_dirty": None}
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        return {"git_commit": commit, "git_dirty": dirty}
    except Exception:
        return {"git_commit": None, "git_dirty": None}


def _base_metadata(repo_root: Optional[Path]) -> dict[str, Any]:
    return {
        **_git_metadata(repo_root),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "clock": CLOCK,
        "clock_info": {
            "implementation": time.get_clock_info("perf_counter").implementation,
            "resolution_seconds": time.get_clock_info("perf_counter").resolution,
            "monotonic": time.get_clock_info("perf_counter").monotonic,
            "adjustable": time.get_clock_info("perf_counter").adjustable,
        },
    }


@dataclass
class _TurnCapture:
    ordinal: int
    transcript: str
    agent_start_enter_ns: int
    agent_start_return_ns: Optional[int] = None
    first_token_ns: Optional[int] = None
    first_audio_ns: Optional[int] = None
    playback_dispatch_ns: Optional[int] = None
    cancel_begin_ns: Optional[int] = None
    cancel_return_ns: Optional[int] = None
    response_text: str = ""
    interrupted: Optional[bool] = None


@dataclass
class _Capture:
    turns: list[_TurnCapture] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    timings: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def current(self) -> Optional[_TurnCapture]:
        return self.turns[-1] if self.turns else None


class _Recorder:
    """Minimal CallRecorder-compatible sink used only by the benchmark."""

    def __init__(self, capture: _Capture):
        self.capture = capture

    def turn_started(self, _turn: int) -> None:
        return None

    def timing(self, name: str, ms: int, *, turn: int = 0) -> None:
        self.capture.timings.append((name, int(ms), int(turn)))

    def agent_said(self, text: str, *, turn: int = 0, interrupted: bool = False) -> None:
        current = self.capture.current
        if current is not None:
            current.response_text = text
            current.interrupted = bool(interrupted)

    def note(self, text: str) -> None:
        self.capture.notes.append(text)


class _NullTracer:
    def __init__(self) -> None:
        self._turn = 0

    def begin_turn(self, _transcript: str) -> int:
        self._turn += 1
        return self._turn

    def begin(self, _turn: int, _name: str) -> None:
        return None

    def end(self, _turn: int, _name: str) -> None:
        return None

    def mark(self, _turn: int, _name: str) -> None:
        return None

    def cancel_turn(self, _turn: int) -> None:
        return None

    def save(self, _call_id: str) -> None:
        return None


class _InstrumentedAgent(Agent):
    """Real Agent with observation-only nanosecond markers."""

    def __init__(self, *args, benchmark_capture: _Capture, **kwargs):
        self._benchmark_capture = benchmark_capture
        super().__init__(*args, **kwargs)

    async def start_turn(self, transcript: str) -> None:
        turn = _TurnCapture(
            ordinal=len(self._benchmark_capture.turns) + 1,
            transcript=transcript,
            agent_start_enter_ns=_ns(),
        )
        self._benchmark_capture.turns.append(turn)
        await super().start_turn(transcript)
        turn.agent_start_return_ns = _ns()

    async def cancel_turn(self) -> None:
        current = self._benchmark_capture.current
        if current is not None and current.cancel_begin_ns is None:
            current.cancel_begin_ns = _ns()
        await super().cancel_turn()
        if current is not None and current.cancel_return_ns is None:
            current.cancel_return_ns = _ns()

    async def _on_llm_token(self, token: str) -> None:
        current = self._benchmark_capture.current
        if current is not None and current.first_token_ns is None:
            current.first_token_ns = _ns()
        await super()._on_llm_token(token)

    async def _on_tts_audio(self, audio_base64: str) -> None:
        current = self._benchmark_capture.current
        if current is not None and current.first_audio_ns is None:
            current.first_audio_ns = _ns()
        await super()._on_tts_audio(audio_base64)

    def _on_playback_done(self) -> None:
        current = self._benchmark_capture.current
        if current is not None and current.playback_dispatch_ns is None:
            current.playback_dispatch_ns = _ns()
        super()._on_playback_done()


class _MemorySession:
    """Duck-typed Phase3 session; no hardware and no system-default audio fallback."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.writes: list[bytes] = []
        self.write_times_ns: list[int] = []
        self.clear_times_ns: list[int] = []
        self._block = asyncio.Event()
        self._write_changed = asyncio.Event()
        self._clear_changed = asyncio.Event()

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1
        self._block.set()

    async def read(self) -> bytes:
        await self._block.wait()
        raise RuntimeError("benchmark capture ended")

    async def write(self, audio: bytes) -> None:
        self.writes.append(audio)
        self.write_times_ns.append(_ns())
        self._write_changed.set()

    async def clear(self) -> None:
        self.clear_times_ns.append(_ns())
        self._clear_changed.set()

    async def wait_for_writes(self, count: int, timeout: float = 15.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.writes) < count:
                self._write_changed.clear()
                await self._write_changed.wait()

    async def wait_for_clears(self, count: int, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.clear_times_ns) < count:
                self._clear_changed.clear()
                await self._clear_changed.wait()


class _OfflineFlux(FluxService):
    """Real Flux message parser with the network boundary disabled."""

    current: Optional["_OfflineFlux"] = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.receipts: list[tuple[str, int, str]] = []
        self.ready = asyncio.Event()

    async def start(self) -> None:
        type(self).current = self
        self._running = True
        self.ready.set()

    async def stop(self) -> None:
        self._running = False
        if type(self).current is self:
            type(self).current = None

    async def send(self, _audio_bytes: bytes) -> None:
        return None

    async def emit(self, event: str, transcript: str = "") -> int:
        at = _ns()
        self.receipts.append((event, at, transcript))
        await self._on_message(
            {"type": "TurnInfo", "event": event, "transcript": transcript}
        )
        return at


class _OfflineLLM:
    """Deterministic provider fake; it reports no network/provider latency."""

    def __init__(self, on_token, on_done, **_kwargs):
        self.on_token = on_token
        self.on_done = on_done
        self.history: list[dict[str, str]] = []
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def start(self, text: str) -> None:
        if self.running:
            await self.cancel()
        self.history.append({"role": "user", "content": text})
        self.running = True

        async def generate() -> None:
            answer = (
                "synthetic long answer used only to exercise playback cancellation"
                if "initial" in text
                else "synthetic replacement answer"
            )
            await self.on_token(answer)
            if self.running:
                self.history.append({"role": "assistant", "content": answer})
                await self.on_done()
            self.running = False

        self.task = asyncio.create_task(generate())

    async def cancel(self) -> None:
        self.running = False
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


class _OfflineTTS:
    def __init__(self, on_audio, on_done, payload_size: int):
        self.on_audio = on_audio
        self.on_done = on_done
        self.payload_size = payload_size
        self.cancelled = False

    async def send(self, _token: str) -> None:
        payload = base64.b64encode(b"\xfe" * self.payload_size).decode()
        await self.on_audio(payload)

    async def flush(self) -> None:
        await self.on_done()

    async def cancel(self) -> None:
        self.cancelled = True


class _OfflinePool:
    def __init__(self) -> None:
        self.services: list[_OfflineTTS] = []

    async def get(self, on_audio, on_done):
        # Turn 1 must still be playing when the synthetic barge-in lands.
        # Turn 2 is intentionally short so the harness itself finishes quickly.
        size = 80_000 if not self.services else 640
        service = _OfflineTTS(on_audio, on_done, size)
        self.services.append(service)
        return service


async def _wait_until(predicate, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def _synthetic_metrics(
    capture: _Capture,
    receipts: list[tuple[str, int, str]],
    session: _MemorySession,
    *,
    status: str,
    scope: str,
) -> list[Metric]:
    eots = [at for event, at, _ in receipts if event == "EndOfTurn"]
    starts = [at for event, at, _ in receipts if event == "StartOfTurn"]
    turns = capture.turns

    metrics: list[Metric] = []
    metrics.append(
        Metric(
            name="eot_to_agent_start",
            samples_ms=_present(
                _ms_between(eots[i], turns[i].agent_start_enter_ns)
                for i in range(min(len(eots), len(turns)))
            ),
            status=status,
            scope=scope,
            start_boundary="injected Flux EndOfTurn enters FluxService._on_message",
            end_boundary="Agent.start_turn entry",
        )
    )
    metrics.append(
        Metric(
            name="agent_start_to_first_token",
            samples_ms=_present(
                _ms_between(turn.agent_start_enter_ns, turn.first_token_ns)
                for turn in turns
            ),
            status=status,
            scope=scope,
            start_boundary="Agent.start_turn entry",
            end_boundary="Agent._on_llm_token first callback",
        )
    )
    metrics.append(
        Metric(
            name="first_token_to_first_tts_audio",
            samples_ms=_present(
                _ms_between(turn.first_token_ns, turn.first_audio_ns) for turn in turns
            ),
            status=status,
            scope=scope,
            start_boundary="Agent._on_llm_token first callback",
            end_boundary="Agent._on_tts_audio first callback",
        )
    )
    metrics.append(
        Metric(
            name="agent_start_to_first_tts_audio",
            samples_ms=_present(
                _ms_between(turn.agent_start_enter_ns, turn.first_audio_ns)
                for turn in turns
            ),
            status=status,
            scope=scope,
            start_boundary="Agent.start_turn entry",
            end_boundary="Agent._on_tts_audio first callback",
        )
    )
    cancelled = [turn for turn in turns if turn.cancel_return_ns is not None]
    if cancelled and len(starts) >= 2:
        # The first StartOfTurn opens the first caller turn while LISTENING.
        # The second one is the synthetic barge-in while RESPONDING.
        metrics.append(
            Metric(
                name="barge_start_to_agent_cancel_return",
                samples_ms=_present(
                    [_ms_between(starts[1], cancelled[0].cancel_return_ns)]
                ),
                status=status,
                scope=scope,
                start_boundary="injected Flux StartOfTurn enters FluxService._on_message",
                end_boundary="Agent.cancel_turn return",
            )
        )
    else:
        metrics.append(
            Metric(
                name="barge_start_to_agent_cancel_return",
                status="NOT_MEASURED",
                scope=scope,
                start_boundary="Flux StartOfTurn",
                end_boundary="Agent.cancel_turn return",
                note="No qualifying barge-in/cancel pair was observed.",
            )
        )

    if len(starts) >= 2 and session.clear_times_ns:
        metrics.append(
            Metric(
                name="barge_start_to_playback_clear",
                samples_ms=_present([_ms_between(starts[1], session.clear_times_ns[0])]),
                status=status,
                scope=scope,
                start_boundary="injected Flux StartOfTurn enters FluxService._on_message",
                end_boundary="benchmark session clear() entry",
            )
        )
    else:
        metrics.append(
            Metric(
                name="barge_start_to_playback_clear",
                status="NOT_MEASURED",
                scope=scope,
                start_boundary="Flux StartOfTurn",
                end_boundary="playback clear",
            )
        )

    return metrics


async def run_offline_benchmark(*, repo_root: Optional[Path] = None) -> BenchmarkReport:
    """Run a hardware/provider-free lifecycle replay through real Flux/Agent/Player code."""

    capture = _Capture()
    recorder = _Recorder(capture)
    session = _MemorySession()
    pool = _OfflinePool()
    holder: dict[str, Any] = {}
    ready = asyncio.Event()

    def flux_factory(eot, sot, interim):
        flux = _OfflineFlux(eot, sot, interim)
        holder["flux"] = flux
        return flux

    def agent_factory(outbound, done):
        agent = _InstrumentedAgent(
            session=outbound,
            on_done=done,
            tts_pool=pool,
            tracer=_NullTracer(),
            recorder=recorder,
            benchmark_capture=capture,
        )
        holder["agent"] = agent
        ready.set()
        return agent

    task: Optional[asyncio.Task] = None
    error: Optional[str] = None

    try:
        with patch("shuo.agent.LLMService", _OfflineLLM):
            task = asyncio.create_task(
                run_bluetooth_conversation(
                    session,
                    flux_factory=flux_factory,
                    agent_factory=agent_factory,
                    call_id="benchmark-offline",
                )
            )
            await asyncio.wait_for(ready.wait(), 5.0)
            flux: _OfflineFlux = holder["flux"]

            await flux.emit("StartOfTurn")
            await flux.emit("EndOfTurn", "synthetic initial benchmark question")
            await session.wait_for_writes(1)

            # Real SHUO barge-in signal: StartOfTurn while RESPONDING.
            await flux.emit("StartOfTurn")
            await session.wait_for_clears(1)
            await _wait_until(lambda: not holder["agent"].is_turn_active, timeout=5.0)

            # New caller turn finishes; duplicate final while RESPONDING must not
            # start an extra normal answer.
            await flux.emit("EndOfTurn", "synthetic replacement benchmark question")
            await flux.emit("EndOfTurn", "synthetic replacement benchmark question")
            await session.wait_for_writes(2)
            await _wait_until(
                lambda: len(capture.turns) >= 2
                and capture.turns[1].playback_dispatch_ns is not None,
                timeout=5.0,
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

    checks = [
        CheckResult(
            name="conversation_runner_completed_scenario",
            passed=error is None,
            status="PROVEN_LOCAL_SYNTHETIC" if error is None else "FAILED",
            note=error or "No provider/device/call boundary was contacted.",
        ),
        CheckResult(
            name="barge_in_cancelled_exactly_one_normal_turn",
            passed=(len([turn for turn in capture.turns if turn.cancel_return_ns]) == 1)
            if error is None
            else None,
            status="PROVEN_LOCAL_SYNTHETIC" if error is None else "UNKNOWN",
        ),
        CheckResult(
            name="playback_clear_observed",
            passed=(len(session.clear_times_ns) == 1) if error is None else None,
            status="PROVEN_LOCAL_SYNTHETIC" if error is None else "UNKNOWN",
        ),
        CheckResult(
            name="replacement_answer_started_once",
            passed=(len(capture.turns) == 2) if error is None else None,
            status="PROVEN_LOCAL_SYNTHETIC" if error is None else "UNKNOWN",
            note="Duplicate final while RESPONDING must not create a third turn.",
        ),
        CheckResult(
            name="replacement_answer_reached_playback_dispatch",
            passed=(
                len(capture.turns) >= 2
                and capture.turns[1].playback_dispatch_ns is not None
            )
            if error is None
            else None,
            status="PROVEN_LOCAL_SYNTHETIC" if error is None else "UNKNOWN",
        ),
        CheckResult(
            name="semantic_answer_quality",
            passed=None,
            status="NOT_MEASURED",
            note="Offline mode uses deterministic fake text; it cannot score model answer quality.",
        ),
    ]

    metrics = _synthetic_metrics(
        capture,
        holder.get("flux").receipts if holder.get("flux") is not None else [],
        session,
        status="PROVEN_LOCAL_SYNTHETIC",
        scope="real SHUO orchestration/Agent/AudioPlayer; fake LLM/TTS; no STT/provider/device",
    )

    return BenchmarkReport(
        mode="offline",
        metrics=metrics,
        checks=checks,
        metadata={**_base_metadata(repo_root), "network_used": False},
        limitations=[
            "Synthetic provider timings are not real LLM/TTS latency.",
            "No Deepgram network, PipeWire, Bluetooth hardware, phone, carrier or handset playback is measured.",
            "Local orchestration measurements are machine/scheduler dependent; deltas are reported, not attributed causally.",
            "Caller-heard/mouth-to-ear latency remains NOT_MEASURED.",
        ],
        raw={
            "turns": [asdict(turn) for turn in capture.turns],
            "session_write_count": len(session.writes),
            "session_clear_count": len(session.clear_times_ns),
            "error": error,
        },
    )


BENCHMARK_SYSTEM_PROMPT = (
    "You are running a synthetic voice-agent benchmark, not a real user conversation. "
    "The benchmark codeword is ORBIT-7. If asked for the benchmark codeword, include "
    "ORBIT-7 in a short natural spoken sentence. If asked for a benchmark fact that is "
    "not provided, say: I don't have that detail. Do not use markdown and do not claim "
    "to be an AI or language model."
)


async def run_provider_benchmark(
    *,
    allow_provider_network: bool,
    repo_root: Optional[Path] = None,
    timeout_seconds: float = 45.0,
) -> BenchmarkReport:
    """Exercise real Groq + configured TTS over the real Agent/Player.

    The caller transcript is injected as a Flux TurnInfo event. Therefore the
    measurement includes real LLM/TTS work but explicitly excludes Deepgram
    turn detection, PipeWire, Bluetooth transport, cellular network and handset.
    """

    if not allow_provider_network:
        raise PermissionError(
            "Provider benchmark is opt-in. Pass --allow-provider-network explicitly."
        )
    provider_error = validate_tts_provider_config()
    if provider_error:
        raise ValueError(provider_error)

    missing = [
        name
        for name in ("GROQ_API_KEY", *tts_required_env_vars())
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise RuntimeError("Missing required environment variable(s): " + ", ".join(missing))

    primary_tts = tts_provider_name()
    fallback_tts = tts_fallback_provider_name()
    tts_label = (
        f"{primary_tts}+fallback:{fallback_tts}"
        if fallback_tts
        else primary_tts
    )

    capture = _Capture()
    recorder = _Recorder(capture)
    session = _MemorySession()
    holder: dict[str, Any] = {}
    ready = asyncio.Event()
    pool = TTSPool(pool_size=1, ttl=8.0, voice_id=CallSettings.builtin().voice_id)
    pool_started = False

    settings = CallSettings(
        system_prompt=BENCHMARK_SYSTEM_PROMPT,
        voice_id=CallSettings.builtin().voice_id,
        prompt_source="benchmark-synthetic",
        voice_source="environment/fallback",
        rules_chars=0,
        knowledge_chars=0,
    )

    def flux_factory(eot, sot, interim):
        flux = _OfflineFlux(eot, sot, interim)
        holder["flux"] = flux
        return flux

    async def agent_factory(outbound, done):
        nonlocal pool_started
        if not pool_started:
            await pool.start()
            pool_started = True
            await pool.wait_ready()
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

    task: Optional[asyncio.Task] = None
    error: Optional[str] = None

    try:
        task = asyncio.create_task(
            run_bluetooth_conversation(
                session,
                flux_factory=flux_factory,
                agent_factory=agent_factory,
                call_id="benchmark-providers",
            )
        )
        await asyncio.wait_for(ready.wait(), timeout_seconds)
        flux: _OfflineFlux = holder["flux"]

        await flux.emit("StartOfTurn")
        await flux.emit(
            "EndOfTurn",
            "Explain how a petrol engine works in a detailed spoken answer.",
        )
        await session.wait_for_writes(1, timeout=timeout_seconds)

        # Interrupt as soon as output reaches the local Bluetooth adapter. This
        # proves cancellation/restart without pretending a handset played it.
        await flux.emit("StartOfTurn")
        await session.wait_for_clears(1, timeout=timeout_seconds)
        await _wait_until(lambda: not holder["agent"].is_turn_active, timeout_seconds)

        await flux.emit("EndOfTurn", "Stop. What is the benchmark codeword?")
        await _wait_until(
            lambda: len(capture.turns) >= 2
            and capture.turns[1].playback_dispatch_ns is not None,
            timeout_seconds,
        )

        await flux.emit("StartOfTurn")
        await flux.emit("EndOfTurn", "What is the benchmark launch city?")
        await _wait_until(
            lambda: len(capture.turns) >= 3
            and capture.turns[2].playback_dispatch_ns is not None,
            timeout_seconds,
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

    answer2 = capture.turns[1].response_text if len(capture.turns) >= 2 else ""
    answer3 = capture.turns[2].response_text if len(capture.turns) >= 3 else ""
    combined = f"{answer2}\n{answer3}".lower()

    checks = [
        CheckResult(
            name="provider_scenario_completed",
            passed=error is None,
            status="PROVEN_PROVIDER_NO_STT_HARDWARE" if error is None else "FAILED",
            note=error or (
                f"Groq + configured TTS ({tts_label}) were exercised; "
                "Deepgram/Bluetooth/cellular were not."
            ),
        ),
        CheckResult(
            name="barge_in_cancelled_old_turn",
            passed=(len([turn for turn in capture.turns if turn.cancel_return_ns]) >= 1)
            if error is None
            else None,
            status="PROVEN_PROVIDER_NO_STT_HARDWARE" if error is None else "UNKNOWN",
        ),
        CheckResult(
            name="replacement_answer_contains_grounded_codeword",
            passed=("orbit-7" in answer2.lower()) if answer2 else False,
            status="PROVEN_PROVIDER_NO_STT_HARDWARE" if answer2 else "NOT_MEASURED",
            note="Deterministic synthetic fact check; not a general answer-quality score.",
        ),
        CheckResult(
            name="unknown_fact_uses_grounded_fallback",
            passed=("i don't have that detail" in answer3.lower()) if answer3 else False,
            status="PROVEN_PROVIDER_NO_STT_HARDWARE" if answer3 else "NOT_MEASURED",
            note="Exact phrase is part of the synthetic benchmark prompt.",
        ),
        CheckResult(
            name="no_ai_identity_break_in_scored_answers",
            passed=("ai language model" not in combined and "as an ai" not in combined)
            if (answer2 or answer3)
            else None,
            status="PROVEN_PROVIDER_NO_STT_HARDWARE" if (answer2 or answer3) else "NOT_MEASURED",
        ),
        CheckResult(
            name="caller_heard_audio",
            passed=None,
            status="NOT_MEASURED",
            note="Local adapter write is not a handset playback acknowledgement.",
        ),
    ]

    metrics = _synthetic_metrics(
        capture,
        holder.get("flux").receipts if holder.get("flux") is not None else [],
        session,
        status="PROVEN_PROVIDER_NO_STT_HARDWARE",
        scope=(
            f"real Groq + configured TTS ({tts_label}) + Agent/AudioPlayer; "
            "injected Flux events; no STT/device/cellular/handset"
        ),
    )

    return BenchmarkReport(
        mode="providers",
        metrics=metrics,
        checks=checks,
        metadata={
            **_base_metadata(repo_root),
            "network_used": True,
            "providers": {"llm": "Groq", "tts": tts_label},
            "llm_model_env": os.getenv("LLM_MODEL") or None,
            "deepgram_used": False,
            "bluetooth_hardware_used": False,
        },
        limitations=[
            "Flux/Deepgram turn detection is not measured because EndOfTurn/StartOfTurn are injected.",
            "Bluetooth/PipeWire/cellular/handset latency is not measured.",
            "Local adapter write proves process dispatch only, not audible playback.",
            "Quality checks are deterministic checks against a synthetic prompt; they are not a general naturalness score.",
            "TTS metadata reflects the configured provider route instead of assuming ElevenLabs.",
            "Provider results vary by network/provider load; report raw samples and provenance rather than causal claims.",
        ],
        raw={
            "turns": [asdict(turn) for turn in capture.turns],
            "answers": {
                "replacement": answer2,
                "unknown_fact": answer3,
            },
            "session_write_count": len(session.writes),
            "session_clear_count": len(session.clear_times_ns),
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Live-log analysis. This does not invent a missing boundary or convert local
# dispatch into caller-heard latency.
# ---------------------------------------------------------------------------

_LOG_TIME = re.compile(r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})")
_CANCEL_RETURN = re.compile(r"AgentCancel_returned .*?elapsed_ms=(?P<ms>\d+(?:\.\d+)?)")
_EOT_TO_AGENT = re.compile(r"Flux EndOfTurn -> Agent start (?P<ms>\d+(?:\.\d+)?)ms")
_FIRST_TOKEN = re.compile(r"LLM first token\s+\+(?P<ms>\d+)ms")
_FIRST_AUDIO = re.compile(r"TTS first audio\s+\+(?P<total>\d+)ms\s+\(TTS latency (?P<tts>\d+)ms\)")
_PLAYBACK = re.compile(r"Playback dispatched\s+\+(?P<ms>\d+)ms total")
_SHADOW_OUTCOME = re.compile(r"BTShadow: .*?outcome=(?P<outcome>\S+).*?(?:speculative_lead_ms=(?P<lead>[-\d.]+|None))?")
_PHASE_BARGE = re.compile(
    r"FluxStartOfTurnEvent phase_before=RESPONDING phase_after=LISTENING .*?actions=ResetAgentTurnAction"
)


def _log_clock_ms(line: str) -> Optional[int]:
    match = _LOG_TIME.search(line)
    if not match:
        return None
    return (
        int(match.group("h")) * 3_600_000
        + int(match.group("m")) * 60_000
        + int(match.group("s")) * 1_000
        + int(match.group("ms"))
    )


def analyze_bluetooth_log(text: str, *, source_name: str = "log") -> BenchmarkReport:
    """Extract only metrics whose boundaries are explicitly present in the log."""

    cancel_ms: list[float] = []
    eot_to_agent_ms: list[float] = []
    first_token_ms: list[float] = []
    first_audio_total_ms: list[float] = []
    tts_incremental_ms: list[float] = []
    playback_total_ms: list[float] = []
    barge_count = 0
    first_write_count = 0
    clear_count = 0
    cancel_blocks = 0
    cancel_blocks_with_active_player = 0
    cancel_blocks_missing_required_clear = 0
    cancel_blocks_without_active_player = 0
    in_cancel_block = False
    cancel_block_player_begin = False
    cancel_block_clear_seen = False
    ready_before_final = 0
    shadow_finals = 0
    useful_leads: list[float] = []

    # Only log-clock pairs are paired when both markers are explicit. Midnight
    # rollover is handled by adding one day when the next marker is smaller.
    pending_barge_log_ms: Optional[int] = None
    barge_to_cancel_log_ms: list[float] = []

    for line in text.splitlines():
        if _PHASE_BARGE.search(line):
            barge_count += 1
            pending_barge_log_ms = _log_clock_ms(line)

        if "BTLifecycle: event=AgentCancel_begin" in line:
            # One normal cancellation block. A playback clear is required only
            # when Agent.cancel_turn observed an actively playing AudioPlayer and
            # therefore logged cancel_stage=player_begin. Cancelling during LLM/TTS
            # startup legitimately has no player clear.
            in_cancel_block = True
            cancel_block_player_begin = False
            cancel_block_clear_seen = False

        if in_cancel_block and "cancel_stage=player_begin" in line:
            cancel_block_player_begin = True

        if in_cancel_block and "BTLifecycle: event=PlaybackClear_returned" in line:
            cancel_block_clear_seen = True

        match = _CANCEL_RETURN.search(line)
        if match:
            cancel_ms.append(float(match.group("ms")))
            if in_cancel_block:
                cancel_blocks += 1
                if cancel_block_player_begin:
                    cancel_blocks_with_active_player += 1
                    if not cancel_block_clear_seen:
                        cancel_blocks_missing_required_clear += 1
                else:
                    cancel_blocks_without_active_player += 1
                in_cancel_block = False
                cancel_block_player_begin = False
                cancel_block_clear_seen = False
            if pending_barge_log_ms is not None:
                end = _log_clock_ms(line)
                if end is not None:
                    if end < pending_barge_log_ms:
                        end += 24 * 3_600_000
                    barge_to_cancel_log_ms.append(float(end - pending_barge_log_ms))
                pending_barge_log_ms = None

        match = _EOT_TO_AGENT.search(line)
        if match:
            eot_to_agent_ms.append(float(match.group("ms")))

        match = _FIRST_TOKEN.search(line)
        if match:
            first_token_ms.append(float(match.group("ms")))

        match = _FIRST_AUDIO.search(line)
        if match:
            first_audio_total_ms.append(float(match.group("total")))
            tts_incremental_ms.append(float(match.group("tts")))

        match = _PLAYBACK.search(line)
        if match:
            playback_total_ms.append(float(match.group("ms")))

        if "BTLifecycle: event=PlaybackFirstWrite_returned" in line:
            first_write_count += 1
        if "BTLifecycle: event=PlaybackClear_returned" in line:
            clear_count += 1

        if "BTShadow:" in line and "outcome=" in line:
            shadow_finals += 1
            if "outcome=ready_before_final" in line and "transcript_match=True" in line:
                ready_before_final += 1
                lead_match = re.search(r"speculative_lead_ms=([-\d.]+)", line)
                if lead_match:
                    useful_leads.append(float(lead_match.group(1)))

    metrics = [
        Metric(
            "agent_cancel_elapsed",
            tuple(cancel_ms),
            "PROVEN_FROM_LOG",
            "normal Agent cancellation; explicit elapsed_ms field",
            "AgentCancel_begin",
            "AgentCancel_returned",
            "source log field",
        ),
        Metric(
            "flux_eot_to_agent_start",
            tuple(eot_to_agent_ms),
            "PROVEN_FROM_LOG",
            "Bluetooth local process; explicit BTLatency field",
            "Flux EndOfTurn callback receipt",
            "Agent start request",
            "source log field",
        ),
        Metric(
            "agent_start_to_llm_first_token",
            tuple(first_token_ms),
            "PROVEN_FROM_LOG",
            "Agent turn; value emitted by Agent",
            "Agent turn start",
            "first LLM content token callback",
            "source log field",
        ),
        Metric(
            "llm_first_token_to_tts_first_audio",
            tuple(tts_incremental_ms),
            "PROVEN_FROM_LOG",
            "Agent turn; value emitted by Agent",
            "first LLM token callback",
            "first TTS audio callback",
            "source log field",
        ),
        Metric(
            "agent_start_to_tts_first_audio",
            tuple(first_audio_total_ms),
            "PROVEN_FROM_LOG",
            "Agent turn; value emitted by Agent",
            "Agent turn start",
            "first TTS audio callback",
            "source log field",
        ),
        Metric(
            "agent_start_to_local_playback_dispatch_complete",
            tuple(playback_total_ms),
            "PROVEN_FROM_LOG",
            "local dispatch only; NOT handset playback",
            "Agent turn start",
            "AudioPlayer local dispatch completion",
            "source log field",
        ),
        Metric(
            "barge_start_log_to_cancel_return_log",
            tuple(barge_to_cancel_log_ms),
            "PROVEN_FROM_LOG_CLOCK",
            "log-clock correlation only; millisecond timestamp resolution",
            "logged FluxStartOfTurnEvent RESPONDING->LISTENING",
            "logged AgentCancel_returned",
            "HH:MM:SS.mmm log clock",
        ),
        Metric(
            "shadow_useful_ready_before_final_lead",
            tuple(useful_leads),
            "PROVEN_FROM_LOG" if useful_leads else "NOT_MEASURED",
            "matching ready_before_final shadow generations only",
            "shadow first-token readiness",
            "final EndOfTurn",
            "source log field",
            "Positive discarded/mismatched lead is deliberately excluded.",
        ),
        Metric(
            "caller_heard_first_audio",
            (),
            "NOT_MEASURED",
            "Bluetooth has no handset playback acknowledgement in this path",
            "caller stop speaking",
            "caller hears first reply sample",
            "unavailable",
        ),
    ]

    checks = [
        CheckResult("normal_barge_in_events", barge_count > 0, "PROVEN_FROM_LOG", f"count={barge_count}"),
        CheckResult(
            "playback_clear_for_active_player_cancels",
            (cancel_blocks_missing_required_clear == 0) if cancel_blocks_with_active_player else None,
            "PROVEN_FROM_LOG" if cancel_blocks_with_active_player else "NOT_MEASURED",
            (
                f"cancel_blocks={cancel_blocks}, "
                f"active_player_cancels={cancel_blocks_with_active_player}, "
                f"required_clears_missing={cancel_blocks_missing_required_clear}, "
                f"cancels_before_player_started={cancel_blocks_without_active_player}, "
                f"all_playback_clear_returns={clear_count}"
            ),
        ),
        CheckResult("local_first_writes_present", first_write_count > 0, "PROVEN_FROM_LOG", f"count={first_write_count}"),
        CheckResult(
            "shadow_ready_before_final_fraction",
            None,
            "OBSERVATION_ONLY",
            f"matching ready_before_final={ready_before_final}; shadow outcome rows={shadow_finals}",
        ),
        CheckResult(
            "semantic_answer_quality",
            None,
            "NOT_MEASURED",
            "Content-free lifecycle logs intentionally do not contain enough answer text to score semantics.",
        ),
    ]

    return BenchmarkReport(
        mode="live_log_analysis",
        metrics=metrics,
        checks=checks,
        metadata={"source_name": source_name, "network_used": None, "clock": "mixed explicit source fields + log HH:MM:SS.mmm"},
        limitations=[
            "This parser reports only boundaries explicitly present in the supplied log.",
            "Playback dispatched/first write is local process evidence, not caller-heard audio.",
            "Log-clock differences have 1ms text resolution and are not substituted for missing monotonic fields.",
            "No missing metric is converted to zero.",
            "Semantic answer quality is not inferable from content-free lifecycle logs.",
        ],
        raw={
            "barge_in_count": barge_count,
            "playback_clear_return_count": clear_count,
            "playback_first_write_return_count": first_write_count,
            "shadow_outcome_rows": shadow_finals,
            "matching_ready_before_final_count": ready_before_final,
        },
    )


def compare_reports(before: BenchmarkReport, after: BenchmarkReport) -> BenchmarkReport:
    """Compare only metrics with identical provenance; make no causal claim."""

    rows: list[dict[str, Any]] = []
    checks: list[CheckResult] = []

    before_map = {metric.name: metric for metric in before.metrics}
    after_map = {metric.name: metric for metric in after.metrics}

    for name in sorted(set(before_map) & set(after_map)):
        left, right = before_map[name], after_map[name]
        same_provenance = (
            left.scope == right.scope
            and left.start_boundary == right.start_boundary
            and left.end_boundary == right.end_boundary
            and left.clock == right.clock
        )
        if not same_provenance:
            checks.append(
                CheckResult(
                    f"compare:{name}",
                    None,
                    "NOT_COMPARABLE",
                    "Metric provenance differs between reports.",
                )
            )
            continue
        if left.median_ms is None or right.median_ms is None:
            checks.append(
                CheckResult(
                    f"compare:{name}",
                    None,
                    "NOT_COMPARABLE",
                    "At least one report has no measured sample.",
                )
            )
            continue
        rows.append(
            {
                "name": name,
                "before_median_ms": left.median_ms,
                "after_median_ms": right.median_ms,
                "delta_median_ms": right.median_ms - left.median_ms,
                "before_count": left.count,
                "after_count": right.count,
                "interpretation": "OBSERVED_DELTA_ONLY_NO_CAUSAL_OR_SIGNIFICANCE_CLAIM",
            }
        )

    return BenchmarkReport(
        mode="comparison",
        checks=checks,
        metadata={
            "before_mode": before.mode,
            "after_mode": after.mode,
            "clock": "inherited per metric",
        },
        limitations=[
            "A numeric delta is not proof that the code change caused it.",
            "No statistical significance threshold is invented by this tool.",
            "Metrics with different provenance are refused rather than normalized or estimated.",
        ],
        raw={"comparisons": rows},
    )
