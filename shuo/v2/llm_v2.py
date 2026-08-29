"""
shuo/v2/llm_v2.py
Parses streaming JSON to extract speech text and emotional states for the robot.
"""

from typing import Callable, Awaitable
from shuo.services.llm import LLMService

class EvaluativeLLMService(LLMService):
    def __init__(
        self,
        on_token: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        on_emotion: Callable[[str], Awaitable[None]],
        system_prompt: str,
    ):
        super().__init__(
            on_token=self._intercept_token,
            on_done=self._intercept_done,
            system_prompt=system_prompt,
        )
        self._real_on_token = on_token
        self._real_on_done = on_done
        self._on_emotion = on_emotion
        self._buffer = ""
        self._is_capturing_text = False
        self._has_captured_emotion = False
        self._has_captured_action = False

    async def _intercept_token(self, token: str) -> None:
        self._buffer += token

        if not getattr(self, '_has_captured_action', False) and not self._is_capturing_text:
            marker = '"intervention_text":'
            idx = self._buffer.find(marker)
            if idx != -1:
                after_marker = self._buffer[idx + len(marker):].lstrip(' \n\r\t')
                if after_marker.startswith('"'):
                    self._is_capturing_text = True
                    text_start = self._buffer.find('"', idx + len(marker)) + 1
                    self._buffer = self._buffer[text_start:]
                    self._has_captured_action = True
        
        if self._is_capturing_text:
            end_idx = -1
            i = 0
            while i < len(self._buffer):
                if self._buffer[i] == '\\':
                    i += 2
                    continue
                if self._buffer[i] == '"':
                    end_idx = i
                    break
                i += 1
            
            if end_idx != -1:
                chunk_to_send = self._buffer[:end_idx]
                if chunk_to_send:
                    clean_chunk = chunk_to_send.replace('\\"', '"').replace('\\n', '\n')
                    await self._real_on_token(clean_chunk)
                
                self._is_capturing_text = False
                self._buffer = self._buffer[end_idx + 1:]
            else:
                if self._buffer.endswith('\\'):
                    chunk_to_send = self._buffer[:-1]
                    self._buffer = self._buffer[-1:]
                else:
                    chunk_to_send = self._buffer
                    self._buffer = ""
                
                if chunk_to_send:
                    clean_chunk = chunk_to_send.replace('\\"', '"').replace('\\n', '\n')
                    await self._real_on_token(clean_chunk)
                
        # Emotion parsing safely
        import re
        if not self._is_capturing_text:
            emotion_match = re.search(r'"emotion"\s*:\s*"([^"]+)"', self._buffer)
            if emotion_match and not getattr(self, '_has_captured_emotion', False):
                emotion = emotion_match.group(1)
                if emotion in ["neutral", "happy", "excited", "surprised", "confused", "sad"]:
                    await self._on_emotion(emotion)
                    self._has_captured_emotion = True
                    
    async def _intercept_done(self) -> None:
        self._buffer = ""
        self._is_capturing_text = False
        self._has_captured_emotion = False
        self._has_captured_action = False
        await self._real_on_done()
