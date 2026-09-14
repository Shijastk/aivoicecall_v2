"""
LLM service with streaming (Groq, OpenAI-compatible).
"""

import os
import asyncio
from typing import Optional, Callable, Awaitable, List, Dict

from openai import AsyncOpenAI

from ..log import ServiceLogger

log = ServiceLogger("LLM")

# The prompt for an install nobody has configured. Kept here rather than in
# the config store because a default invented *there* would put words in the
# twin's mouth that no operator wrote (see
# ConfigDocument.resolved_system_prompt) -- this module owns the fallback, and
# `shuo/runtime_config.py` decides when to use it.
SYSTEM_PROMPT = """You are a helpful voice assistant. Keep your responses concise and conversational, as they will be spoken aloud. Avoid using markdown, bullet points, or other formatting that doesn't work well in speech. Be friendly and natural."""


class LLMService:
    """
    OpenAI streaming LLM service.

    Manages conversation history and streams tokens via callback.
    """

    def __init__(
        self,
        on_token: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        system_prompt: Optional[str] = None,
    ):
        self._on_token = on_token
        self._on_done = on_done

        # Fixed for the lifetime of this service, which is the lifetime of
        # the call. A prompt that could change between turns is a twin that
        # can contradict what it said three turns ago -- failure mode 3 in
        # CLAUDE.md, introduced by our own plumbing rather than by the model.
        self._system_prompt = system_prompt or SYSTEM_PROMPT

        self._client = AsyncOpenAI(
            api_key=os.getenv("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
        self._task: Optional[asyncio.Task] = None
        self._running = False
        
        self._history: List[Dict[str, str]] = []
    
    @property
    def is_active(self) -> bool:
        return self._running and self._task is not None
    
    @property
    def history(self) -> List[Dict[str, str]]:
        return self._history.copy()
    
    def clear_history(self) -> None:
        self._history = []
    
    async def start(self, user_message: str) -> None:
        """Start generating a response."""
        if self._running:
            await self.cancel()
        
        self._history.append({"role": "user", "content": user_message})
        
        self._running = True
        self._task = asyncio.create_task(self._generate())
        log.connected()
    
    async def cancel(self) -> None:
        """Cancel ongoing generation."""
        self._running = False
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        log.cancelled()
    
    async def _generate(self) -> None:
        """Generate response and stream tokens."""
        assistant_response = ""
        
        try:
            messages = [
                {"role": "system", "content": self._system_prompt}
            ] + self._history
            
            model = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")

            extra_body = {}

            if model.startswith("openai/gpt-oss"):
                extra_body = {
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                }

            elif model == "qwen/qwen3.6-27b":
                extra_body = {
                    "reasoning_effort": "none",
                }

            stream = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                max_tokens=500,
                temperature=0.7,
                extra_body=extra_body,
            )
            
            async for chunk in stream:
                if not self._running:
                    break
                
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    token = delta.content
                    assistant_response += token
                    await self._on_token(token)
            
            if self._running and assistant_response:
                self._history.append({"role": "assistant", "content": assistant_response})
                await self._on_done()
        
        except asyncio.CancelledError:
            if assistant_response:
                self._history.append({"role": "assistant", "content": assistant_response + "..."})
            raise
        
        except Exception as e:
            log.error("Generation failed", e)
            await self._on_done()
        
        finally:
            self._running = False
            self._task = None

class ShadowLLMProbe:
    """Provider-layer Phase-4B first-token probe.

    It copies committed history, adds the eager user transcript, discards all
    generated text, never calls TTS and never mutates LLMService history.
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        history_provider: Callable[[], List[Dict[str, str]]],
        client=None,
    ):
        self._system_prompt = system_prompt
        self._history_provider = history_provider
        self._client = client or AsyncOpenAI(
            api_key=os.getenv("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )

    async def first_token_at(self, user_message: str) -> float:
        import time

        history_snapshot = [dict(message) for message in self._history_provider()]
        messages = [
            {"role": "system", "content": self._system_prompt},
            *history_snapshot,
            {"role": "user", "content": user_message},
        ]

        model = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
        extra_body = {}
        if model.startswith("openai/gpt-oss"):
            extra_body = {"reasoning_effort": "low", "include_reasoning": False}
        elif model == "qwen/qwen3.6-27b":
            extra_body = {"reasoning_effort": "none"}

        stream = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            max_tokens=500,
            temperature=0.7,
            extra_body=extra_body,
        )

        try:
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    return time.perf_counter()
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

        raise RuntimeError("shadow LLM produced no content token")
