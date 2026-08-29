"""
shuo/v2/services/tts_shunya.py
Shunya Labs TTS Integration (Primary: Indian English).
Streams HTTP chunked audio and converts to base64 for browser WebSocket.
"""

import aiohttp
import base64
import os
import asyncio
from typing import Callable
from shuo.log import get_logger

logger = get_logger("shuo.v2.tts_shunya")

class ShunyaTTSService:
    """Streams Indian English audio directly from Shunya Labs via HTTP chunks."""
    
    def __init__(self, on_audio: Callable[[str], None], on_done: Callable[[], None]):
        self._on_audio = on_audio
        self._on_done = on_done
        self._api_key = os.getenv("SHUNYA_API_KEY", "")
        self._voice = "Sunita"  # Indian English Female
        self._model = "zero-indic"
        self._buffer = ""
        self._active = False

    async def send(self, text: str) -> None:
        """Accumulates text tokens until flush is called."""
        self._buffer += text

    async def flush(self) -> None:
        """Sends the accumulated text to Shunya API and streams the audio back."""
        if not self._buffer.strip():
            await self._on_done_callback()
            return

        if not self._api_key:
            logger.error("SHUNYA_API_KEY is not set.")
            await self._on_done_callback()
            return

        self._active = True
        url = "https://tts.shunyalabs.ai/v1/audio/speech"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self._model,
            "input": self._buffer.strip(),
            "voice": self._voice,
            "language": "en",
            "response_format": "pcm"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Shunya TTS Error {response.status}: {error_text}")
                        await self._on_done_callback()
                        return

                    is_first_chunk = True
                    
                    async for chunk in response.content.iter_chunked(2048):
                        if not self._active:
                            break
                        if chunk:
                            # 100% WORKING FIX: Strip the 44-byte WAV header if it exists
                            if is_first_chunk:
                                is_first_chunk = False
                                if chunk.startswith(b'RIFF'):
                                    chunk = chunk[44:] # Remove header to prevent initial "pop" sound
                                    
                            b64_audio = base64.b64encode(chunk).decode('utf-8')
                            if asyncio.iscoroutinefunction(self._on_audio):
                                await self._on_audio(b64_audio)
                            else:
                                self._on_audio(b64_audio)
                            
        except Exception as e:
            logger.error(f"Shunya streaming failed: {e}")
            
        finally:
            self._buffer = ""
            await self._on_done_callback()

            
    async def _on_done_callback(self):
        """Safely call the completion callback."""
        if asyncio.iscoroutinefunction(self._on_done):
            await self._on_done()
        else:
            self._on_done()

    async def cancel(self) -> None:
        """Stops the streaming process on user barge-in."""
        self._active = False
        self._buffer = ""