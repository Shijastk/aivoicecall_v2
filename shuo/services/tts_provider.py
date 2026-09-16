from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional, Type

from ..log import ServiceLogger
from .tts import TTSService as ElevenLabsTTSService
from .tts_espeak import EspeakTTSService

log = ServiceLogger("TTSRouter")

_SUPPORTED_PRIMARY = {"elevenlabs", "espeak"}
_SUPPORTED_FALLBACK = {"", "espeak"}
_MAX_REPLAY_CHARS = 4096


def tts_provider_name() -> str:
    return (os.getenv("TTS_PROVIDER") or "elevenlabs").strip().lower()


def tts_fallback_provider_name() -> str:
    return (os.getenv("TTS_FALLBACK_PROVIDER") or "").strip().lower()


def tts_required_env_vars() -> tuple[str, ...]:
    """Secrets required for the selected routing policy.

    An explicitly configured eSpeak fallback is allowed to carry the call when
    the ElevenLabs key is absent, so that configuration has no ElevenLabs-key
    startup requirement.
    """
    primary = tts_provider_name()
    fallback = tts_fallback_provider_name()
    if primary == "elevenlabs" and fallback != "espeak":
        return ("ELEVENLABS_API_KEY",)
    return ()


def espeak_requested() -> bool:
    return (
        tts_provider_name() == "espeak"
        or tts_fallback_provider_name() == "espeak"
    )


def validate_tts_provider_config() -> Optional[str]:
    primary = tts_provider_name()
    fallback = tts_fallback_provider_name()
    if primary not in _SUPPORTED_PRIMARY:
        return (
            f"Unsupported TTS_PROVIDER={primary!r}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_PRIMARY))}"
        )
    if fallback not in _SUPPORTED_FALLBACK:
        return (
            f"Unsupported TTS_FALLBACK_PROVIDER={fallback!r}. "
            "Supported: espeak or empty"
        )
    if primary == "espeak" and fallback:
        return "TTS_FALLBACK_PROVIDER must be empty when TTS_PROVIDER=espeak"
    return None


class FallbackTTSService:
    """ElevenLabs primary with an eSpeak pre-audio emergency fallback.

    The fallback may replay only text that has not produced primary audio yet.
    Once a primary audio chunk has been emitted, replay is disabled for the rest
    of that turn so a mid-answer provider failure never restarts the answer in a
    second voice.
    """

    def __init__(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        *,
        voice_id: Optional[str],
        primary_cls: Type = ElevenLabsTTSService,
        fallback_cls: Type = EspeakTTSService,
    ) -> None:
        self._on_audio = on_audio
        self._on_done = on_done
        self._voice_id = voice_id
        self._primary = primary_cls(
            on_audio=self._on_primary_audio,
            on_done=self._on_primary_done,
            voice_id=voice_id,
        )
        self._fallback = fallback_cls(
            on_audio=self._on_fallback_audio,
            on_done=self._on_fallback_done,
            voice_id=None,
        )
        self._using_fallback = False
        self._primary_had_audio = False
        self._replay_text = ""
        self._replay_disabled = False
        self._flush_requested = False
        self._done_emitted = False
        self._switch_lock = asyncio.Lock()
        self._primary_failure: Optional[str] = None

    @property
    def is_active(self) -> bool:
        if self._using_fallback:
            return self._fallback.is_active
        return self._primary.is_active or self._fallback.is_active

    @property
    def fatal_error(self) -> Optional[str]:
        if self.is_active:
            return None
        return (
            self._fallback.fatal_error
            or self._primary_failure
            or self._primary.fatal_error
        )

    @property
    def warm_idle_started_at(self) -> Optional[float]:
        if self._using_fallback:
            return self._fallback.warm_idle_started_at
        return (
            self._primary.warm_idle_started_at
            or self._fallback.warm_idle_started_at
        )

    def bind(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
    ) -> None:
        self._on_audio = on_audio
        self._on_done = on_done
        self._primary_had_audio = False
        self._replay_text = ""
        self._replay_disabled = False
        self._flush_requested = False
        self._done_emitted = False
        self._primary_failure = None
        self._using_fallback = (
            not self._primary.is_active and self._fallback.is_active
        )

        self._primary.bind(self._on_primary_audio, self._on_primary_done)
        self._fallback.bind(self._on_fallback_audio, self._on_fallback_done)

    async def start(self) -> None:
        # eSpeak has no long-lived speech process; start() only verifies the
        # executable. Validate the configured emergency path up front so it
        # cannot silently be unavailable during an ElevenLabs outage.
        await self._fallback.start()

        try:
            await self._primary.start()
        except asyncio.CancelledError:
            await self._fallback.cancel()
            raise
        except Exception as exc:
            self._primary_failure = str(exc)
            try:
                await self._primary.cancel()
            except Exception as cleanup_exc:
                log.error(f"ElevenLabs startup cleanup failed ({cleanup_exc})")
            self._using_fallback = True
            log.error(
                "ElevenLabs startup failed; using local eSpeak fallback "
                f"({exc})"
            )

    async def send(self, text: str) -> None:
        if not text:
            return

        async with self._switch_lock:
            if self._using_fallback:
                target = self._fallback
            else:
                if not self._primary_had_audio and not self._replay_disabled:
                    if len(self._replay_text) + len(text) <= _MAX_REPLAY_CHARS:
                        self._replay_text += text
                    else:
                        self._replay_text = ""
                        self._replay_disabled = True
                        log.error(
                            "Pre-audio fallback replay disabled: buffered text "
                            f"exceeded {_MAX_REPLAY_CHARS} characters"
                        )
                target = self._primary

        await target.send(text)

    async def flush(self) -> None:
        async with self._switch_lock:
            self._flush_requested = True
            target = self._fallback if self._using_fallback else self._primary
        await target.flush()

    async def stop(self) -> None:
        await self.cancel()

    async def cancel(self) -> None:
        # Both providers are owned by this wrapper. Cancel them independently;
        # neither cleanup is allowed to strand the other.
        errors = []
        for provider in (self._primary, self._fallback):
            try:
                await provider.cancel()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    async def _on_primary_audio(self, audio: str) -> None:
        async with self._switch_lock:
            if self._using_fallback:
                return
            self._primary_had_audio = True
            self._replay_text = ""
            self._replay_disabled = True
        await self._on_audio(audio)

    async def _on_primary_done(self) -> None:
        async with self._switch_lock:
            if self._using_fallback:
                return

            self._primary_failure = self._primary.fatal_error

            if self._primary_had_audio or self._replay_disabled:
                finish_without_fallback = True
                cancel_unused_fallback = True
                replay = ""
                flush_after_replay = False
            elif self._fallback.is_active:
                self._using_fallback = True
                finish_without_fallback = False
                cancel_unused_fallback = False
                replay, self._replay_text = self._replay_text, ""
                flush_after_replay = self._flush_requested
                log.error(
                    "ElevenLabs ended before first audio; switching this turn "
                    "to local eSpeak fallback"
                )
            else:
                finish_without_fallback = True
                cancel_unused_fallback = True
                replay = ""
                flush_after_replay = False

        if finish_without_fallback:
            if cancel_unused_fallback:
                await self._fallback.cancel()
            await self._emit_done_once()
            return

        if replay:
            await self._fallback.send(replay)
        if flush_after_replay:
            await self._fallback.flush()

    async def _on_fallback_audio(self, audio: str) -> None:
        await self._on_audio(audio)

    async def _on_fallback_done(self) -> None:
        await self._emit_done_once()

    async def _emit_done_once(self) -> None:
        if self._done_emitted:
            return
        self._done_emitted = True
        await self._on_done()


def build_tts_service(
    on_audio: Callable[[str], Awaitable[None]],
    on_done: Callable[[], Awaitable[None]],
    *,
    voice_id: Optional[str] = None,
    elevenlabs_cls: Type = ElevenLabsTTSService,
    espeak_cls: Type = EspeakTTSService,
):
    error = validate_tts_provider_config()
    if error:
        raise ValueError(error)

    primary = tts_provider_name()
    fallback = tts_fallback_provider_name()

    if primary == "espeak":
        return espeak_cls(
            on_audio=on_audio,
            on_done=on_done,
            voice_id=None,
        )

    if fallback == "espeak":
        if not (os.getenv("ELEVENLABS_API_KEY") or "").strip():
            log.error(
                "ELEVENLABS_API_KEY is absent; using configured local eSpeak "
                "fallback without contacting ElevenLabs"
            )
            return espeak_cls(
                on_audio=on_audio,
                on_done=on_done,
                voice_id=None,
            )
        return FallbackTTSService(
            on_audio=on_audio,
            on_done=on_done,
            voice_id=voice_id,
            primary_cls=elevenlabs_cls,
            fallback_cls=espeak_cls,
        )

    return elevenlabs_cls(
        on_audio=on_audio,
        on_done=on_done,
        voice_id=voice_id,
    )
