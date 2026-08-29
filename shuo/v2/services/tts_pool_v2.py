"""
shuo/v2/services/tts_pool_v2.py
A lightweight TTS pool substitute for the V2 architecture.
Returns the V2TTSRouter without triggering legacy v0 ElevenLabs connections.
"""

from typing import Callable, Any
from shuo.v2.services.tts_router import V2TTSRouter

class TTSPoolV2:
    """
    Mocks the interface of the v0 TTSPool.
    Instead of connecting to ElevenLabs, it returns our new HTTP streaming router.
    """
    def __init__(self, language: str):
        self.language = language

    async def start(self) -> None:
        """No-op. HTTP connections (aiohttp) do not require pre-warming like WebSockets."""
        pass

    async def get(self, on_audio: Callable[[str], None], on_done: Callable[[], None]) -> Any:
        """
        Dispenses a new V2TTSRouter instance configured for the session's language.
        This seamlessly plugs into AgentV2 without modifying its core flow.
        """
        return V2TTSRouter(
            language=self.language,
            on_audio=on_audio,
            on_done=on_done
        )

    async def stop(self) -> None:
        """No-op."""
        pass