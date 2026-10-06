from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import importlib.util
import os
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..log import ServiceLogger
from .phrase_buffer import BoundedPhraseBuffer

log = ServiceLogger("TTS-Pocket")

# Pocket's catalog aliases use the ungated non-cloning weights. Passing an
# hf:// WAV here invokes the separate voice-cloning path, which requires gated
# model access and is intentionally not part of SHUO's local test provider.
_DEFAULT_VOICE = "alba"
_AUDIO_QUEUE_CHUNKS = 4
_WORKER_STOP_TIMEOUT = 1.0
_NETWORK_TRUE_VALUES = {"1", "true", "yes", "on"}


def _configured_model_path() -> Optional[Path]:
    raw = (os.getenv("POCKET_TTS_CONFIG") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _quantization_enabled() -> bool:
    return (
        (os.getenv("POCKET_TTS_QUANTIZE") or "").strip().lower()
        in _NETWORK_TRUE_VALUES
    )


def pocket_tts_available() -> bool:
    """Return whether the optional Pocket TTS package is importable."""
    return importlib.util.find_spec("pocket_tts") is not None


def _configure_hf_cache_resolution() -> bool:
    """Prefer Pocket's already-populated Hugging Face cache at runtime.

    Reference-host Phase 5 evidence showed Pocket 3.1.0 could stall for more
    than 60 seconds while validating already-cached hf:// assets over the
    network, then complete immediately after the failed HEAD request. Running
    the same cached model with HF_HUB_OFFLINE=1 loaded in ~2.3 seconds.

    Pocket is therefore cache-only by default once selected. Operators who
    intentionally need the vendor to populate/refresh the cache can opt back
    into network resolution with POCKET_TTS_ALLOW_NETWORK=1.

    The setting is process-scoped because Hugging Face reads this environment
    switch during the lazy Pocket import/model load. It is applied only when
    the Pocket provider actually starts, so the default ElevenLabs path remains
    unchanged.
    """
    allow_network = (
        (os.getenv("POCKET_TTS_ALLOW_NETWORK") or "").strip().lower()
        in _NETWORK_TRUE_VALUES
    )
    if allow_network:
        return False
    os.environ["HF_HUB_OFFLINE"] = "1"
    return True


def float_audio_to_mulaw_8k(
    samples,
    sample_rate: int,
    rate_state=None,
):
    """Convert a Pocket float audio chunk to SHUO's mono G.711 mu-law/8 kHz.

    The helper intentionally keeps Pocket's native PCM inside this provider
    boundary. ``rate_state`` is returned so native streaming chunks can share
    resampler state within one generated phrase.
    """
    if sample_rate <= 0:
        raise RuntimeError(f"invalid Pocket TTS sample rate: {sample_rate}")

    try:
        import audioop
    except ImportError as exc:
        raise RuntimeError(
            "Pocket TTS PCM conversion requires audioop; on Python 3.13+ "
            "install the repository's documented audioop-lts compatibility shim"
        ) from exc

    import numpy as np

    if hasattr(samples, "detach"):
        samples = samples.detach().cpu().numpy()
    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if array.size == 0:
        return b"", rate_state

    array = np.clip(array, -1.0, 1.0)
    pcm16 = (array * 32767.0).astype("<i2", copy=False).tobytes()
    if sample_rate != 8_000:
        pcm16, rate_state = audioop.ratecv(
            pcm16,
            2,
            1,
            sample_rate,
            8_000,
            rate_state,
        )
    return audioop.lin2ulaw(pcm16, 2), rate_state


class _PocketRuntime:
    """Process-local model/voice cache with serialized native inference."""

    def __init__(self) -> None:
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._model = None
        self._voices = {}

    def ensure_loaded(self, voice_source: str) -> None:
        with self._load_lock:
            if self._model is None:
                if not pocket_tts_available():
                    raise RuntimeError(
                        "Pocket TTS requested but the optional 'pocket-tts' package "
                        "is not installed"
                    )
                # Vendor SDK stays inside this provider module. Import lazily so
                # default ElevenLabs startup does not acquire the heavyweight
                # PyTorch/Pocket import or model-download path.
                from pocket_tts import TTSModel

                model_config = _configured_model_path()
                if model_config is not None:
                    if not model_config.is_file():
                        raise RuntimeError(
                            f"Pocket TTS config does not exist: {model_config}"
                        )
                    self._model = TTSModel.load_model(
                        config=str(model_config),
                        quantize=_quantization_enabled(),
                    )
                else:
                    self._model = TTSModel.load_model()

            if voice_source not in self._voices:
                self._voices[voice_source] = self._model.get_state_for_audio_prompt(
                    voice_source
                )

    @property
    def sample_rate(self) -> int:
        if self._model is None:
            raise RuntimeError("Pocket TTS model is not loaded")
        return int(self._model.sample_rate)

    def stream(self, voice_source: str, text: str, cancel_event: threading.Event):
        self.ensure_loaded(voice_source)
        with self._inference_lock:
            if cancel_event.is_set():
                return
            voice_state = self._voices[voice_source]
            for chunk in self._model.generate_audio_stream(
                voice_state,
                text,
                copy_state=True,
            ):
                if cancel_event.is_set():
                    break
                yield chunk


_RUNTIME = _PocketRuntime()


class PocketTTSService:
    """Local Pocket TTS provider exposing SHUO's existing mu-law/8 kHz interface.

    LLM text is released through the same bounded phrase seam used by the prior
    local provider. Pocket's synchronous CPU generator runs off the asyncio event
    loop and its native streaming chunks cross back through a bounded queue.
    Native PCM never leaves this module and no raw audio file is written.
    """

    # Pocket sounds unnatural when every ~24 chars are synthesised as a
    # separate utterance. Keep steady-state conversational sentences together,
    # but bound only the first phrase more tightly so token streaming can reach
    # native synthesis without waiting for a 128-character prefix.
    FIRST_PHRASE_CHARS = 48
    FIRST_PHRASE_MIN_SOFT_CHARS = 32
    PHRASE_CHARS = 128
    PHRASE_MIN_SOFT_CHARS = 80

    def __init__(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        voice_id: Optional[str] = None,
        *,
        voice_source: Optional[str] = None,
        runtime=None,
    ) -> None:
        self._on_audio = on_audio
        self._on_done = on_done
        self._voice_source = (
            voice_source
            or (os.getenv("POCKET_TTS_VOICE") or "").strip()
            or _DEFAULT_VOICE
        )
        self._runtime = runtime or _RUNTIME

        self._running = False
        self._fatal_error: Optional[str] = None
        self._warm_idle_started_at: Optional[float] = None
        self._buffer = BoundedPhraseBuffer(
            self.PHRASE_CHARS,
            min_soft_chars=self.PHRASE_MIN_SOFT_CHARS,
            first_max_chars=self.FIRST_PHRASE_CHARS,
            first_min_soft_chars=self.FIRST_PHRASE_MIN_SOFT_CHARS,
        )
        self._done_emitted = False
        self._lock = asyncio.Lock()
        self._cancel_event: Optional[threading.Event] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._active_queue: Optional[asyncio.Queue] = None

    @property
    def is_active(self) -> bool:
        return self._running

    @property
    def fatal_error(self) -> Optional[str]:
        return self._fatal_error

    @property
    def warm_idle_started_at(self) -> Optional[float]:
        return self._warm_idle_started_at

    def bind(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
    ) -> None:
        self._on_audio = on_audio
        self._on_done = on_done
        self._buffer = BoundedPhraseBuffer(
            self.PHRASE_CHARS,
            min_soft_chars=self.PHRASE_MIN_SOFT_CHARS,
            first_max_chars=self.FIRST_PHRASE_CHARS,
            first_min_soft_chars=self.FIRST_PHRASE_MIN_SOFT_CHARS,
        )
        self._done_emitted = False
        self._fatal_error = None
        self._cancel_event = None
        self._worker_task = None
        self._active_queue = None

    async def start(self) -> None:
        if self._running:
            return
        if not pocket_tts_available() and self._runtime is _RUNTIME:
            raise RuntimeError(
                "Pocket TTS requested but the optional 'pocket-tts' package is not "
                "installed. Install requirements-pocket-tts.txt first."
            )

        model_config = _configured_model_path()
        if model_config is not None and self._runtime is _RUNTIME:
            if not model_config.is_file():
                raise RuntimeError(
                    f"Pocket TTS config does not exist: {model_config}"
                )

            voice_path = Path(self._voice_source).expanduser()
            if not voice_path.is_file():
                raise RuntimeError(
                    "Custom Pocket TTS model requires a local Malayalam voice "
                    "prompt. Set POCKET_TTS_VOICE to an existing WAV file. "
                    f"Resolved value: {voice_path}"
                )

            self._voice_source = str(voice_path)

        cache_only = _configure_hf_cache_resolution()
        try:
            await asyncio.to_thread(self._runtime.ensure_loaded, self._voice_source)
        except Exception as exc:
            if cache_only:
                raise RuntimeError(
                    "Pocket TTS failed to load from the local Hugging Face cache. "
                    "If this is the first model load, run once with "
                    "POCKET_TTS_ALLOW_NETWORK=1 to populate the cache: "
                    f"{exc}"
                ) from exc
            raise RuntimeError(f"Pocket TTS failed to load: {exc}") from exc

        self._warm_idle_started_at = time.monotonic()
        self._running = True
        self._fatal_error = None
        log.info(f"Local provider ready voice={self._voice_source!r}")

    async def send(self, text: str) -> None:
        if not text or not self._running:
            return

        async with self._lock:
            if not self._running:
                return
            for phrase in self._buffer.feed(text):
                if not self._running:
                    return
                if not await self._synthesize_phrase(phrase):
                    return

    async def flush(self) -> None:
        if not self._running:
            return

        async with self._lock:
            if not self._running:
                return
            pending = self._buffer.flush()
            if pending and not await self._synthesize_phrase(pending):
                return
            self._running = False

        await self._emit_done_once()

    async def stop(self) -> None:
        if not self._running:
            return
        await self.flush()

    async def cancel(self) -> None:
        self._running = False
        self._buffer = BoundedPhraseBuffer(
            self.PHRASE_CHARS,
            min_soft_chars=self.PHRASE_MIN_SOFT_CHARS,
            first_max_chars=self.FIRST_PHRASE_CHARS,
            first_min_soft_chars=self.FIRST_PHRASE_MIN_SOFT_CHARS,
        )
        cancel_event = self._cancel_event
        if cancel_event is not None:
            cancel_event.set()
        self._wake_cancelled_consumer()
        await self._wait_for_worker()

    def _wake_cancelled_consumer(self) -> None:
        queue = self._active_queue
        if queue is None:
            return
        # Cancellation makes queued audio obsolete. Make room for a local
        # sentinel without waiting on the producer thread so an uncancelled
        # send()/flush() waiter cannot remain blocked on queue.get().
        while queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(("cancelled", None))
        except asyncio.QueueFull:
            pass

    async def _synthesize_phrase(self, text: str) -> bool:
        if not text.strip():
            return True

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_AUDIO_QUEUE_CHUNKS)
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        self._active_queue = queue
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._produce_phrase,
                text,
                loop,
                queue,
                cancel_event,
            )
        )
        self._worker_task = worker

        try:
            while True:
                kind, payload = await queue.get()
                if kind == "audio":
                    if self._running and not cancel_event.is_set():
                        await self._on_audio(
                            base64.b64encode(payload).decode("ascii")
                        )
                elif kind == "error":
                    await self._fail(f"Pocket TTS synthesis failed: {payload}")
                    return False
                elif kind in ("done", "cancelled"):
                    break

            await self._wait_for_specific_worker(worker)
            return self._running
        except asyncio.CancelledError:
            cancel_event.set()
            await self._wait_for_specific_worker(worker)
            raise
        finally:
            if self._cancel_event is cancel_event:
                self._cancel_event = None
            if self._worker_task is worker:
                self._worker_task = None
            if self._active_queue is queue:
                self._active_queue = None

    def _produce_phrase(
        self,
        text: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        cancel_event: threading.Event,
    ) -> None:
        rate_state = None
        try:
            sample_rate = int(self._runtime.sample_rate)
            for chunk in self._runtime.stream(
                self._voice_source,
                text,
                cancel_event,
            ):
                if cancel_event.is_set():
                    return
                mulaw, rate_state = float_audio_to_mulaw_8k(
                    chunk,
                    sample_rate,
                    rate_state,
                )
                if mulaw and not self._put_from_thread(
                    loop,
                    queue,
                    ("audio", mulaw),
                    cancel_event,
                ):
                    return
        except Exception as exc:
            if not cancel_event.is_set():
                self._put_from_thread(
                    loop,
                    queue,
                    ("error", str(exc)),
                    cancel_event,
                )
                self._put_from_thread(
                    loop,
                    queue,
                    ("done", None),
                    cancel_event,
                )
                return
        if not cancel_event.is_set():
            self._put_from_thread(
                loop,
                queue,
                ("done", None),
                cancel_event,
            )

    @staticmethod
    def _put_from_thread(
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        item,
        cancel_event: threading.Event,
    ) -> bool:
        if cancel_event.is_set():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        except RuntimeError:
            return False
        while True:
            try:
                future.result(timeout=0.05)
                return True
            except concurrent.futures.TimeoutError:
                if cancel_event.is_set():
                    future.cancel()
                    return False
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                return False
            except Exception:
                return False

    async def _wait_for_worker(self) -> None:
        worker = self._worker_task
        if worker is None or worker.done():
            return
        await self._wait_for_specific_worker(worker)

    async def _wait_for_specific_worker(self, worker: asyncio.Task) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=_WORKER_STOP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.error(
                "Pocket TTS inference worker did not stop within the bounded "
                f"{_WORKER_STOP_TIMEOUT:.1f}s cancellation window"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"Pocket TTS worker cleanup failed ({exc})")

    async def _fail(self, message: str) -> None:
        if self._fatal_error is None:
            self._fatal_error = message
            log.error(message)
        self._running = False
        cancel_event = self._cancel_event
        if cancel_event is not None:
            cancel_event.set()
        self._wake_cancelled_consumer()
        await self._emit_done_once()

    async def _emit_done_once(self) -> None:
        if self._done_emitted:
            return
        self._done_emitted = True
        await self._on_done()
