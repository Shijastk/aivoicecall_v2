from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import time
from typing import Awaitable, Callable, Optional

import websockets

from ..log import ServiceLogger

log = ServiceLogger("TTS-Sarvam")

SARVAM_CLONE_WS_URL = (
    "wss://api.sarvam.ai/voices/clone/ws?send_completion_event=true"
)
_DEFAULT_LANGUAGE = "ml-IN"
_DEFAULT_PACE = 1.0
_DEFAULT_MIN_BUFFER_SIZE = 30
_DEFAULT_MAX_CHUNK_LENGTH = 200


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


class SarvamVoiceCloneTTSService:
    """Sarvam saved-voice clone WebSocket behind SHUO's mu-law/8 kHz seam.

    The provider receives incremental LLM text, requests native G.711 mu-law at
    8 kHz, and forwards Sarvam's base64 audio frames unchanged to AudioPlayer.
    One service object owns one synthesis turn. The pool pre-connects the next
    service in parallel after checkout, preserving SHUO's existing warm path.

    ``voice_id`` is intentionally ignored. SHUO's current per-call catalogue
    resolves ElevenLabs provider IDs; feeding one of those to Sarvam would fail
    at runtime. Until the catalogue becomes provider-aware, Sarvam's saved clone
    is selected explicitly with ``SARVAM_VOICE_ID``.
    """

    def __init__(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        voice_id: Optional[str] = None,
        *,
        connect=None,
    ) -> None:
        del voice_id
        self._on_audio = on_audio
        self._on_done = on_done
        self._connect = connect or websockets.connect

        self._api_key = (os.getenv("SARVAM_API_KEY") or "").strip()
        self._voice_id = (os.getenv("SARVAM_VOICE_ID") or "").strip()
        self._language = (
            (os.getenv("SARVAM_TTS_LANGUAGE_CODE") or "").strip()
            or _DEFAULT_LANGUAGE
        )
        self._pace = _env_float(
            "SARVAM_TTS_PACE",
            _DEFAULT_PACE,
            minimum=0.5,
            maximum=2.0,
        )
        self._min_buffer_size = _env_int(
            "SARVAM_TTS_MIN_BUFFER_SIZE",
            _DEFAULT_MIN_BUFFER_SIZE,
            minimum=30,
            maximum=200,
        )
        self._max_chunk_length = _env_int(
            "SARVAM_TTS_MAX_CHUNK_LENGTH",
            _DEFAULT_MAX_CHUNK_LENGTH,
            minimum=50,
            maximum=500,
        )

        self._ws = None
        self._receive_task: Optional[asyncio.Task] = None
        self._running = False
        self._fatal_error: Optional[str] = None
        self._warm_idle_started_at: Optional[float] = None
        self._done_emitted = False
        self._send_lock = asyncio.Lock()

    @property
    def is_active(self) -> bool:
        return self._running and self._ws is not None

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
        self._fatal_error = None
        self._done_emitted = False

    def _validate_config(self) -> None:
        if not self._api_key:
            raise RuntimeError(
                "SARVAM_API_KEY is required for TTS_PROVIDER=sarvam"
            )
        if not self._voice_id:
            raise RuntimeError(
                "SARVAM_VOICE_ID is required for TTS_PROVIDER=sarvam"
            )
        if not self._language:
            raise RuntimeError(
                "SARVAM_TTS_LANGUAGE_CODE must not be empty"
            )
        if self._max_chunk_length < self._min_buffer_size:
            raise RuntimeError(
                "SARVAM_TTS_MAX_CHUNK_LENGTH must be >= "
                "SARVAM_TTS_MIN_BUFFER_SIZE"
            )

    async def start(self) -> None:
        if self._running:
            return
        self._validate_config()

        try:
            # Sarvam documents API-key auth through this WebSocket subprotocol.
            # It also avoids the extra_headers/additional_headers split between
            # supported websockets releases.
            self._ws = await self._connect(
                SARVAM_CLONE_WS_URL,
                subprotocols=[
                    f"api-subscription-key.{self._api_key}"
                ],
            )

            config = {
                "type": "config",
                "data": {
                    "target_language_code": self._language,
                    "voice_id": self._voice_id,
                    "output_audio_codec": "mulaw",
                    "speech_sample_rate": 8000,
                    "pace": self._pace,
                    "min_buffer_size": self._min_buffer_size,
                    "max_chunk_length": self._max_chunk_length,
                },
            }
            idle_started_at = time.monotonic()
            await self._ws.send(json.dumps(config))
            self._warm_idle_started_at = idle_started_at
            self._running = True
            self._receive_task = asyncio.create_task(
                self._receive_loop()
            )
            log.connected()
        except BaseException:
            await self._close_socket()
            raise

    async def send(self, text: str) -> None:
        if not text:
            return
        if not self.is_active:
            raise RuntimeError(
                self._fatal_error or "Sarvam TTS is not active"
            )

        message = json.dumps(
            {"type": "text", "data": {"text": text}}
        )
        async with self._send_lock:
            if not self.is_active:
                raise RuntimeError(
                    self._fatal_error or "Sarvam TTS is not active"
                )
            try:
                await self._ws.send(message)
            except Exception as exc:
                self._fatal_error = (
                    f"Sarvam TTS send failed: {exc}"
                )
                raise

    async def flush(self) -> None:
        if not self.is_active:
            return
        async with self._send_lock:
            if not self.is_active:
                return
            try:
                await self._ws.send(
                    json.dumps({"type": "flush"})
                )
            except Exception as exc:
                self._fatal_error = (
                    f"Sarvam TTS flush failed: {exc}"
                )
                raise

    async def stop(self) -> None:
        await self.cancel()

    async def cancel(self) -> None:
        self._running = False
        receive_task, self._receive_task = (
            self._receive_task,
            None,
        )
        if (
            receive_task is not None
            and receive_task is not asyncio.current_task()
        ):
            receive_task.cancel()
            await asyncio.gather(
                receive_task,
                return_exceptions=True,
            )
        await self._close_socket()
        log.cancelled()

    async def _close_socket(self) -> None:
        self._running = False
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _finish_from_receiver(self) -> None:
        self._running = False
        await self._close_socket()
        await self._emit_done_once()

    async def _receive_loop(self) -> None:
        try:
            while self._running and self._ws is not None:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                message = json.loads(raw)
                message_type = message.get("type")
                data = message.get("data") or {}

                if message_type == "audio":
                    encoded = data.get("audio")
                    if not isinstance(encoded, str) or not encoded:
                        raise RuntimeError(
                            "Sarvam TTS returned an empty audio frame"
                        )
                    try:
                        decoded = base64.b64decode(
                            encoded,
                            validate=True,
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            "Sarvam TTS returned invalid base64 audio"
                        ) from exc
                    if not decoded:
                        raise RuntimeError(
                            "Sarvam TTS returned an empty audio frame"
                        )
                    await self._on_audio(encoded)
                    continue

                if (
                    message_type == "event"
                    and data.get("event_type") == "final"
                ):
                    await self._finish_from_receiver()
                    return

                if message_type == "error":
                    detail = (
                        data.get("message")
                        or data.get("code")
                        or "unknown provider error"
                    )
                    self._fatal_error = (
                        f"Sarvam TTS error: {detail}"
                    )
                    await self._finish_from_receiver()
                    return

                # Ignore provider metadata / forward-compatible messages.

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._fatal_error is None:
                self._fatal_error = (
                    f"Sarvam TTS receive failed: {exc}"
                )
            await self._finish_from_receiver()

    async def _emit_done_once(self) -> None:
        if self._done_emitted:
            return
        self._done_emitted = True
        await self._on_done()
