"""
LLM service with streaming (Groq, OpenAI-compatible).
"""

import os
import asyncio
import time
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


class PreparedLLMResponse:
    """A speculative provider stream paused after its first content token.

    Phase 4C keeps at most the first content token before final EndOfTurn. The
    remainder stays unread in the provider stream and is consumed token-by-token
    only after exact final validation. Cancelling the draft never mutates normal
    conversation history.
    """

    def __init__(
        self,
        *,
        client,
        request_kwargs: Dict,
        system_prompt: str,
        history_snapshot: List[Dict[str, str]],
        user_message: str,
    ):
        self._client = client
        self._request_kwargs = dict(request_kwargs)
        self._system_prompt = system_prompt
        self._history_snapshot = [dict(message) for message in history_snapshot]
        self._user_message = user_message
        self._stream = None
        self._prefetch_task: Optional[asyncio.Task] = None
        self._first_token: Optional[str] = None
        self._first_token_at: Optional[float] = None
        self._consumed = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._closed_event = asyncio.Event()

    @property
    def first_token_at(self) -> Optional[float]:
        return self._first_token_at

    def matches(
        self,
        *,
        system_prompt: str,
        history: List[Dict[str, str]],
        user_message: str,
    ) -> bool:
        return (
            self._system_prompt == system_prompt
            and self._history_snapshot == history
            and self._user_message == user_message
        )

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("prepared LLM response is closed")
        if self._prefetch_task is None:
            self._prefetch_task = asyncio.create_task(self._prefetch_first_token())

    async def wait_first_token(self) -> float:
        self.start()
        assert self._prefetch_task is not None
        await asyncio.shield(self._prefetch_task)
        if self._first_token_at is None or self._first_token is None:
            raise RuntimeError("prepared LLM response produced no content token")
        return self._first_token_at

    async def consume(
        self,
        on_token: Callable[[str], Awaitable[None]],
    ) -> None:
        if self._consumed:
            raise RuntimeError("prepared LLM response already consumed")
        self._consumed = True
        await self.wait_first_token()
        try:
            assert self._first_token is not None
            await on_token(self._first_token)
            assert self._stream is not None
            async for chunk in self._stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    await on_token(delta.content)
        finally:
            await self._close_stream()

    async def cancel(self) -> None:
        task = self._prefetch_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_stream()

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def _prefetch_first_token(self) -> None:
        try:
            self._stream = await self._client.chat.completions.create(
                **self._request_kwargs
            )
            async for chunk in self._stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    self._first_token = delta.content
                    self._first_token_at = time.perf_counter()
                    return
            raise RuntimeError("prepared LLM response produced no content token")
        except BaseException:
            await self._close_stream()
            raise

    async def _close_stream(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            stream = self._stream
            self._stream = None
            if stream is not None:
                close = getattr(stream, "close", None)
                if close is None:
                    close = getattr(stream, "aclose", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            self._closed = True
            self._closed_event.set()


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
            api_key=(
                os.getenv("LLM_API_KEY")
                or os.getenv("GROQ_API_KEY", "")
                or "local"
            ),
            base_url=os.getenv(
                "LLM_BASE_URL",
                "https://api.groq.com/openai/v1",
            ),
        )
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._request_seq = 0
        
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

    async def start_prepared(
        self,
        user_message: str,
        prepared: PreparedLLMResponse,
    ) -> bool:
        """Commit a validated speculative stream without a second LLM request."""
        if self._running:
            await self.cancel()

        if prepared.first_token_at is None or not prepared.matches(
            system_prompt=self._system_prompt,
            history=self._history,
            user_message=user_message,
        ):
            await prepared.cancel()
            return False

        self._history.append({"role": "user", "content": user_message})
        self._running = True
        self._task = asyncio.create_task(self._generate_prepared(prepared))
        log.connected()
        log.info(
            f"LLMRequest: prepared_reuse history_messages={len(self._history)}"
        )
        return True
    
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

            self._request_seq += 1
            request_seq = self._request_seq
            history_messages = len(self._history)
            system_prompt_chars = len(self._system_prompt)
            total_prompt_chars = sum(
                len(str(message.get("content") or ""))
                for message in messages
            )

            log.info(
                "LLMRequest: begin "
                f"seq={request_seq} "
                f"model={model} "
                f"message_count={len(messages)} "
                f"history_messages={history_messages} "
                f"system_prompt_chars={system_prompt_chars} "
                f"total_prompt_chars={total_prompt_chars}"
            )

            extra_body = {}

            if model.startswith("openai/gpt-oss"):
                extra_body = {
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                }

            elif model in {
                "qwen/qwen3.6-27b",
                "qwen/qwen3.8-27b",
            }:
                extra_body = {
                    "reasoning_effort": "none",
                }

            request_started_at = time.perf_counter()

            stream = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                max_tokens=500,
                temperature=0.7,
                extra_body=extra_body,
            )

            stream_opened_at = time.perf_counter()
            log.info(
                "LLMRequest: stream_open "
                f"seq={request_seq} "
                f"elapsed_ms={(stream_opened_at - request_started_at) * 1000:.1f}"
            )

            first_content_logged = False

            async for chunk in stream:
                if not self._running:
                    break
                
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    if not first_content_logged:
                        first_content_at = time.perf_counter()
                        log.info(
                            "LLMRequest: first_token "
                            f"seq={request_seq} "
                            f"ttft_ms={(first_content_at - request_started_at) * 1000:.1f} "
                            f"stream_to_token_ms={(first_content_at - stream_opened_at) * 1000:.1f}"
                        )
                        first_content_logged = True

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

    async def _generate_prepared(self, prepared: PreparedLLMResponse) -> None:
        """Resume one validated provider stream using normal token/history hooks."""
        assistant_response = ""

        async def on_prepared_token(token: str) -> None:
            nonlocal assistant_response
            if not self._running:
                return
            assistant_response += token
            await self._on_token(token)

        try:
            await prepared.consume(on_prepared_token)
            if self._running and assistant_response:
                self._history.append(
                    {"role": "assistant", "content": assistant_response}
                )
                await self._on_done()
        except asyncio.CancelledError:
            if assistant_response:
                self._history.append(
                    {"role": "assistant", "content": assistant_response + "..."}
                )
            raise
        except Exception as e:
            log.error("Prepared generation failed", e)
            await self._on_done()
        finally:
            await prepared.cancel()
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
            api_key=(
                os.getenv("LLM_API_KEY")
                or os.getenv("GROQ_API_KEY", "")
                or "local"
            ),
            base_url=os.getenv(
                "LLM_BASE_URL",
                "https://api.groq.com/openai/v1",
            ),
        )

    def create_prepared(self, user_message: str) -> PreparedLLMResponse:
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
        elif model in {
            "qwen/qwen3.6-27b",
            "qwen/qwen3.8-27b",
        }:
            extra_body = {"reasoning_effort": "none"}

        return PreparedLLMResponse(
            client=self._client,
            request_kwargs={
                "model": model,
                "messages": messages,
                "stream": True,
                "max_tokens": 500,
                "temperature": 0.7,
                "extra_body": extra_body,
            },
            system_prompt=self._system_prompt,
            history_snapshot=history_snapshot,
            user_message=user_message,
        )

    async def first_token_at(self, user_message: str) -> float:
        prepared = self.create_prepared(user_message)
        prepared.start()
        try:
            return await prepared.wait_first_token()
        finally:
            await prepared.cancel()
