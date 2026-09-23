from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Type

from shuo.bluetooth.codec import BluetoothOutboundCodec, FrameReframer
from shuo.services.flux import FluxService
from shuo.services.tts_pocket import PocketTTSService, pocket_tts_available

from .android_cellular_rx import (
    AdbTelephonyRxBridge,
    AndroidCellularRxCodec,
    AndroidCellularRxError,
    compile_android_rx_bridge,
    preflight_android_cellular_rx,
    push_android_rx_bridge,
)
from .android_cellular_tx import (
    AdbTelephonyTxBridge,
    AndroidBuildTools,
    AndroidCellularTxError,
    compile_android_tx_bridge,
    preflight_android_cellular_tx,
    push_android_tx_bridge,
)


DEFAULT_EOT_THRESHOLD = 0.8
DEFAULT_THINKING_PAUSE_MS = 650
DEFAULT_BARGE_DELAY_MS = 350
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 20.0
MAX_SCENARIO_SECONDS = 300.0
TX_PCM_RATE = 16_000
TX_PCM_WIDTH = 2
TX_FRAME_BYTES = 640  # 20 ms @ 16 kHz mono PCM16
RX_MULAW_FRAME_BYTES = 160  # 20 ms @ 8 kHz mono mu-law


class AndroidCellularLoopError(RuntimeError):
    """Fail-closed error for the real-cellular synthetic caller controller."""


@dataclass(frozen=True)
class LoopCheck:
    name: str
    passed: Optional[bool]
    note: str = ""


@dataclass(frozen=True)
class LoopMetric:
    name: str
    value: Optional[float]
    unit: str
    status: str = "MEASURED_LOCAL"
    note: str = ""


@dataclass
class AndroidCellularLoopReport:
    checks: list[LoopCheck] = field(default_factory=list)
    metrics: list[LoopMetric] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        scored = [item for item in self.checks if item.passed is not None]
        return bool(scored) and all(item.passed for item in scored)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "shuo.android-cellular-closed-loop/v1",
            "passed": self.passed,
            "checks": [asdict(item) for item in self.checks],
            "metrics": [asdict(item) for item in self.metrics],
            "metadata": self.metadata,
            "limitations": self.limitations,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


@dataclass(frozen=True)
class _Stimulus:
    name: str
    text: str


@dataclass(frozen=True)
class _PreparedStimulus:
    name: str
    pcm: bytes

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm) / (TX_PCM_RATE * TX_PCM_WIDTH)


@dataclass(frozen=True)
class _RxStart:
    ordinal: int
    at_ns: int


@dataclass(frozen=True)
class _RxEnd:
    ordinal: int
    at_ns: int
    transcript: str


class _RxTurnMonitor:
    """In-memory-only response turn tracker.

    Final transcripts are retained only long enough to score deterministic
    checks. They are never printed or serialized by AndroidCellularLoopReport.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self.starts: list[_RxStart] = []
        self.ends: list[_RxEnd] = []

    @property
    def start_count(self) -> int:
        return len(self.starts)

    @property
    def end_count(self) -> int:
        return len(self.ends)

    async def on_start(self) -> None:
        async with self._condition:
            self.starts.append(
                _RxStart(
                    ordinal=len(self.starts) + 1,
                    at_ns=time.perf_counter_ns(),
                )
            )
            self._condition.notify_all()

    async def on_end(self, transcript: str) -> None:
        async with self._condition:
            self.ends.append(
                _RxEnd(
                    ordinal=len(self.ends) + 1,
                    at_ns=time.perf_counter_ns(),
                    transcript=transcript,
                )
            )
            self._condition.notify_all()

    async def wait_for_start(
        self,
        previous_count: int,
        *,
        timeout_seconds: float,
    ) -> _RxStart:
        async with asyncio.timeout(timeout_seconds):
            async with self._condition:
                await self._condition.wait_for(
                    lambda: len(self.starts) > previous_count
                )
                return self.starts[previous_count]

    async def wait_for_end_after(
        self,
        start_ns: int,
        previous_count: int,
        *,
        timeout_seconds: float,
    ) -> _RxEnd:
        async with asyncio.timeout(timeout_seconds):
            async with self._condition:
                await self._condition.wait_for(
                    lambda: any(
                        item.at_ns > start_ns
                        for item in self.ends[previous_count:]
                    )
                )
                for item in self.ends[previous_count:]:
                    if item.at_ns > start_ns:
                        return item
        raise AssertionError("unreachable")

    def ended_after(self, start_ns: int, previous_count: int) -> bool:
        return any(
            item.at_ns > start_ns
            for item in self.ends[previous_count:]
        )


def _scenario_stimuli() -> tuple[_Stimulus, ...]:
    return (
        _Stimulus(
            "seed",
            (
                "For this test, please remember two things. "
                "My fruit is mango, and the codeword is orbit seven."
            ),
        ),
        _Stimulus(
            "normal_one",
            "In one short sentence, what kind of work do you usually do?",
        ),
        _Stimulus(
            "pause_a",
            "I have another question about",
        ),
        _Stimulus(
            "pause_b",
            "how you usually work with clients. Please answer briefly.",
        ),
        _Stimulus(
            "normal_two",
            "Can you give me one short example from that kind of work?",
        ),
        _Stimulus(
            "barge_one_setup",
            (
                "Please explain that in detail for several sentences "
                "so I can understand your process."
            ),
        ),
        _Stimulus(
            "barge_one_interrupt",
            "Sorry to interrupt. What fruit did I ask you to remember?",
        ),
        _Stimulus(
            "normal_three",
            "Okay. Now tell me one practical challenge you often solve.",
        ),
        _Stimulus(
            "barge_two_setup",
            (
                "Please give me a detailed explanation of how you would "
                "approach a new client project from beginning to end."
            ),
        ),
        _Stimulus(
            "barge_two_interrupt",
            "Stop there. What codeword did I ask you to remember?",
        ),
        _Stimulus(
            "continuity_final",
            "Before we finish, what fruit did I tell you earlier?",
        ),
    )


async def synthesize_pocket_pcm(
    text: str,
    *,
    timeout_seconds: float = 45.0,
    voice_source: Optional[str] = None,
    pocket_cls: Type = PocketTTSService,
) -> bytes:
    """Pocket -> existing SHUO mu-law boundary -> isolated TX PCM, memory only."""

    if not text.strip():
        raise ValueError("text must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if pocket_cls is PocketTTSService and not pocket_tts_available():
        raise AndroidCellularLoopError(
            "Pocket TTS is not installed; install requirements-pocket-tts.txt"
        )

    done = asyncio.Event()
    pcm_chunks: list[bytes] = []
    codec = BluetoothOutboundCodec()

    async def on_audio(audio_base64: str) -> None:
        mulaw = base64.b64decode(audio_base64, validate=True)
        pcm = codec.feed(mulaw)
        if pcm:
            pcm_chunks.append(pcm)

    async def on_done() -> None:
        done.set()

    service = pocket_cls(
        on_audio=on_audio,
        on_done=on_done,
        voice_id=None,
        voice_source=voice_source,
    )
    try:
        await asyncio.wait_for(service.start(), timeout=timeout_seconds)
        await asyncio.wait_for(service.send(text), timeout=timeout_seconds)
        await asyncio.wait_for(service.flush(), timeout=timeout_seconds)
        await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        fatal_error = getattr(service, "fatal_error", None)
        if fatal_error:
            raise AndroidCellularLoopError(
                f"Pocket TTS failed while preparing caller speech: {fatal_error}"
            )
        tail = codec.finish()
        if tail:
            pcm_chunks.append(tail)
    finally:
        await service.cancel()

    pcm = b"".join(pcm_chunks)
    if not pcm:
        raise AndroidCellularLoopError("Pocket TTS produced no caller PCM")
    if len(pcm) % TX_PCM_WIDTH:
        raise AndroidCellularLoopError(
            "Pocket caller PCM ended with an incomplete PCM16 sample"
        )
    return pcm


async def prepare_scenario_stimuli(
    *,
    timeout_seconds: float,
    voice_source: Optional[str] = None,
    pocket_cls: Type = PocketTTSService,
) -> dict[str, _PreparedStimulus]:
    prepared: dict[str, _PreparedStimulus] = {}
    for item in _scenario_stimuli():
        prepared[item.name] = _PreparedStimulus(
            name=item.name,
            pcm=await synthesize_pocket_pcm(
                item.text,
                timeout_seconds=timeout_seconds,
                voice_source=voice_source,
                pocket_cls=pocket_cls,
            ),
        )
    return prepared


async def play_pcm_realtime(
    bridge: AdbTelephonyTxBridge,
    pcm: bytes,
) -> tuple[int, int]:
    """Pace synthetic caller PCM at the validated 20 ms telephony boundary."""

    if not pcm:
        raise ValueError("pcm must not be empty")
    if len(pcm) % TX_PCM_WIDTH:
        raise AndroidCellularLoopError("TX PCM must be PCM16-aligned")

    loop = asyncio.get_running_loop()
    reframer = FrameReframer(TX_FRAME_BYTES)
    start_ns = time.perf_counter_ns()
    target = loop.time()

    async def send_piece(piece: bytes) -> None:
        nonlocal target
        await bridge.write_pcm16(piece)
        target += len(piece) / (TX_PCM_RATE * TX_PCM_WIDTH)
        delay = target - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

    for frame in reframer.feed(pcm):
        await send_piece(frame)

    tail = reframer.finish()
    if tail:
        await send_piece(tail)

    return start_ns, time.perf_counter_ns()


async def _feed_downlink_to_flux(
    rx: AdbTelephonyRxBridge,
    flux: FluxService,
) -> None:
    codec = AndroidCellularRxCodec()
    reframer = FrameReframer(RX_MULAW_FRAME_BYTES)

    while True:
        pcm = await rx.read_pcm()
        mulaw = codec.feed(pcm)
        for frame in reframer.feed(mulaw):
            await flux.send(frame)


def _contains_mango(text: str) -> bool:
    return "mango" in text.casefold()


def _contains_orbit_seven(text: str) -> bool:
    value = text.casefold()
    return "orbit" in value and ("seven" in value or "7" in value)


async def run_android_cellular_closed_loop(
    *,
    tools: AndroidBuildTools,
    repo_root: Path,
    build_dir: Path,
    serial: Optional[str],
    allow_provider_network: bool,
    thinking_pause_ms: int = DEFAULT_THINKING_PAUSE_MS,
    barge_delay_ms: int = DEFAULT_BARGE_DELAY_MS,
    response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    max_scenario_seconds: float = MAX_SCENARIO_SECONDS,
    voice_source: Optional[str] = None,
    flux_cls: Type = FluxService,
) -> AndroidCellularLoopReport:
    """Run a deterministic real-cellular synthetic caller with manual call control.

    This function never dials, answers or hangs up. The real call must already be
    active. Caller and SHUO response transcripts exist only in process memory for
    deterministic checks and are excluded from the report.
    """

    if not allow_provider_network:
        raise PermissionError(
            "closed-loop cellular benchmark contacts Deepgram; "
            "pass --allow-provider-network explicitly"
        )
    if not os.getenv("DEEPGRAM_API_KEY", "").strip():
        raise AndroidCellularLoopError("DEEPGRAM_API_KEY is required")
    if not (1 <= thinking_pause_ms < 800):
        raise ValueError(
            "thinking_pause_ms must be between 1 and 799 so the default test "
            "does not intentionally exceed the approved 0.8 final EOT threshold"
        )
    if barge_delay_ms <= 0:
        raise ValueError("barge_delay_ms must be positive")
    if response_timeout_seconds <= 0:
        raise ValueError("response_timeout_seconds must be positive")
    if not (0 < max_scenario_seconds <= MAX_SCENARIO_SECONDS):
        raise ValueError("max_scenario_seconds must be >0 and <=300")

    tx_preflight = await preflight_android_cellular_tx(
        tools,
        serial=serial,
        require_active_call=True,
    )
    rx_preflight = await preflight_android_cellular_rx(
        tools,
        serial=tx_preflight.serial,
        require_active_call=True,
    )
    if rx_preflight.serial != tx_preflight.serial:
        raise AndroidCellularLoopError("RX/TX ADB target mismatch")

    # Prepare all caller speech before opening the observer Flux connection.
    # No caller audio is written to disk.
    prepared = await prepare_scenario_stimuli(
        timeout_seconds=response_timeout_seconds,
        voice_source=voice_source,
    )

    tx_dex = await compile_android_tx_bridge(
        repo_root=repo_root,
        build_dir=build_dir / "tx",
        tools=tools,
    )
    rx_dex = await compile_android_rx_bridge(
        repo_root=repo_root,
        build_dir=build_dir / "rx",
        tools=tools,
    )
    await push_android_tx_bridge(
        tools=tools,
        serial=tx_preflight.serial,
        dex_path=tx_dex,
    )
    await push_android_rx_bridge(
        tools=tools,
        serial=rx_preflight.serial,
        dex_path=rx_dex,
    )

    monitor = _RxTurnMonitor()
    tx = AdbTelephonyTxBridge(
        adb=tools.adb,
        serial=tx_preflight.serial,
    )
    rx = AdbTelephonyRxBridge(
        adb=tools.adb,
        serial=rx_preflight.serial,
    )
    flux = flux_cls(
        monitor.on_end,
        monitor.on_start,
        eot_threshold=DEFAULT_EOT_THRESHOLD,
        message_observer=lambda *_args: None,
    )

    feed_task: Optional[asyncio.Task] = None
    started_ns: Optional[int] = None
    ended_ns: Optional[int] = None
    checks: list[LoopCheck] = []
    metrics: list[LoopMetric] = []

    async def speak(name: str) -> tuple[int, int]:
        return await play_pcm_realtime(tx, prepared[name].pcm)

    async def wait_response(
        start_before: int,
        end_before: int,
    ) -> tuple[_RxStart, _RxEnd]:
        start = await monitor.wait_for_start(
            start_before,
            timeout_seconds=response_timeout_seconds,
        )
        end = await monitor.wait_for_end_after(
            start.at_ns,
            end_before,
            timeout_seconds=response_timeout_seconds,
        )
        return start, end

    async def normal_turn(name: str) -> _RxEnd:
        start_before = monitor.start_count
        end_before = monitor.end_count
        await speak(name)
        _start, end = await wait_response(start_before, end_before)
        return end

    async def barge_turn(
        setup_name: str,
        interrupt_name: str,
        *,
        label: str,
    ) -> _RxEnd:
        start_before = monitor.start_count
        end_before = monitor.end_count
        await speak(setup_name)

        original_start = await monitor.wait_for_start(
            start_before,
            timeout_seconds=response_timeout_seconds,
        )
        await asyncio.sleep(barge_delay_ms / 1000.0)

        still_speaking = not monitor.ended_after(
            original_start.at_ns,
            end_before,
        )
        checks.append(
            LoopCheck(
                name=f"{label}_interrupt_sent_while_remote_speaking",
                passed=still_speaking,
                note=(
                    "RX StartOfTurn observed and no RX EndOfTurn had occurred "
                    "before synthetic caller interruption"
                ),
            )
        )

        interrupt_start_ns, _ = await speak(interrupt_name)

        # The interrupted response may first produce its own EOT. Require a new
        # response start after the interrupt before accepting the replacement.
        replacement_start_baseline = monitor.start_count
        replacement_end_baseline = monitor.end_count
        replacement_start = await monitor.wait_for_start(
            replacement_start_baseline,
            timeout_seconds=response_timeout_seconds,
        )
        replacement_end = await monitor.wait_for_end_after(
            replacement_start.at_ns,
            replacement_end_baseline,
            timeout_seconds=response_timeout_seconds,
        )

        metrics.append(
            LoopMetric(
                name=f"{label}_interrupt_to_replacement_start_ms",
                value=(
                    replacement_start.at_ns - interrupt_start_ns
                ) / 1_000_000.0,
                unit="ms",
                note=(
                    "process-local controller timestamp to observed remote "
                    "VOICE_DOWNLINK speech start; not caller-heard latency"
                ),
            )
        )
        return replacement_end

    try:
        await tx.start()
        await rx.start()
        await flux.start()
        feed_task = asyncio.create_task(_feed_downlink_to_flux(rx, flux))
        started_ns = time.perf_counter_ns()

        async with asyncio.timeout(max_scenario_seconds):
            seed = await normal_turn("seed")
            checks.append(
                LoopCheck(
                    "seed_response_completed",
                    True,
                    "remote response completed after continuity seed",
                )
            )

            await normal_turn("normal_one")

            # Thinking pause: both halves were pre-synthesized before the test,
            # so no TTS generation delay is silently added to the pause.
            pause_start_count = monitor.start_count
            pause_end_count = monitor.end_count
            await speak("pause_a")
            pause_started_ns = time.perf_counter_ns()
            await asyncio.sleep(thinking_pause_ms / 1000.0)
            premature_remote_start = monitor.start_count > pause_start_count
            pause_ended_ns = time.perf_counter_ns()
            checks.append(
                LoopCheck(
                    "thinking_pause_no_premature_remote_response",
                    not premature_remote_start,
                    f"configured_pause_ms={thinking_pause_ms}",
                )
            )
            metrics.append(
                LoopMetric(
                    "thinking_pause_observed_ms",
                    (pause_ended_ns - pause_started_ns) / 1_000_000.0,
                    "ms",
                )
            )
            await speak("pause_b")
            await wait_response(pause_start_count, pause_end_count)

            await normal_turn("normal_two")

            first_replacement = await barge_turn(
                "barge_one_setup",
                "barge_one_interrupt",
                label="barge_in_1",
            )
            checks.append(
                LoopCheck(
                    "barge_in_1_continuity_fruit",
                    _contains_mango(first_replacement.transcript),
                    "replacement response is checked in memory for mango",
                )
            )

            await normal_turn("normal_three")

            second_replacement = await barge_turn(
                "barge_two_setup",
                "barge_two_interrupt",
                label="barge_in_2",
            )
            checks.append(
                LoopCheck(
                    "barge_in_2_continuity_codeword",
                    _contains_orbit_seven(second_replacement.transcript),
                    "replacement response is checked in memory for orbit seven",
                )
            )

            final = await normal_turn("continuity_final")
            checks.append(
                LoopCheck(
                    "late_session_continuity_fruit",
                    _contains_mango(final.transcript),
                    "final response is checked in memory for mango",
                )
            )

            response_turns = monitor.end_count
            checks.append(
                LoopCheck(
                    "at_least_ten_remote_response_turns",
                    response_turns >= 10,
                    f"rx_end_of_turn_count={response_turns}",
                )
            )

        ended_ns = time.perf_counter_ns()
    except asyncio.TimeoutError as exc:
        raise AndroidCellularLoopError(
            "closed-loop scenario exceeded its bounded timeout"
        ) from exc
    finally:
        if feed_task is not None:
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
            except (AndroidCellularRxError, asyncio.TimeoutError):
                pass
        try:
            await flux.stop()
        finally:
            try:
                await rx.close()
            finally:
                await tx.close()

    if started_ns is not None and ended_ns is not None:
        metrics.append(
            LoopMetric(
                "scenario_duration_ms",
                (ended_ns - started_ns) / 1_000_000.0,
                "ms",
                note="controller scenario only; excludes local pre-synthesis/setup",
            )
        )

    checks.insert(
        0,
        LoopCheck(
            "closed_loop_scenario_completed",
            True,
            "real cellular RX and TX controller completed without raw-audio persistence",
        ),
    )

    return AndroidCellularLoopReport(
        checks=checks,
        metrics=metrics,
        metadata={
            "adb_device": "explicit_reference_target",
            "android_sdk": rx_preflight.sdk_int,
            "eot_threshold": DEFAULT_EOT_THRESHOLD,
            "thinking_pause_ms": thinking_pause_ms,
            "barge_delay_ms": barge_delay_ms,
            "response_timeout_seconds": response_timeout_seconds,
            "max_scenario_seconds": max_scenario_seconds,
            "providers": {
                "observer_stt_turn_detection": "Deepgram Flux",
                "synthetic_caller_tts": "Pocket",
            },
            "real_cellular_used": True,
            "raw_audio_written": False,
            "call_control": "manual",
            "rx_start_of_turn_count": monitor.start_count,
            "rx_end_of_turn_count": monitor.end_count,
        },
        limitations=[
            "The synthetic caller is deterministic software, not a real human.",
            "Deepgram observes SHUO downlink speech to advance and score the script.",
            "Transcripts are used only in memory for boolean continuity checks and are not serialized.",
            "Call establishment and hangup remain manual; this is not Phase 6 call control.",
            "Controller-local timestamps are not caller-heard or mouth-to-ear latency.",
            "This scenario does not by itself accept Phase 5.",
        ],
    )
