"""
External services for the shuo voice agent pipeline.

Deepgram Flux  -- STT + turn detection
OpenAI         -- LLM streaming
ElevenLabs     -- TTS streaming + connection pool
Player         -- paces audio out through the carrier session

Telephony no longer lives here. It moved to `shuo.carrier`, behind a
provider interface, in Phase 1.
"""

from .flux import FluxService
from .llm import LLMService
from .tts import TTSService
from .tts_pool import TTSPool
from .player import AudioPlayer

__all__ = [
    "FluxService",
    "LLMService",
    "TTSService",
    "TTSPool",
    "AudioPlayer",
]
