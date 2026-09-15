"""
ElevenLabs Text-to-Speech service with WebSocket streaming.
"""

import os
import json
import asyncio
import time
from typing import Optional, Callable, Awaitable

import websockets
from websockets.client import WebSocketClientProtocol

from ..log import ServiceLogger

log = ServiceLogger("TTS")


# "George" -- premade, British (context.md decision 31, which reverses 26
# conditionally). The fallback must be a voice that can actually be
# *synthesised*, which is not the same question as whether it suits the
# persona:
#
#   Rachel (the original default)  wrong accent, produces audio
#   Krish  (decision 26)           right accent, produces NOTHING
#                                  on a free plan -- payment_required
#   George (here)                  wrong accent, produces audio
#
# Decision 26 ranked persona above accent-correctness, which was right while
# the Indian voice worked. It does not any more, and a silent call is
# strictly worse than an off-accent one: an accent break is a bad turn, no
# audio is no conversation at all. Restore `MmiGAbOYCaIFzgNItUWa` here the
# moment the plan is upgraded.
FALLBACK_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"


def env_voice_id() -> str:
    """
    The voice to use when nothing more specific was chosen.

    Read through a function rather than at import so a test (or a deploy
    that sets the variable late) is not stuck with whatever the environment
    held when this module first loaded.

    An ELEVENLABS_VOICE_ID set to the empty string resolves to the fallback
    rather than to "", which is what a bare `os.getenv(name, default)`
    would return -- an empty voice ID builds a URL that 404s, and the
    symptom (a call with no audio) points nowhere near the environment.
    """
    return os.getenv("ELEVENLABS_VOICE_ID", "").strip() or FALLBACK_VOICE_ID


class TTSService:
    """
    ElevenLabs streaming TTS service.

    Sends text chunks, receives audio chunks via callback.
    Audio is returned as base64-encoded mulaw at 8kHz for Twilio.
    """

    def __init__(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
        voice_id: Optional[str] = None,
    ):
        self._on_audio = on_audio
        self._on_done = on_done
        
        self._ws: Optional[WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._running = False
        self._warm_idle_started_at: Optional[float] = None

        # Whether this connection has ever emitted an audio chunk. Sole
        # discriminator between a stream that ended and one that was
        # refused -- ElevenLabs closes with 1008 either way.
        self._produced_audio = False

        # Why this connection died, if it died for a reason we can name.
        # A refusal is reported on the socket and then the socket goes
        # away, so the reason has to be captured where it arrives or it is
        # lost. The pool reads this to explain a discarded connection.
        self._fatal_error: Optional[str] = None
        
        self._api_key = os.getenv("ELEVENLABS_API_KEY", "")
        # The operator's choice when one reached us (W2 resolves it from the
        # config store before the pool warms), the environment otherwise.
        # `voice_id` is a *provider* ID here, never a catalogue one -- the
        # mapping happens in `shuo/runtime_config.py`, so nothing in this
        # module has to know the catalogue exists.
        self._voice_id = voice_id or env_voice_id()
    
    @property
    def is_active(self) -> bool:
        return self._running and self._ws is not None

    @property
    def fatal_error(self) -> Optional[str]:
        """Reason this connection is unusable, or None if it is healthy."""
        return self._fatal_error

    @property
    def warm_idle_started_at(self) -> Optional[float]:
        """Monotonic time immediately before sending successful initialization.

        Excludes the handshake but conservatively includes send/backpressure
        time. This is a local bound, not an acknowledgement of provider receipt.
        """
        return self._warm_idle_started_at

    def bind(
        self,
        on_audio: Callable[[str], Awaitable[None]],
        on_done: Callable[[], Awaitable[None]],
    ) -> None:
        """Rebind callbacks (used by connection pool to assign per-turn handlers)."""
        self._on_audio = on_audio
        self._on_done = on_done
        # A pooled connection is dispensed once per turn, so the
        # "did this turn produce audio" flag has to reset with the callbacks.
        self._produced_audio = False
    
    async def start(self) -> None:
        """Open WebSocket connection to ElevenLabs."""
        if self._running:
            return
        
        url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}/stream-input?"
            f"model_id=eleven_turbo_v2_5&"
            f"output_format=ulaw_8000"
        )
        
        try:
            self._ws = await websockets.connect(url)
            self._running = True

            # Log ElevenLabs region (expect "Netherlands" from DE)
            # websockets v15: response headers live on ws.response.headers
            resp = getattr(self._ws, "response", None)
            hdrs = getattr(resp, "headers", {}) if resp else {}
            region = hdrs.get("x-region", "unknown")
            log.info(f"Region: {region}")
            
            init_message = {
                "text": " ",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                },
                "xi_api_key": self._api_key,
            }
            idle_started_at = time.monotonic()
            await self._ws.send(json.dumps(init_message))
            self._warm_idle_started_at = idle_started_at
            
            self._receive_task = asyncio.create_task(self._receive_loop())
            log.connected()
            
        except Exception as e:
            log.error("Connection failed", e)
            raise
    
    async def send(self, text: str) -> None:
        """Send text chunk for synthesis."""
        if not self._ws or not self._running:
            return
        
        try:
            message = {
                "text": text,
                "try_trigger_generation": True,
            }
            await self._ws.send(json.dumps(message))
        except Exception as e:
            log.error("Send failed", e)
    
    async def flush(self) -> None:
        """
        End the turn: synthesise everything buffered, then close.

        Both callers (`_on_llm_done`, `stop`) mean "no more text is
        coming", which is end-of-stream, not a mid-utterance flush.

        The payload matters more than it looks. Measured against the live
        socket, terminator vs. what comes back:

            {"text": "", "flush": true}   5 chunks, no isFinal, never closes
            {"text": " ", "flush": true}  6 chunks, no isFinal, never closes
            {"text": ""}                  7 chunks, isFinal @1344ms, closes

        Only a bare empty string is the EOS sentinel; adding `flush` makes
        ElevenLabs read it as a flush of an ongoing stream and hold the
        context open. That silently broke turn completion: without
        `isFinal` there is no `_on_done`, so `player.mark_tts_done` is
        never called, `_next_frame` returns None with `_tts_done` still
        False, and the playback loop parks in the underrun branch forever
        -- the turn only ends when the caller barges in. It also cost the
        tail of the utterance, which is the two chunks in that table.
        """
        if not self._ws or not self._running:
            return

        try:
            await self._ws.send(json.dumps({"text": ""}))
        except Exception as e:
            log.error("Flush failed", e)
    
    async def stop(self) -> None:
        """Close connection gracefully after flushing."""
        if not self._running:
            return
        
        try:
            await self.flush()
            await asyncio.sleep(0.2)
        except Exception as e:
            log.error("Stop failed", e)
        finally:
            await self._cleanup()
        
        log.disconnected()
    
    async def cancel(self) -> None:
        """Abort connection immediately."""
        self._running = False
        await self._cleanup()
        log.cancelled()
    
    async def _cleanup(self) -> None:
        """Clean up resources."""
        self._running = False
        
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
    
    async def _receive_loop(self) -> None:
        """Background task to receive audio chunks."""
        try:
            while self._running and self._ws:
                try:
                    message = await self._ws.recv()
                    await self._handle_message(message)
                except websockets.exceptions.ConnectionClosed as e:
                    # A close is only unremarkable once audio has actually
                    # been produced. ElevenLabs rejects a *generation*, not
                    # a connection: auth, quota and voice-entitlement errors
                    # all arrive on a socket that connected cleanly, and only
                    # once the first chunk schedule fires. Closing here
                    # having emitted nothing is that rejection, and it was
                    # indistinguishable from a healthy end of stream.
                    if not self._produced_audio:
                        if self._fatal_error is None:
                            self._fatal_error = (
                                f"closed after 0 audio chunks "
                                f"(code={e.code} reason={e.reason!r})"
                            )
                        log.error(
                            f"Closed after 0 audio chunks -- code={e.code} "
                            f"reason={e.reason!r}"
                        )
                    break
                except Exception as e:
                    log.error("Receive failed", e)
                    break
        finally:
            if self._running:
                self._running = False
                await self._on_done()
    
    async def _handle_message(self, message: str) -> None:
        """Parse and handle ElevenLabs response."""
        try:
            data = json.loads(message)
            
            if "audio" in data and data["audio"]:
                self._produced_audio = True
                audio_base64 = data["audio"]
                await self._on_audio(audio_base64)

            elif data.get("error"):
                # 🔴 Never let this fall through silently again. This branch
                # is the entire reason a dead turn looked like a hang: the
                # payload matched neither `audio` nor `isFinal`, so it was
                # dropped without a line of output, the socket closed, and
                # the player -- never started, because no chunk ever arrived
                # -- sat idle until the caller barged in. The failure was
                # fully diagnosable in the frame being thrown away.
                kind = data.get("error")
                detail = data.get("message", "")
                self._fatal_error = f"{kind}: {detail}"
                log.error(
                    f"ElevenLabs refused generation: {kind} -- "
                    f"{detail} (code {data.get('code')})"
                )
                # Entitlement is the one refusal the operator can act on,
                # and the one the API's own voice listing does NOT predict:
                # /v1/voices returns every voice in the workspace, while
                # synthesis of a `professional` (library) voice is a
                # separate, paid entitlement. Reading the list and
                # concluding "this voice is permitted" is the trap, so name
                # the fix rather than just the error.
                if kind == "payment_required" or "upgrade" in detail.lower():
                    log.error(
                        f"Voice {self._voice_id} is not synthesisable on this "
                        f"plan. Set ELEVENLABS_VOICE_ID to a 'premade' voice "
                        f"or upgrade -- /v1/voices listing it is not proof."
                    )
            
            if data.get("isFinal", False):
                await self._on_done()
            
        except json.JSONDecodeError:
            log.error(f"Invalid JSON: {message[:100]}")
