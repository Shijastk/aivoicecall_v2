"""
Carrier abstraction -- the seam between shuo and a telephony provider.

Two objects:

    Carrier          account-level. Places calls, builds answer-URL XML,
                     validates webhook signatures, hangs calls up.

    CarrierSession   per-call. Owns one media WebSocket. Parses inbound
                     frames into typed Events and writes outbound audio.

The split exists because per-call state is unavoidable: the stream and
call identifiers, and the media-before-start buffer, belong to one call
and must not leak between calls.

The interface is modelled on the *Plivo* shape rather than Twilio's,
because Vobiz is a deliberate Plivo clone -- so Vobiz and Plivo both drop
in behind it, and Twilio is the one that needs adapting.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..types import CallContext, Event, MediaEvent, StreamStartEvent
from ..log import get_logger

logger = get_logger("shuo.carrier")


# Media frames buffered while we wait for `start` to arrive.
# 20ms per frame, so 250 frames = 5 seconds. Bounded so a `start` that
# never comes cannot grow this without limit.
MAX_PENDING_MEDIA_FRAMES = 250


@dataclass(frozen=True)
class OriginateResult:
    """Result of placing an outbound call."""
    call_id: str
    raw: Dict[str, Any]


class CarrierSession(ABC):
    """
    One call's media stream.

    Subclasses implement `_parse_one`. Everything else -- the
    media-before-start buffer, identifier tracking, the outbound send
    path -- is shared, because those are the parts that carriers get
    wrong in the same ways.
    """

    def __init__(self, websocket: Any, carrier_name: str, context: Optional[CallContext] = None):
        self._ws = websocket
        self._carrier_name = carrier_name
        self._context = context

        self.call_id: Optional[str] = context.call_id if context else None
        self.stream_id: Optional[str] = None
        self.started = False

        # Vobiz has been observed sending `media` before `start`. Hold
        # those frames until identifiers populate, then replay them in
        # order so no caller audio is lost from the top of the call.
        self._pending_media: deque = deque(maxlen=MAX_PENDING_MEDIA_FRAMES)
        self._dropped_pending = 0
        self._warned_non_inbound = False
        self._closed = False

        # Protocol surprises worth logging once per call rather than 50
        # times a second (see VobizSession._warn_once).
        self._warned: set = set()

    # ── Inbound ─────────────────────────────────────────────────────

    @abstractmethod
    def _parse_one(self, data: Dict[str, Any]) -> Optional[Event]:
        """
        Carrier-specific parse of a single decoded WebSocket frame.

        Returns None for frames that carry no domain event (keepalives,
        `connected` handshakes, and anything unrecognised).
        """

    def parse_message(self, data: Dict[str, Any]) -> List[Event]:
        """
        Parse one inbound frame into zero or more Events.

        Returns a list rather than a single Event because a `start` frame
        also drains any media buffered ahead of it, and that must reach
        the loop as StreamStartEvent followed by the buffered audio, in
        arrival order.
        """
        event = self._parse_one(data)
        if event is None:
            return []

        if isinstance(event, StreamStartEvent):
            self.started = True
            self.stream_id = event.stream_sid
            if event.call_id:
                # Key on call_id, never stream_id: a reconnect replays
                # `start` with a NEW stream id on the SAME call.
                if self.call_id and self.call_id != event.call_id:
                    logger.warning(
                        f"{self._carrier_name}: call_id changed "
                        f"{self.call_id} -> {event.call_id}; keeping the new one"
                    )
                self.call_id = event.call_id

            drained = list(self._pending_media)
            self._pending_media.clear()
            if drained:
                logger.info(
                    f"{self._carrier_name}: replaying {len(drained)} media frame(s) "
                    f"buffered before `start`"
                )
            if self._dropped_pending:
                logger.warning(
                    f"{self._carrier_name}: dropped {self._dropped_pending} media frame(s) "
                    f"waiting for `start` (buffer holds {MAX_PENDING_MEDIA_FRAMES})"
                )
                self._dropped_pending = 0
            return [event] + drained

        if isinstance(event, MediaEvent):
            if event.track != "inbound" and not self._warned_non_inbound:
                self._warned_non_inbound = True
                logger.warning(
                    f"{self._carrier_name}: received '{event.track}' track audio. "
                    f"Expected inbound only -- check the audioTrack setting. "
                    f"Non-inbound audio is being dropped so the agent does not "
                    f"transcribe itself."
                )
            if not self.started:
                if len(self._pending_media) == self._pending_media.maxlen:
                    self._dropped_pending += 1
                self._pending_media.append(event)
                return []

        return [event]

    # ── Outbound ────────────────────────────────────────────────────

    @abstractmethod
    def _play_audio_frame(self, payload_b64: str) -> Dict[str, Any]:
        """Build the carrier's outbound audio message."""

    @abstractmethod
    def _clear_audio_frame(self) -> Dict[str, Any]:
        """Build the carrier's buffer-flush message."""

    @abstractmethod
    def _checkpoint_frame(self, name: str) -> Dict[str, Any]:
        """Build the carrier's playback-checkpoint message."""

    @abstractmethod
    def _stop_frame(self) -> Optional[Dict[str, Any]]:
        """
        Build the carrier's stream-stop message.

        None for carriers that have no server-initiated stop (Twilio).
        """

    async def play_audio(self, payload_b64: str) -> None:
        """Send one chunk of base64 mu-law audio to the caller."""
        await self._send(self._play_audio_frame(payload_b64))

    async def clear_audio(self) -> None:
        """Flush audio the carrier has buffered but not yet played."""
        await self._send(self._clear_audio_frame())

    async def checkpoint(self, name: str) -> None:
        """
        Ask the carrier to tell us when playback reaches this point.

        The acknowledgement arrives as a PlaybackMarkEvent. This is the
        only authoritative signal for what the caller actually heard.
        """
        await self._send(self._checkpoint_frame(name))

    async def send_stop(self) -> None:
        """
        Signal end-of-stream over the WebSocket.

        Must be sent *before* any REST hangup: otherwise a phantom
        reconnect can overwrite the call's transcript with empty data.
        """
        if self._closed:
            return
        self._closed = True
        if not self.started:
            # The stream never started, so there is no stream to stop and
            # no identifier to name it with.
            return
        frame = self._stop_frame()
        if frame is not None:
            await self._send(frame)

    async def _send(self, message: Dict[str, Any]) -> None:
        import json
        try:
            await self._ws.send_text(json.dumps(message))
        except Exception as e:  # carrier hung up mid-write
            logger.debug(f"{self._carrier_name}: send failed ({e})")

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def decode_payload(payload: str) -> bytes:
        return base64.b64decode(payload)

    @staticmethod
    def encode_payload(audio: bytes) -> str:
        return base64.b64encode(audio).decode("ascii")


class Carrier(ABC):
    """Account-level telephony provider."""

    name: str = "unknown"

    @abstractmethod
    def answer_xml(
        self,
        ws_url: str,
        *,
        status_callback_url: Optional[str] = None,
        record: bool = False,
        recording_callback_url: Optional[str] = None,
    ) -> str:
        """
        XML returned from the answer URL, instructing the carrier to fork
        media to `ws_url`.

        Must be servable in well under a second: a slow answer URL is
        dead air on the caller's handset.
        """

    # ── Recording (optional per carrier) ────────────────────────────

    @property
    def records_via_rest(self) -> bool:
        """
        True when recording cannot be expressed in the answer XML and must
        be started over REST once the call is up.

        Vobiz is the case that needs this: its XML <Record> element has no
        channel attribute, so dual-channel recording is only reachable
        through the REST Record sub-resource.
        """
        return False

    async def start_recording(
        self, call_id: str, *, callback_url: Optional[str] = None
    ) -> Optional[str]:
        """Start recording a live call. Returns a recording id if known."""
        return None

    async def stop_recording(self, call_id: str) -> None:
        """Stop recording a live call. Should be idempotent."""
        return None

    @abstractmethod
    async def originate(
        self,
        to_number: str,
        *,
        answer_url: str,
        persona_id: str = "default",
        record: bool = False,
        ring_url: Optional[str] = None,
        hangup_url: Optional[str] = None,
    ) -> OriginateResult:
        """
        Place an outbound call.

        `ring_url` and `hangup_url` are where the carrier reports progress on
        an attempt that may never be answered. They are optional because a
        carrier that does not support them must still be able to place a call
        -- the call log then simply cannot distinguish a missed call from a
        cancelled one, which is a poorer log rather than a broken one.
        """

    @abstractmethod
    async def hangup(self, call_id: str) -> None:
        """End a live call."""

    @abstractmethod
    def new_session(
        self, websocket: Any, context: Optional[CallContext] = None
    ) -> CarrierSession:
        """Create a per-call media session bound to this WebSocket."""

    @abstractmethod
    def validate_signature(
        self,
        *,
        url: str,
        headers: Dict[str, str],
        body: bytes,
        form: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Validate a webhook signature.

        Called unconditionally -- an absent signature header is a
        validation failure, not a reason to skip the check.
        """
