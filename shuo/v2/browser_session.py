"""
shuo/v2/browser_session.py
Virtual CarrierSession adapter for direct browser connections.
"""

import base64
from typing import List
from fastapi import WebSocket

from shuo.types import StreamStartEvent, StreamStopEvent, MediaEvent, Event
from shuo.log import get_logger

logger = get_logger("shuo.v2.browser_session")

class BrowserSession:
    def __init__(self, websocket: WebSocket, call_id: str):
        self.websocket = websocket
        self.call_id = call_id
        self.stream_sid = call_id

    async def play_audio(self, payload_b64: str) -> None:
        try:
            await self.websocket.send_json({
                "event": "media",
                "media": {"payload": payload_b64}
            })
            await self.websocket.send_json({
                "event": "mark",
                "name": "playedStream"
            })
        except Exception as e:
            logger.debug(f"Failed to stream to browser: {e}")

    async def send_state(self, state_name: str) -> None:
        try:
            await self.websocket.send_json({
                "event": "state",
                "state": state_name
            })
        except Exception:
            pass

    async def send_emotion(self, emotion_name: str) -> None:
        try:
            await self.websocket.send_json({
                "event": "emotion",
                "emotion": emotion_name
            })
        except Exception:
            pass

    async def send_transcript(self, speaker: str, text: str, is_final: bool = True) -> None:
        try:
            await self.websocket.send_json({
                "event": "transcript",
                "speaker": speaker,
                "text": text,
                "isFinal": is_final
            })
        except Exception:
            pass
    async def send_stop(self) -> None:
        try:
            await self.websocket.send_json({"event": "stop"})
        except Exception:
            pass

    def parse_message(self, data: dict) -> List[Event]:
        event_type = data.get("event")
        if event_type == "start":
            return [StreamStartEvent(stream_sid=self.stream_sid, call_id=self.call_id)]
        elif event_type == "media":
            payload = data.get("media", {}).get("payload", "")
            if payload:
                try:
                    audio_bytes = base64.b64decode(payload)
                    return [MediaEvent(track="inbound", audio_bytes=audio_bytes)]
                except Exception:
                    pass
        elif event_type == "stop":
            return [StreamStopEvent()]
        return []