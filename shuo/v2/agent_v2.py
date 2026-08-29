"""
shuo/v2/agent_v2.py
V2 Agent Pipeline with Emotion & Action propagation.
"""

import time
from typing import Optional, Callable

from shuo.agent import Agent, _ms_since
from shuo.carrier.base import CarrierSession
from shuo.tracer import Tracer
from shuo.call_monitor import CallRecorder
from shuo.services.tts_pool import TTSPool
from shuo.runtime_config import CallSettings
from shuo.log import ServiceLogger

from shuo.v2.llm_v2 import EvaluativeLLMService
from shuo.v2.prompts import get_evaluative_system_prompt

log = ServiceLogger("AgentV2")

class AgentV2(Agent):
    def __init__(
        self,
        session: CarrierSession,
        on_done: Callable[[Optional[str]], None],
        tts_pool: TTSPool,
        tracer: Tracer,
        language: str = "en",
        persona_id: str = "default",
        settings: Optional[CallSettings] = None,
        recorder: Optional[CallRecorder] = None,
    ):
        super().__init__(
            session=session,
            on_done=on_done,
            tts_pool=tts_pool,
            tracer=tracer,
            persona_id=persona_id,
            settings=settings,
            recorder=recorder
        )
        prompt = get_evaluative_system_prompt(language)
        self._llm = EvaluativeLLMService(
            on_token=self._on_llm_token,
            on_done=self._on_llm_done,
            on_emotion=self._handle_emotion,
            system_prompt=prompt,
        )

    async def _handle_emotion(self, emotion: str) -> None:
        """Sends custom emotion/animation commands to the frontend robot."""
        if hasattr(self._session, "send_emotion"):
            await self._session.send_emotion(emotion)

    async def start_turn(self, transcript: str) -> None:
        await super().start_turn(transcript)
        if hasattr(self._session, "send_state"):
            await self._session.send_state("thinking")

    async def _on_tts_audio(self, audio_base64: str) -> None:
        if not self._active:
            return

        if not self._got_first_audio:
            self._got_first_audio = True
            self._t_first_audio = time.monotonic()
            self._tracer.mark(self._turn, "tts_first_audio")
            ttft = _ms_since(self._t0)
            log.info(f"⏱  V2 DIRECT STREAM: TTS first audio +{ttft}ms")
            self._recorder.timing("tts_first_audio", ttft, turn=self._turn)
            
            if hasattr(self._session, "send_state"):
                await self._session.send_state("speaking")

        await self._session.play_audio(audio_base64)

    async def _on_tts_done(self) -> None:
        if not self._active:
            return
        self._tracer.end(self._turn, "tts")
        if not self._got_first_audio:
            if hasattr(self._session, "send_state"):
                await self._session.send_state("listening")
            self._end_turn(checkpoint=None)
            return
        self._tracer.end(self._turn, "player")
        if hasattr(self._session, "send_state"):
            await self._session.send_state("listening")
        self._end_turn(checkpoint=None)
    async def _on_llm_token(self, token: str) -> None:
        await super()._on_llm_token(token)
        text = "".join(self._response).strip()
        if text and hasattr(self._session, "send_transcript"):
            await self._session.send_transcript("agent", text, is_final=False)

    async def _on_llm_done(self) -> None:
        await super()._on_llm_done()
        text = "".join(self._response).strip()
        if text and hasattr(self._session, "send_transcript"):
            await self._session.send_transcript("agent", text, is_final=True)