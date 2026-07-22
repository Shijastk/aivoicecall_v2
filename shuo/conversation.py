"""
The main event loop for shuo.

This is the explicit, readable loop that drives the entire system:

    while connected:
        event = receive()                               # I/O (from queue)
        state, actions = process_event(state, event)    # PURE
        for action in actions:
            dispatch(action)                            # I/O

Events come from:
- The carrier WebSocket (audio packets, playback acks, DTMF)
- The turn detector (turn events)
- The agent (playback complete)

The loop is carrier-agnostic: it holds a CarrierSession and never
mentions Twilio or Vobiz.
"""

import json
import asyncio
from typing import Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .types import (
    AppState, CallContext,
    Event, StreamStartEvent, StreamStopEvent,
    FluxStartOfTurnEvent, FluxEndOfTurnEvent, AgentTurnDoneEvent,
    PlaybackMarkEvent, AudioClearedEvent, DtmfEvent,
    FeedFluxAction, StartAgentTurnAction, ResetAgentTurnAction,
)
from .state import process_event
from . import config as cfg
from .carrier import Carrier, get_carrier
from .services.flux import FluxService
from .services.tts_pool import TTSPool
from .agent import Agent
from .tracer import Tracer
from .log import Logger, get_logger

logger = get_logger("shuo.conversation")


async def run_conversation(
    websocket: WebSocket,
    context: CallContext,
    carrier: Optional[Carrier] = None,
) -> None:
    """
    Main event loop for a single call.

    1. Create shared event queue and a carrier session
    2. Create the turn detector (always-on STT + turn detection)
    3. Start the carrier reader
    4. On StreamStart, create Agent
    5. Process events through the pure state machine
    6. Dispatch actions inline
    """
    carrier = carrier or get_carrier()
    session = carrier.new_session(websocket, context)

    event_log = Logger(verbose=False)
    event_queue: asyncio.Queue[Event] = asyncio.Queue()
    tracer = Tracer()

    agent: Optional[Agent] = None
    tts_pool = TTSPool(pool_size=1, ttl=8.0)
    recording_started = False
    background_tasks: set = set()

    logger.info(
        f"Call starting  carrier={carrier.name}  direction={context.direction.name.lower()}  "
        f"persona={context.persona_id}  call_id={context.call_id}"
    )

    # ── Turn detector callbacks (push events to queue) ──────────────

    async def on_flux_end_of_turn(transcript: str) -> None:
        await event_queue.put(FluxEndOfTurnEvent(transcript=transcript))

    async def on_flux_start_of_turn() -> None:
        await event_queue.put(FluxStartOfTurnEvent())

    flux = FluxService(
        on_end_of_turn=on_flux_end_of_turn,
        on_start_of_turn=on_flux_start_of_turn,
    )

    # ── Carrier WebSocket reader ────────────────────────────────────

    async def read_carrier() -> None:
        """Background task to read from the carrier and push to the queue."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON frame from carrier")
                    continue

                # One frame can yield several events: a `start` also
                # drains any media that arrived ahead of it.
                #
                # A frame we cannot parse is one lost 20ms of audio, not a
                # reason to hang up on the caller. Only socket-level
                # failures (the outer handlers) end the call.
                try:
                    events = session.parse_message(data)
                except Exception as e:
                    logger.warning(f"Skipping unparseable carrier frame: {e}")
                    continue

                stop = False
                for event in events:
                    await event_queue.put(event)
                    if isinstance(event, StreamStopEvent):
                        stop = True
                if stop:
                    break

        except WebSocketDisconnect as e:
            # 1006 is an abnormal mid-call media drop and is otherwise
            # invisible -- always log the code.
            code = getattr(e, "code", None)
            if code == 1006:
                logger.warning("Carrier WebSocket closed abnormally (1006) -- mid-call media drop")
            else:
                logger.info(f"Carrier WebSocket closed (code {code})")
            await event_queue.put(StreamStopEvent())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            event_log.error("Carrier reader", e)
            await event_queue.put(StreamStopEvent())

    # ── Initialize ──────────────────────────────────────────────────

    state = AppState(call_id=context.call_id)
    reader_task = asyncio.create_task(read_carrier())

    try:
        while True:
            # ─── RECEIVE ────────────────────────────────────────────
            event = await event_queue.get()

            event_log.event(event)

            # Initialize services on stream start
            if isinstance(event, StreamStartEvent):
                # Dual-channel recording cannot be expressed in Vobiz's
                # answer XML (its <Record> element has no channel
                # attribute), so it starts over REST as soon as the call
                # id is known -- which is exactly now.
                if (
                    carrier.records_via_rest
                    and cfg.record_calls()
                    and session.call_id
                    and not recording_started
                ):
                    recording_started = True
                    # Keep a reference: a bare create_task can be garbage
                    # collected mid-flight, silently cancelling itself.
                    task = asyncio.create_task(
                        _start_recording(carrier, session.call_id)
                    )
                    background_tasks.add(task)
                    task.add_done_callback(background_tasks.discard)

                if agent is None:
                    await flux.start()
                    await tts_pool.start()
                    agent = Agent(
                        session=session,
                        on_done=lambda: event_queue.put_nowait(AgentTurnDoneEvent()),
                        tts_pool=tts_pool,
                        tracer=tracer,
                        persona_id=context.persona_id,
                    )
                else:
                    # A reconnect replays `start` on the same call. Keep
                    # the agent and its history; only the stream id moved.
                    logger.info("Stream restarted on the same call -- history preserved")

            # ─── TRACE-ONLY EVENTS ──────────────────────────────────
            if isinstance(event, PlaybackMarkEvent):
                _trace_playback_mark(tracer, event.name)
            elif isinstance(event, AudioClearedEvent):
                logger.debug("Carrier acknowledged audio flush")
            elif isinstance(event, DtmfEvent):
                logger.info(f"DTMF: {event.digit}")

            # ─── UPDATE (pure) ──────────────────────────────────────
            old_phase = state.phase
            state, actions = process_event(state, event)
            event_log.transition(old_phase, state.phase)

            # ─── DISPATCH (side effects) ────────────────────────────
            for action in actions:
                event_log.action(action)
                try:
                    if isinstance(action, FeedFluxAction):
                        await flux.send(action.audio_bytes)

                    elif isinstance(action, StartAgentTurnAction):
                        if agent:
                            await agent.start_turn(action.transcript)

                    elif isinstance(action, ResetAgentTurnAction):
                        if agent:
                            await agent.cancel_turn()

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # A vendor hiccup should cost one turn, not the call.
                    # Drop back to LISTENING so the caller can simply
                    # repeat themselves.
                    event_log.error(f"Action {type(action).__name__} failed", e)
                    if isinstance(action, StartAgentTurnAction):
                        if agent:
                            try:
                                await agent.cancel_turn()
                            except Exception:
                                pass
                        event_queue.put_nowait(AgentTurnDoneEvent())

            # ─── EXIT CHECK ─────────────────────────────────────────
            if isinstance(event, StreamStopEvent):
                break

    except Exception as e:
        event_log.error("Call loop", e)
        raise

    finally:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

        # Let in-flight background work finish before teardown. Abandoning
        # a recording-start request mid-flight loses the recording for the
        # whole call, and a short call can otherwise exit before the
        # request has even been sent.
        if background_tasks:
            await asyncio.wait(set(background_tasks), timeout=5.0)

        if agent:
            await agent.cleanup()

        await tts_pool.stop()
        await flux.stop()

        # Tell the carrier the stream is over *before* anything tears the
        # call down over REST, or a phantom reconnect can overwrite the
        # transcript with empty data.
        await session.send_stop()

        # Traces are keyed on call_id, not stream_id: a reconnect changes
        # the stream id mid-call and would otherwise split the trace.
        tracer.save(session.call_id or context.call_id or "unknown")

        Logger.websocket_disconnected()


async def _start_recording(carrier: Carrier, call_id: str) -> None:
    """
    Kick off dual-channel recording without blocking the call.

    Failure is logged, never raised: losing a recording is bad for review
    but must not drop a live conversation.
    """
    try:
        await carrier.start_recording(
            call_id, callback_url=cfg.recording_callback_url() or None
        )
    except Exception as e:
        logger.error(f"Could not start recording for {call_id}: {e}")


def _trace_playback_mark(tracer: Tracer, name: str) -> None:
    """
    Record a carrier playback acknowledgement against its turn.

    Checkpoints are named "turn-<n>" by the player.
    """
    if not name.startswith("turn-"):
        return
    try:
        turn = int(name.split("-", 1)[1])
    except (ValueError, IndexError):
        return
    tracer.mark(turn, "carrier_playback_done")
    logger.debug(f"Carrier confirmed playback of {name}")
