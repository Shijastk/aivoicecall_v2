"""
shuo/v2/services/tts_azure.py
Microsoft Azure TTS Integration (Fallback: Malayalam).
Wraps text in SSML and streams HTTP chunked audio to base64.
"""

import aiohttp
import base64
import os
import asyncio
from typing import Callable
from shuo.log import get_logger

logger = get_logger("shuo.v2.tts_azure")

class AzureTTSService:
    """Streams Malayalam audio directly from Azure via HTTP chunks."""
    
    def __init__(self, on_audio: Callable[[str], None], on_done: Callable[[], None]):
        self._on_audio = on_audio
        self._on_done = on_done
        self._api_key = os.getenv("AZURE_SPEECH_KEY", "")
        self._region = os.getenv("AZURE_SPEECH_REGION", "centralindia")
        self._voice = "ml-IN-SobhanaNeural"  # Malayalam Female
        self._buffer = ""
        self._active = False

    async def send(self, text: str) -> None:
        """Accumulates text tokens."""
        self._buffer += text

    async def flush(self) -> None:
        """Wraps text in SSML, sends to Azure, and streams the audio."""
        if not self._buffer.strip():
            await self._on_done_callback()
            return

        if not self._api_key:
            logger.error("AZURE_SPEECH_KEY is not set.")
            await self._on_done_callback()
            return

        self._active = True
        url = f"https://{self._region}.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": self._api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "raw-16khz-16bit-mono-pcm" # High definition PCM for Web
        }
        
        # Format text into SSML for Azure
        ssml = (
            f"<speak version='1.0' xml:lang='ml-IN'>"
            f"<voice name='{self._voice}'>{self._buffer.strip()}</voice>"
            f"</speak>"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=ssml) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Azure TTS Error {response.status}: {error_text}")
                        await self._on_done_callback()
                        return

                    # Stream chunks immediately to eliminate latency
                    async for chunk in response.content.iter_chunked(2048):
                        if not self._active:
                            break
                        if chunk:
                            b64_audio = base64.b64encode(chunk).decode('utf-8')
                            if asyncio.iscoroutinefunction(self._on_audio):
                                await self._on_audio(b64_audio)
                            else:
                                self._on_audio(b64_audio)
                            
        except Exception as e:
            logger.error(f"Azure streaming failed: {e}")
            
        finally:
            self._buffer = ""
            await self._on_done_callback()

    async def _on_done_callback(self):
        if asyncio.iscoroutinefunction(self._on_done):
            await self._on_done()
        else:
            self._on_done()

    async def cancel(self) -> None:
        """Stops the streaming process."""
        self._active = False
        self._buffer = ""