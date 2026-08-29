import json
import asyncio
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from shuo.types import AppState, Phase, StreamStartEvent, StreamStopEvent
from shuo.types import FeedFluxAction, StartAgentTurnAction, ResetAgentTurnAction
from shuo.state import process_event
from shuo.services.flux import FluxService
from shuo.v2.services.tts_pool_v2 import TTSPoolV2
from shuo.tracer import Tracer
from shuo.call_monitor import MONITOR, LISTENING, SPEAKING
from shuo.runtime_config import load_call_settings
from shuo.conversation import _TurnCompletion, _trace_playback_timeout
from shuo.log import get_logger

from shuo.v2.browser_session import BrowserSession
from shuo.v2.agent_v2 import AgentV2

logger = get_logger("shuo.v2.conversation")

async def run_web_conversation(
    websocket: WebSocket, 
    call_id: str, 
    persona_id: str, 
    language: str
) -> None:
    """
    Dedicated WebRTC/WebSocket event loop.
    Reuses pure state logic but avoids Carrier recording dependencies.
    """
    import time
    session = BrowserSession(websocket, call_id)
    event_queue: asyncio.Queue = asyncio.Queue()
    timer_state = {"last_speech_time": time.monotonic(), "count": 0}
    tracer = Tracer()
    
    recorder = MONITOR.begin(
        persona=persona_id,
        direction="inbound",
        attempt=call_id,
        to_number="Web",
        from_number="WebUser"
    )
    
    completion = _TurnCompletion(
        queue=event_queue,
        grace_seconds=0, 
        on_timeout=lambda name: _trace_playback_timeout(tracer, name)
    )

    # Turn detector callbacks
    async def on_flux_end_of_turn(transcript: str) -> None:
        from shuo.types import FluxEndOfTurnEvent
        await session.send_transcript("user", transcript, is_final=True)
        await event_queue.put(FluxEndOfTurnEvent(transcript=transcript))

    async def on_flux_start_of_turn() -> None:
        from shuo.types import FluxStartOfTurnEvent
        await event_queue.put(FluxStartOfTurnEvent())

    async def on_flux_interim(transcript: str) -> None:
        recorder.caller_partial(transcript)
        await session.send_transcript("user", transcript, is_final=False)
        import time
        timer_state["last_speech_time"] = time.monotonic()
        timer_state["count"] = 0

    flux = FluxService(
        on_end_of_turn=on_flux_end_of_turn,
        on_start_of_turn=on_flux_start_of_turn,
        on_interim=on_flux_interim,
    )

    async def read_browser():
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                
                events = session.parse_message(data)
                for event in events:
                    await event_queue.put(event)
                    if isinstance(event, StreamStopEvent):
                        return
        except WebSocketDisconnect:
            await event_queue.put(StreamStopEvent())
        except Exception as e:
            logger.error(f"Browser reader failed: {e}")
            await event_queue.put(StreamStopEvent())

    reader_task = asyncio.create_task(read_browser())
    state = AppState(call_id=call_id)
    settings = load_call_settings()
    recorder.configured(settings.describe())
    
    agent = None
    tts_pool = None
    
    try:
        import time
        timer_state["last_speech_time"] = time.monotonic()
        timer_state["count"] = 0
        
        while True:
            event = await event_queue.get()
            
            from shuo.types import FluxStartOfTurnEvent, FluxEndOfTurnEvent
            if isinstance(event, (FluxStartOfTurnEvent, FluxEndOfTurnEvent)):
                timer_state["last_speech_time"] = time.monotonic()
                timer_state["count"] = 0
            
            if isinstance(event, StreamStartEvent):
                recorder.identify(call_id)
                if agent is None:
                    await flux.start()
                    
                    # For v2, we pool our new TTS engine
                    tts_pool = TTSPoolV2(language=language)
                    await tts_pool.start()
                    
                    agent = AgentV2(
                        session=session,
                        on_done=completion.arm,
                        tts_pool=tts_pool,
                        tracer=tracer,
                        language=language,
                        persona_id=persona_id,
                        settings=settings,
                        recorder=recorder,
                    )

            # Pure state machine update (100% bug-free reuse)
            old_phase = state.phase
            state, actions = process_event(state, event)
            recorder.phase(SPEAKING if state.phase == Phase.RESPONDING else LISTENING)
            
            # Action dispatching
            for action in actions:
                try:
                    if isinstance(action, FeedFluxAction):
                        await flux.send(action.audio_bytes)
                    elif isinstance(action, StartAgentTurnAction):
                        completion.void("new turn")
                        recorder.caller_said(action.transcript)
                        if agent: 
                            await agent.start_turn(action.transcript)
                    elif isinstance(action, ResetAgentTurnAction):
                        completion.void("interrupted")
                        if agent: 
                            await agent.cancel_turn()
                except Exception as e:
                    logger.error(f"Action dispatch error: {e}")
                    
            if isinstance(event, StreamStopEvent):
                break
                
            now = time.monotonic()
            if state.phase == Phase.LISTENING and (now - timer_state["last_speech_time"] > 15.0):
                timer_state["count"] += 1
                timer_state["last_speech_time"] = now
                if timer_state["count"] == 1:
                    from shuo.types import FluxEndOfTurnEvent
                    await event_queue.put(FluxEndOfTurnEvent(transcript="Are you still there?"))
                else:
                    await session.send_stop()
                    break
                
    finally:
        recorder.ended("Browser call disconnected", "completed")
        reader_task.cancel()
        await completion.close()
        if agent: 
            await agent.cleanup()
        if tts_pool: 
            await tts_pool.stop()
        await flux.stop()
        await session.send_stop()
        tracer.save(call_id)
        logger.info("Web conversation loop terminated cleanly.")