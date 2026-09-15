from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Type

from ..agent import Agent
from ..runtime_config import CallSettings, load_call_settings
from ..services.flux import FluxService
from ..services.llm import ShadowLLMProbe
from ..services.tts_pool import TTSPool
from ..tracer import Tracer
from .conversation import run_bluetooth_conversation
from .phase3_session import Phase3AiOnlySession
from .shuo_media import BluetoothOutboundMedia
from .speculative import AsyncCapacityGate, SpeculativeTurnCoordinator


@dataclass(frozen=True)
class BluetoothProductionDeps:
    """Injectable production dependencies.

    Defaults are the existing SHUO services. Tests replace these with fakes so
    no provider or hardware contact occurs.
    """

    flux_cls: Type = FluxService
    shadow_probe_cls: Type = ShadowLLMProbe
    tts_pool_cls: Type = TTSPool
    agent_cls: Type = Agent
    tracer_factory: Callable[[], Tracer] = Tracer
    settings_loader: Callable[[], CallSettings] = load_call_settings
    conversation_runner: Callable = run_bluetooth_conversation


async def run_production_bluetooth_conversation(
    session: Phase3AiOnlySession,
    *,
    persona_id: str = "default",
    stream_id: str = "bluetooth-local",
    call_id: str = "bluetooth-local",
    settings: Optional[CallSettings] = None,
    eager_eot_threshold: Optional[float] = None,
    shadow_speculation: bool = False,
    shadow_early_transcripts: bool = False,
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
      5. existing Agent streams LLM -> TTS -> AudioPlayer -> Bluetooth adapter
      6. teardown always stops the pool and saves the local trace

    ``eager_eot_threshold`` is Phase-4A measurement-only configuration. It is
    disabled by default and is passed only to Flux; it does not start the agent
    before final EndOfTurn or change committed conversation behavior.

    Monitor/call-history/recording policy for this manual Bluetooth entrypoint:
    those carrier-facing observers are intentionally not populated here. Agent
    therefore uses its disabled recorder/tape defaults. A synthetic Bluetooth
    validation session must not create a carrier call-history row or pretend to
    have a provider recording/call id. Local timing trace remains enabled.
    """

    if shadow_early_transcripts and not shadow_speculation:
        raise ValueError("shadow_early_transcripts requires shadow_speculation")
    if shadow_speculation and eager_eot_threshold is None:
        raise ValueError("shadow_speculation requires an explicit eager_eot_threshold")

    resolved = settings or deps.settings_loader()
    tracer = deps.tracer_factory()
    shadow_gate = AsyncCapacityGate(1) if shadow_speculation else None

    pool = deps.tts_pool_cls(
        pool_size=1,
        ttl=8.0,
        voice_id=resolved.voice_id,
    )
    pool_started = False

    def flux_factory(on_eot, on_sot, on_interim, on_eager=None, on_resumed=None):
        kwargs = {
            "on_end_of_turn": on_eot,
            "on_start_of_turn": on_sot,
            "on_interim": on_interim,
        }
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

        return deps.agent_cls(
            session=outbound,
            on_done=on_done,
            tts_pool=pool,
            tracer=tracer,
            persona_id=persona_id,
            settings=resolved,
        )

    def speculation_factory(agent):
        if shadow_gate is None:
            raise RuntimeError("shadow speculation gate was not initialized")
        probe = deps.shadow_probe_cls(
            system_prompt=resolved.system_prompt,
            history_provider=lambda: agent.history,
        )
        return SpeculativeTurnCoordinator(
            probe=probe, capacity_gate=shadow_gate,
            early_transcripts=shadow_early_transcripts,
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
        await deps.conversation_runner(session, **runner_kwargs)
    finally:
        if pool_started:
            await pool.stop()
        tracer.save(call_id)
