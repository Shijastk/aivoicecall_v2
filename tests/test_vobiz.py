"""
Vobiz carrier: wire format, answer XML, and signature validation.

Every assertion here corresponds to a documented Vobiz behaviour that
differs from Twilio, from Plivo, or from what a reasonable person would
assume. The failure mode for most of them is silence or garbled audio
rather than an exception, which is why they are pinned in tests.

Inbound frames are built with the same VobizProtocol the fake-Vobiz stub
uses, so the test suite and the stub cannot drift apart.
"""

import json
import base64
import xml.etree.ElementTree as ET

import pytest

from shuo.types import (
    StreamStartEvent, MediaEvent,
    PlaybackMarkEvent, AudioClearedEvent, DtmfEvent,
)
from shuo.carrier.vobiz import VobizCarrier, VobizSession, _base_url
from fake_vobiz import VobizProtocol, sign_headers, extract_ws_url, MULAW_SILENCE_FRAME


AUTH_ID = "MA-test-auth-id"
AUTH_TOKEN = "test-auth-token"


@pytest.fixture
def carrier(monkeypatch):
    monkeypatch.setenv("VOBIZ_AUTH_ID", AUTH_ID)
    monkeypatch.setenv("VOBIZ_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("VOBIZ_PHONE_NUMBER", "+911234567890")
    monkeypatch.delenv("VOBIZ_PARENT_AUTH_TOKEN", raising=False)
    return VobizCarrier()


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


@pytest.fixture
def session():
    return VobizSession(FakeWebSocket(), "vobiz")


# =============================================================================
# ANSWER XML
# =============================================================================

class TestAnswerXml:
    def test_is_well_formed(self, carrier):
        xml = carrier.answer_xml("wss://h.test/ws?persona=candidate&direction=outbound")
        ET.fromstring(xml)  # raises if the & is unescaped

    def test_websocket_url_is_element_text_not_an_attribute(self, carrier):
        """Vobiz reads the URL from <Stream>'s text content."""
        url = "wss://h.test/ws?persona=candidate&direction=outbound"
        xml = carrier.answer_xml(url)
        assert extract_ws_url(xml) == url

    def test_content_type_is_explicit_mulaw(self, carrier):
        """
        The <Stream> contentType default is audio/x-l16;rate=8000. Leaving
        it unset silently puts us on the L16 path, where inbound is
        big-endian and outbound must be little-endian -- the single most
        likely silent-corruption bug in this port.
        """
        xml = carrier.answer_xml("wss://h.test/ws")
        stream = ET.fromstring(xml).find(".//Stream")
        assert stream.get("contentType") == "audio/x-mulaw;rate=8000"

    def test_bidirectional_and_keep_call_alive_are_both_set(self, carrier):
        """keepCallAlive requires bidirectional, else the call drops when the XML ends."""
        stream = ET.fromstring(carrier.answer_xml("wss://h.test/ws")).find(".//Stream")
        assert stream.get("bidirectional") == "true"
        assert stream.get("keepCallAlive") == "true"

    def test_audio_track_is_inbound(self, carrier):
        """
        bidirectional="true" requires audioTrack inbound; "both"/"outbound"
        make Vobiz hang up with "End Of XML Instructions".
        """
        stream = ET.fromstring(carrier.answer_xml("wss://h.test/ws")).find(".//Stream")
        assert stream.get("audioTrack") == "inbound"

    def test_status_callback_is_wired_when_given(self, carrier):
        xml = carrier.answer_xml("wss://h.test/ws", status_callback_url="https://h.test/s")
        stream = ET.fromstring(xml).find(".//Stream")
        assert stream.get("statusCallbackUrl") == "https://h.test/s"
        assert stream.get("statusCallbackMethod") == "POST"

    def test_no_xml_record_element_in_rest_stereo_mode(self, carrier):
        """
        Vobiz's XML <Record> element has NO channel attribute, so it can
        only produce mono. Dual-channel comes from the REST Record API,
        which is the default mode -- so no <Record> should be emitted.
        """
        xml = carrier.answer_xml("wss://h.test/ws", record=True)
        assert ET.fromstring(xml).find(".//Record") is None

    def test_xml_record_mode_places_record_before_stream(self, monkeypatch):
        """
        When XML recording IS used, <Record/> must be a self-closing
        sibling placed BEFORE <Stream>, with redirect="false" so the
        recording callback does not interrupt the stream.
        """
        monkeypatch.setenv("VOBIZ_AUTH_ID", AUTH_ID)
        monkeypatch.setenv("VOBIZ_AUTH_TOKEN", AUTH_TOKEN)
        monkeypatch.setenv("VOBIZ_RECORDING_MODE", "xml")
        c = VobizCarrier()

        xml = c.answer_xml("wss://h.test/ws", record=True)
        root = ET.fromstring(xml)
        children = [el.tag for el in root]

        assert children.index("Record") < children.index("Stream")
        record = root.find(".//Record")
        assert record.get("recordSession") == "true"
        assert record.get("redirect") == "false"
        assert len(list(record)) == 0  # self-closing


# =============================================================================
# INBOUND PARSING
# =============================================================================

class TestInboundParsing:
    def test_start_reads_nested_ids(self, session):
        events = session.parse_message(VobizProtocol.start("stream-1", "call-1"))
        assert events == [StreamStartEvent(stream_sid="stream-1", call_id="call-1")]

    def test_start_falls_back_to_top_level_ids(self, session):
        """
        The documented shape nests the ids, but two of Vobiz's own sample
        repos read them at the top level. Hedge rather than lose the call.
        """
        frame = {"event": "start", "streamId": "stream-9", "callId": "call-9", "start": {}}
        events = session.parse_message(frame)
        assert events == [StreamStartEvent(stream_sid="stream-9", call_id="call-9")]

    def test_start_without_stream_id_is_ignored(self, session):
        assert session.parse_message({"event": "start", "start": {}}) == []

    def test_media(self, session):
        session.parse_message(VobizProtocol.start("s1", "c1"))
        events = session.parse_message(
            VobizProtocol.media("s1", b"\xde\xad\xbe\xef", seq=1, chunk=1)
        )
        assert events == [MediaEvent(audio_bytes=b"\xde\xad\xbe\xef", track="inbound")]

    def test_played_stream_name_is_top_level(self, session):
        """
        Vobiz's playedStream is exactly {event, name} -- no streamId, no
        sequenceNumber. Plivo includes both; do not port Plivo's model.
        """
        session.parse_message(VobizProtocol.start("s1", "c1"))
        events = session.parse_message(VobizProtocol.played_stream("turn-4"))
        assert events == [PlaybackMarkEvent(name="turn-4")]

    def test_played_stream_nested_name_is_not_accepted(self, session):
        """Guards against silently 'fixing' this into the Twilio/Plivo shape."""
        session.parse_message(VobizProtocol.start("s1", "c1"))
        events = session.parse_message(
            {"event": "playedStream", "playedStream": {"name": "turn-4"}}
        )
        assert events == []

    def test_cleared_audio(self, session):
        session.parse_message(VobizProtocol.start("s1", "c1"))
        events = session.parse_message(VobizProtocol.cleared_audio("s1", seq=7))
        assert events == [AudioClearedEvent()]

    def test_dtmf_plivo_shape(self, session):
        """Undocumented on Vobiz; handled defensively in the Plivo shape."""
        session.parse_message(VobizProtocol.start("s1", "c1"))
        events = session.parse_message(VobizProtocol.dtmf("s1", "7", seq=3))
        assert events == [DtmfEvent(digit="7")]

    def test_unknown_event_is_ignored(self, session):
        assert session.parse_message({"event": "somethingNew"}) == []


class TestMediaBeforeStart:
    def test_frames_are_buffered_and_replayed(self, session):
        """
        Vobiz publishes no ordering guarantee that media cannot precede
        start. These frames are the top of the caller's first utterance.
        """
        for chunk in range(3):
            assert session.parse_message(
                VobizProtocol.media("s1", bytes([chunk]) * 160, seq=chunk, chunk=chunk)
            ) == []

        events = session.parse_message(VobizProtocol.start("s1", "c1"))
        assert isinstance(events[0], StreamStartEvent)
        assert [e.audio_bytes[0] for e in events[1:]] == [0, 1, 2]


class TestReconnect:
    def test_new_stream_id_same_call_id(self, session):
        """maxRetries retries issue a NEW streamId on the SAME callId."""
        session.parse_message(VobizProtocol.start("stream-1", "call-1"))
        session.parse_message(VobizProtocol.start("stream-2", "call-1"))
        assert session.stream_id == "stream-2"
        assert session.call_id == "call-1"

    @pytest.mark.asyncio
    async def test_outbound_frames_follow_the_new_stream_id(self):
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        s.parse_message(VobizProtocol.start("stream-1", "call-1"))
        s.parse_message(VobizProtocol.start("stream-2", "call-1"))
        await s.play_audio("QUJD")
        assert ws.sent[-1]["streamId"] == "stream-2"


# =============================================================================
# OUTBOUND FRAMES
# =============================================================================

class TestOutboundFrames:
    @pytest.mark.asyncio
    async def test_play_audio_uses_short_content_type_and_int_sample_rate(self):
        """
        THE trap: outbound playAudio uses the SHORT contentType with a
        separate integer sampleRate. The ";rate=8000" form is valid only
        as a <Stream> XML attribute and is rejected here.
        """
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        s.parse_message(VobizProtocol.start("s1", "c1"))
        await s.play_audio("QUJD")

        assert ws.sent[-1] == {
            "event": "playAudio",
            "streamId": "s1",
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate": 8000,
                "payload": "QUJD",
            },
        }
        assert isinstance(ws.sent[-1]["media"]["sampleRate"], int)
        assert ";rate=" not in ws.sent[-1]["media"]["contentType"]

    @pytest.mark.asyncio
    async def test_stream_id_is_sibling_of_media_not_inside_it(self):
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        s.parse_message(VobizProtocol.start("s1", "c1"))
        await s.play_audio("QUJD")
        assert "streamId" in ws.sent[-1]
        assert "streamId" not in ws.sent[-1]["media"]

    @pytest.mark.asyncio
    async def test_clear_audio(self):
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        s.parse_message(VobizProtocol.start("s1", "c1"))
        await s.clear_audio()
        assert ws.sent[-1] == {"event": "clearAudio", "streamId": "s1"}

    @pytest.mark.asyncio
    async def test_checkpoint(self):
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        s.parse_message(VobizProtocol.start("s1", "c1"))
        await s.checkpoint("turn-2")
        assert ws.sent[-1] == {"event": "checkpoint", "streamId": "s1", "name": "turn-2"}

    @pytest.mark.asyncio
    async def test_stop_is_sent_once(self):
        """
        `stop` must reach Vobiz before any REST hangup, or a phantom
        reconnect can overwrite the transcript with empty data.
        """
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        s.parse_message(VobizProtocol.start("s1", "c1"))
        await s.send_stop()
        await s.send_stop()
        assert ws.sent == [{"event": "stop", "streamId": "s1"}]

    @pytest.mark.asyncio
    async def test_stop_is_not_sent_when_the_stream_never_started(self):
        """
        A call that dies before `start` has no streamId, and Vobiz's stop
        frame requires one. Sending {"streamId": null} is meaningless.
        """
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        await s.send_stop()
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_checkpoint_name_round_trips(self):
        """The name we send is echoed back verbatim on playedStream."""
        ws = FakeWebSocket()
        s = VobizSession(ws, "vobiz")
        s.parse_message(VobizProtocol.start("s1", "c1"))
        await s.checkpoint("turn-11")

        ack = VobizProtocol.played_stream(ws.sent[-1]["name"])
        assert s.parse_message(ack) == [PlaybackMarkEvent(name="turn-11")]


# =============================================================================
# SIGNATURE VALIDATION
# =============================================================================

class TestSignature:
    def test_known_good_v2_vector(self, carrier, monkeypatch):
        """
        Published reference vector for the V2 construction, independently
        reproduced: HMAC-SHA256('my_auth_token', 'https://answer.url' + '12345').
        """
        monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "my_auth_token")
        c = VobizCarrier()
        assert c.validate_signature(
            url="https://answer.url",
            headers={
                "X-Vobiz-Signature-V2": "ehV3IKhLysWBxC1sy8INm0qGoQYdYsHwuoKjsX7FsXc=",
                "X-Vobiz-Signature-V2-Nonce": "12345",
            },
            body=b"",
        )

    def test_query_string_is_stripped_before_signing(self, carrier, monkeypatch):
        """
        The signed URL excludes the query string -- so the same signature
        must validate whether or not query params are present.
        """
        monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "my_auth_token")
        c = VobizCarrier()
        assert c.validate_signature(
            url="https://answer.url?persona=candidate&direction=outbound",
            headers={
                "X-Vobiz-Signature-V2": "ehV3IKhLysWBxC1sy8INm0qGoQYdYsHwuoKjsX7FsXc=",
                "X-Vobiz-Signature-V2-Nonce": "12345",
            },
            body=b"",
        )

    def test_v3_uses_a_period_separator(self, carrier):
        """
        Vobiz V3 is NOT Plivo V3. Plivo V3 signs the full URL plus sorted
        POST params; Vobiz V3 is V2 with a literal '.' before the nonce.
        """
        url = "https://h.test/answer"
        assert carrier.validate_signature(
            url=url, headers=sign_headers(url, AUTH_TOKEN, version="v3"), body=b""
        )

    def test_v2_accepted(self, carrier):
        url = "https://h.test/answer"
        assert carrier.validate_signature(
            url=url, headers=sign_headers(url, AUTH_TOKEN, version="v2"), body=b""
        )

    def test_v3_signature_is_not_valid_as_v2(self, carrier):
        """The separator genuinely changes the digest."""
        url = "https://h.test/answer"
        v3 = sign_headers(url, AUTH_TOKEN, version="v3")
        forged = {
            "X-Vobiz-Signature-V2": v3["X-Vobiz-Signature-V3"],
            "X-Vobiz-Signature-V2-Nonce": v3["X-Vobiz-Signature-V3-Nonce"],
        }
        assert not carrier.validate_signature(url=url, headers=forged, body=b"")

    def test_missing_headers_are_rejected(self, carrier):
        """Absent signature is a failure, not a reason to skip the check."""
        assert not carrier.validate_signature(
            url="https://h.test/answer", headers={}, body=b""
        )

    def test_wrong_token_rejected(self, carrier):
        url = "https://h.test/answer"
        assert not carrier.validate_signature(
            url=url, headers=sign_headers(url, "not-the-token"), body=b""
        )

    def test_tampered_url_rejected(self, carrier):
        headers = sign_headers("https://h.test/answer", AUTH_TOKEN)
        assert not carrier.validate_signature(
            url="https://h.test/evil", headers=headers, body=b""
        )

    def test_header_lookup_is_case_insensitive(self, carrier):
        """
        Vobiz spells the main-account variant 'MA' where Plivo spells it
        'Ma', and HTTP headers arrive in arbitrary case.
        """
        url = "https://h.test/answer"
        signed = sign_headers(url, AUTH_TOKEN, version="v3")
        lowered = {k.lower(): v for k, v in signed.items()}
        assert carrier.validate_signature(url=url, headers=lowered, body=b"")

    def test_comma_separated_signatures(self, carrier):
        """
        Plivo emits comma-joined signatures when an account has several
        active auth tokens. Undocumented on Vobiz, but splitting is a
        strict superset of the documented single-value behaviour.
        """
        url = "https://h.test/answer"
        signed = sign_headers(url, AUTH_TOKEN, version="v3")
        signed["X-Vobiz-Signature-V3"] = f"someOtherSig,{signed['X-Vobiz-Signature-V3']}"
        assert carrier.validate_signature(url=url, headers=signed, body=b"")

    def test_main_account_header_with_parent_token(self, monkeypatch):
        """Sub-account callbacks sign MA-* with the PARENT account token."""
        monkeypatch.setenv("VOBIZ_AUTH_ID", AUTH_ID)
        monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "sub-token")
        monkeypatch.setenv("VOBIZ_PARENT_AUTH_TOKEN", "parent-token")
        c = VobizCarrier()

        url = "https://h.test/answer"
        signed = sign_headers(url, "parent-token", version="v3")
        headers = {
            "X-Vobiz-Signature-MA-V3": signed["X-Vobiz-Signature-V3"],
            "X-Vobiz-Signature-V3-Nonce": signed["X-Vobiz-Signature-V3-Nonce"],
        }
        assert c.validate_signature(url=url, headers=headers, body=b"")

    def test_no_token_configured_rejects(self, monkeypatch):
        monkeypatch.setenv("VOBIZ_AUTH_ID", AUTH_ID)
        monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "")
        c = VobizCarrier()
        url = "https://h.test/answer"
        assert not c.validate_signature(
            url=url, headers=sign_headers(url, AUTH_TOKEN), body=b""
        )


class TestBaseUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://h.test/answer?a=1&b=2", "https://h.test/answer"),
        ("https://h.test/answer", "https://h.test/answer"),
        ("https://h.test/answer/", "https://h.test/answer/"),
        ("https://h.test:8443/answer", "https://h.test:8443/answer"),
        ("https://h.test/answer#frag", "https://h.test/answer"),
    ])
    def test_strips_only_query_params_and_fragment(self, url, expected):
        """
        Trailing slashes are significant, percent-encoding is not
        normalised, and the host is not lowercased -- our reconstruction
        has to match Vobiz's byte for byte.
        """
        assert _base_url(url) == expected


# =============================================================================
# ACCOUNT URLS
# =============================================================================

class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload else "")
        self.content = self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeAsyncClient:
    """Captures the HTTP calls the carrier makes."""

    calls = []
    next_response = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _record(self, method, url, headers=None, json=None):
        FakeAsyncClient.calls.append({"method": method, "url": url, "json": json})
        return FakeAsyncClient.next_response or FakeResponse(204)

    async def post(self, url, headers=None, json=None):
        return await self._record("POST", url, headers, json)

    async def delete(self, url, headers=None, json=None):
        return await self._record("DELETE", url, headers, json)


@pytest.fixture
def fake_http(monkeypatch):
    import shuo.carrier.vobiz as v
    FakeAsyncClient.calls = []
    FakeAsyncClient.next_response = None
    monkeypatch.setattr(v.httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


class TestRestSemantics:
    """
    The trailing slash is a resource discriminator on Vobiz, not cosmetic.
    Getting it backwards on hangup leaves a billed call running.
    """

    @pytest.mark.asyncio
    async def test_hangup_has_no_trailing_slash(self, carrier, fake_http):
        await carrier.hangup("call-1")
        url = fake_http.calls[-1]["url"]
        assert url.endswith("/Call/call-1")
        assert not url.endswith("/")

    @pytest.mark.asyncio
    async def test_hangup_raises_rather_than_swallowing(self, carrier, fake_http):
        """A hangup that silently no-ops leaves a billed call running."""
        fake_http.next_response = FakeResponse(404, text="not found")
        with pytest.raises(RuntimeError, match="hangup failed"):
            await carrier.hangup("call-1")

    @pytest.mark.asyncio
    async def test_originate_posts_to_call_with_trailing_slash(self, carrier, fake_http):
        fake_http.next_response = FakeResponse(
            200, {"api_id": "a", "message": "Call fired", "request_uuid": "req-1"}
        )
        result = await carrier.originate("+919876543210", answer_url="https://h.test/answer")

        call = fake_http.calls[-1]
        assert call["url"].endswith("/Call/")
        assert result.call_id == "req-1"

    @pytest.mark.asyncio
    async def test_originate_sends_answer_method_explicitly(self, carrier, fake_http):
        """The OpenAPI schema and the SDK both require it, unlike the prose docs."""
        fake_http.next_response = FakeResponse(200, {"request_uuid": "req-1"})
        await carrier.originate("+919876543210", answer_url="https://h.test/answer")
        assert fake_http.calls[-1]["json"]["answer_method"] == "POST"

    @pytest.mark.asyncio
    async def test_originate_never_sends_machine_detection(self, carrier, fake_http):
        """
        AMD off means the key is ABSENT. It is a string-typed field, so a
        JSON boolean would either 400 or silently enable AMD -- whose
        initial-silence floor alone blows the first-turn budget.
        """
        fake_http.next_response = FakeResponse(200, {"request_uuid": "req-1"})
        await carrier.originate("+919876543210", answer_url="https://h.test/answer")
        body = fake_http.calls[-1]["json"]
        assert not any(k.startswith("machine_detection") for k in body)

    @pytest.mark.asyncio
    async def test_start_recording_requests_stereo(self, carrier, fake_http):
        fake_http.next_response = FakeResponse(200, {"recording_id": "rec-1"})
        rec = await carrier.start_recording("call-1", callback_url="https://h.test/rec")

        call = fake_http.calls[-1]
        assert call["url"].endswith("/Call/call-1/Record/")  # slash REQUIRED here
        assert call["json"]["record_channel_type"] == "stereo"
        assert call["json"]["file_format"] == "wav"
        # REST default is 60 SECONDS -- every recording would truncate at 1 min.
        assert call["json"]["time_limit"] == 3600
        # Doc-only field, absent from the OpenAPI spec: do not send it.
        assert "callback_method" not in call["json"]
        assert rec == "rec-1"

    @pytest.mark.asyncio
    async def test_recording_failure_returns_none_not_raises(self, carrier, fake_http):
        """Losing a recording must never drop a live call."""
        fake_http.next_response = FakeResponse(404, text="no such endpoint")
        assert await carrier.start_recording("call-1") is None

    @pytest.mark.asyncio
    async def test_404_produces_the_R0_diagnostic(self, carrier, fake_http, caplog):
        """
        R0 is 'does POST .../Record/ exist on Vobiz at all' -- two research
        sources contradict each other. A 404 is the answer, and this log
        line is what the operator reads during the live call, so it must
        say what it means rather than surfacing as a bare stack trace.
        """
        import logging
        fake_http.next_response = FakeResponse(404, text="not found")

        with caplog.at_level(logging.ERROR):
            result = await carrier.start_recording("call-1")

        assert result is None
        msg = " ".join(r.message for r in caplog.records)
        assert "404" in msg
        assert "may not exist" in msg
        assert "UNAVAILABLE" in msg

    @pytest.mark.asyncio
    async def test_transport_error_is_contained(self, carrier, monkeypatch):
        """
        A DNS failure or timeout must not escape as an unretrieved task
        exception -- that would bury the R0 signal in asyncio noise.
        """
        import shuo.carrier.vobiz as v

        class ExplodingClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *a, **kw):
                raise OSError("name resolution failed")

        monkeypatch.setattr(v.httpx, "AsyncClient", ExplodingClient)
        assert await carrier.start_recording("call-1") is None


class TestMediaFormatGuard:
    def test_mismatched_format_is_reported(self, session, caplog):
        """
        If Vobiz ignores our contentType and sends L16, the bytes still
        decode and still reach STT -- as garbage, with no error anywhere.
        """
        frame = VobizProtocol.start("s1", "c1")
        frame["start"]["mediaFormat"] = {"encoding": "audio/x-l16", "sampleRate": 16000}

        import logging
        with caplog.at_level(logging.ERROR):
            session.parse_message(frame)
        assert any("assumes" in r.message for r in caplog.records)

    def test_matching_format_is_silent(self, session, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            session.parse_message(VobizProtocol.start("s1", "c1"))
        assert not caplog.records


class TestUrls:
    def test_account_url_preserves_path_casing(self, carrier):
        """
        401 means credentials, full stop -- Vobiz's auth gate runs before
        routing, so it says nothing about casing. 404 is the path symptom.
        """
        assert carrier._account_url.endswith(f"/Account/{AUTH_ID}")
        assert "/account/" not in carrier._account_url

    def test_auth_headers(self, carrier):
        h = carrier._headers()
        assert h["X-Auth-ID"] == AUTH_ID
        assert h["X-Auth-Token"] == AUTH_TOKEN

    def test_records_via_rest_by_default(self, carrier):
        """Stereo is unreachable from XML, so REST is the default path."""
        assert carrier.records_via_rest is True
