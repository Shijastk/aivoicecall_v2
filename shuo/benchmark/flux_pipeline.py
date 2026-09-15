from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import statistics
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from shuo.bluetooth.conversation import run_bluetooth_conversation
from shuo.runtime_config import CallSettings
from shuo.services.flux import FluxService
from shuo.services.tts import TTSService
from shuo.services.tts_pool import TTSPool

from .conversation import (
    BENCHMARK_SYSTEM_PROMPT,
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


FIXTURE_SCHEMA = "shuo.flux-caller-fixture/v1"
DEFAULT_FIXTURE_TEXT = "What is the benchmark codeword?"
PCM_SAMPLE_RATE = 16_000
PCM_SAMPLE_WIDTH = 2
PCM_CHANNELS = 1
PCM_FRAME_MS = 20
PCM_FRAME_BYTES = PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH * PCM_CHANNELS * PCM_FRAME_MS // 1000
SILENCE_FRAME = b"\x00\x00" * (PCM_SAMPLE_RATE * PCM_FRAME_MS // 1000)
DEFAULT_FIXTURE_PATH = Path("var/benchmark/flux_codeword.s16le")
DEFAULT_MANIFEST_PATH = Path("var/benchmark/flux_codeword.json")


def _build_seed_history(turns: int) -> list[dict[str, str]]:
    """Build deterministic completed prior turns for TTFT depth testing.

    Benchmark-only: one turn means one user + one assistant message.
    No production conversation state is changed.
    """
    history: list[dict[str, str]] = []
    for index in range(1, turns + 1):
        history.extend(
            [
                {
                    "role": "user",
                    "content": (
                        f"Previous benchmark turn {index}: "
                        "I am checking a routine detail before continuing this conversation."
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        f"Previous benchmark reply {index}: "
                        "Understood. I will keep that routine detail in context."
                    ),
                },
            ]
        )
    return history


@dataclass(frozen=True)
class FluxFixtureManifest:
    schema: str
    source_text: str
    sha256: str
    bytes: int
    sample_rate: int
    sample_width_bytes: int
    channels: int
    encoding: str
    generator: str
    voice_id: str

    @property
    def duration_ms(self) -> float:
        bytes_per_second = self.sample_rate * self.sample_width_bytes * self.channels
        return self.bytes / bytes_per_second * 1000.0

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["duration_ms"] = self.duration_ms
        return out


@dataclass(frozen=True)
class _SpeechWindow:
    generation: int
    first_read_ns: int
    last_read_ns: int
    first_read_sequence: int
    last_read_sequence: int


@dataclass(frozen=True)
class _FluxTurn:
    start_of_turn_ns: Optional[int]
    end_of_turn_ns: int
    transcript: str
    trigger: Optional[str]
    turn_index: Optional[int]
    final_speech_send_enter_ns: Optional[int]
    final_speech_send_return_ns: Optional[int]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split_pcm_frames(data: bytes) -> tuple[bytes, ...]:
    if not data:
        raise ValueError("fixture audio is empty")
    if len(data) % (PCM_SAMPLE_WIDTH * PCM_CHANNELS):
        raise ValueError("fixture ends with an incomplete S16LE sample")
    return tuple(data[offset : offset + PCM_FRAME_BYTES] for offset in range(0, len(data), PCM_FRAME_BYTES))


def _normalised_words(text: str) -> list[str]:
    word = []
    words: list[str] = []
    for char in text.casefold():
        if char.isalnum():
            word.append(char)
        elif word:
            words.append("".join(word))
            word.clear()
    if word:
        words.append("".join(word))
    return words


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = _normalised_words(reference)
    hyp = _normalised_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            substitution = previous[j - 1] + (ref_word != hyp_word)
            insertion = current[j - 1] + 1
            deletion = previous[j] + 1
            current.append(min(substitution, insertion, deletion))
        previous = current
    return previous[-1] / len(ref)


def load_flux_fixture(audio_path: Path, manifest_path: Path) -> tuple[bytes, FluxFixtureManifest]:
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw_manifest.get("schema") != FIXTURE_SCHEMA:
        raise ValueError(f"Unsupported fixture schema in {manifest_path}")

    manifest = FluxFixtureManifest(
        schema=raw_manifest["schema"],
        source_text=raw_manifest["source_text"],
        sha256=raw_manifest["sha256"],
        bytes=int(raw_manifest["bytes"]),
        sample_rate=int(raw_manifest["sample_rate"]),
        sample_width_bytes=int(raw_manifest["sample_width_bytes"]),
        channels=int(raw_manifest["channels"]),
        encoding=raw_manifest["encoding"],
        generator=raw_manifest["generator"],
        voice_id=raw_manifest.get("voice_id", ""),
    )

    if (
        manifest.sample_rate != PCM_SAMPLE_RATE
        or manifest.sample_width_bytes != PCM_SAMPLE_WIDTH
        or manifest.channels != PCM_CHANNELS
        or manifest.encoding != "s16le"
    ):
        raise ValueError(
            "fixture audio contract mismatch: expected s16le/16000Hz/mono/16-bit"
        )

    audio = audio_path.read_bytes()
    if len(audio) != manifest.bytes:
        raise ValueError(
            f"fixture byte count mismatch: manifest={manifest.bytes} actual={len(audio)}"
        )
    digest = _sha256(audio)
    if digest != manifest.sha256:
        raise ValueError(
            f"fixture SHA-256 mismatch: manifest={manifest.sha256} actual={digest}"
        )
    _split_pcm_frames(audio)
    return audio, manifest


async def prepare_flux_fixture(
    *,
    audio_path: Path = DEFAULT_FIXTURE_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    allow_provider_network: bool,
    overwrite: bool = False,
    timeout_seconds: float = 45.0,
) -> FluxFixtureManifest:
    """Generate one synthetic caller fixture, then freeze it by SHA-256.

    Fixture generation is deliberately separate from benchmark execution. The
    benchmark never regenerates speech, so provider synthesis variance cannot
    change the caller stimulus between before/after runs.
    """

    if not allow_provider_network:
        raise PermissionError(
            "Fixture generation is opt-in. Pass --allow-provider-network explicitly."
        )
    if audio_path.exists() or manifest_path.exists():
        if not overwrite:
            raise FileExistsError(
                "fixture already exists; reuse it or pass --overwrite to replace both files"
            )

    if not os.getenv("ELEVENLABS_API_KEY", "").strip():
        raise RuntimeError("Missing required environment variable: ELEVENLABS_API_KEY")

    settings = CallSettings.builtin()
    chunks: list[bytes] = []
    done = asyncio.Event()

    async def on_audio(audio_base64: str) -> None:
        chunks.append(base64.b64decode(audio_base64))

    async def on_done() -> None:
        done.set()

    tts = TTSService(on_audio=on_audio, on_done=on_done, voice_id=settings.voice_id)
    try:
        await tts.start()
        await tts.send(DEFAULT_FIXTURE_TEXT)
        await tts.flush()
        await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        if tts.fatal_error:
            raise RuntimeError(f"ElevenLabs fixture generation failed: {tts.fatal_error}")
        mulaw = b"".join(chunks)
        if not mulaw:
            raise RuntimeError("ElevenLabs fixture generation returned no audio")
    finally:
        await tts.cancel()

    # The existing Bluetooth outbound converter is the repository-owned
    # mulaw/8k -> S16LE/16k boundary. Use it once while preparing the fixture;
    # benchmark execution then reuses the exact frozen PCM bytes.
    from shuo.bluetooth.codec import BluetoothOutboundCodec

    codec = BluetoothOutboundCodec()
    pcm = codec.feed(mulaw) + codec.finish()
    if not pcm:
        raise RuntimeError("fixture conversion produced no S16LE audio")
    if len(pcm) % 2:
        raise RuntimeError("fixture conversion produced an incomplete S16LE sample")

    manifest = FluxFixtureManifest(
        schema=FIXTURE_SCHEMA,
        source_text=DEFAULT_FIXTURE_TEXT,
        sha256=_sha256(pcm),
        bytes=len(pcm),
        sample_rate=PCM_SAMPLE_RATE,
        sample_width_bytes=PCM_SAMPLE_WIDTH,
        channels=PCM_CHANNELS,
        encoding="s16le",
        generator=(
            "current shuo.services.tts.TTSService ulaw_8000 -> "
            "shuo.bluetooth.codec.BluetoothOutboundCodec"
        ),
        voice_id=settings.voice_id,
    )

    audio_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(pcm)
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


class _RealtimeReplaySession:
    """20ms-paced S16LE source plus in-memory outbound sink; no device access."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.writes: list[bytes] = []
        self.write_times_ns: list[int] = []
        self.clear_times_ns: list[int] = []
        self.read_count = 0
        self._running = False
        self._next_deadline_ns: Optional[int] = None
        self._speech_frames: deque[bytes] = deque()
        self._speech_generation = 0
        self._speech_first_read_ns: Optional[int] = None
        self._speech_first_read_sequence: Optional[int] = None
        self._speech_done = asyncio.Event()
        self._speech_windows: dict[int, _SpeechWindow] = {}
        self.speech_end_sequences: set[int] = set()
        self.max_source_lateness_ms = 0.0

    async def start(self) -> None:
        self.started += 1
        self._running = True
        self._next_deadline_ns = None

    async def stop(self) -> None:
        self.stopped += 1
        self._running = False
        self._speech_done.set()

    async def read(self) -> bytes:
        if not self._running:
            raise RuntimeError("benchmark replay session is stopped")

        now = _ns()
        if self._next_deadline_ns is None:
            self._next_deadline_ns = now
        delay_ns = self._next_deadline_ns - now
        if delay_ns > 0:
            await asyncio.sleep(delay_ns / 1_000_000_000)
        actual_ns = _ns()
        lateness_ns = max(0, actual_ns - self._next_deadline_ns)
        self.max_source_lateness_ms = max(
            self.max_source_lateness_ms, lateness_ns / 1_000_000.0
        )
        self._next_deadline_ns += PCM_FRAME_MS * 1_000_000

        self.read_count += 1
        sequence = self.read_count
        if self._speech_frames:
            if self._speech_first_read_ns is None:
                self._speech_first_read_ns = actual_ns
                self._speech_first_read_sequence = sequence
            frame = self._speech_frames.popleft()
            if not self._speech_frames:
                generation = self._speech_generation
                first_ns = self._speech_first_read_ns
                first_seq = self._speech_first_read_sequence
                if first_ns is None or first_seq is None:
                    raise RuntimeError("speech fixture ended without a recorded first frame")
                self._speech_windows[generation] = _SpeechWindow(
                    generation=generation,
                    first_read_ns=first_ns,
                    last_read_ns=actual_ns,
                    first_read_sequence=first_seq,
                    last_read_sequence=sequence,
                )
                self.speech_end_sequences.add(sequence)
                self._speech_first_read_ns = None
                self._speech_first_read_sequence = None
                self._speech_done.set()
            return frame

        return SILENCE_FRAME

    async def write(self, audio: bytes) -> None:
        self.writes.append(audio)
        self.write_times_ns.append(_ns())

    async def clear(self) -> None:
        self.clear_times_ns.append(_ns())

    async def play_fixture(self, pcm: bytes, *, timeout_seconds: float) -> _SpeechWindow:
        if self._speech_frames:
            raise RuntimeError("a speech fixture is already active")
        frames = _split_pcm_frames(pcm)
        self._speech_generation += 1
        generation = self._speech_generation
        self._speech_done.clear()
        self._speech_frames.extend(frames)
        await asyncio.wait_for(self._speech_done.wait(), timeout=timeout_seconds)
        return self._speech_windows[generation]


class _MeasuredFlux(FluxService):
    def __init__(self, *args, source_session: _RealtimeReplaySession, **kwargs):
        super().__init__(*args, **kwargs)
        self._source_session = source_session
        self._send_count = 0
        self._start_of_turn_ns: Optional[int] = None
        self._speech_send_bounds: dict[int, tuple[int, int]] = {}
        self.turns: list[_FluxTurn] = []
        self._turn_changed = asyncio.Event()

    async def send(self, audio_bytes: bytes) -> None:
        self._send_count += 1
        sequence = self._send_count
        send_enter_ns = _ns()
        await super().send(audio_bytes)
        send_return_ns = _ns()
        if sequence in self._source_session.speech_end_sequences:
            self._speech_send_bounds[sequence] = (send_enter_ns, send_return_ns)

    async def _on_message(self, message, *args, **kwargs) -> None:
        if isinstance(message, dict):
            msg_type = message.get("type")
            event = message.get("event")
            transcript = message.get("transcript") or ""
            trigger = message.get("trigger")
            turn_index = message.get("turn_index")
        else:
            msg_type = getattr(message, "type", None)
            event = getattr(message, "event", None)
            transcript = getattr(message, "transcript", "") or ""
            trigger = getattr(message, "trigger", None)
            turn_index = getattr(message, "turn_index", None)

        received_ns = _ns()
        if msg_type == "TurnInfo" and event == "StartOfTurn":
            self._start_of_turn_ns = received_ns

        if msg_type == "TurnInfo" and event == "EndOfTurn":
            matching_sequence = max(self._source_session.speech_end_sequences, default=0)
            send_bounds = self._speech_send_bounds.get(matching_sequence)
            self.turns.append(
                _FluxTurn(
                    start_of_turn_ns=self._start_of_turn_ns,
                    end_of_turn_ns=received_ns,
                    transcript=transcript.strip(),
                    trigger=str(trigger) if trigger is not None else None,
                    turn_index=int(turn_index) if turn_index is not None else None,
                    final_speech_send_enter_ns=send_bounds[0] if send_bounds else None,
                    final_speech_send_return_ns=send_bounds[1] if send_bounds else None,
                )
            )
            self._start_of_turn_ns = None

        await super()._on_message(message, *args, **kwargs)
        if msg_type == "TurnInfo" and event == "EndOfTurn":
            self._turn_changed.set()

    async def wait_for_turn(self, count: int, *, timeout_seconds: float) -> _FluxTurn:
        async with asyncio.timeout(timeout_seconds):
            while len(self.turns) < count:
                self._turn_changed.clear()
                await self._turn_changed.wait()
        return self.turns[count - 1]


async def _run_flux_pipeline_once(
    *,
    pcm: bytes,
    manifest: FluxFixtureManifest,
    timeout_seconds: float,
    eot_threshold: Optional[float] = None,
    history_turns: int = 0,
) -> dict[str, Any]:
    capture = _Capture()
    recorder = _Recorder(capture)
    session = _RealtimeReplaySession()
    holder: dict[str, Any] = {}
    ready = asyncio.Event()
    settings = CallSettings(
        system_prompt=BENCHMARK_SYSTEM_PROMPT,
        voice_id=CallSettings.builtin().voice_id,
        prompt_source="benchmark-synthetic",
        voice_source="environment/fallback",
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
            eot_threshold=eot_threshold,
        )
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

        # Benchmark-only history injection. Agent.history is intentionally
        # read-only, while LLMService owns the mutable persistent history.
        # Seed before the measured caller turn starts so Agent.start_turn()
        # appends the measured transcript exactly as production does.
        if history_turns:
            agent._llm._history = [
                dict(message) for message in _build_seed_history(history_turns)
            ]

        holder["agent"] = agent
        ready.set()
        return agent

    task: Optional[asyncio.Task] = None
    error: Optional[str] = None
    speech_window: Optional[_SpeechWindow] = None
    flux_turn: Optional[_FluxTurn] = None

    try:
        task = asyncio.create_task(
            run_bluetooth_conversation(
                session,
                flux_factory=flux_factory,
                agent_factory=agent_factory,
                call_id="benchmark-flux-pipeline",
            )
        )
        await asyncio.wait_for(ready.wait(), timeout_seconds)

        # Reader is active and emits silence at the same 20ms source cadence as
        # the validated Bluetooth boundary. Give the provider a few frames of
        # established silence before the frozen speech bytes begin. This wait is
        # stimulus setup only and is excluded from all measured boundaries.
        target_reads = session.read_count + 5
        await _wait_until(lambda: session.read_count >= target_reads, timeout_seconds)

        speech_window = await session.play_fixture(pcm, timeout_seconds=timeout_seconds)
        flux: _MeasuredFlux = holder["flux"]
        flux_turn = await flux.wait_for_turn(1, timeout_seconds=timeout_seconds)

        await _wait_until(
            lambda: len(capture.turns) >= 1 and capture.turns[0].first_audio_ns is not None,
            timeout_seconds,
        )
        await _wait_until(
            lambda: len(capture.turns) >= 1 and capture.turns[0].playback_dispatch_ns is not None,
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

    turn_capture = capture.turns[0] if capture.turns else None
    answer = turn_capture.response_text if turn_capture is not None else ""
    return {
        "error": error,
        "speech": asdict(speech_window) if speech_window is not None else None,
        "flux": asdict(flux_turn) if flux_turn is not None else None,
        "agent": asdict(turn_capture) if turn_capture is not None else None,
        "answer": answer,
        "first_local_write_ns": session.write_times_ns[0] if session.write_times_ns else None,
        "source_read_count": session.read_count,
        "source_max_lateness_ms": session.max_source_lateness_ms,
        "expected_text": manifest.source_text,
        "transcript_wer": (
            word_error_rate(manifest.source_text, flux_turn.transcript)
            if flux_turn is not None
            else None
        ),
    }


def _metric_from_runs(
    runs: list[dict[str, Any]],
    *,
    name: str,
    start_getter,
    end_getter,
    start_boundary: str,
    end_boundary: str,
    scope: str,
) -> Metric:
    samples = []
    for row in runs:
        start = start_getter(row)
        end = end_getter(row)
        value = _ms_between(start, end)
        if value is not None:
            samples.append(value)
    return Metric(
        name=name,
        samples_ms=tuple(samples),
        status="PROVEN_PROVIDER_PIPELINE_NO_DEVICE",
        scope=scope,
        start_boundary=start_boundary,
        end_boundary=end_boundary,
    )


async def run_flux_pipeline_benchmark(
    *,
    audio_path: Path = DEFAULT_FIXTURE_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    allow_provider_network: bool,
    repo_root: Optional[Path] = None,
    runs: int = 5,
    timeout_seconds: float = 45.0,
    eot_threshold: Optional[float] = None,
    history_turns: int = 0,
) -> BenchmarkReport:
    """Replay frozen speech through real Flux -> Agent -> Groq -> ElevenLabs.

    This is a provider-pipeline benchmark. It intentionally has no PipeWire,
    Bluetooth hardware, phone, carrier or cellular network and therefore cannot
    produce caller-heard or cellular mouth-to-ear latency.
    """

    if not allow_provider_network:
        raise PermissionError(
            "Flux pipeline benchmark is opt-in. Pass --allow-provider-network explicitly."
        )
    if runs <= 0:
        raise ValueError("runs must be positive")
    if not 0 <= history_turns <= 64:
        raise ValueError("history_turns must be between 0 and 64")
    if eot_threshold is not None and not (0.5 <= eot_threshold <= 1.0):
        raise ValueError("eot_threshold must be between 0.5 and 1.0")

    missing = [
        name
        for name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise RuntimeError("Missing required environment variable(s): " + ", ".join(missing))

    pcm, manifest = load_flux_fixture(audio_path, manifest_path)
    raw_runs: list[dict[str, Any]] = []
    for _ in range(runs):
        raw_runs.append(
            await _run_flux_pipeline_once(
                pcm=pcm,
                manifest=manifest,
                timeout_seconds=timeout_seconds,
                eot_threshold=eot_threshold,
                history_turns=history_turns,
            )
        )

    successful = [row for row in raw_runs if row["error"] is None]
    transcripts = [row["flux"]["transcript"] for row in successful if row.get("flux")]
    answers = [row.get("answer", "") for row in successful]
    triggers = Counter(
        row["flux"].get("trigger") or "MISSING"
        for row in successful
        if row.get("flux")
    )
    wers = [float(row["transcript_wer"]) for row in successful if row.get("transcript_wer") is not None]

    def speech_first(row):
        return row["speech"]["first_read_ns"] if row.get("speech") else None

    def speech_last(row):
        return row["speech"]["last_read_ns"] if row.get("speech") else None

    def flux_sot(row):
        return row["flux"]["start_of_turn_ns"] if row.get("flux") else None

    def flux_eot(row):
        return row["flux"]["end_of_turn_ns"] if row.get("flux") else None

    def flux_speech_send_return(row):
        return row["flux"]["final_speech_send_return_ns"] if row.get("flux") else None

    def agent_start(row):
        return row["agent"]["agent_start_enter_ns"] if row.get("agent") else None

    def first_token(row):
        return row["agent"]["first_token_ns"] if row.get("agent") else None

    def first_audio(row):
        return row["agent"]["first_audio_ns"] if row.get("agent") else None

    llm_model = os.getenv("LLM_MODEL") or "llama-3.3-70b-versatile(default)"
    agent_voice_id = CallSettings.builtin().voice_id
    metric_scope = (
        "frozen synthetic S16LE16k source -> Bluetooth inbound codec -> real Deepgram Flux -> "
        "real Groq -> real ElevenLabs -> local Bluetooth outbound adapter; "
        f"fixture_sha256={manifest.sha256}; deepgram=flux-general-en; "
        f"eot_threshold={eot_threshold if eot_threshold is not None else 'provider-default-0.7'}; "
        f"history_turns={history_turns}; history_messages={history_turns * 2}; "
        f"llm={llm_model}; "
        f"tts_model=eleven_turbo_v2_5; agent_voice_id={agent_voice_id}; "
        "no PipeWire/device/cellular/handset"
    )

    metrics = [
        _metric_from_runs(
            successful,
            name="source_first_fixture_frame_to_flux_start_of_turn",
            start_getter=speech_first,
            end_getter=flux_sot,
            start_boundary="benchmark replay session returns first frozen caller-audio PCM frame",
            end_boundary="Deepgram TurnInfo StartOfTurn callback receipt",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="source_last_fixture_frame_to_flux_end_of_turn",
            start_getter=speech_last,
            end_getter=flux_eot,
            start_boundary="benchmark replay session returns final frozen caller-audio PCM frame",
            end_boundary="Deepgram TurnInfo EndOfTurn callback receipt",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="flux_final_fixture_frame_send_return_to_end_of_turn",
            start_getter=flux_speech_send_return,
            end_getter=flux_eot,
            start_boundary="FluxService.send returned for converted final caller-fixture frame",
            end_boundary="Deepgram TurnInfo EndOfTurn callback receipt",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="flux_eot_to_agent_start",
            start_getter=flux_eot,
            end_getter=agent_start,
            start_boundary="Deepgram TurnInfo EndOfTurn callback receipt",
            end_boundary="Agent.start_turn entry",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="agent_start_to_llm_first_token",
            start_getter=agent_start,
            end_getter=first_token,
            start_boundary="Agent.start_turn entry",
            end_boundary="Agent._on_llm_token first callback",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="llm_first_token_to_tts_first_audio",
            start_getter=first_token,
            end_getter=first_audio,
            start_boundary="Agent._on_llm_token first callback",
            end_boundary="Agent._on_tts_audio first callback",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="source_last_fixture_frame_to_tts_first_audio",
            start_getter=speech_last,
            end_getter=first_audio,
            start_boundary="benchmark replay session returns final frozen caller-audio PCM frame",
            end_boundary="Agent._on_tts_audio first callback",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="flux_final_fixture_frame_send_return_to_tts_first_audio",
            start_getter=flux_speech_send_return,
            end_getter=first_audio,
            start_boundary="FluxService.send returned for converted final caller-fixture frame",
            end_boundary="Agent._on_tts_audio first callback",
            scope=metric_scope,
        ),
        _metric_from_runs(
            successful,
            name="flux_eot_to_local_first_playback_write",
            start_getter=flux_eot,
            end_getter=lambda row: row.get("first_local_write_ns"),
            start_boundary="Deepgram TurnInfo EndOfTurn callback receipt",
            end_boundary="benchmark local session.write receives first outbound Bluetooth PCM chunk",
            scope=metric_scope,
        ),
        Metric(
            name="caller_heard_first_audio",
            status="NOT_MEASURED",
            scope="requires a separately authorized real device/cellular boundary",
            start_boundary="caller speech end",
            end_boundary="caller hears AI audio",
        ),
    ]

    errors = [row["error"] for row in raw_runs if row["error"] is not None]
    checks = [
        CheckResult(
            name="all_requested_runs_completed",
            passed=(len(successful) == runs),
            status="PROVEN_PROVIDER_PIPELINE_NO_DEVICE" if len(successful) == runs else "FAILED",
            note=f"successful={len(successful)} requested={runs}" + (f"; first_error={errors[0]}" if errors else ""),
        ),
        CheckResult(
            name="fixture_hash_verified_before_network_run",
            passed=True,
            status="PROVEN_LOCAL",
            note=f"sha256={manifest.sha256}",
        ),
        CheckResult(
            name="deepgram_final_transcript_present",
            passed=(len(transcripts) == len(successful) and all(text.strip() for text in transcripts)) if successful else False,
            status="PROVEN_PROVIDER_PIPELINE_NO_DEVICE" if successful else "NOT_MEASURED",
        ),
        CheckResult(
            name="end_to_end_grounded_codeword_answer",
            passed=(all("orbit-7" in answer.lower() for answer in answers) and len(answers) == len(successful)) if successful else False,
            status="PROVEN_PROVIDER_PIPELINE_NO_DEVICE" if successful else "NOT_MEASURED",
            note="Synthetic deterministic grounding check; not a general answer-quality score.",
        ),
        CheckResult(
            name="no_ai_identity_break_in_answers",
            passed=(all("ai language model" not in answer.lower() and "as an ai" not in answer.lower() for answer in answers)) if successful else None,
            status="PROVEN_PROVIDER_PIPELINE_NO_DEVICE" if successful else "NOT_MEASURED",
        ),
        CheckResult(
            name="caller_heard_audio",
            passed=None,
            status="NOT_MEASURED",
            note="No PipeWire, Bluetooth device, phone, carrier or cellular network is present in this mode.",
        ),
    ]

    return BenchmarkReport(
        mode="flux_pipeline",
        metrics=metrics,
        checks=checks,
        metadata={
            **_base_metadata(repo_root),
            "network_used": True,
            "providers": ["Deepgram", "Groq", "ElevenLabs"],
            "deepgram_model": "flux-general-en",
            "deepgram_encoding": "mulaw",
            "deepgram_sample_rate": 8000,
            "llm_model_env": os.getenv("LLM_MODEL") or None,
            "agent_voice_id": agent_voice_id,
            "fixture": {
                **manifest.to_dict(),
                "audio_path": str(audio_path),
                "manifest_path": str(manifest_path),
            },
            "runs_requested": runs,
            "runs_successful": len(successful),
            "percentile_method": "nearest-rank empirical sample percentile",
            "percentile_caution": (
                "p95 is an empirical nearest-rank sample percentile; with small n it is close to/max of the sample and is not a population guarantee"
            ),
        },
        limitations=[
            "This is real Deepgram/Groq/ElevenLabs provider traffic with a frozen synthetic source, not a real phone call.",
            "PipeWire, Bluetooth hardware/radio, handset, carrier and cellular-network latency are not measured.",
            "The source boundary is the frozen fixture frame boundary, not a human mouth or handset microphone. The TTS-generated fixture may contain leading/trailing non-speech, so fixture-end -> EOT is not labelled acoustic speech-end latency.",
            "Local session.write is process dispatch only; it is not caller-heard playback.",
            (
                "Each sample uses a fresh provider session. "
                + (
                    f"The Agent is deterministically seeded with {history_turns} completed prior turns "
                    f"({history_turns * 2} history messages) before the measured turn."
                    if history_turns
                    else "The Agent starts with empty conversation history."
                )
                + " Connection setup occurs before the measured speech boundary."
            ),
            "Provider/network variation is observed, not causally attributed.",
            "p95 is reported as an empirical nearest-rank sample percentile; use more samples before treating tails as stable.",
        ],
        raw={
            "runs": raw_runs,
            "deepgram_end_of_turn_triggers": dict(triggers),
            "transcript_word_error_rate_samples": wers,
            "transcript_word_error_rate_median": (float(statistics.median(wers)) if wers else None),
            "source_max_lateness_ms": [row["source_max_lateness_ms"] for row in raw_runs],
        },
    )
