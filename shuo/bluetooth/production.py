from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Optional, Type

from ..agent import Agent
from ..runtime_config import CallSettings, load_call_settings
from ..services.flux import FluxService
from ..services.local_malayalam_speech import LocalMalayalamSpeechService
from ..services.llm import ShadowLLMProbe
from ..services.player import PREROLL_FRAMES
from ..services.tts_pool import TTSPool
from ..tracer import Tracer
from ..log import ServiceLogger
from .tts_audio_verify import OutboundAudioTranscriptCapture
from .conversation import run_bluetooth_conversation
from .phase3_session import Phase3AiOnlySession
from .shuo_media import BluetoothOutboundMedia
from .speculative import AsyncCapacityGate, SpeculativeTurnCoordinator


DEEPGRAM_FLUX_SPEECH_PROVIDER = "deepgram-flux"
LOCAL_MALAYALAM_SPEECH_PROVIDER = "local-malayalam"

log = ServiceLogger("BluetoothProduction")

_LOCAL_MALAYALAM_PROMPT_PREFIX = """This Bluetooth call is operating in Malayalam mode.
Reply in natural spoken Malayalam by default.
Do not switch to English merely because the system prompt is written in English or because the transcript contains an English-looking word.
Keep proper nouns, product names, code, URLs, and common technical English terms in English when that is natural in Malayalam speech.
If the caller explicitly asks to continue in English, you may switch languages.
Do not invent a personal name or identity that is not present in the configured facts."""


def _with_local_malayalam_settings(
    settings: CallSettings,
) -> CallSettings:
    return replace(
        settings,
        system_prompt=(
            _LOCAL_MALAYALAM_PROMPT_PREFIX
            + "\n\n"
            + settings.system_prompt
        ),
        prompt_source=(
            f"{settings.prompt_source}+local-malayalam"
        ),
    )


@dataclass(frozen=True)
class BluetoothProductionDeps:
    """Injectable production dependencies.

    Defaults are the existing SHUO services. Tests replace these with fakes so
    no provider or hardware contact occurs.
    """

    flux_cls: Type = FluxService
    local_speech_cls: Type = LocalMalayalamSpeechService
    shadow_probe_cls: Type = ShadowLLMProbe
    tts_pool_cls: Type = TTSPool
    agent_cls: Type = Agent
    tracer_factory: Callable[[], Tracer] = Tracer
    settings_loader: Callable[[], CallSettings] = load_call_settings
    conversation_runner: Callable = run_bluetooth_conversation
    tts_audio_capture_cls: Type = OutboundAudioTranscriptCapture


async def run_production_bluetooth_conversation(
    session: Phase3AiOnlySession,
    *,
    persona_id: str = "default",
    stream_id: str = "bluetooth-local",
    call_id: str = "bluetooth-local",
    speech_provider: str = DEEPGRAM_FLUX_SPEECH_PROVIDER,
    settings: Optional[CallSettings] = None,
    eager_eot_threshold: Optional[float] = None,
    eot_threshold: Optional[float] = None,
    shadow_speculation: bool = False,
    shadow_early_transcripts: bool = False,
    prepared_response_reuse: bool = False,
    tts_phrase_chars: Optional[int] = None,
    llm_history_max_chars: Optional[int] = None,
    llm_provider_timing: bool = False,
    llm_warmup: bool = False,
    parallel_startup: bool = False,
    diagnostics=None,
    player_preroll_frames: int = PREROLL_FRAMES,
    verify_tts_audio_transcript: bool = False,
    diagnose_local_barge_in_probes: bool = False,
    diagnose_agent_text: bool = False,
    deps: BluetoothProductionDeps = BluetoothProductionDeps(),
) -> None:
    """Wire the real SHUO services to an already-built Bluetooth session.

    This function is deliberately Bluetooth-only and opt-in. Nothing imports
    or calls it from ``main.py`` or the carrier server.

    Lifecycle:
      1. resolve one immutable call settings snapshot
      2. Phase-3 Bluetooth session starts inside the orchestrator
      3. real Flux starts inside the orchestrator
      4. async Agent factory starts TTSPool and awaits usable initial readiness
      5. optional Bluetooth-only LLM warmup primes the Agent's existing client
      6. existing Agent streams LLM -> TTS -> AudioPlayer -> Bluetooth adapter
      7. teardown always stops the pool and saves the local trace

    ``eager_eot_threshold`` is Phase-4A measurement-only configuration. It is
    disabled by default and is passed only to Flux; it does not start the agent
    before final EndOfTurn or change committed conversation behavior.

    Monitor/call-history/recording policy for this manual Bluetooth entrypoint:
    those carrier-facing observers are intentionally not populated here. Agent
    therefore uses its disabled recorder/tape defaults. A synthetic Bluetooth
    validation session must not create a carrier call-history row or pretend to
    have a provider recording/call id. Local timing trace remains enabled.
    """

    if speech_provider not in {
        DEEPGRAM_FLUX_SPEECH_PROVIDER,
        LOCAL_MALAYALAM_SPEECH_PROVIDER,
    }:
        raise ValueError(
            "speech_provider must be 'deepgram-flux' "
            "or 'local-malayalam'"
        )

    if (
        speech_provider == LOCAL_MALAYALAM_SPEECH_PROVIDER
        and (
            shadow_speculation
            or shadow_early_transcripts
            or prepared_response_reuse
            or eager_eot_threshold is not None
        )
    ):
        raise ValueError(
            "local-malayalam speech provider does not support "
            "Deepgram eager/shadow speculation flags"
        )

    if shadow_early_transcripts and not shadow_speculation:
        raise ValueError("shadow_early_transcripts requires shadow_speculation")
    if prepared_response_reuse and not shadow_speculation:
        raise ValueError("prepared_response_reuse requires shadow_speculation")
    if shadow_speculation and eager_eot_threshold is None:
        raise ValueError("shadow_speculation requires an explicit eager_eot_threshold")
    if tts_phrase_chars is not None and (
        isinstance(tts_phrase_chars, bool)
        or not isinstance(tts_phrase_chars, int)
        or not 24 <= tts_phrase_chars <= 160
    ):
        raise ValueError("tts_phrase_chars must be an integer between 24 and 160")
    if llm_history_max_chars is not None and (
        isinstance(llm_history_max_chars, bool)
        or not isinstance(llm_history_max_chars, int)
        or llm_history_max_chars <= 0
    ):
        raise ValueError("llm_history_max_chars must be a positive integer")
    if player_preroll_frames not in (2, 3):
        raise ValueError("player_preroll_frames must be 2 or 3")
    if (
        verify_tts_audio_transcript
        and speech_provider != LOCAL_MALAYALAM_SPEECH_PROVIDER
    ):
        raise ValueError(
            "verify_tts_audio_transcript requires "
            "speech_provider=local-malayalam"
        )
    if (
        diagnose_local_barge_in_probes
        and speech_provider != LOCAL_MALAYALAM_SPEECH_PROVIDER
    ):
        raise ValueError(
            "diagnose_local_barge_in_probes requires "
            "speech_provider=local-malayalam"
        )
    if (
        diagnose_agent_text
        and speech_provider != LOCAL_MALAYALAM_SPEECH_PROVIDER
    ):
        raise ValueError(
            "diagnose_agent_text requires "
            "speech_provider=local-malayalam"
        )

    resolved = settings or deps.settings_loader()

    if speech_provider == LOCAL_MALAYALAM_SPEECH_PROVIDER:
        resolved = _with_local_malayalam_settings(
            resolved
        )

    tracer = deps.tracer_factory()
    if diagnose_agent_text:
        tracer.enable_agent_text_capture()
    tts_audio_capture = (
        deps.tts_audio_capture_cls()
        if verify_tts_audio_transcript
        else None
    )
    shadow_gate = AsyncCapacityGate(1) if shadow_speculation else None

    pool = deps.tts_pool_cls(
        pool_size=1,
        ttl=8.0,
        voice_id=resolved.voice_id,
    )
    pool_started = False

    def flux_factory(
        on_eot,
        on_sot,
        on_interim,
        on_eager=None,
        on_resumed=None,
    ):
        if speech_provider == LOCAL_MALAYALAM_SPEECH_PROVIDER:
            kwargs = {
                "on_end_of_turn": on_eot,
                "on_start_of_turn": on_sot,
                "on_interim": on_interim,
            }
            if diagnose_local_barge_in_probes:
                kwargs["barge_in_probe_ms"] = (
                    256,
                    384,
                    512,
                )
            return deps.local_speech_cls(**kwargs)

        kwargs = {
            "on_end_of_turn": on_eot,
            "on_start_of_turn": on_sot,
            "on_interim": on_interim,
        }
        if diagnostics is not None:
            kwargs["message_observer"] = diagnostics.flux_message
        if shadow_early_transcripts:
            kwargs["include_empty_interims"] = True
            kwargs["diagnose_updates"] = True
        if on_eager is not None:
            kwargs["on_eager_end_of_turn"] = on_eager
        if on_resumed is not None:
            kwargs["on_turn_resumed"] = on_resumed
        if eager_eot_threshold is not None:
            # Keep existing injected fakes/providers source-compatible when the
            # feature is off; only the explicit measurement path receives the
            # new argument.
            kwargs["eager_eot_threshold"] = eager_eot_threshold
        if eot_threshold is not None:
            kwargs["eot_threshold"] = eot_threshold
        return deps.flux_cls(**kwargs)

    async def agent_factory(
        outbound: BluetoothOutboundMedia,
        on_done,
    ):
        nonlocal pool_started
        if not pool_started:
            await pool.start()
            pool_started = True
            # Ownership is recorded before this cancellable barrier so startup
            # failure/abort still stops the pool in the outer finally block.
            await pool.wait_ready()

        agent_kwargs = {
            "session": outbound,
            "on_done": on_done,
            "tts_pool": pool,
            "tracer": tracer,
            "persona_id": persona_id,
            "settings": resolved,
        }
        if tts_phrase_chars is not None:
            agent_kwargs["tts_phrase_chars"] = tts_phrase_chars
        if llm_history_max_chars is not None:
            agent_kwargs["llm_history_max_chars"] = llm_history_max_chars
        if llm_provider_timing:
            agent_kwargs["llm_provider_timing"] = True
        if player_preroll_frames != PREROLL_FRAMES:
            agent_kwargs["player_preroll_frames"] = player_preroll_frames
        agent = deps.agent_cls(**agent_kwargs)
        if llm_warmup:
            await agent.warmup_llm()
        return agent

    def speculation_factory(agent):
        if shadow_gate is None:
            raise RuntimeError("shadow speculation gate was not initialized")
        probe_kwargs = {
            "system_prompt": resolved.system_prompt,
            "history_provider": lambda: agent.history,
        }
        if llm_history_max_chars is not None:
            probe_kwargs["history_max_chars"] = llm_history_max_chars
        if llm_provider_timing:
            probe_kwargs["capture_provider_timing"] = True
        probe = deps.shadow_probe_cls(**probe_kwargs)
        return SpeculativeTurnCoordinator(
            probe=probe, capacity_gate=shadow_gate,
            early_transcripts=shadow_early_transcripts,
            prepared_reuse=prepared_response_reuse,
        )

    try:
        runner_kwargs = {
            "flux_factory": flux_factory,
            "agent_factory": agent_factory,
            "stream_id": stream_id,
            "call_id": call_id,
        }
        if shadow_speculation:
            runner_kwargs["speculation_factory"] = speculation_factory
        if diagnostics is not None:
            runner_kwargs["diagnostics"] = diagnostics
        if tts_audio_capture is not None:
            runner_kwargs["outbound_audio_observer"] = tts_audio_capture
        if parallel_startup:
            runner_kwargs["parallel_service_startup"] = True
        await deps.conversation_runner(session, **runner_kwargs)
    finally:
        if pool_started:
            await pool.stop()
        if tts_audio_capture is not None:
            try:
                path = await tts_audio_capture.transcribe_and_save(
                    call_id
                )
                if path is not None:
                    log.info(
                        "TTS outbound transcript verification "
                        f"saved path={path}"
                    )
            except Exception as exc:
                # Diagnostic-only: never turn a completed/failed live call into
                # a different failure because post-call verification failed.
                log.error(
                    "TTS outbound transcript verification failed "
                    f"({type(exc).__name__})"
                )
        tracer.save(call_id)
