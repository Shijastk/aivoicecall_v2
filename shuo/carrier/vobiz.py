"""
Vobiz carrier -- the production transport.

Vobiz (vobiz.ai, Ilaimitado Private Limited, Bengaluru) runs its media
plane in AWS ap-south-1, so the WebSocket hop from the SBC to our EC2 box
is ~1-5ms instead of Twilio's ~200ms transatlantic leg.

Vobiz is a deliberate Plivo clone, but NOT a faithful one. The
divergences that matter are called out inline; assuming Plivo semantics
is the main way this integration breaks.

Protocol facts this file is built on (all verified against Vobiz's own
docs, its sample repos, and Plivo's published schemas):

  * Vobiz NEVER sends an inbound `stop` event. The WebSocket close is
    the only in-band end-of-stream signal. `stop` is outbound-only.
  * `playedStream.name` is TOP-LEVEL and there is no streamId on it.
    Acks must be matched by name.
  * Outbound `playAudio` uses the SHORT contentType ("audio/x-mulaw")
    with a separate integer `sampleRate` -- NOT the ";rate=8000" form,
    which is only valid as a <Stream> XML attribute.
  * <Stream contentType> defaults to "audio/x-l16;rate=8000". Leaving it
    unset silently puts us on the L16 path, where Vobiz's inbound payload
    is big-endian and outbound must be little-endian. We set mu-law
    explicitly: it is byte-oriented and endian-free.
  * bidirectional="true" REQUIRES audioTrack to be "inbound" (or absent).
    "both"/"outbound" cause the call to hang up with "End Of XML
    Instructions".
  * A maxRetries reconnect issues a NEW streamId on the SAME callId.
"""

from __future__ import annotations

import os
import hmac
import base64
import hashlib
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse
from xml.sax.saxutils import quoteattr

import httpx

from ..types import (
    CallContext, Event,
    StreamStartEvent, MediaEvent,
    PlaybackMarkEvent, AudioClearedEvent, DtmfEvent,
)
from ..log import get_logger
from .base import Carrier, CarrierSession, OriginateResult

logger = get_logger("shuo.carrier.vobiz")


# 20ms of 8kHz mu-law. Vobiz emits one media frame per 20ms, ~50/second.
MULAW_CONTENT_TYPE = "audio/x-mulaw"
MULAW_SAMPLE_RATE = 8000
STREAM_CONTENT_TYPE = "audio/x-mulaw;rate=8000"

# Signature headers. Note Vobiz spells the main-account variant "MA"
# where Plivo spells it "Ma" -- header lookup must be case-insensitive.
SIG_V3 = "X-Vobiz-Signature-V3"
SIG_V3_NONCE = "X-Vobiz-Signature-V3-Nonce"
SIG_MA_V3 = "X-Vobiz-Signature-MA-V3"
SIG_V2 = "X-Vobiz-Signature-V2"
SIG_V2_NONCE = "X-Vobiz-Signature-V2-Nonce"
SIG_MA_V2 = "X-Vobiz-Signature-MA-V2"


class VobizSession(CarrierSession):
    """One Vobiz <Stream> WebSocket."""

    def _parse_one(self, data: Dict[str, Any]) -> Optional[Event]:
        event_type = data.get("event")

        if event_type == "start":
            start = data.get("start", {}) or {}
            # IDs are nested under `start`. Two of Vobiz's own sample
            # repos read them at the top level instead, and a third
            # hedges with `message.get(...) or start.get(...)`. We hedge
            # the same way: nested is documented, top-level is what some
            # samples observe.
            stream_id = start.get("streamId") or data.get("streamId")
            call_id = start.get("callId") or data.get("callId")
            if not stream_id:
                logger.warning(f"Vobiz `start` with no streamId: {data}")
                return None
            self._check_media_format(start.get("mediaFormat") or {})
            return StreamStartEvent(stream_sid=stream_id, call_id=call_id)

        if event_type == "media":
            media = data.get("media", {}) or {}
            payload = media.get("payload") or ""
            if not payload:
                # Every documented shape nests the payload under `media`,
                # but that shape has never been confirmed against a live
                # Vobiz call. A frame we cannot find audio in is dropped
                # silently otherwise, and the symptom -- an agent that
                # never hears anything -- points nowhere near here.
                self._warn_once(
                    "media-no-payload",
                    f"Vobiz `media` frame carried no media.payload; keys seen: "
                    f"top-level={sorted(data)} media={sorted(media)}",
                )
                return None
            return MediaEvent(
                audio_bytes=self.decode_payload(payload),
                track=media.get("track") or "inbound",
            )

        if event_type == "playedStream":
            # `name` is TOP-LEVEL on Vobiz (Plivo nests nothing but does
            # include streamId and sequenceNumber; Vobiz drops both).
            name = data.get("name")
            if name:
                return PlaybackMarkEvent(name=str(name))
            logger.warning(f"Vobiz playedStream with no name: {data}")
            return None

        if event_type == "clearedAudio":
            return AudioClearedEvent()

        if event_type == "dtmf":
            # Undocumented on Vobiz. Handled defensively in the Plivo
            # shape, which is what Vobiz's own pipecat serializer assumes.
            dtmf = data.get("dtmf", {}) or {}
            digit = dtmf.get("digit")
            if digit:
                return DtmfEvent(digit=str(digit))
            return None

        if event_type == "stop":
            # Vobiz does not send this -- `stop` is outbound-only, and
            # every protocol page says the socket close is the only
            # end-of-stream signal. One doc page contradicts this, so
            # accept it if it ever shows up rather than stalling the call.
            from ..types import StreamStopEvent
            logger.info("Received an inbound `stop` from Vobiz (undocumented but handled)")
            return StreamStopEvent()

        # Once per event type, at WARNING: an event name we do not handle
        # is how "the carrier is streaming audio under a name we never
        # look for" presents, and at DEBUG that is invisible at the
        # default log level.
        self._warn_once(
            f"unknown-event:{event_type}",
            f"Ignoring unknown Vobiz event {event_type!r}; keys={sorted(data)}",
        )
        return None

    def _warn_once(self, key: str, message: str) -> None:
        """Log a protocol surprise once per call, not 50 times a second."""
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message)

    def _check_media_format(self, media_format: Dict[str, Any]) -> None:
        """
        Assert the carrier is actually sending what we asked for.

        The whole pipeline downstream assumes 8kHz mu-law. If Vobiz ever
        ignores our <Stream contentType> and sends L16, the bytes still
        arrive, still base64-decode, and still reach STT -- as garbage.
        There is no error anywhere; you just get a transcript of noise, or
        chipmunk audio from a 2x rate mismatch. So say it loudly, once.
        """
        encoding = media_format.get("encoding")
        sample_rate = media_format.get("sampleRate")
        if encoding is None and sample_rate is None:
            return  # nothing declared; nothing to check

        if encoding != MULAW_CONTENT_TYPE or sample_rate != MULAW_SAMPLE_RATE:
            logger.error(
                f"Vobiz negotiated {encoding!r}@{sample_rate!r} but this pipeline "
                f"assumes {MULAW_CONTENT_TYPE!r}@{MULAW_SAMPLE_RATE}. Audio will be "
                f"decoded incorrectly with NO further error -- check the <Stream> "
                f"contentType attribute (its default is audio/x-l16;rate=8000)."
            )

    # ── Outbound frames ─────────────────────────────────────────────

    def _play_audio_frame(self, payload_b64: str) -> Dict[str, Any]:
        return {
            "event": "playAudio",
            "streamId": self.stream_id,
            "media": {
                # SHORT content type + separate integer sample rate.
                # The ";rate=8000" form is <Stream>-attribute-only and is
                # rejected here.
                "contentType": MULAW_CONTENT_TYPE,
                "sampleRate": MULAW_SAMPLE_RATE,
                "payload": payload_b64,
            },
        }

    def _clear_audio_frame(self) -> Dict[str, Any]:
        return {"event": "clearAudio", "streamId": self.stream_id}

    def _checkpoint_frame(self, name: str) -> Dict[str, Any]:
        return {"event": "checkpoint", "streamId": self.stream_id, "name": name}

    def _stop_frame(self) -> Optional[Dict[str, Any]]:
        return {"event": "stop", "streamId": self.stream_id}


class VobizCarrier(Carrier):
    """Vobiz account-level operations."""

    name = "vobiz"

    def __init__(self) -> None:
        self._base_url = os.getenv("VOBIZ_BASE_URL", "https://api.vobiz.ai/api/v1").rstrip("/")
        self._auth_id = os.getenv("VOBIZ_AUTH_ID", "")
        self._auth_token = os.getenv("VOBIZ_AUTH_TOKEN", "")
        self._parent_auth_token = os.getenv("VOBIZ_PARENT_AUTH_TOKEN", "")
        self._from_number = os.getenv("VOBIZ_PHONE_NUMBER", "")
        # "rest_stereo" is the only route to dual-channel: the XML
        # <Record> element has no channel attribute on Vobiz.
        self._recording_mode = os.getenv("VOBIZ_RECORDING_MODE", "rest_stereo").strip().lower()
        self._recording_format = os.getenv("VOBIZ_RECORDING_FORMAT", "wav").strip().lower()
        self._recording_time_limit = int(os.getenv("VOBIZ_RECORDING_TIME_LIMIT", "3600"))
        self._stream_timeout = int(os.getenv("VOBIZ_STREAM_TIMEOUT", "3600"))

    # ── URLs ────────────────────────────────────────────────────────

    @property
    def _account_url(self) -> str:
        """
        Path casing is significant -- `Account`, `Call`, `Record` are all
        capitalised.

        Diagnosing failures: **401 means credentials, full stop.** Vobiz
        runs a global auth gate before routing, so a bad token returns an
        identical 401 on a correct path, a nonexistent path, and the bare
        root -- it carries zero information about casing. **404 is the
        path-casing / trailing-slash symptom**, and only once credentials
        are valid.
        """
        return f"{self._base_url}/Account/{self._auth_id}"

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Auth-ID": self._auth_id,
            "X-Auth-Token": self._auth_token,
            "Content-Type": "application/json",
        }

    def _require_credentials(self) -> None:
        if not self._auth_id or not self._auth_token:
            raise ValueError(
                "Missing Vobiz credentials: set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN"
            )

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
        VobizXML connecting the call to our WebSocket.

        <Record/> must be a SELF-CLOSING sibling placed BEFORE <Stream>,
        with redirect="false" so the recording callback does not interrupt
        the streaming flow. This is Vobiz's documented pattern for
        recording and streaming simultaneously.

        Note the XML <Record> element has NO channel/stereo attribute on
        Vobiz -- this element yields a MONO session recording. Stereo is
        only reachable through the REST Record API (start_recording).
        """
        parts: List[str] = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]

        if record and self._recording_mode == "xml":
            record_attrs = [
                'recordSession="true"',
                # Do not let the recording callback redirect call flow.
                'redirect="false"',
                'playBeep="false"',
                f'maxLength="{self._recording_time_limit}"',
                f'fileFormat="{self._recording_format}"',
            ]
            if recording_callback_url:
                record_attrs.append(f"callbackUrl={quoteattr(recording_callback_url)}")
                record_attrs.append('callbackMethod="POST"')
            parts.append(f"  <Record {' '.join(record_attrs)}/>")

        stream_attrs = [
            # keepCallAlive requires bidirectional; without it the call
            # hangs up the moment the XML completes.
            'bidirectional="true"',
            'keepCallAlive="true"',
            # Must be explicit: the default is audio/x-l16;rate=8000,
            # which drops us onto the endianness-trap path.
            f'contentType="{STREAM_CONTENT_TYPE}"',
            # bidirectional="true" requires inbound; "both"/"outbound"
            # cause an immediate hangup.
            'audioTrack="inbound"',
            # Default is 86400 (24h). Bounds a socket that gets stuck.
            f'streamTimeout="{self._stream_timeout}"',
            # Explicitly 0: a retry is documented to replay `start` with a
            # NEW streamId, which silently voids every pending checkpoint.
            'maxRetries="0"',
        ]
        if status_callback_url:
            stream_attrs.append(f"statusCallbackUrl={quoteattr(status_callback_url)}")
            stream_attrs.append('statusCallbackMethod="POST"')

        # The WebSocket URL is the element's TEXT CONTENT, not an attribute.
        parts.append(f"  <Stream {' '.join(stream_attrs)}>{_escape_text(ws_url)}</Stream>")
        parts.append("</Response>")
        return "\n".join(parts)

    # ── Origination ─────────────────────────────────────────────────

    async def originate(
        self,
        to_number: str,
        *,
        answer_url: str,
        persona_id: str = "default",
        record: bool = False,
    ) -> OriginateResult:
        self._require_credentials()
        if not self._from_number:
            raise ValueError("Missing VOBIZ_PHONE_NUMBER")

        # E.164 goes in JSON bodies with a literal '+' (only URL *paths*
        # need %2B).
        body: Dict[str, Any] = {
            "from": self._from_number,
            "to": to_number,
            "answer_url": answer_url,
            "answer_method": "POST",
        }

        # Answering-machine detection is OFF by default and stays off:
        # machine_detection_initial_silence defaults to 4500ms, which
        # alone blows the entire first-turn latency budget.

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._account_url}/Call/",
                headers=self._headers(),
                json=body,
            )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Vobiz originate failed ({resp.status_code}): {resp.text[:400]}"
            )

        data = resp.json() if resp.content else {}
        # Vobiz/Plivo return request_uuid at create time; the call_uuid
        # only becomes known later (and arrives on the `start` frame).
        call_id = (
            data.get("request_uuid")
            or data.get("call_uuid")
            or data.get("callUuid")
            or ""
        )
        logger.info(f"Vobiz call queued: {call_id or '(no id returned)'}")
        return OriginateResult(call_id=call_id, raw=data)

    async def hangup(self, call_id: str) -> None:
        """
        End a live call.

        Always send the WebSocket `stop` frame first (CarrierSession
        .send_stop) -- hanging up over REST while the socket is live can
        trigger a phantom reconnect that overwrites the transcript.

        NOTE the missing trailing slash. The slash is a resource
        discriminator here, not cosmetic: `/Call/{uuid}` carries
        get-live-call and hangup-call, while `/Call/{uuid}/` is the
        *queued-call* resource and defines no DELETE. Vobiz's own
        narrative doc page shows the slashed form; the OpenAPI spec and
        the SDK do not. (Contrast `/Call/` and `/Record/`, which both
        DO require the slash.)

        Failures raise. A hangup that silently no-ops leaves a billed
        call running.
        """
        self._require_credentials()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{self._account_url}/Call/{call_id}",
                headers=self._headers(),
            )
        # 204 No Content on success -- do not JSON-decode the body.
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Vobiz hangup failed for {call_id} ({resp.status_code}): "
                f"{resp.text[:300]}"
            )

    # ── Recording ───────────────────────────────────────────────────

    async def start_recording(
        self, call_id: str, *, callback_url: Optional[str] = None
    ) -> Optional[str]:
        """
        Attempt a DUAL-CHANNEL (stereo) recording on a live call.

        The XML <Record> element cannot do this: Vobiz documents 15
        attributes on it and none is a channel selector. If stereo is
        reachable at all it is through this REST sub-resource, via
        `record_channel_type="stereo"` -- the literal is "stereo", not
        Twilio's "dual", and the default is "mono", so omitting it
        silently yields a single mixed channel.

        🔴 THREE UNRESOLVED UNKNOWNS, in the order they would bite. None
        is guesswork we can code around; each needs one live call.

        R0. Whether this endpoint exists on Vobiz at all. Research found
            Vobiz's Recordings API documenting only list / retrieve /
            download / export / delete -- NOT start-recording -- while a
            separate source specifies this exact endpoint with a full
            parameter table. The two contradict each other. A 404 here
            settles it.
        R1. Whether a recording captures audio injected via `playAudio`.
            Undocumented by both Vobiz and Plivo.
        R2. What "stereo" means with no B-leg. Vobiz documents it as
            separating "caller and callee" / "each leg". A <Stream> agent
            has ONE PSTN leg; the agent is a WebSocket. The second
            channel may simply be silent.

        Failure is logged and swallowed -- losing a recording must never
        drop a live call. Check the log on the first call rather than
        assuming this worked.
        """
        self._require_credentials()
        if not call_id:
            logger.warning("start_recording called without a call_id")
            return None

        body: Dict[str, Any] = {
            "record_channel_type": "stereo",
            # WAV is losslessly splittable into L/R. MP3 stereo is
            # joint-stereo and lossy across channels, which corrupts
            # per-channel ASR.
            "file_format": self._recording_format,
            # The API default is 60 SECONDS -- far too short for a call.
            "time_limit": self._recording_time_limit,
        }
        if callback_url:
            body["callback_url"] = callback_url
            # `callback_method` is documented on the prose page but absent
            # from the OpenAPI spec and the SDK. POST is the default
            # anyway, so sending it buys nothing and risks a 400 on a
            # strict validator.

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._account_url}/Call/{call_id}/Record/",
                    headers=self._headers(),
                    json=body,
                )
        except Exception as e:
            logger.error(f"Vobiz start_recording request failed: {e}")
            return None

        if resp.status_code >= 400:
            if resp.status_code == 404:
                logger.error(
                    "Vobiz start_recording returned 404 -- this endpoint may not "
                    "exist on Vobiz (unknown R0). Dual-channel recording is "
                    "UNAVAILABLE; fall back to muxing the agent channel from our "
                    "own TTS buffer. Note 401 would mean credentials, not path."
                )
            else:
                logger.error(
                    f"Vobiz start_recording failed ({resp.status_code}): {resp.text[:300]}"
                )
            return None

        # Vobiz publishes two mutually contradictory 200 examples and no
        # schema. `recording_id` appears in both; `api_id` and `url` do
        # not. Never string-compare `message`.
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        recording_id = data.get("recording_id")
        logger.info(
            f"Recording requested (stereo/{self._recording_format}) id={recording_id}. "
            f"Verify channel separation on the artifact -- see R1/R2."
        )
        return recording_id

    async def stop_recording(self, call_id: str) -> None:
        """Stop recording. Idempotent -- returns 204 with an empty body."""
        self._require_credentials()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{self._account_url}/Call/{call_id}/Record/",
                    headers=self._headers(),
                )
        except Exception as e:
            logger.debug(f"stop_recording failed (call likely already over): {e}")

    @property
    def records_via_rest(self) -> bool:
        """True when recording must be started over REST after the call connects."""
        return self._recording_mode == "rest_stereo"

    # ── Sessions ────────────────────────────────────────────────────

    def new_session(
        self, websocket: Any, context: Optional[CallContext] = None
    ) -> CarrierSession:
        return VobizSession(websocket, self.name, context)

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
        Validate X-Vobiz-Signature-V3 (preferred) or -V2.

            V2:  base64(HMAC-SHA256(auth_token, base_url + nonce))
            V3:  base64(HMAC-SHA256(auth_token, base_url + "." + nonce))

        where base_url is the callback URL with query, params and fragment
        stripped. Neither version signs the POST body.

        ⚠ Vobiz V3 is NOT Plivo V3. Plivo V3 signs the full URL including
        the query string plus sorted POST parameters. Vobiz V3 is
        effectively "Plivo V2 with a period". Do not reuse plivo-python's
        validator here.

        Because the body is unsigned, a valid signature authenticates the
        URL only -- callers should still cross-check CallUUID against
        known call state before acting on a payload.
        """
        if not self._auth_token:
            logger.error("VOBIZ_AUTH_TOKEN is not set; cannot validate webhooks")
            return False

        base_url = _base_url(url)
        keys = [self._auth_token]
        if self._parent_auth_token:
            keys.append(self._parent_auth_token)

        # (signature header, nonce header, separator)
        attempts: List[Tuple[str, str, str]] = [
            (SIG_V3, SIG_V3_NONCE, "."),
            (SIG_MA_V3, SIG_V3_NONCE, "."),
            (SIG_V2, SIG_V2_NONCE, ""),
            (SIG_MA_V2, SIG_V2_NONCE, ""),
        ]

        matched = False
        saw_any_header = False

        for sig_header, nonce_header, sep in attempts:
            received = _header(headers, sig_header)
            nonce = _header(headers, nonce_header)
            if not received or not nonce:
                continue
            saw_any_header = True

            message = f"{base_url}{sep}{nonce}".encode("utf-8")
            for key in keys:
                expected = base64.b64encode(
                    hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()
                ).decode("ascii")
                # Vobiz does not document comma-joined signatures, but
                # Plivo emits them when an account has several active auth
                # tokens. Splitting is a strict superset and costs nothing.
                for candidate in received.split(","):
                    # Accumulate rather than short-circuit so comparison
                    # time does not depend on which candidate matched.
                    matched |= hmac.compare_digest(candidate.strip(), expected)

        if not saw_any_header:
            logger.warning(
                "Vobiz webhook carried no signature headers -- rejecting. "
                f"Headers seen: {sorted(headers)}"
            )
            return False

        if not matched:
            logger.warning(
                f"Vobiz signature mismatch for base_url={base_url!r}. "
                f"If this is the public URL you registered with Vobiz, the "
                f"reconstruction is wrong -- check PUBLIC_URL and any proxy "
                f"rewriting the path."
            )
        return matched


# =============================================================================
# HELPERS
# =============================================================================

def _base_url(url: str) -> str:
    """
    The signed form of a callback URL: scheme + netloc + path, with
    params, query and fragment removed.

    Everything else is used verbatim -- trailing slashes are significant,
    percent-encoding is not normalised, and the host is not lowercased.
    Our reconstruction must match Vobiz's byte for byte.
    """
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup (Vobiz spells the variant 'MA', Plivo 'Ma')."""
    lowered = name.lower()
    for k, v in headers.items():
        if k.lower() == lowered:
            return v
    return None


def _escape_text(text: str) -> str:
    """Escape XML text content (the <Stream> URL carries & in its query)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
