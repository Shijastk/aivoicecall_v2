from __future__ import annotations

import asyncio
import audioop
import base64
import io
import shutil
import time
import wave
from asyncio.subprocess import PIPE
from typing import Awaitable, Callable, Optional

from ..log import ServiceLogger
from .phrase_buffer import BoundedPhraseBuffer

log = ServiceLogger("TTS-eSpeak")


def find_espeak_executable() -> Optional[str]:
    """Return an installed eSpeak NG/compatible binary, if available."""
    return shutil.which("espeak-ng") or shutil.which("espeak")


def wav_to_mulaw_8k(wav_bytes: bytes) -> bytes:
    """Convert an in-memory PCM WAV into SHUO's mono G.711 mu-law/8 kHz contract."""
    if not wav_bytes:
        return b""

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            compression = reader.getcomptype()
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"eSpeak returned invalid WAV audio: {exc}") from exc

    if compression != "NONE":
        raise RuntimeError(f"eSpeak returned compressed WAV audio: {compression}")
    if channels not in (1, 2):
        raise RuntimeError(f"unsupported eSpeak channel count: {channels}")
    if sample_width not in (1, 2, 3, 4):
        raise RuntimeError(f"unsupported eSpeak sample width: {sample_width}")
    if sample_rate <= 0:
        raise RuntimeError(f"invalid eSpeak sample rate: {sample_rate}")
    if not frames:
        return b""

    if channels == 2:
        frames = audioop.tomono(frames, sample_width, 0.5, 0.5)

    if sample_width != 2:
        frames = audioop.lin2lin(frames, sample_width, 2)

    if sample_rate != 8_000:
        frames, _ = audioop.ratecv(
            frames,
            2,
            1,
            sample_rate,
            8_000,
            None,
        )

    return audioop.lin2ulaw(frames, 2)


class EspeakTTSService:
    """Local, cost-free TTS provider exposing the existing mu-law/8 kHz interface.

    eSpeak produces PCM WAV, but PCM is contained entirely inside this provider
    module. Each bounded phrase is converted to base64 G.711 mu-law/8 kHz before
    invoking ``on_audio``; no raw audio file is written.
    """

    DEFAULT_VOICE = "en"
    DEFAULT_SPEED = 175
    PHRASE_CHARS = 24

    def __init__(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        voice_id: Optional[str] = None,
        *,
        executable: Optional[str] = None,
    ) -> None:
        self._on_audio = on_audio
        self._on_done = on_done
        self._voice = self.DEFAULT_VOICE
        self._speed = self.DEFAULT_SPEED
        self._requested_executable = executable
        self._executable: Optional[str] = None

        self._running = False
        self._fatal_error: Optional[str] = None
        self._warm_idle_started_at: Optional[float] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._buffer = BoundedPhraseBuffer(self.PHRASE_CHARS)
        self._done_emitted = False
        self._lock = asyncio.Lock()

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
        self._buffer = BoundedPhraseBuffer(self.PHRASE_CHARS)
        self._done_emitted = False
        self._fatal_error = None

    async def start(self) -> None:
        if self._running:
            return

        executable = self._requested_executable or find_espeak_executable()
        if not executable:
            raise RuntimeError(
                "eSpeak TTS requested but no 'espeak-ng' (or compatible 'espeak') "
                "binary is installed"
            )

        self._executable = executable
        self._warm_idle_started_at = time.monotonic()
        self._running = True
        self._fatal_error = None
        log.info(f"Local provider ready executable={executable}")

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
        self._buffer = BoundedPhraseBuffer(self.PHRASE_CHARS)
        await self._terminate_process()

    async def _synthesize_phrase(self, text: str) -> bool:
        if not text.strip():
            return True
        executable = self._executable
        if not executable:
            await self._fail("eSpeak executable is not initialized")
            return False

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--stdout",
                "--stdin",
                "-v",
                self._voice,
                "-s",
                str(self._speed),
                "-z",
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
            )
            self._process = process
            stdout, stderr = await process.communicate(text.encode("utf-8"))
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                await self._fail(
                    f"eSpeak exited with code {process.returncode}"
                    + (f": {detail}" if detail else "")
                )
                return False

            if not self._running:
                return False

            mulaw = wav_to_mulaw_8k(stdout)
            if not mulaw:
                await self._fail("eSpeak produced no audio")
                return False

            await self._on_audio(base64.b64encode(mulaw).decode("ascii"))
            return True
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await self._terminate_process(process)
            raise
        except Exception as exc:
            await self._fail(f"eSpeak synthesis failed: {exc}")
            return False
        finally:
            if self._process is process:
                self._process = None

    async def _fail(self, message: str) -> None:
        if self._fatal_error is None:
            self._fatal_error = message
            log.error(message)
        self._running = False
        await self._emit_done_once()

    async def _emit_done_once(self) -> None:
        if self._done_emitted:
            return
        self._done_emitted = True
        await self._on_done()

    async def _terminate_process(
        self,
        process: Optional[asyncio.subprocess.Process] = None,
    ) -> None:
        child = process or self._process
        if child is None or child.returncode is not None:
            return
        try:
            child.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(child.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            try:
                child.kill()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(child.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                log.error("eSpeak child did not exit after terminate/kill")
