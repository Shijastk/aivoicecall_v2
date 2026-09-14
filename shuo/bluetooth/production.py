from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Type

from ..agent import Agent
from ..runtime_config import CallSettings, load_call_settings
from ..services.flux import FluxService
from ..services.tts_pool import TTSPool
from ..tracer import Tracer
from .conversation import run_bluetooth_conversation
from .phase3_session import Phase3AiOnlySession
from .shuo_media import BluetoothOutboundMedia


@dataclass(frozen=True)
class BluetoothProductionDeps:
    """Injectable production dependencies.

    Defaults are the existing SHUO services. Tests replace these with fakes so
    no provider or hardware contact occurs.
    """

    flux_cls: Type = FluxService
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
    deps: BluetoothProductionDeps = BluetoothProductionDeps(),
) -> None:
    """Wire the real SHUO services to an already-built Bluetooth session.

    This function is deliberately Bluetooth-only and opt-in. Nothing imports
    or calls it from ``main.py`` or the carrier server.

    Lifecycle:
      1. resolve one immutable call settings snapshot
      2. Phase-3 Bluetooth session starts inside the orchestrator
      3. real Flux starts inside the orchestrator
      4. async Agent factory starts the per-call TTSPool
      5. existing Agent streams LLM -> TTS -> AudioPlayer -> Bluetooth adapter
      6. teardown always stops the pool and saves the local trace

    Monitor/call-history/recording policy for this manual Bluetooth entrypoint:
    those carrier-facing observers are intentionally not populated here. Agent
    therefore uses its disabled recorder/tape defaults. A synthetic Bluetooth
    validation session must not create a carrier call-history row or pretend to
    have a provider recording/call id. Local timing trace remains enabled.
    """

    resolved = settings or deps.settings_loader()
    tracer = deps.tracer_factory()

    pool = deps.tts_pool_cls(
        pool_size=1,
        ttl=8.0,
        voice_id=resolved.voice_id,
    )
    pool_started = False

    def flux_factory(on_eot, on_sot, on_interim):
        return deps.flux_cls(
            on_end_of_turn=on_eot,
            on_start_of_turn=on_sot,
            on_interim=on_interim,
        )

    async def agent_factory(
        outbound: BluetoothOutboundMedia,
        on_done,
    ):
        nonlocal pool_started
        if not pool_started:
            await pool.start()
            pool_started = True

        return deps.agent_cls(
            session=outbound,
            on_done=on_done,
            tts_pool=pool,
            tracer=tracer,
            persona_id=persona_id,
            settings=resolved,
        )

    try:
        await deps.conversation_runner(
            session,
            flux_factory=flux_factory,
            agent_factory=agent_factory,
            stream_id=stream_id,
            call_id=call_id,
        )
    finally:
        if pool_started:
            await pool.stop()
        tracer.save(call_id)
