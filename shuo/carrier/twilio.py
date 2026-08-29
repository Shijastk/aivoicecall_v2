"""
Twilio Media Streams carrier.

Kept as a fully working adapter, not a stub. It is the structural control
for the Vobiz implementation: if `CARRIER=twilio` still places a real
call after the refactor, the abstraction is honest.

Twilio cannot legally originate to Indian numbers from an Indian number
and costs ~4.36/min to Indian mobiles, so this is a development and
regression path, not the production one.
"""

from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, Optional
from xml.sax.saxutils import quoteattr

from ..types import (
    CallContext, CallDirection, Event,
    StreamStartEvent, StreamStopEvent, MediaEvent,
    PlaybackMarkEvent, DtmfEvent,
)
from ..log import get_logger
from .base import Carrier, CarrierSession, OriginateResult

logger = get_logger("shuo.carrier.twilio")


class TwilioSession(CarrierSession):
    """One Twilio Media Streams WebSocket."""

    def _parse_one(self, data: Dict[str, Any]) -> Optional[Event]:
        event_type = data.get("event")

        if event_type == "connected":
            logger.info("Twilio WebSocket connected")
            return None

        if event_type == "start":
            start = data.get("start", {}) or {}
            stream_sid = start.get("streamSid") or data.get("streamSid")
            if not stream_sid:
                return None
            return StreamStartEvent(
                stream_sid=stream_sid,
                call_id=start.get("callSid"),
            )

        if event_type == "media":
            media = data.get("media", {}) or {}
            payload = media.get("payload") or ""
            if not payload:
                return None
            # Twilio reports "inbound" / "outbound" here.
            track = media.get("track") or "inbound"
            return MediaEvent(
                audio_bytes=self.decode_payload(payload),
                track=track,
            )

        if event_type == "mark":
            mark = data.get("mark", {}) or {}
            name = mark.get("name")
            if name:
                return PlaybackMarkEvent(name=name)
            return None

        if event_type == "dtmf":
            dtmf = data.get("dtmf", {}) or {}
            digit = dtmf.get("digit")
            if digit:
                return DtmfEvent(digit=str(digit))
            return None

        if event_type == "stop":
            return StreamStopEvent()

        return None

    # ── Outbound frames ─────────────────────────────────────────────

    def _play_audio_frame(self, payload_b64: str) -> Dict[str, Any]:
        return {
            "event": "media",
            "streamSid": self.stream_id,
            "media": {"payload": payload_b64},
        }

    def _clear_audio_frame(self) -> Dict[str, Any]:
        return {"event": "clear", "streamSid": self.stream_id}

    def _checkpoint_frame(self, name: str) -> Dict[str, Any]:
        return {
            "event": "mark",
            "streamSid": self.stream_id,
            "mark": {"name": name},
        }

    def _stop_frame(self) -> Optional[Dict[str, Any]]:
        # Twilio has no server-initiated stream stop.
        return None


class TwilioCarrier(Carrier):
    """Twilio account-level operations."""

    name = "twilio"

    def __init__(self) -> None:
        self._account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self._from_number = os.getenv("TWILIO_PHONE_NUMBER", "")
        self._edge = os.getenv("TWILIO_EDGE", "frankfurt")
        self._region = os.getenv("TWILIO_REGION", "us1")

    # ── Answer URL ──────────────────────────────────────────────────

    def answer_xml(
        self,
        ws_url: str,
        *,
        status_callback_url: Optional[str] = None,
        record: bool = False,
        recording_callback_url: Optional[str] = None,
    ) -> str:
        """
        TwiML connecting the call to our WebSocket.

        `record-from-answer-dual` is Twilio's dual-channel recording:
        caller on one channel, agent on the other. That separation is
        what makes a recording usable for scoring interruptions and
        measuring real mouth-to-ear latency.
        """
        record_attr = "record-from-answer-dual" if record else "do-not-record"
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Response>\n"
            f'  <Connect record="{record_attr}">\n'
            f"    <Stream url={quoteattr(ws_url)} track=\"inbound_track\" />\n"
            f"  </Connect>\n"
            f"</Response>"
        )

    # ── Origination ─────────────────────────────────────────────────

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
        from twilio.rest import Client

        if not all([self._account_sid, self._auth_token, self._from_number]):
            raise ValueError(
                "Missing Twilio credentials: TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER"
            )

        client = Client(
            self._account_sid,
            self._auth_token,
            edge=self._edge,
            region=self._region,
        )

        def _create():
            # Twilio has no ring_url/hangup_url pair; it has one status
            # callback subscribed to named events, and the event name arrives
            # in the body as `CallStatus`. `/hangup` is the URL used because
            # both handlers read the same form and `completed` is the event
            # that carries the outcome; `ring_url` is accepted and folded into
            # the same subscription rather than being silently dropped.
            kwargs: Dict[str, Any] = {
                "to": to_number,
                "from_": self._from_number,
                "url": answer_url,
            }
            callback = hangup_url or ring_url
            if callback:
                kwargs["status_callback"] = callback
                kwargs["status_callback_event"] = ["ringing", "answered", "completed"]
                kwargs["status_callback_method"] = "POST"
            return client.calls.create(**kwargs)

        # The Twilio SDK is synchronous; keep it off the event loop.
        call = await asyncio.to_thread(_create)
        return OriginateResult(call_id=call.sid, raw={"sid": call.sid})

    async def hangup(self, call_id: str) -> None:
        from twilio.rest import Client

        client = Client(self._account_sid, self._auth_token,
                        edge=self._edge, region=self._region)

        def _hangup():
            client.calls(call_id).update(status="completed")

        await asyncio.to_thread(_hangup)

    # ── Sessions ────────────────────────────────────────────────────

    def new_session(
        self, websocket: Any, context: Optional[CallContext] = None
    ) -> CarrierSession:
        return TwilioSession(websocket, self.name, context)

    # ── Signature validation ────────────────────────────────────────

    def validate_signature(
        self,
        *,
        url: str,
        headers: Dict[str, str],
        body: bytes,
        form: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Twilio: HMAC-SHA1 over url + sorted POST params, base64, in
        X-Twilio-Signature.
        """
        signature = _header(headers, "X-Twilio-Signature")
        if not signature or not self._auth_token:
            return False
        try:
            from twilio.request_validator import RequestValidator
        except ImportError:
            logger.error("twilio package missing; cannot validate signature")
            return False
        validator = RequestValidator(self._auth_token)
        return bool(validator.validate(url, form or {}, signature))


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup."""
    lowered = name.lower()
    for k, v in headers.items():
        if k.lower() == lowered:
            return v
    return None
