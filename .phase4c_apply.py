from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one marker, found {count}: {old[:80]!r}")
    file.write_text(text.replace(old, new, 1))


PREPARED_CLASS = r'''

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
'''


# shuo/services/llm.py
replace_once(
    "shuo/services/llm.py",
    "\n\nclass LLMService:\n",
    PREPARED_CLASS + "\n\nclass LLMService:\n",
)

replace_once(
    "shuo/services/llm.py",
    '''        self._task = asyncio.create_task(self._generate())
        log.connected()
    
    async def cancel(self) -> None:
''',
    '''        self._task = asyncio.create_task(self._generate())
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
            "LLMRequest: prepared_reuse history_messages=%d",
            len(self._history),
        )
        return True
    
    async def cancel(self) -> None:
''',
)

replace_once(
    "shuo/services/llm.py",
    '''        finally:
            self._running = False
            self._task = None

class ShadowLLMProbe:
''',
    '''        finally:
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
''',
)

replace_once(
    "shuo/services/llm.py",
    '''        self._client = client or AsyncOpenAI(
            api_key=os.getenv("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
''',
    '''        self._client = client or AsyncOpenAI(
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
''',
)

old_probe_method = '''    async def first_token_at(self, user_message: str) -> float:
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
        elif model in {
            "qwen/qwen3.6-27b",
            "qwen/qwen3.8-27b",
        }:
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
'''
new_probe_method = '''    def create_prepared(self, user_message: str) -> PreparedLLMResponse:
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
'''
replace_once("shuo/services/llm.py", old_probe_method, new_probe_method)


# shuo/agent.py
replace_once(
    "shuo/agent.py",
    "from .services.llm import LLMService\n",
    "from .services.llm import LLMService, PreparedLLMResponse\n",
)
replace_once(
    "shuo/agent.py",
    '''    async def start_turn(self, transcript: str) -> None:
        """Start a new agent turn."""
''',
    '''    async def start_turn(
        self,
        transcript: str,
        prepared_response: Optional[PreparedLLMResponse] = None,
    ) -> None:
        """Start a new agent turn."""
''',
)
replace_once(
    "shuo/agent.py",
    '''        # Start LLM
        self._tracer.begin(self._turn, "llm")
        await self._llm.start(transcript)

        tts_ms = int((self._t_tts_conn - self._t0) * 1000)
''',
    '''        # Start LLM. Phase 4C may reuse one exact-match provider stream;
        # validation failure immediately falls back to the normal final-EOT path.
        self._tracer.begin(self._turn, "llm")
        reused = False
        if prepared_response is not None:
            reused = await self._llm.start_prepared(transcript, prepared_response)
        if not reused:
            await self._llm.start(transcript)
        else:
            log.info(f"Lifecycle: turn={self._turn} prepared_response_reused")

        tts_ms = int((self._t_tts_conn - self._t0) * 1000)
''',
)


# shuo/bluetooth/speculative.py
replace_once(
    "shuo/bluetooth/speculative.py",
    '''class ShadowProbe(Protocol):
    async def first_token_at(self, transcript: str) -> float: ...


@dataclass(frozen=True)
''',
    '''class ShadowProbe(Protocol):
    async def first_token_at(self, transcript: str) -> float: ...
    def create_prepared(self, transcript: str): ...


class PreparedResponse(Protocol):
    @property
    def first_token_at(self) -> Optional[float]: ...
    def start(self) -> None: ...
    async def wait_first_token(self) -> float: ...
    async def wait_closed(self) -> None: ...
    async def cancel(self) -> None: ...


@dataclass(frozen=True)
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''    started_at: Optional[float] = None
    first_token_at: Optional[float] = None


class AsyncCapacityGate:
''',
    '''    started_at: Optional[float] = None
    first_token_at: Optional[float] = None
    prepared: Optional[PreparedResponse] = None
    promoted: bool = False


class AsyncCapacityGate:
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''    def __init__(
        self, *, probe: ShadowProbe, capacity_gate: AsyncCapacityGate,
        early_transcripts: bool = False,
    ):
        self._probe = probe
''',
    '''    def __init__(
        self, *, probe: ShadowProbe, capacity_gate: AsyncCapacityGate,
        early_transcripts: bool = False,
        prepared_reuse: bool = False,
    ):
        if prepared_reuse and not callable(getattr(probe, "create_prepared", None)):
            raise ValueError("prepared reuse requires a prepared-response probe")
        self._probe = probe
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''        self._observations: deque[ShadowObservation] = deque(maxlen=256)
        self._early_transcripts = early_transcripts
        self._stable_text = ""
''',
    '''        self._observations: deque[ShadowObservation] = deque(maxlen=256)
        self._early_transcripts = early_transcripts
        self._prepared_reuse = prepared_reuse
        self._committed: Optional[tuple[int, str, PreparedResponse]] = None
        self._stable_text = ""
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''    @property
    def active_generation_id(self) -> Optional[int]:
        candidate = self._candidate
        return candidate.generation_id if candidate is not None else None

    def on_start(self) -> None:
''',
    '''    @property
    def active_generation_id(self) -> Optional[int]:
        candidate = self._candidate
        return candidate.generation_id if candidate is not None else None

    def take_committed(self, transcript: str):
        """Transfer one exact final-matching prepared stream to the Agent."""
        committed = self._committed
        if committed is None:
            return None
        generation_id, committed_transcript, prepared = committed
        self._committed = None
        if transcript.strip() != committed_transcript:
            self._schedule_prepared_cancel(prepared)
            log.info(
                "BTPrepared: generation=%d discarded reason=take_mismatch",
                generation_id,
            )
            return None
        log.info("BTPrepared: generation=%d handed_to_agent", generation_id)
        return prepared

    def on_start(self) -> None:
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''    def on_start(self) -> None:
        self._diagnose("start_boundary", "reset")
''',
    '''    def on_start(self) -> None:
        self._discard_committed("new_turn")
        self._diagnose("start_boundary", "reset")
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''        self._attempts += 1
        self._last_trigger_at = now
        self._generation_id += 1
        candidate = _Candidate(self._generation_id, transcript, now, trigger)
        self._candidate = self._last_candidate = candidate
        task = asyncio.create_task(self._run_probe(candidate))
''',
    '''        self._attempts += 1
        self._last_trigger_at = now
        self._generation_id += 1
        candidate = _Candidate(self._generation_id, transcript, now, trigger)
        if self._prepared_reuse:
            try:
                candidate.prepared = self._probe.create_prepared(transcript)
            except Exception as exc:
                self._record(
                    candidate,
                    outcome=f"probe_error:{type(exc).__name__}",
                )
                return "prepare_error"
        self._candidate = self._last_candidate = candidate
        task = asyncio.create_task(
            self._run_prepared(candidate)
            if self._prepared_reuse
            else self._run_probe(candidate)
        )
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''    def on_resumed(self, *, observed_at: Optional[float] = None) -> None:
        self._diagnose("resumed", "stability_reset")
''',
    '''    def on_resumed(self, *, observed_at: Optional[float] = None) -> None:
        self._discard_committed("resumed")
        self._diagnose("resumed", "stability_reset")
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''        if candidate.first_token_at is not None and candidate.first_token_at <= final_at:
            shadow_ttft_ms = None
''',
    '''        if (
            self._prepared_reuse
            and candidate.prepared is not None
            and candidate.first_token_at is not None
            and candidate.first_token_at <= final_at
        ):
            candidate.promoted = True
            self._committed = (
                candidate.generation_id,
                final_transcript,
                candidate.prepared,
            )
            shadow_ttft_ms = None
            if candidate.started_at is not None:
                shadow_ttft_ms = max(
                    0.0,
                    (candidate.first_token_at - candidate.started_at) * 1000.0,
                )
            self._record(
                candidate,
                outcome="promoted_ready_before_final",
                eager_to_final_ms=eager_to_final_ms,
                shadow_ttft_ms=shadow_ttft_ms,
                first_token_before_final=True,
                transcript_match=True,
            )
            self._candidate = None
            return

        if candidate.first_token_at is not None and candidate.first_token_at <= final_at:
            shadow_ttft_ms = None
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''    async def cleanup(self) -> None:
        self._diagnose("cleanup", "closed")
        self._closed = True
        if self._candidate is not None:
''',
    '''    async def cleanup(self) -> None:
        self._diagnose("cleanup", "closed")
        self._closed = True
        self._discard_committed("cleanup")
        if self._candidate is not None:
''',
)
replace_once(
    "shuo/bluetooth/speculative.py",
    '''    def _cancel_task(self, candidate: _Candidate) -> None:
        task = candidate.task
''',
    '''    async def _run_prepared(self, candidate: _Candidate) -> None:
        acquired = False
        prepared = candidate.prepared
        if prepared is None:
            return
        try:
            acquired = await self._capacity_gate.acquire()
            if not acquired:
                if self._candidate is candidate:
                    self._record(candidate, outcome="capacity_skip")
                    self._candidate = None
                await prepared.cancel()
                return

            if self._candidate is not candidate or self._closed:
                await prepared.cancel()
                return

            candidate.started_at = time.perf_counter()
            prepared.start()
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                candidate.first_token_at = await prepared.wait_first_token()

            if self._candidate is candidate and not self._closed:
                ttft_ms = max(
                    0.0,
                    (candidate.first_token_at - candidate.started_at) * 1000.0,
                )
                log.info(
                    "BTPrepared: first token ready generation=%d ttft=%.1fms "
                    "trigger_to_first_token_ms=%.1f",
                    candidate.generation_id,
                    ttft_ms,
                    (candidate.first_token_at - candidate.trigger_at) * 1000.0,
                )

            # Keep the single provider stream alive but unread until final EOT
            # either commits it to Agent or invalidation closes it.
            await prepared.wait_closed()

        except asyncio.CancelledError:
            if not candidate.promoted:
                await prepared.cancel()
            raise
        except Exception as exc:
            if self._candidate is candidate:
                self._record(candidate, outcome=f"probe_error:{type(exc).__name__}")
                self._candidate = None
            if not candidate.promoted:
                await prepared.cancel()
        finally:
            if acquired:
                self._capacity_gate.release()

    def _schedule_prepared_cancel(self, prepared: PreparedResponse) -> None:
        task = asyncio.create_task(prepared.cancel())
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _discard_committed(self, reason: str) -> None:
        committed = self._committed
        if committed is None:
            return
        generation_id, _transcript, prepared = committed
        self._committed = None
        self._schedule_prepared_cancel(prepared)
        log.info(
            "BTPrepared: generation=%d discarded reason=%s",
            generation_id,
            reason,
        )

    def _cancel_task(self, candidate: _Candidate) -> None:
        task = candidate.task
''',
)


# shuo/bluetooth/conversation.py
replace_once(
    "shuo/bluetooth/conversation.py",
    "    async def start_turn(self, transcript: str) -> None: ...\n",
    "    async def start_turn(self, transcript: str, prepared_response=None) -> None: ...\n",
)
replace_once(
    "shuo/bluetooth/conversation.py",
    '''    def on_final(self, transcript: str, *, observed_at: Optional[float] = None) -> None: ...
    async def cleanup(self) -> None: ...
''',
    '''    def on_final(self, transcript: str, *, observed_at: Optional[float] = None) -> None: ...
    def take_committed(self, transcript: str): ...
    async def cleanup(self) -> None: ...
''',
)
replace_once(
    "shuo/bluetooth/conversation.py",
    '''                    await agent.start_turn(action.transcript)
                    _latency_log.info("BTLifecycle: event=AgentStart_returned normal_turn=%d", normal_turn)
''',
    '''                    prepared_response = None
                    if speculator is not None:
                        prepared_response = speculator.take_committed(
                            action.transcript
                        )
                    if prepared_response is None:
                        await agent.start_turn(action.transcript)
                    else:
                        _latency_log.info(
                            "BTPrepared: event=AgentStart reuse=true normal_turn=%d",
                            normal_turn,
                        )
                        try:
                            await agent.start_turn(
                                action.transcript,
                                prepared_response=prepared_response,
                            )
                        except BaseException:
                            cancel = getattr(prepared_response, "cancel", None)
                            if cancel is not None:
                                result = cancel()
                                if inspect.isawaitable(result):
                                    await result
                            raise
                    _latency_log.info("BTLifecycle: event=AgentStart_returned normal_turn=%d", normal_turn)
''',
)


# shuo/bluetooth/production.py
replace_once(
    "shuo/bluetooth/production.py",
    '''    shadow_speculation: bool = False,
    shadow_early_transcripts: bool = False,
    deps: BluetoothProductionDeps = BluetoothProductionDeps(),
''',
    '''    shadow_speculation: bool = False,
    shadow_early_transcripts: bool = False,
    prepared_response_reuse: bool = False,
    deps: BluetoothProductionDeps = BluetoothProductionDeps(),
''',
)
replace_once(
    "shuo/bluetooth/production.py",
    '''    if shadow_early_transcripts and not shadow_speculation:
        raise ValueError("shadow_early_transcripts requires shadow_speculation")
    if shadow_speculation and eager_eot_threshold is None:
''',
    '''    if shadow_early_transcripts and not shadow_speculation:
        raise ValueError("shadow_early_transcripts requires shadow_speculation")
    if prepared_response_reuse and not shadow_speculation:
        raise ValueError("prepared_response_reuse requires shadow_speculation")
    if shadow_speculation and eager_eot_threshold is None:
''',
)
replace_once(
    "shuo/bluetooth/production.py",
    '''        return SpeculativeTurnCoordinator(
            probe=probe, capacity_gate=shadow_gate,
            early_transcripts=shadow_early_transcripts,
        )
''',
    '''        return SpeculativeTurnCoordinator(
            probe=probe, capacity_gate=shadow_gate,
            early_transcripts=shadow_early_transcripts,
            prepared_reuse=prepared_response_reuse,
        )
''',
)


# scripts/run_bluetooth_ai.py
replace_once(
    "scripts/run_bluetooth_ai.py",
    '''    parser.add_argument(
        "--shadow-early-transcripts", action="store_true",
        help="Opt in to bounded repeated-Update shadow probes before eager EOT.",
    )
    args = parser.parse_args()
''',
    '''    parser.add_argument(
        "--shadow-early-transcripts", action="store_true",
        help="Opt in to bounded repeated-Update shadow probes before eager EOT.",
    )
    parser.add_argument(
        "--prepared-response-reuse",
        action="store_true",
        help=(
            "Phase-4C opt-in: reuse a matching first-token-ready speculative "
            "LLM stream after final EndOfTurn; normal generation is fallback."
        ),
    )
    args = parser.parse_args()
''',
)
replace_once(
    "scripts/run_bluetooth_ai.py",
    '''    if args.shadow_early_transcripts and not args.shadow_speculation:
        parser.error("--shadow-early-transcripts requires --shadow-speculation")
    if args.shadow_speculation and args.eager_eot_threshold is None:
''',
    '''    if args.shadow_early_transcripts and not args.shadow_speculation:
        parser.error("--shadow-early-transcripts requires --shadow-speculation")
    if args.prepared_response_reuse and not args.shadow_speculation:
        parser.error("--prepared-response-reuse requires --shadow-speculation")
    if args.shadow_speculation and args.eager_eot_threshold is None:
''',
)
replace_once(
    "scripts/run_bluetooth_ai.py",
    '''        shadow_speculation=args.shadow_speculation,
        shadow_early_transcripts=args.shadow_early_transcripts,
    )
''',
    '''        shadow_speculation=args.shadow_speculation,
        shadow_early_transcripts=args.shadow_early_transcripts,
        prepared_response_reuse=args.prepared_response_reuse,
    )
''',
)


TESTS = r'''import asyncio
from types import SimpleNamespace

import pytest

from shuo.bluetooth.conversation import run_bluetooth_conversation
from shuo.bluetooth.production import run_production_bluetooth_conversation
from shuo.bluetooth.speculative import AsyncCapacityGate, SpeculativeTurnCoordinator
from shuo.services.llm import LLMService, ShadowLLMProbe


def chunk(text=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
    )


class CountingStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.reads = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        self.reads += 1
        return self.chunks.pop(0)

    async def close(self):
        self.closed = True


class BlockingStream(CountingStream):
    def __init__(self, chunks):
        super().__init__(chunks)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def __anext__(self):
        if self.reads == 0:
            self.entered.set()
            await self.release.wait()
        return await super().__anext__()


class FakeCompletions:
    def __init__(self, stream):
        self.stream = stream
        self.calls = 0
        self.kwargs = None

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return self.stream


class FakeClient:
    def __init__(self, stream):
        self.completions = FakeCompletions(stream)
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.mark.asyncio
async def test_prepared_stream_prefetches_only_first_content_token_then_reuses_rest():
    history = [{"role": "user", "content": "old"}]
    stream = CountingStream([chunk(None), chunk("first"), chunk(" second")])
    client = FakeClient(stream)
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: history,
        client=client,
    )

    prepared = probe.create_prepared("question")
    prepared.start()
    assert isinstance(await prepared.wait_first_token(), float)
    assert client.completions.calls == 1
    assert stream.reads == 2
    assert stream.closed is False

    tokens = []

    async def collect(token):
        tokens.append(token)

    await prepared.consume(collect)
    assert tokens == ["first", " second"]
    assert client.completions.calls == 1
    assert stream.reads == 3
    assert stream.closed is True
    assert history == [{"role": "user", "content": "old"}]


@pytest.mark.asyncio
async def test_llm_service_commits_prepared_stream_with_normal_history_semantics(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")
    tokens = []
    done = asyncio.Event()

    async def on_token(token):
        tokens.append(token)

    async def on_done():
        done.set()

    service = LLMService(on_token=on_token, on_done=on_done, system_prompt="system")
    service._history = [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
    ]
    stream = CountingStream([chunk("new"), chunk(" answer")])
    client = FakeClient(stream)
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: service.history,
        client=client,
    )
    prepared = probe.create_prepared("final question")
    prepared.start()
    await prepared.wait_first_token()

    assert await service.start_prepared("final question", prepared) is True
    await asyncio.wait_for(done.wait(), 1)
    if service._task is not None:
        await service._task
    assert client.completions.calls == 1
    assert tokens == ["new", " answer"]
    assert service.history == [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
        {"role": "user", "content": "final question"},
        {"role": "assistant", "content": "new answer"},
    ]


@pytest.mark.asyncio
async def test_prepared_history_mismatch_fails_closed_without_history_mutation(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")

    async def noop(*args):
        return None

    service = LLMService(on_token=noop, on_done=noop, system_prompt="system")
    service._history = [{"role": "user", "content": "old"}]
    stream = CountingStream([chunk("draft")])
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: service.history,
        client=FakeClient(stream),
    )
    prepared = probe.create_prepared("question")
    prepared.start()
    await prepared.wait_first_token()
    service._history.append({"role": "assistant", "content": "changed"})
    before = service.history

    assert await service.start_prepared("question", prepared) is False
    assert service.history == before
    assert stream.closed is True


@pytest.mark.asyncio
async def test_prepared_cancellation_matches_normal_partial_history_semantics(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unused")
    got_first = asyncio.Event()
    release_second = asyncio.Event()

    class TwoStageStream:
        def __init__(self):
            self.index = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return chunk("first")
            if self.index == 2:
                await release_second.wait()
                return chunk(" second")
            raise StopAsyncIteration

        async def close(self):
            self.closed = True

    async def on_token(token):
        if token == "first":
            got_first.set()

    async def on_done():
        return None

    service = LLMService(on_token=on_token, on_done=on_done, system_prompt="system")
    stream = TwoStageStream()
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: service.history,
        client=FakeClient(stream),
    )
    prepared = probe.create_prepared("question")
    prepared.start()
    await prepared.wait_first_token()
    assert await service.start_prepared("question", prepared) is True
    await got_first.wait()
    await service.cancel()

    assert service.history == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "first..."},
    ]
    assert stream.closed is True


@pytest.mark.asyncio
async def test_coordinator_promotes_only_matching_first_token_ready_stream_once():
    stream = CountingStream([chunk("first"), chunk(" rest")])
    client = FakeClient(stream)
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: [],
        client=client,
    )
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
        prepared_reuse=True,
    )
    coordinator.on_eager("hello", observed_at=10.0)
    await asyncio.sleep(0)
    candidate = coordinator._candidate
    assert candidate is not None
    await candidate.prepared.wait_first_token()
    await asyncio.sleep(0)

    coordinator.on_final("hello", observed_at=10.5)
    assert coordinator.observations[-1].outcome == "promoted_ready_before_final"
    prepared = coordinator.take_committed("hello")
    assert prepared is not None
    assert coordinator.take_committed("hello") is None

    tokens = []

    async def collect(token):
        tokens.append(token)

    await prepared.consume(collect)
    assert tokens == ["first", " rest"]
    assert client.completions.calls == 1
    await coordinator.cleanup()


@pytest.mark.asyncio
async def test_final_before_first_token_cancels_prepared_and_falls_back():
    stream = BlockingStream([chunk("draft")])
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: [],
        client=FakeClient(stream),
    )
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
        prepared_reuse=True,
    )
    coordinator.on_eager("hello", observed_at=1.0)
    await asyncio.sleep(0)
    await asyncio.wait_for(stream.entered.wait(), 1)
    coordinator.on_final("hello", observed_at=1.2)
    await asyncio.sleep(0)

    assert coordinator.take_committed("hello") is None
    assert coordinator.observations[-1].outcome == "not_ready_by_final"
    assert stream.closed is True
    await coordinator.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["mismatch", "resume"])
async def test_invalidated_prepared_stream_is_never_promoted(boundary):
    stream = CountingStream([chunk("draft"), chunk(" rest")])
    probe = ShadowLLMProbe(
        system_prompt="system",
        history_provider=lambda: [],
        client=FakeClient(stream),
    )
    coordinator = SpeculativeTurnCoordinator(
        probe=probe,
        capacity_gate=AsyncCapacityGate(1),
        prepared_reuse=True,
    )
    coordinator.on_eager("hello", observed_at=1.0)
    await asyncio.sleep(0)
    await coordinator._candidate.prepared.wait_first_token()
    await asyncio.sleep(0)

    if boundary == "resume":
        coordinator.on_resumed(observed_at=1.1)
        await asyncio.sleep(0)
        coordinator.on_final("hello", observed_at=1.2)
    else:
        coordinator.on_final("hello changed", observed_at=1.2)
    await asyncio.sleep(0)

    assert coordinator.take_committed("hello") is None
    assert coordinator.take_committed("hello changed") is None
    await coordinator.cleanup()
    assert stream.closed is True


class FakeSession:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.block = asyncio.Event()

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def read(self):
        await self.block.wait()
        raise RuntimeError("ended")

    async def write(self, audio):
        return None

    async def clear(self):
        return None


@pytest.mark.asyncio
async def test_conversation_routes_committed_response_to_agent_once():
    session = FakeSession()
    holder = {}
    ready = asyncio.Event()
    prepared = SimpleNamespace(cancel=lambda: None)

    class Flux:
        def __init__(self, eot, sot, interim, eager, resumed):
            self.eot = eot

        async def start(self):
            ready.set()

        async def stop(self):
            return None

        async def send(self, audio):
            return None

    class Agent:
        history = []

        def __init__(self, done):
            self.done = done
            self.calls = []

        async def start_turn(self, transcript, prepared_response=None):
            self.calls.append((transcript, prepared_response))
            self.done(None)

        async def cancel_turn(self):
            return None

        async def cleanup(self):
            return None

    class Speculator:
        def __init__(self):
            self.taken = False

        def on_start(self): pass
        def on_interim(self, *args, **kwargs): pass
        def on_eager(self, *args, **kwargs): pass
        def on_resumed(self, *args, **kwargs): pass
        def on_final(self, transcript, **kwargs): self.final = transcript
        def take_committed(self, transcript):
            if self.taken:
                return None
            self.taken = True
            return prepared
        async def cleanup(self): pass

    def flux_factory(eot, sot, interim, eager, resumed):
        holder["flux"] = Flux(eot, sot, interim, eager, resumed)
        return holder["flux"]

    def agent_factory(outbound, done):
        holder["agent"] = Agent(done)
        return holder["agent"]

    def speculation_factory(agent):
        return Speculator()

    task = asyncio.create_task(run_bluetooth_conversation(
        session,
        flux_factory=flux_factory,
        agent_factory=agent_factory,
        speculation_factory=speculation_factory,
    ))
    try:
        await ready.wait()
        await holder["flux"].eot("question")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert holder["agent"].calls == [("question", prepared)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_production_rejects_prepared_reuse_without_shadow_before_resources():
    with pytest.raises(ValueError, match="requires shadow_speculation"):
        await run_production_bluetooth_conversation(
            object(),
            prepared_response_reuse=True,
        )
'''

test_path = Path("tests/test_bluetooth_phase4c.py")
if test_path.exists():
    raise SystemExit("tests/test_bluetooth_phase4c.py already exists")
test_path.write_text(TESTS)

print("Phase 4C deterministic source application complete")
