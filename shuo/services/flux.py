"""
Deepgram Flux service -- always-on STT + turn detection.

A single persistent WebSocket to Deepgram using the v2 listen API.
Receives all Twilio audio continuously and emits turn events.

Replaces both local VAD (Silero) and separate STT (Deepgram v1).
"""

import os
import asyncio
import time
from collections.abc import Mapping
from typing import Any, Optional, Callable, Awaitable

from deepgram import AsyncDeepgramClient, DeepgramClientEnvironment

from ..log import ServiceLogger

log = ServiceLogger("Flux")


def _field(message: Any, name: str, default: Any = None) -> Any:
    """
    Read one field off a Deepgram message, whether it arrived as a model
    or as a plain dict.

    🔴 It is a dict, in practice. The SDK types its socket responses as

        Union[ListenV2Connected, ListenV2TurnInfo, Any, ...]

    and `construct_type` short-circuits any union containing `Any`
    (deepgram/core/unchecked_base_model.py:222) by returning the decoded
    JSON untouched. So no message is ever coerced into a model, and
    `getattr(message, "type")` is always None -- every TurnInfo is
    silently discarded, the agent never takes a turn, and nothing in the
    log says why. Read both shapes so a future SDK that does construct
    models keeps working.
    """
    if isinstance(message, Mapping):
        value = message.get(name, default)
    else:
        value = getattr(message, name, default)
    return default if value is None else value


class FluxService:
    """
    Deepgram Flux streaming service.

    Audio format: mulaw 8kHz (direct from Twilio, no conversion needed).
    Turn events: StartOfTurn (barge-in), EndOfTurn (with transcript).

    ``eager_eot_threshold`` is opt-in measurement support. When unset the
    provider request and callback behavior are unchanged. When set, Flux emits
    EagerEndOfTurn/TurnResumed events; this service records sanitized timing
    only. It does NOT start the agent early in this phase.
    """

    def __init__(
        self,
        on_end_of_turn: Callable[[str], Awaitable[None]],
        on_start_of_turn: Callable[[], Awaitable[None]],
        on_interim: Optional[Callable[[str], Awaitable[None]]] = None,
        on_eager_end_of_turn: Optional[Callable[[str], Awaitable[None]]] = None,
        on_turn_resumed: Optional[Callable[[], Awaitable[None]]] = None,
        eager_eot_threshold: Optional[float] = None,
    ):
        if eager_eot_threshold is not None and not (
            0.3 <= eager_eot_threshold <= 0.7
        ):
            raise ValueError(
                "eager_eot_threshold must be between 0.3 and 0.7 for the "
                "measurement slice so Flux's default final EOT threshold "
                "is not changed"
            )

        self._on_end_of_turn = on_end_of_turn
        self._on_start_of_turn = on_start_of_turn
        self._on_interim = on_interim
        self._on_eager_end_of_turn = on_eager_end_of_turn
        self._on_turn_resumed = on_turn_resumed
        self._eager_eot_threshold = eager_eot_threshold

        self._api_key = os.getenv("DEEPGRAM_API_KEY", "")
        self._client: Optional[AsyncDeepgramClient] = None
        self._connection = None
        self._cm = None
        self._listener_task: Optional[asyncio.Task] = None
        self._running = False

        # Deepgram is a black box until it says something. These counters
        # are the only way to tell "the carrier sent us no audio" apart
        # from "we sent audio and Deepgram stayed silent" -- the two
        # failure modes look identical in the log otherwise.
        self._frames_sent = 0
        self._bytes_sent = 0
        self._messages_seen = 0

        # Phase 4A measurement state only. No transcript content is logged.
        self._eager_started_at: Optional[float] = None
        self._eager_turn_index: Optional[int] = None
        self._eager_transcript: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self._running and self._connection is not None

    async def start(self) -> None:
        """Connect to Deepgram Flux (always-on for the duration of the call)."""
        if self._running:
            return

        try:
            # deepgram-sdk 7.x added a required `agent_rest` field. Without
            # it construction raises and every call dies at connect time.
            deepgram_eu = DeepgramClientEnvironment(
                base="wss://api.eu.deepgram.com",
                production="wss://api.eu.deepgram.com",
                agent="wss://agent.eu.deepgram.com",
                agent_rest="https://agent.eu.deepgram.com",
            )
            self._client = AsyncDeepgramClient(
                api_key=self._api_key,
                environment=deepgram_eu,
            )

            connect_kwargs = {
                "model": "flux-general-en",
                "encoding": "mulaw",
                "sample_rate": 8000,
            }
            if self._eager_eot_threshold is not None:
                # Deepgram documents threshold values as strings in the SDK
                # examples. Do not alter eot_threshold here: Phase 4A measures
                # eager lead without silently changing final-EOT behavior.
                connect_kwargs["eager_eot_threshold"] = str(
                    self._eager_eot_threshold
                )

            self._cm = self._client.listen.v2.connect(**connect_kwargs)
            self._connection = await self._cm.__aenter__()

            # Event names are the *values* of deepgram.core.events.EventType
            # -- "open", "message", "error", "close", all lowercase. The
            # emitter looks callbacks up by exact key, so a capitalised
            # "Error" registers a handler that can never fire: Deepgram
            # could reject the stream and the call would look healthy.
            self._connection.on("message", self._on_message)
            self._connection.on("error", self._on_error)
            self._connection.on("close", self._on_close)

            self._listener_task = asyncio.create_task(
                self._connection.start_listening()
            )
            # start_listening() swallows everything into its ERROR/CLOSE
            # events, but a task that dies before those fire is otherwise
            # invisible -- nothing awaits it until cleanup cancels it.
            self._listener_task.add_done_callback(self._on_listener_done)

            self._running = True
            log.connected()
            if self._eager_eot_threshold is not None:
                log.info(
                    "Eager EOT measurement enabled "
                    f"threshold={self._eager_eot_threshold:.2f}"
                )

        except Exception as e:
            log.error("Connection failed", e)
            await self._cleanup()
            raise

    async def send(self, audio_bytes: bytes) -> None:
        """Send audio chunk to Deepgram Flux."""
        if not self._connection or not self._running:
            # Audio arriving with no socket to put it on is a silent
            # transcript loss, so say it once rather than never.
            if self._frames_sent == 0:
                log.error("Dropping caller audio -- no Deepgram connection")
            return

        try:
            await self._connection.send_media(audio_bytes)
        except Exception as e:
            log.error("Send failed", e)
            return

        self._frames_sent += 1
        self._bytes_sent += len(audio_bytes)
        # First frame proves the carrier -> STT path end to end; the
        # heartbeat (~every 5s at 50 frames/sec) proves it stayed up.
        if self._frames_sent == 1:
            log.info(f"→ first caller audio forwarded ({len(audio_bytes)} bytes)")
        elif self._frames_sent % 250 == 0:
            log.info(
                f"→ {self._frames_sent} frames / {self._bytes_sent} bytes sent, "
                f"{self._messages_seen} messages back"
            )

    async def stop(self) -> None:
        """Disconnect from Deepgram Flux."""
        self._running = False
        await self._cleanup()
        log.disconnected()

    async def _cleanup(self) -> None:
        """Clean up resources."""
        self._running = False
        self._clear_eager_measurement()

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self._cm:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._cm = None

        self._connection = None
        self._client = None

    def _clear_eager_measurement(self) -> None:
        self._eager_started_at = None
        self._eager_turn_index = None
        self._eager_transcript = None

    async def _on_message(self, message, *args, **kwargs) -> None:
        """Handle Flux messages -- parse TurnInfo events."""
        try:
            msg_type = _field(message, "type")

            self._messages_seen += 1
            if self._messages_seen == 1:
                # Deepgram's first message is `Connected`. Seeing it is the
                # difference between "the socket opened" and "Deepgram is
                # actually serving this model on this endpoint".
                log.info(f"← first message from Deepgram: {msg_type}")

            if msg_type == "FatalError":
                # The stream is over. Loud, because every later turn will
                # silently produce nothing.
                log.error(
                    f"Deepgram FatalError: "
                    f"{_field(message, 'description') or message}"
                )
                return

            if msg_type == "TurnInfo":
                event = _field(message, "event")

                if event == "EndOfTurn":
                    transcript = (_field(message, "transcript", "") or "").strip()
                    if self._eager_started_at is not None:
                        now = time.perf_counter()
                        turn_index = _field(message, "turn_index")
                        same_turn = (
                            self._eager_turn_index is None
                            or turn_index is None
                            or turn_index == self._eager_turn_index
                        )
                        transcript_match = transcript == (
                            self._eager_transcript or ""
                        )
                        lead_ms = (now - self._eager_started_at) * 1000
                        log.info(
                            "Eager EOT measurement: final "
                            f"after {lead_ms:.1f}ms "
                            f"turn_match={same_turn} "
                            f"transcript_match={transcript_match} "
                            f"transcript_chars={len(transcript)}"
                        )
                        self._clear_eager_measurement()

                    await self._on_end_of_turn(transcript)

                elif event == "StartOfTurn":
                    await self._on_start_of_turn()

                elif (
                    event == "EagerEndOfTurn"
                    and self._eager_eot_threshold is not None
                ):
                    transcript = (_field(message, "transcript", "") or "").strip()
                    self._eager_started_at = time.perf_counter()
                    self._eager_turn_index = _field(message, "turn_index")
                    self._eager_transcript = transcript
                    log.info(
                        "Eager EOT measurement: candidate "
                        f"turn={self._eager_turn_index} "
                        f"transcript_chars={len(transcript)}"
                    )
                    if self._on_eager_end_of_turn is not None:
                        await self._on_eager_end_of_turn(transcript)

                elif (
                    event == "TurnResumed"
                    and self._eager_eot_threshold is not None
                ):
                    if self._eager_started_at is not None:
                        elapsed_ms = (
                            time.perf_counter() - self._eager_started_at
                        ) * 1000
                        log.info(
                            "Eager EOT measurement: resumed "
                            f"after {elapsed_ms:.1f}ms "
                            f"turn={self._eager_turn_index}"
                        )
                    else:
                        log.info("Eager EOT measurement: resumed without candidate")
                    if self._on_turn_resumed is not None:
                        await self._on_turn_resumed()
                    self._clear_eager_measurement()

                elif event == "Update" and self._on_interim:
                    # Flux carries interim text on the same TurnInfo
                    # message; there is no separate v1-style `Results`.
                    transcript = _field(message, "transcript", "") or ""
                    if transcript:
                        await self._on_interim(transcript.strip())

        except Exception as e:
            log.error("Message handling failed", e)

    async def _on_error(self, error, *args, **kwargs) -> None:
        """Handle Deepgram errors."""
        log.error("Deepgram: " + str(error))

    async def _on_close(self, *args, **kwargs) -> None:
        """
        Deepgram closed the socket.

        Mid-call this is fatal and otherwise invisible: `send` keeps
        succeeding into a dead socket and no transcript ever comes back.
        Deepgram also closes a stream that has received no audio, so a
        close ~10s in with `frames=0` says the carrier never forked media
        to us -- which is a different bug entirely.
        """
        if self._running:
            log.error(
                f"Deepgram closed the stream mid-call "
                f"(frames={self._frames_sent}, messages={self._messages_seen})"
            )
        else:
            log.debug(
                f"stream closed (frames={self._frames_sent}, "
                f"messages={self._messages_seen})"
            )

    def _on_listener_done(self, task: "asyncio.Task") -> None:
        """Surface a listener task that died without emitting anything."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Listener task died", exc)
