from __future__ import annotations

import asyncio
import inspect
import time
from typing import Awaitable, Callable, Optional, Protocol, Union

from ..state import process_event
from ..log import get_logger
from ..types import (
    AgentTurnDoneEvent,
    AppState,
    Event,
    FeedFluxAction,
    FluxEndOfTurnEvent,
    FluxStartOfTurnEvent,
    ResetAgentTurnAction,
    StartAgentTurnAction,
    StreamStartEvent,
    StreamStopEvent,
)
from .phase3_session import Phase3AiOnlySession
from .shuo_inbound import BluetoothInboundEvents
from .shuo_media import BluetoothOutboundMedia


EVENT_QUEUE_CAPACITY = 256

_latency_log = get_logger("shuo.bluetooth.latency")


class BluetoothFlux(Protocol):
    async def start(self) -> None: ...
    async def send(self, audio_bytes: bytes) -> None: ...
    async def stop(self) -> None: ...


class BluetoothAgent(Protocol):
    @property
    def history(self): ...

    async def start_turn(self, transcript: str) -> None: ...
    async def cancel_turn(self) -> None: ...
    async def cleanup(self) -> None: ...


class BluetoothSpeculator(Protocol):
    def on_eager(self, transcript: str, *, observed_at: Optional[float] = None) -> None: ...
    def on_resumed(self, *, observed_at: Optional[float] = None) -> None: ...
    def on_final(self, transcript: str, *, observed_at: Optional[float] = None) -> None: ...
    async def cleanup(self) -> None: ...


FluxFactory = Callable[..., BluetoothFlux]

AgentFactory = Callable[
    [BluetoothOutboundMedia, Callable[[Optional[str]], None]],
    Union[BluetoothAgent, Awaitable[BluetoothAgent]],
]
SpeculationFactory = Callable[[BluetoothAgent], BluetoothSpeculator]


class BluetoothConversationError(RuntimeError):
    pass


async def run_bluetooth_conversation(
    session: Phase3AiOnlySession,
    *,
    flux_factory: FluxFactory,
    agent_factory: AgentFactory,
    speculation_factory: Optional[SpeculationFactory] = None,
    stream_id: str = "bluetooth-local",
    call_id: str = "bluetooth-local",
) -> None:
    """Run the SHUO state/action loop over one Phase-3 Bluetooth session.

    This is intentionally an opt-in runner. It is not imported by ``main.py``
    or the production carrier server.

    Completion semantics are deliberately different from carrier playback:
    Bluetooth currently has no authoritative handset-played acknowledgement.
    When Agent reports that its player has *dispatched* the final frame, this
    runner queues ``AgentTurnDoneEvent`` directly. It never fabricates a
    ``PlaybackMarkEvent`` and never claims the handset has played the audio.
    """

    event_queue: asyncio.Queue[Event] = asyncio.Queue(
        maxsize=EVENT_QUEUE_CAPACITY
    )
    inbound = BluetoothInboundEvents(session)
    outbound = BluetoothOutboundMedia(session)

    agent: Optional[BluetoothAgent] = None
    speculator: Optional[BluetoothSpeculator] = None
    flux: Optional[BluetoothFlux] = None
    reader_task: Optional[asyncio.Task[None]] = None
    session_started = False

    last_flux_eot_at: Optional[float] = None

    async def on_flux_end_of_turn(transcript: str) -> None:
        nonlocal last_flux_eot_at
        last_flux_eot_at = time.perf_counter()
        if speculator is not None:
            speculator.on_final(transcript, observed_at=last_flux_eot_at)
        _latency_log.info(
            "BTLatency: Flux EndOfTurn received transcript_chars=%d",
            len(transcript),
        )
        await event_queue.put(FluxEndOfTurnEvent(transcript=transcript))

    async def on_flux_start_of_turn() -> None:
        _latency_log.info("BTLatency: Flux StartOfTurn received")
        await event_queue.put(FluxStartOfTurnEvent())

    async def on_flux_interim(_transcript: str) -> None:
        # Slice 3 has no monitor/history integration yet. Interim text is not a
        # state-machine event in the carrier loop either.
        return None

    async def on_flux_eager_end_of_turn(transcript: str) -> None:
        if speculator is not None:
            speculator.on_eager(transcript, observed_at=time.perf_counter())

    async def on_flux_turn_resumed() -> None:
        if speculator is not None:
            speculator.on_resumed(observed_at=time.perf_counter())

    def on_agent_done(_checkpoint: Optional[str]) -> None:
        # Local dispatch completion only. Do not synthesize PlaybackMarkEvent.
        try:
            event_queue.put_nowait(AgentTurnDoneEvent())
        except asyncio.QueueFull as exc:
            raise BluetoothConversationError(
                "Bluetooth event queue is full while completing an agent turn"
            ) from exc

    async def read_bluetooth() -> None:
        try:
            while True:
                event = await inbound.read_event()
                await event_queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Capture/process disappearance ends this media session. Detailed
            # process diagnostics remain owned by Phase-3 endpoint logging.
            await event_queue.put(StreamStopEvent())

    state = AppState(call_id=call_id)

    try:
        await session.start()
        session_started = True

        if speculation_factory is None:
            flux = flux_factory(
                on_flux_end_of_turn,
                on_flux_start_of_turn,
                on_flux_interim,
            )
        else:
            flux = flux_factory(
                on_flux_end_of_turn,
                on_flux_start_of_turn,
                on_flux_interim,
                on_flux_eager_end_of_turn,
                on_flux_turn_resumed,
            )
        await flux.start()

        created_agent = agent_factory(outbound, on_agent_done)
        agent = (
            await created_agent
            if inspect.isawaitable(created_agent)
            else created_agent
        )
        if speculation_factory is not None:
            speculator = speculation_factory(agent)

        await event_queue.put(
            StreamStartEvent(stream_sid=stream_id, call_id=call_id)
        )
        reader_task = asyncio.create_task(read_bluetooth())

        while True:
            event = await event_queue.get()
            state, actions = process_event(state, event)

            for action in actions:
                if isinstance(action, FeedFluxAction):
                    await flux.send(action.audio_bytes)

                elif isinstance(action, StartAgentTurnAction):
                    if last_flux_eot_at is None:
                        _latency_log.info(
                            "BTLatency: Agent start requested without recorded Flux EndOfTurn"
                        )
                    else:
                        eot_to_agent_ms = (
                            time.perf_counter() - last_flux_eot_at
                        ) * 1000.0
                        _latency_log.info(
                            "BTLatency: Flux EndOfTurn -> Agent start %.1fms",
                            eot_to_agent_ms,
                        )
                    await agent.start_turn(action.transcript)

                elif isinstance(action, ResetAgentTurnAction):
                    await agent.cancel_turn()

            if isinstance(event, StreamStopEvent):
                break

    finally:
        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

        try:
            inbound.finish()
        except Exception:
            # Teardown must continue even if capture ended on a partial sample.
            pass

        if speculator is not None:
            try:
                await speculator.cleanup()
            except Exception:
                pass

        if agent is not None:
            try:
                await agent.cleanup()
            except Exception:
                pass

        if flux is not None:
            try:
                await flux.stop()
            except Exception:
                pass

        if session_started:
            await session.stop()
