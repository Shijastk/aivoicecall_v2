from pathlib import Path


def read(path):
    return Path(path).read_text(encoding="utf-8-sig")


def write(path, text):
    Path(path).write_text(text, encoding="utf-8")


def replace_once(path, old, new):
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one marker, found {count}: {old[:120]!r}")
    write(path, text.replace(old, new, 1))


# ---------------------------------------------------------------------------
# New bounded phrase buffer. It never buffers a complete response: punctuation
# or the configured hard character cap releases text incrementally.
# ---------------------------------------------------------------------------
phrase_path = Path("shuo/services/phrase_buffer.py")
if phrase_path.exists():
    raise SystemExit(f"unexpected existing file: {phrase_path}")
phrase_path.write_text('''"""Small deterministic text buffer for streaming LLM -> TTS handoff."""\n\n\nclass BoundedPhraseBuffer:\n    """Emit natural text fragments without ever waiting for a full response.\n\n    Text is preserved byte-for-byte at the Python string level: concatenating all\n    emitted chunks plus ``flush()`` exactly reproduces the input sequence. Hard\n    punctuation may release early; otherwise ``max_chars`` is an absolute bound.\n    No timer/task is created, so cancellation has no background ownership.\n    """\n\n    def __init__(self, max_chars: int, *, min_soft_chars: int = 24):\n        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:\n            raise ValueError("max_chars must be a positive integer")\n        if isinstance(min_soft_chars, bool) or not isinstance(min_soft_chars, int) or min_soft_chars <= 0:\n            raise ValueError("min_soft_chars must be a positive integer")\n        self._max_chars = max_chars\n        self._min_soft_chars = min(min_soft_chars, max_chars)\n        self._buffer = ""\n\n    @property\n    def buffered_chars(self) -> int:\n        return len(self._buffer)\n\n    def feed(self, text: str) -> list[str]:\n        if not text:\n            return []\n        self._buffer += text\n        emitted = []\n        while self._buffer:\n            cut = self._find_hard_boundary()\n            if cut is not None and cut <= self._max_chars:\n                emitted.append(self._take(cut))\n                continue\n\n            cut = self._find_soft_boundary()\n            if cut is not None and cut <= self._max_chars:\n                emitted.append(self._take(cut))\n                continue\n\n            if len(self._buffer) < self._max_chars:\n                break\n\n            cut = self._bounded_cut()\n            emitted.append(self._take(cut))\n        return emitted\n\n    def flush(self) -> str:\n        text = self._buffer\n        self._buffer = ""\n        return text\n\n    def _take(self, cut: int) -> str:\n        text = self._buffer[:cut]\n        self._buffer = self._buffer[cut:]\n        return text\n\n    def _find_hard_boundary(self):\n        for index, char in enumerate(self._buffer):\n            if char in "?!":\n                return index + 1\n            # A period is ambiguous at the current edge (3.8, abbreviations).\n            # Require following whitespace unless max_chars forces a release.\n            if char == "." and index + 1 < len(self._buffer) and self._buffer[index + 1].isspace():\n                return index + 2\n        return None\n\n    def _find_soft_boundary(self):\n        for index, char in enumerate(self._buffer):\n            cut = index + 1\n            if cut < self._min_soft_chars or char not in ",;:":\n                continue\n            if index + 1 < len(self._buffer) and self._buffer[index + 1].isspace():\n                return index + 2\n        return None\n\n    def _bounded_cut(self) -> int:\n        window = self._buffer[: self._max_chars]\n        # Prefer retaining a word boundary, but never choose a very early space\n        # that would defeat the configured batching cap. Include the whitespace\n        # so concatenation remains exact.\n        split_at = max((i for i, ch in enumerate(window) if ch.isspace()), default=-1)\n        if split_at >= self._max_chars // 2:\n            return split_at + 1\n        return self._max_chars\n''', encoding="utf-8")


# ---------------------------------------------------------------------------
# LLM: optional bounded request history + opt-in provider usage metrics.
# Canonical history remains untouched. System/digital-twin facts are never
# subject to the history budget.
# ---------------------------------------------------------------------------
replace_once(
    "shuo/services/llm.py",
    'SYSTEM_PROMPT = """You are a helpful voice assistant. Keep your responses concise and conversational, as they will be spoken aloud. Avoid using markdown, bullet points, or other formatting that doesn\'t work well in speech. Be friendly and natural."""\n\n\nclass PreparedLLMResponse:',
    '''SYSTEM_PROMPT = """You are a helpful voice assistant. Keep your responses concise and conversational, as they will be spoken aloud. Avoid using markdown, bullet points, or other formatting that doesn't work well in speech. Be friendly and natural."""\n\n\ndef _read_usage_field(value, name):\n    if value is None:\n        return None\n    if isinstance(value, dict):\n        return value.get(name)\n    direct = getattr(value, name, None)\n    if direct is not None:\n        return direct\n    extra = getattr(value, "model_extra", None)\n    if isinstance(extra, dict):\n        return extra.get(name)\n    return None\n\n\ndef _bounded_prompt_history(history, max_chars):\n    """Return a recent message suffix while retaining canonical history elsewhere.\n\n    The latest message is always retained even if it alone exceeds the budget.\n    If trimming lands on an assistant message, that orphan is removed so the\n    prompt starts at a user boundary. The immutable system prompt is outside this\n    budget and therefore digital-twin facts/rules are never trimmed here.\n    """\n    copied = [dict(message) for message in history]\n    if max_chars is None:\n        return copied, 0, 0\n    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:\n        raise ValueError("history_max_chars must be a positive integer")\n\n    total_chars = sum(len(str(message.get("content") or "")) for message in copied)\n    if total_chars <= max_chars:\n        return copied, 0, 0\n\n    kept_reversed = []\n    kept_chars = 0\n    for message in reversed(copied):\n        chars = len(str(message.get("content") or ""))\n        if kept_reversed and kept_chars + chars > max_chars:\n            break\n        kept_reversed.append(message)\n        kept_chars += chars\n        if kept_chars >= max_chars:\n            break\n\n    kept = list(reversed(kept_reversed))\n    while len(kept) > 1 and kept[0].get("role") == "assistant":\n        kept_chars -= len(str(kept[0].get("content") or ""))\n        kept.pop(0)\n\n    dropped_messages = len(copied) - len(kept)\n    dropped_chars = total_chars - kept_chars\n    return kept, dropped_messages, dropped_chars\n\n\ndef _log_provider_usage(label: str, usage) -> None:\n    if usage is None:\n        log.info(f"LLMUsage: label={label} available=false")\n        return\n\n    def ms(name):\n        value = _read_usage_field(usage, name)\n        if value is None:\n            return "na"\n        try:\n            return f"{float(value) * 1000:.1f}"\n        except (TypeError, ValueError):\n            return "na"\n\n    details = _read_usage_field(usage, "prompt_tokens_details")\n    cached = _read_usage_field(details, "cached_tokens")\n    prompt_tokens = _read_usage_field(usage, "prompt_tokens")\n    completion_tokens = _read_usage_field(usage, "completion_tokens")\n    total_tokens = _read_usage_field(usage, "total_tokens")\n    log.info(\n        "LLMUsage: "\n        f"label={label} "\n        f"prompt_tokens={prompt_tokens if prompt_tokens is not None else 'na'} "\n        f"cached_tokens={cached if cached is not None else 'na'} "\n        f"completion_tokens={completion_tokens if completion_tokens is not None else 'na'} "\n        f"total_tokens={total_tokens if total_tokens is not None else 'na'} "\n        f"queue_ms={ms('queue_time')} "\n        f"prompt_ms={ms('prompt_time')} "\n        f"completion_ms={ms('completion_time')} "\n        f"server_total_ms={ms('total_time')}"\n    )\n\n\nclass PreparedLLMResponse:'''
)

replace_once(
    "shuo/services/llm.py",
    '''        history_snapshot: List[Dict[str, str]],\n        user_message: str,\n    ):''',
    '''        history_snapshot: List[Dict[str, str]],\n        user_message: str,\n        capture_provider_timing: bool = False,\n    ):'''
)
replace_once(
    "shuo/services/llm.py",
    '''        self._user_message = user_message\n        self._stream = None''',
    '''        self._user_message = user_message\n        self._capture_provider_timing = capture_provider_timing\n        self._usage = None\n        self._request_started_at: Optional[float] = None\n        self._stream_opened_at: Optional[float] = None\n        self._stream = None'''
)
replace_once(
    "shuo/services/llm.py",
    '''    @property\n    def first_token_at(self) -> Optional[float]:\n        return self._first_token_at\n\n    def matches(''',
    '''    @property\n    def first_token_at(self) -> Optional[float]:\n        return self._first_token_at\n\n    @property\n    def usage(self):\n        return self._usage\n\n    @property\n    def request_started_at(self) -> Optional[float]:\n        return self._request_started_at\n\n    @property\n    def stream_opened_at(self) -> Optional[float]:\n        return self._stream_opened_at\n\n    def matches('''
)
replace_once(
    "shuo/services/llm.py",
    '''            assert self._stream is not None\n            async for chunk in self._stream:\n                delta = chunk.choices[0].delta if chunk.choices else None''',
    '''            assert self._stream is not None\n            async for chunk in self._stream:\n                chunk_usage = getattr(chunk, "usage", None)\n                if chunk_usage is not None:\n                    self._usage = chunk_usage\n                delta = chunk.choices[0].delta if chunk.choices else None'''
)
replace_once(
    "shuo/services/llm.py",
    '''    async def _prefetch_first_token(self) -> None:\n        try:\n            self._stream = await self._client.chat.completions.create(\n                **self._request_kwargs\n            )\n            async for chunk in self._stream:\n                delta = chunk.choices[0].delta if chunk.choices else None''',
    '''    async def _prefetch_first_token(self) -> None:\n        try:\n            self._request_started_at = time.perf_counter()\n            self._stream = await self._client.chat.completions.create(\n                **self._request_kwargs\n            )\n            self._stream_opened_at = time.perf_counter()\n            async for chunk in self._stream:\n                chunk_usage = getattr(chunk, "usage", None)\n                if chunk_usage is not None:\n                    self._usage = chunk_usage\n                delta = chunk.choices[0].delta if chunk.choices else None'''
)
replace_once(
    "shuo/services/llm.py",
    '''        on_done: Callable[[], Awaitable[None]],\n        system_prompt: Optional[str] = None,\n    ):''',
    '''        on_done: Callable[[], Awaitable[None]],\n        system_prompt: Optional[str] = None,\n        *,\n        history_max_chars: Optional[int] = None,\n        capture_provider_timing: bool = False,\n    ):'''
)
replace_once(
    "shuo/services/llm.py",
    '''        self._request_seq = 0\n        \n        self._history: List[Dict[str, str]] = []''',
    '''        self._request_seq = 0\n        if history_max_chars is not None and (\n            isinstance(history_max_chars, bool)\n            or not isinstance(history_max_chars, int)\n            or history_max_chars <= 0\n        ):\n            raise ValueError("history_max_chars must be a positive integer")\n        self._history_max_chars = history_max_chars\n        self._capture_provider_timing = bool(capture_provider_timing)\n        \n        self._history: List[Dict[str, str]] = []'''
)
replace_once(
    "shuo/services/llm.py",
    '''            messages = [\n                {"role": "system", "content": self._system_prompt}\n            ] + self._history\n            \n            model = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")''',
    '''            prompt_history, dropped_messages, dropped_chars = _bounded_prompt_history(\n                self._history, self._history_max_chars\n            )\n            messages = [\n                {"role": "system", "content": self._system_prompt}\n            ] + prompt_history\n            \n            model = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")'''
)
replace_once(
    "shuo/services/llm.py",
    '''                f"message_count={len(messages)} "\n                f"history_messages={history_messages} "\n                f"system_prompt_chars={system_prompt_chars} "\n                f"total_prompt_chars={total_prompt_chars}"\n            )''',
    '''                f"message_count={len(messages)} "\n                f"history_messages={history_messages} "\n                f"prompt_history_messages={len(prompt_history)} "\n                f"history_dropped_messages={dropped_messages} "\n                f"history_dropped_chars={dropped_chars} "\n                f"system_prompt_chars={system_prompt_chars} "\n                f"total_prompt_chars={total_prompt_chars}"\n            )'''
)
replace_once(
    "shuo/services/llm.py",
    '''            stream = await self._client.chat.completions.create(\n                model=model,\n                messages=messages,\n                stream=True,\n                max_tokens=500,\n                temperature=0.7,\n                extra_body=extra_body,\n            )''',
    '''            request_kwargs = {\n                "model": model,\n                "messages": messages,\n                "stream": True,\n                "max_tokens": 500,\n                "temperature": 0.7,\n                "extra_body": extra_body,\n            }\n            if self._capture_provider_timing:\n                request_kwargs["stream_options"] = {"include_usage": True}\n\n            stream = await self._client.chat.completions.create(**request_kwargs)'''
)
replace_once(
    "shuo/services/llm.py",
    '''            first_content_logged = False\n\n            async for chunk in stream:\n                if not self._running:\n                    break''',
    '''            first_content_logged = False\n            provider_usage = None\n\n            async for chunk in stream:\n                chunk_usage = getattr(chunk, "usage", None)\n                if chunk_usage is not None:\n                    provider_usage = chunk_usage\n                if not self._running:\n                    break'''
)
replace_once(
    "shuo/services/llm.py",
    '''                    assistant_response += token\n                    await self._on_token(token)\n            \n            if self._running and assistant_response:''',
    '''                    assistant_response += token\n                    await self._on_token(token)\n\n            if self._capture_provider_timing:\n                _log_provider_usage(f"normal_seq_{request_seq}", provider_usage)\n            \n            if self._running and assistant_response:'''
)
replace_once(
    "shuo/services/llm.py",
    '''        try:\n            await prepared.consume(on_prepared_token)\n            if self._running and assistant_response:''',
    '''        try:\n            await prepared.consume(on_prepared_token)\n            if self._capture_provider_timing:\n                _log_provider_usage("prepared_reuse", prepared.usage)\n            if self._running and assistant_response:'''
)
replace_once(
    "shuo/services/llm.py",
    '''        history_provider: Callable[[], List[Dict[str, str]]],\n        client=None,\n    ):''',
    '''        history_provider: Callable[[], List[Dict[str, str]]],\n        client=None,\n        history_max_chars: Optional[int] = None,\n        capture_provider_timing: bool = False,\n    ):'''
)
replace_once(
    "shuo/services/llm.py",
    '''        self._history_provider = history_provider\n        self._client = client or AsyncOpenAI(''',
    '''        self._history_provider = history_provider\n        if history_max_chars is not None and (\n            isinstance(history_max_chars, bool)\n            or not isinstance(history_max_chars, int)\n            or history_max_chars <= 0\n        ):\n            raise ValueError("history_max_chars must be a positive integer")\n        self._history_max_chars = history_max_chars\n        self._capture_provider_timing = bool(capture_provider_timing)\n        self._client = client or AsyncOpenAI('''
)
replace_once(
    "shuo/services/llm.py",
    '''        history_snapshot = [dict(message) for message in self._history_provider()]\n        messages = [\n            {"role": "system", "content": self._system_prompt},\n            *history_snapshot,\n            {"role": "user", "content": user_message},\n        ]''',
    '''        history_snapshot = [dict(message) for message in self._history_provider()]\n        request_history, _dropped_messages, _dropped_chars = _bounded_prompt_history(\n            [*history_snapshot, {"role": "user", "content": user_message}],\n            self._history_max_chars,\n        )\n        messages = [\n            {"role": "system", "content": self._system_prompt},\n            *request_history,\n        ]'''
)
replace_once(
    "shuo/services/llm.py",
    '''        return PreparedLLMResponse(\n            client=self._client,\n            request_kwargs={\n                "model": model,\n                "messages": messages,\n                "stream": True,\n                "max_tokens": 500,\n                "temperature": 0.7,\n                "extra_body": extra_body,\n            },\n            system_prompt=self._system_prompt,\n            history_snapshot=history_snapshot,\n            user_message=user_message,\n        )''',
    '''        request_kwargs = {\n            "model": model,\n            "messages": messages,\n            "stream": True,\n            "max_tokens": 500,\n            "temperature": 0.7,\n            "extra_body": extra_body,\n        }\n        if self._capture_provider_timing:\n            request_kwargs["stream_options"] = {"include_usage": True}\n\n        return PreparedLLMResponse(\n            client=self._client,\n            request_kwargs=request_kwargs,\n            system_prompt=self._system_prompt,\n            history_snapshot=history_snapshot,\n            user_message=user_message,\n            capture_provider_timing=self._capture_provider_timing,\n        )'''
)


# ---------------------------------------------------------------------------
# Agent: Bluetooth-only options are passed by production wiring, while defaults
# preserve every existing carrier/browser call site.
# ---------------------------------------------------------------------------
replace_once(
    "shuo/agent.py",
    '''from .services.tts_pool import TTSPool\nfrom .services.player import AudioPlayer''',
    '''from .services.tts_pool import TTSPool\nfrom .services.phrase_buffer import BoundedPhraseBuffer\nfrom .services.player import AudioPlayer, PREROLL_FRAMES'''
)
replace_once(
    "shuo/agent.py",
    '''        recorder: Optional[CallRecorder] = None,\n        tape: Optional[CallTape] = None,\n    ):''',
    '''        recorder: Optional[CallRecorder] = None,\n        tape: Optional[CallTape] = None,\n        tts_phrase_chars: Optional[int] = None,\n        llm_history_max_chars: Optional[int] = None,\n        llm_provider_timing: bool = False,\n        player_preroll_frames: int = PREROLL_FRAMES,\n    ):'''
)
replace_once(
    "shuo/agent.py",
    '''            on_done=self._on_llm_done,\n            system_prompt=self._settings.system_prompt,\n        )''',
    '''            on_done=self._on_llm_done,\n            system_prompt=self._settings.system_prompt,\n            history_max_chars=llm_history_max_chars,\n            capture_provider_timing=llm_provider_timing,\n        )\n        self._tts_phrase_chars = tts_phrase_chars\n        self._player_preroll_frames = player_preroll_frames'''
)
replace_once(
    "shuo/agent.py",
    '''        self._response: List[str] = []\n\n        # Latency milestones''',
    '''        self._response: List[str] = []\n        self._phrase_buffer: Optional[BoundedPhraseBuffer] = None\n        self._tts_text_chunks = 0\n\n        # Latency milestones'''
)
replace_once(
    "shuo/agent.py",
    '''        self._got_first_audio = False\n        self._response = []\n\n        # Begin tracing this turn''',
    '''        self._got_first_audio = False\n        self._response = []\n        self._phrase_buffer = (\n            BoundedPhraseBuffer(self._tts_phrase_chars)\n            if self._tts_phrase_chars is not None\n            else None\n        )\n        self._tts_text_chunks = 0\n\n        # Begin tracing this turn'''
)
replace_once(
    "shuo/agent.py",
    '''            checkpoint_name=self._checkpoint,\n            tape=self._tape,\n        )''',
    '''            checkpoint_name=self._checkpoint,\n            tape=self._tape,\n            preroll_frames=self._player_preroll_frames,\n        )'''
)
replace_once(
    "shuo/agent.py",
    '''        self._response.append(token)\n        await self._tts.send(token)\n\n    async def _on_llm_done(self) -> None:\n        """LLM finished -> flush TTS."""\n        if not self._active or not self._tts:\n            return\n        self._tracer.end(self._turn, "llm")\n        await self._tts.flush()''',
    '''        self._response.append(token)\n        if self._phrase_buffer is None:\n            self._tts_text_chunks += 1\n            await self._tts.send(token)\n        else:\n            for chunk in self._phrase_buffer.feed(token):\n                self._tts_text_chunks += 1\n                await self._tts.send(chunk)\n\n    async def _on_llm_done(self) -> None:\n        """LLM finished -> flush any bounded phrase fragment, then end TTS."""\n        if not self._active or not self._tts:\n            return\n        self._tracer.end(self._turn, "llm")\n        if self._phrase_buffer is not None:\n            pending = self._phrase_buffer.flush()\n            if pending:\n                self._tts_text_chunks += 1\n                await self._tts.send(pending)\n            log.info(\n                f"TTS text batching turn={self._turn} chunks={self._tts_text_chunks} "\n                f"max_chars={self._tts_phrase_chars}"\n            )\n        await self._tts.flush()'''
)
replace_once(
    "shuo/agent.py",
    '''        self._active = False\n        self._tts = None\n        self._player = None\n        self._checkpoint = None''',
    '''        self._active = False\n        self._tts = None\n        self._player = None\n        self._phrase_buffer = None\n        self._checkpoint = None'''
)
replace_once(
    "shuo/agent.py",
    '''            self._tts = None\n\n        if self._player:''',
    '''            self._tts = None\n        self._phrase_buffer = None\n\n        if self._player:'''
)


# ---------------------------------------------------------------------------
# Player: controlled C5-compliant 2/3-frame A/B knob. Default remains 3.
# ---------------------------------------------------------------------------
replace_once(
    "shuo/services/player.py",
    '''        checkpoint_name: Optional[str] = None,\n        tape: Optional["CallTape"] = None,\n    ):''',
    '''        checkpoint_name: Optional[str] = None,\n        tape: Optional["CallTape"] = None,\n        *,\n        preroll_frames: int = PREROLL_FRAMES,\n    ):'''
)
replace_once(
    "shuo/services/player.py",
    '''        self._checkpoint_name = checkpoint_name\n        # The local recording's right channel''',
    '''        self._checkpoint_name = checkpoint_name\n        if preroll_frames not in (2, 3):\n            raise ValueError("preroll_frames must be 2 or 3 (rules.md C5)")\n        self._preroll_frames = preroll_frames\n        self._preroll_bytes = preroll_frames * FRAME_BYTES\n        self._preroll_seconds = preroll_frames * FRAME_SECONDS\n        # The local recording's right channel'''
)
replace_once(
    "shuo/services/player.py",
    '''    @property\n    def bytes_sent(self) -> int:\n        return self._bytes_sent\n\n    @property\n    def frames_sent''',
    '''    @property\n    def bytes_sent(self) -> int:\n        return self._bytes_sent\n\n    @property\n    def preroll_frames(self) -> int:\n        return self._preroll_frames\n\n    @property\n    def frames_sent'''
)
replace_once(
    "shuo/services/player.py",
    '''            await self._await_preroll()\n\n            deadline = _now()''',
    '''            await self._await_preroll()\n            log.info(\n                f"Playback start preroll_frames={self._preroll_frames} "\n                f"buffered_ms={len(self._buffer) // 8}"\n            )\n\n            deadline = _now()'''
)
replace_once(
    "shuo/services/player.py",
    '''        limit = _now() + PREROLL_SECONDS\n        while (\n            self._running\n            and not self._tts_done\n            and len(self._buffer) < PREROLL_BYTES''',
    '''        limit = _now() + self._preroll_seconds\n        while (\n            self._running\n            and not self._tts_done\n            and len(self._buffer) < self._preroll_bytes'''
)


# ---------------------------------------------------------------------------
# Bluetooth startup: content-free spans + optional safe parallel Flux/TTS warmup.
# ---------------------------------------------------------------------------
replace_once(
    "shuo/bluetooth/conversation.py",
    '''    speculation_factory: Optional[SpeculationFactory] = None,\n    stream_id: str = "bluetooth-local",\n    call_id: str = "bluetooth-local",\n) -> None:''',
    '''    speculation_factory: Optional[SpeculationFactory] = None,\n    stream_id: str = "bluetooth-local",\n    call_id: str = "bluetooth-local",\n    parallel_service_startup: bool = False,\n) -> None:'''
)
replace_once(
    "shuo/bluetooth/conversation.py",
    '''    state = AppState(call_id=call_id)\n\n    try:\n        await session.start()\n        session_started = True\n\n        if speculation_factory is None:''',
    '''    state = AppState(call_id=call_id)\n    startup_started_at = time.perf_counter()\n\n    async def build_agent() -> BluetoothAgent:\n        created_agent = agent_factory(outbound, on_agent_done)\n        return (\n            await created_agent\n            if inspect.isawaitable(created_agent)\n            else created_agent\n        )\n\n    try:\n        stage_started_at = time.perf_counter()\n        await session.start()\n        session_started = True\n        _latency_log.info(\n            "BTStartup: stage=session_ready stage_ms=%.1f total_ms=%.1f",\n            (time.perf_counter() - stage_started_at) * 1000,\n            (time.perf_counter() - startup_started_at) * 1000,\n        )\n\n        if speculation_factory is None:'''
)
replace_once(
    "shuo/bluetooth/conversation.py",
    '''        await flux.start()\n\n        created_agent = agent_factory(outbound, on_agent_done)\n        agent = (\n            await created_agent\n            if inspect.isawaitable(created_agent)\n            else created_agent\n        )\n        if speculation_factory is not None:\n            speculator = speculation_factory(agent)''',
    '''        if parallel_service_startup:\n            stage_started_at = time.perf_counter()\n            flux_task = asyncio.create_task(flux.start())\n            agent_task = asyncio.create_task(build_agent())\n            try:\n                _flux_result, agent = await asyncio.gather(flux_task, agent_task)\n            except BaseException:\n                for task in (flux_task, agent_task):\n                    if not task.done():\n                        task.cancel()\n                await asyncio.gather(flux_task, agent_task, return_exceptions=True)\n                if (\n                    agent_task.done()\n                    and not agent_task.cancelled()\n                    and agent_task.exception() is None\n                ):\n                    agent = agent_task.result()\n                raise\n            _latency_log.info(\n                "BTStartup: stage=services_ready mode=parallel stage_ms=%.1f total_ms=%.1f",\n                (time.perf_counter() - stage_started_at) * 1000,\n                (time.perf_counter() - startup_started_at) * 1000,\n            )\n        else:\n            stage_started_at = time.perf_counter()\n            await flux.start()\n            _latency_log.info(\n                "BTStartup: stage=flux_ready mode=serial stage_ms=%.1f total_ms=%.1f",\n                (time.perf_counter() - stage_started_at) * 1000,\n                (time.perf_counter() - startup_started_at) * 1000,\n            )\n            stage_started_at = time.perf_counter()\n            agent = await build_agent()\n            _latency_log.info(\n                "BTStartup: stage=agent_ready mode=serial stage_ms=%.1f total_ms=%.1f",\n                (time.perf_counter() - stage_started_at) * 1000,\n                (time.perf_counter() - startup_started_at) * 1000,\n            )\n        if speculation_factory is not None:\n            speculator = speculation_factory(agent)'''
)
replace_once(
    "shuo/bluetooth/conversation.py",
    '''        reader_task = asyncio.create_task(read_bluetooth())\n\n        while True:''',
    '''        reader_task = asyncio.create_task(read_bluetooth())\n        _latency_log.info(\n            "BTStartup: stage=reader_started parallel=%s total_ms=%.1f",\n            parallel_service_startup,\n            (time.perf_counter() - startup_started_at) * 1000,\n        )\n\n        while True:'''
)


# ---------------------------------------------------------------------------
# Production wiring: add explicit Phase 4D knobs without passing new kwargs when
# disabled, preserving injected fakes and all existing defaults.
# ---------------------------------------------------------------------------
replace_once(
    "shuo/bluetooth/production.py",
    '''from ..services.llm import ShadowLLMProbe\nfrom ..services.tts_pool import TTSPool''',
    '''from ..services.llm import ShadowLLMProbe\nfrom ..services.player import PREROLL_FRAMES\nfrom ..services.tts_pool import TTSPool'''
)
replace_once(
    "shuo/bluetooth/production.py",
    '''    shadow_early_transcripts: bool = False,\n    prepared_response_reuse: bool = False,\n    deps: BluetoothProductionDeps = BluetoothProductionDeps(),''',
    '''    shadow_early_transcripts: bool = False,\n    prepared_response_reuse: bool = False,\n    tts_phrase_chars: Optional[int] = None,\n    llm_history_max_chars: Optional[int] = None,\n    llm_provider_timing: bool = False,\n    parallel_startup: bool = False,\n    player_preroll_frames: int = PREROLL_FRAMES,\n    deps: BluetoothProductionDeps = BluetoothProductionDeps(),'''
)
replace_once(
    "shuo/bluetooth/production.py",
    '''    if shadow_speculation and eager_eot_threshold is None:\n        raise ValueError("shadow_speculation requires an explicit eager_eot_threshold")\n\n    resolved = settings or deps.settings_loader()''',
    '''    if shadow_speculation and eager_eot_threshold is None:\n        raise ValueError("shadow_speculation requires an explicit eager_eot_threshold")\n    if tts_phrase_chars is not None and (\n        isinstance(tts_phrase_chars, bool)\n        or not isinstance(tts_phrase_chars, int)\n        or not 24 <= tts_phrase_chars <= 160\n    ):\n        raise ValueError("tts_phrase_chars must be an integer between 24 and 160")\n    if llm_history_max_chars is not None and (\n        isinstance(llm_history_max_chars, bool)\n        or not isinstance(llm_history_max_chars, int)\n        or llm_history_max_chars <= 0\n    ):\n        raise ValueError("llm_history_max_chars must be a positive integer")\n    if player_preroll_frames not in (2, 3):\n        raise ValueError("player_preroll_frames must be 2 or 3")\n\n    resolved = settings or deps.settings_loader()'''
)
replace_once(
    "shuo/bluetooth/production.py",
    '''        return deps.agent_cls(\n            session=outbound,\n            on_done=on_done,\n            tts_pool=pool,\n            tracer=tracer,\n            persona_id=persona_id,\n            settings=resolved,\n        )''',
    '''        agent_kwargs = {\n            "session": outbound,\n            "on_done": on_done,\n            "tts_pool": pool,\n            "tracer": tracer,\n            "persona_id": persona_id,\n            "settings": resolved,\n        }\n        if tts_phrase_chars is not None:\n            agent_kwargs["tts_phrase_chars"] = tts_phrase_chars\n        if llm_history_max_chars is not None:\n            agent_kwargs["llm_history_max_chars"] = llm_history_max_chars\n        if llm_provider_timing:\n            agent_kwargs["llm_provider_timing"] = True\n        if player_preroll_frames != PREROLL_FRAMES:\n            agent_kwargs["player_preroll_frames"] = player_preroll_frames\n        return deps.agent_cls(**agent_kwargs)'''
)
replace_once(
    "shuo/bluetooth/production.py",
    '''        probe = deps.shadow_probe_cls(\n            system_prompt=resolved.system_prompt,\n            history_provider=lambda: agent.history,\n        )''',
    '''        probe_kwargs = {\n            "system_prompt": resolved.system_prompt,\n            "history_provider": lambda: agent.history,\n        }\n        if llm_history_max_chars is not None:\n            probe_kwargs["history_max_chars"] = llm_history_max_chars\n        if llm_provider_timing:\n            probe_kwargs["capture_provider_timing"] = True\n        probe = deps.shadow_probe_cls(**probe_kwargs)'''
)
replace_once(
    "shuo/bluetooth/production.py",
    '''        if shadow_speculation:\n            runner_kwargs["speculation_factory"] = speculation_factory\n        await deps.conversation_runner(session, **runner_kwargs)''',
    '''        if shadow_speculation:\n            runner_kwargs["speculation_factory"] = speculation_factory\n        if parallel_startup:\n            runner_kwargs["parallel_service_startup"] = True\n        await deps.conversation_runner(session, **runner_kwargs)'''
)


# ---------------------------------------------------------------------------
# Explicit manual runner controls. No default main.py/carrier behavior changes.
# ---------------------------------------------------------------------------
replace_once(
    "scripts/run_bluetooth_ai.py",
    '''def parse_args() -> argparse.Namespace:\n    parser = argparse.ArgumentParser(''',
    '''def _phase4d_phrase_chars(value: str) -> int:\n    try:\n        chars = int(value)\n    except ValueError as exc:\n        raise argparse.ArgumentTypeError("must be an integer") from exc\n    if not 24 <= chars <= 160:\n        raise argparse.ArgumentTypeError("must be between 24 and 160")\n    return chars\n\n\ndef _positive_chars(value: str) -> int:\n    try:\n        chars = int(value)\n    except ValueError as exc:\n        raise argparse.ArgumentTypeError("must be an integer") from exc\n    if chars <= 0:\n        raise argparse.ArgumentTypeError("must be positive")\n    return chars\n\n\ndef parse_args() -> argparse.Namespace:\n    parser = argparse.ArgumentParser('''
)
replace_once(
    "scripts/run_bluetooth_ai.py",
    '''    parser.add_argument(\n        "--prepared-response-reuse",\n        action="store_true",\n        help=(\n            "Phase-4C opt-in: reuse a matching first-token-ready speculative "\n            "LLM stream after final EndOfTurn; normal generation is fallback."\n        ),\n    )\n    args = parser.parse_args()''',
    '''    parser.add_argument(\n        "--prepared-response-reuse",\n        action="store_true",\n        help=(\n            "Phase-4C opt-in: reuse a matching first-token-ready speculative "\n            "LLM stream after final EndOfTurn; normal generation is fallback."\n        ),\n    )\n    parser.add_argument(\n        "--tts-phrase-chars",\n        type=_phase4d_phrase_chars,\n        default=None,\n        help=(\n            "Phase-4D opt-in: batch LLM text into punctuation/size-bounded TTS "\n            "phrases; explicit 24-160 character cap required."\n        ),\n    )\n    parser.add_argument(\n        "--llm-history-max-chars",\n        type=_positive_chars,\n        default=None,\n        help=(\n            "Phase-4D opt-in: bound provider-visible conversation history by "\n            "characters while retaining canonical in-memory history and the full system prompt."\n        ),\n    )\n    parser.add_argument(\n        "--llm-provider-timing",\n        action="store_true",\n        help="Phase-4D opt-in: request/log content-free streaming usage timing when supported.",\n    )\n    parser.add_argument(\n        "--parallel-startup",\n        action="store_true",\n        help="Phase-4D opt-in: warm Flux and TTS/Agent concurrently with fail-clean teardown.",\n    )\n    parser.add_argument(\n        "--player-preroll-frames",\n        type=int,\n        choices=(2, 3),\n        default=3,\n        help="Phase-4D controlled A/B knob; rules.md C5 permits only 2 or 3 frames.",\n    )\n    args = parser.parse_args()'''
)
replace_once(
    "scripts/run_bluetooth_ai.py",
    '''        shadow_early_transcripts=args.shadow_early_transcripts,\n        prepared_response_reuse=args.prepared_response_reuse,\n    )''',
    '''        shadow_early_transcripts=args.shadow_early_transcripts,\n        prepared_response_reuse=args.prepared_response_reuse,\n        tts_phrase_chars=args.tts_phrase_chars,\n        llm_history_max_chars=args.llm_history_max_chars,\n        llm_provider_timing=args.llm_provider_timing,\n        parallel_startup=args.parallel_startup,\n        player_preroll_frames=args.player_preroll_frames,\n    )'''
)


# ---------------------------------------------------------------------------
# Phase 4D offline coverage.
# ---------------------------------------------------------------------------
test_path = Path("tests/test_bluetooth_phase4d.py")
if test_path.exists():
    raise SystemExit(f"unexpected existing file: {test_path}")
test_path.write_text('''import asyncio\nfrom types import SimpleNamespace\n\nimport pytest\n\nfrom shuo.bluetooth.conversation import run_bluetooth_conversation\nfrom shuo.bluetooth.production import BluetoothProductionDeps, run_production_bluetooth_conversation\nfrom shuo.runtime_config import CallSettings\nfrom shuo.services.llm import LLMService, ShadowLLMProbe\nfrom shuo.services.phrase_buffer import BoundedPhraseBuffer\nfrom shuo.services.player import AudioPlayer\n\n\ndef _chunk(text=None, usage=None):\n    choices = [] if text is None else [SimpleNamespace(delta=SimpleNamespace(content=text))]\n    return SimpleNamespace(choices=choices, usage=usage)\n\n\nclass FakeStream:\n    def __init__(self, chunks):\n        self._chunks = list(chunks)\n        self.closed = False\n\n    def __aiter__(self):\n        return self\n\n    async def __anext__(self):\n        if not self._chunks:\n            raise StopAsyncIteration\n        return self._chunks.pop(0)\n\n    async def close(self):\n        self.closed = True\n\n\nclass CaptureClient:\n    def __init__(self, stream):\n        self.stream = stream\n        self.kwargs = None\n        self.chat = SimpleNamespace(\n            completions=SimpleNamespace(create=self.create)\n        )\n\n    async def create(self, **kwargs):\n        self.kwargs = kwargs\n        return self.stream\n\n\ndef test_phrase_buffer_preserves_exact_text_and_is_bounded():\n    buffer = BoundedPhraseBuffer(32)\n    tokens = [\n        "Hello", ", this is a bounded phrase. ",\n        "The next sentence is intentionally longer than the cap", " and ends now!"\n    ]\n    emitted = []\n    for token in tokens:\n        emitted.extend(buffer.feed(token))\n    tail = buffer.flush()\n    if tail:\n        emitted.append(tail)\n\n    assert "".join(emitted) == "".join(tokens)\n    assert all(len(chunk) <= 32 for chunk in emitted)\n    assert len(emitted) < len("".join(tokens))\n\n\ndef test_phrase_buffer_rejects_invalid_capacity():\n    with pytest.raises(ValueError):\n        BoundedPhraseBuffer(0)\n\n\n@pytest.mark.asyncio\nasync def test_llm_context_budget_keeps_system_and_latest_user_but_not_canonical_history(monkeypatch):\n    monkeypatch.setenv("GROQ_API_KEY", "unused")\n    done = asyncio.Event()\n\n    async def on_token(_token):\n        pass\n\n    async def on_done():\n        done.set()\n\n    usage = SimpleNamespace(\n        prompt_tokens=9, completion_tokens=1, total_tokens=10,\n        queue_time=0.010, prompt_time=0.020, completion_time=0.030, total_time=0.050,\n        prompt_tokens_details=SimpleNamespace(cached_tokens=2),\n    )\n    client = CaptureClient(FakeStream([_chunk("ok"), _chunk(usage=usage)]))\n    service = LLMService(\n        on_token=on_token, on_done=on_done, system_prompt="DIGITAL TWIN FACTS",\n        history_max_chars=24, capture_provider_timing=True,\n    )\n    service._client = client\n    service._history = [\n        {"role": "user", "content": "old question that is deliberately long"},\n        {"role": "assistant", "content": "old answer that is deliberately long"},\n    ]\n\n    await service.start("latest question")\n    await asyncio.wait_for(done.wait(), 1)\n\n    sent = client.kwargs["messages"]\n    assert sent[0] == {"role": "system", "content": "DIGITAL TWIN FACTS"}\n    assert sent[-1] == {"role": "user", "content": "latest question"}\n    assert all("old question" not in message["content"] for message in sent)\n    assert client.kwargs["stream_options"] == {"include_usage": True}\n    # Canonical history is retained locally; only the provider-visible prompt is bounded.\n    assert service.history[0]["content"].startswith("old question")\n    assert service.history[-1] == {"role": "assistant", "content": "ok"}\n\n\n@pytest.mark.asyncio\nasync def test_shadow_probe_uses_same_budget_and_usage_opt_in(monkeypatch):\n    monkeypatch.setenv("GROQ_API_KEY", "unused")\n    stream = FakeStream([_chunk("first")])\n    client = CaptureClient(stream)\n    history = [\n        {"role": "user", "content": "older user message that is too large"},\n        {"role": "assistant", "content": "older assistant message that is too large"},\n    ]\n    probe = ShadowLLMProbe(\n        system_prompt="FACTS", history_provider=lambda: history, client=client,\n        history_max_chars=16, capture_provider_timing=True,\n    )\n    prepared = probe.create_prepared("latest")\n    prepared.start()\n    await prepared.wait_first_token()\n    try:\n        assert client.kwargs["messages"] == [\n            {"role": "system", "content": "FACTS"},\n            {"role": "user", "content": "latest"},\n        ]\n        assert client.kwargs["stream_options"] == {"include_usage": True}\n    finally:\n        await prepared.cancel()\n    assert stream.closed is True\n\n\nclass BlockingSession:\n    def __init__(self):\n        self.started = 0\n        self.stopped = 0\n        self._wait = asyncio.Event()\n\n    async def start(self):\n        self.started += 1\n\n    async def stop(self):\n        self.stopped += 1\n\n    async def read(self):\n        await self._wait.wait()\n        raise AssertionError("unreachable")\n\n    async def write(self, _audio):\n        pass\n\n    async def clear(self):\n        pass\n\n\nclass ParallelFlux:\n    def __init__(self, *callbacks, flux_entered, agent_entered):\n        self.flux_entered = flux_entered\n        self.agent_entered = agent_entered\n        self.stopped = 0\n\n    async def start(self):\n        self.flux_entered.set()\n        await self.agent_entered.wait()\n\n    async def stop(self):\n        self.stopped += 1\n\n    async def send(self, _audio):\n        pass\n\n\nclass MinimalAgent:\n    def __init__(self):\n        self.cleaned = 0\n\n    @property\n    def history(self):\n        return []\n\n    async def start_turn(self, _transcript, prepared_response=None):\n        pass\n\n    async def cancel_turn(self):\n        pass\n\n    async def cleanup(self):\n        self.cleaned += 1\n\n\n@pytest.mark.asyncio\nasync def test_parallel_startup_allows_independent_flux_and_agent_warmups():\n    session = BlockingSession()\n    flux_entered = asyncio.Event()\n    agent_entered = asyncio.Event()\n    ready = asyncio.Event()\n    holder = {}\n\n    def flux_factory(*callbacks):\n        holder["flux"] = ParallelFlux(\n            *callbacks, flux_entered=flux_entered, agent_entered=agent_entered\n        )\n        return holder["flux"]\n\n    async def agent_factory(_outbound, _done):\n        agent_entered.set()\n        await flux_entered.wait()\n        holder["agent"] = MinimalAgent()\n        ready.set()\n        return holder["agent"]\n\n    task = asyncio.create_task(run_bluetooth_conversation(\n        session, flux_factory=flux_factory, agent_factory=agent_factory,\n        parallel_service_startup=True,\n    ))\n    await asyncio.wait_for(ready.wait(), 1)\n    await asyncio.sleep(0)\n    task.cancel()\n    with pytest.raises(asyncio.CancelledError):\n        await task\n\n    assert session.started == 1\n    assert session.stopped == 1\n    assert holder["flux"].stopped == 1\n    assert holder["agent"].cleaned == 1\n\n\n@pytest.mark.asyncio\nasync def test_parallel_startup_cancels_sibling_on_failure():\n    session = BlockingSession()\n    agent_cancelled = asyncio.Event()\n\n    class FailingFlux:\n        async def start(self):\n            raise RuntimeError("flux boom")\n        async def stop(self):\n            pass\n        async def send(self, _audio):\n            pass\n\n    def flux_factory(*_callbacks):\n        return FailingFlux()\n\n    async def agent_factory(_outbound, _done):\n        try:\n            await asyncio.Event().wait()\n        except asyncio.CancelledError:\n            agent_cancelled.set()\n            raise\n\n    with pytest.raises(RuntimeError, match="flux boom"):\n        await run_bluetooth_conversation(\n            session, flux_factory=flux_factory, agent_factory=agent_factory,\n            parallel_service_startup=True,\n        )\n\n    assert agent_cancelled.is_set()\n    assert session.stopped == 1\n\n\nclass PlayerSession:\n    async def play_audio(self, _audio):\n        pass\n    async def checkpoint(self, _name):\n        pass\n    async def clear_audio(self):\n        pass\n\n\ndef test_player_preroll_is_limited_to_rules_c5_values():\n    player = AudioPlayer(PlayerSession(), preroll_frames=2)\n    assert player.preroll_frames == 2\n    with pytest.raises(ValueError):\n        AudioPlayer(PlayerSession(), preroll_frames=1)\n    with pytest.raises(ValueError):\n        AudioPlayer(PlayerSession(), preroll_frames=4)\n\n\nclass CapturePool:\n    def __init__(self, pool_size, ttl, voice_id):\n        self.started = False\n    async def start(self):\n        self.started = True\n    async def wait_ready(self):\n        assert self.started\n    async def stop(self):\n        pass\n\n\nclass CaptureAgent:\n    captured = None\n    def __init__(self, **kwargs):\n        CaptureAgent.captured = kwargs\n        self.history = []\n\n\nclass CaptureProbe:\n    captured = None\n    def __init__(self, **kwargs):\n        CaptureProbe.captured = kwargs\n\n\nclass CaptureTracer:\n    def save(self, _call_id):\n        pass\n\n\ndef _settings():\n    return CallSettings(\n        system_prompt="facts", voice_id="voice", prompt_source="test",\n        voice_source="test", rules_chars=0, knowledge_chars=0,\n    )\n\n\n@pytest.mark.asyncio\nasync def test_phase4d_options_are_explicitly_wired_only_on_bluetooth_path():\n    captured = {}\n\n    async def runner(session, *, agent_factory, speculation_factory, **kwargs):\n        captured.update(kwargs)\n        agent = await agent_factory(object(), lambda _checkpoint: None)\n        speculation_factory(agent)\n\n    await run_production_bluetooth_conversation(\n        object(), settings=_settings(), eager_eot_threshold=0.3,\n        shadow_speculation=True, tts_phrase_chars=48, llm_history_max_chars=4096,\n        llm_provider_timing=True, parallel_startup=True, player_preroll_frames=2,\n        deps=BluetoothProductionDeps(\n            tts_pool_cls=CapturePool, agent_cls=CaptureAgent,\n            shadow_probe_cls=CaptureProbe, tracer_factory=CaptureTracer,\n            conversation_runner=runner,\n        ),\n    )\n\n    assert captured["parallel_service_startup"] is True\n    assert CaptureAgent.captured["tts_phrase_chars"] == 48\n    assert CaptureAgent.captured["llm_history_max_chars"] == 4096\n    assert CaptureAgent.captured["llm_provider_timing"] is True\n    assert CaptureAgent.captured["player_preroll_frames"] == 2\n    assert CaptureProbe.captured["history_max_chars"] == 4096\n    assert CaptureProbe.captured["capture_provider_timing"] is True\n\n\n@pytest.mark.asyncio\n@pytest.mark.parametrize(\n    "kwargs, message",\n    [\n        ({"tts_phrase_chars": 23}, "tts_phrase_chars"),\n        ({"tts_phrase_chars": 161}, "tts_phrase_chars"),\n        ({"llm_history_max_chars": 0}, "llm_history_max_chars"),\n        ({"player_preroll_frames": 1}, "player_preroll_frames"),\n    ],\n)\nasync def test_phase4d_invalid_options_fail_before_resources(kwargs, message):\n    with pytest.raises(ValueError, match=message):\n        await run_production_bluetooth_conversation(object(), **kwargs)\n''', encoding="utf-8")

print("Phase 4D deterministic source application complete")
