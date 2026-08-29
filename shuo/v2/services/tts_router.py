"""
shuo/v2/services/tts_router.py
Dynamically routes text chunks to either Shunya Labs (English) or Azure (Malayalam).
Strictly adheres to the TTSService interface expected by agent_v2.py.
"""

from typing import Callable
from shuo.v2.services.tts_shunya import ShunyaTTSService
from shuo.v2.services.tts_azure import AzureTTSService

class V2TTSRouter:
    """
    Acts as a proxy TTSService. Instantiates the correct underlying 
    engine based on the session language, ensuring 0 architecture breakage.
    """
    def __init__(
        self, 
        language: str, 
        on_audio: Callable[[str], None], 
        on_done: Callable[[], None]
    ):
        self.language = language
        
        # Route to the correct engine
        if self.language == "en":
            self._engine = ShunyaTTSService(on_audio, on_done)
        elif self.language == "ml":
            self._engine = AzureTTSService(on_audio, on_done)
        else:
            self._engine = ShunyaTTSService(on_audio, on_done)

    async def send(self, text: str) -> None:
        """Called by the LLM token loop. Forwards to the active engine."""
        await self._engine.send(text)

    async def flush(self) -> None:
        """Called when LLM generation is complete."""
        await self._engine.flush()

    async def cancel(self) -> None:
        """Called on user barge-in to stop TTS generation."""
        await self._engine.cancel()