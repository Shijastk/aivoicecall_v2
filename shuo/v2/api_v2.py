"""
shuo/v2/api_v2.py
FastAPI Router for direct browser WebSocket connections.
"""

import json
import uuid
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from shuo.types import AppState, Phase, StreamStartEvent, StreamStopEvent
from shuo.types import FeedFluxAction, StartAgentTurnAction, ResetAgentTurnAction
from shuo.state import process_event
from shuo.services.flux import FluxService
from shuo.tracer import Tracer
from shuo.call_monitor import MONITOR, LISTENING, SPEAKING
from shuo.runtime_config import load_call_settings
from shuo.conversation import _TurnCompletion, _trace_playback_timeout
from shuo.log import get_logger

from shuo.v2.browser_session import BrowserSession
from shuo.v2.agent_v2 import AgentV2
from shuo.v2.services.tts_pool_v2 import TTSPoolV2

logger = get_logger("shuo.v2.api")
router = APIRouter(prefix="/v2", tags=["V2 Direct Browser Calling"])

# NEW: Dummy GET route purely for Swagger UI (/docs) visibility
@router.get("/call/{persona}/{language}", summary="WebSocket connection for v2 Audio Stream")
async def v2_browser_call_docs(persona: str, language: str):
    """
    **DOCUMENTATION ONLY:**
    This endpoint is a WebSocket route. Do not use HTTP GET.
    
    Connect your frontend using:
    `ws://localhost:3040/v2/call/{persona}/{language}`
    
    - `persona`: e.g., 'candidate'
    - `language`: 'en' (English/Shunya) or 'ml' (Malayalam/Azure)
    """
    return {"message": "This is a WebSocket endpoint. Please connect using ws://"}


@router.websocket("/call/{persona}/{language}")
async def v2_browser_call(websocket: WebSocket, persona: str, language: str):
    await websocket.accept()
    
    call_id = f"web-{uuid.uuid4().hex[:8]}"
    logger.info(f"Incoming v2 Web Call: {call_id} | Lang: {language}")
    
    import time
    session = BrowserSession(websocket, call_id)
    event_queue: asyncio.Queue = asyncio.Queue()
    timer_state = {"last_speech_time": time.monotonic(), "count": 0}
    tracer = Tracer()
    
    recorder = MONITOR.begin(
        persona=persona,
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
                    events = session.parse_message(data)
                    for event in events:
                        await event_queue.put(event)
                        if isinstance(event, StreamStopEvent):
                            return
                except json.JSONDecodeError:
                    continue
        except WebSocketDisconnect:
            await event_queue.put(StreamStopEvent())

    reader_task = asyncio.create_task(read_browser())
    state = AppState(call_id=call_id)
    settings = load_call_settings()
    recorder.configured(settings.describe())
    
    agent = None
    tts_pool = None
    
    # Send initial idle state to the frontend
    await session.send_state("listening")
    
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
                    tts_pool = TTSPoolV2(language=language)
                    await tts_pool.start()
                    
                    agent = AgentV2(
                        session=session,
                        on_done=completion.arm,
                        tts_pool=tts_pool,
                        tracer=tracer,
                        language=language,
                        persona_id=persona,
                        settings=settings,
                        recorder=recorder,
                    )

            old_phase = state.phase
            state, actions = process_event(state, event)
            recorder.phase(SPEAKING if state.phase == Phase.RESPONDING else LISTENING)
            
            # If we returned to listening phase, notify the frontend
            if old_phase == Phase.RESPONDING and state.phase == Phase.LISTENING:
                await session.send_state("listening")
            
            for action in actions:
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