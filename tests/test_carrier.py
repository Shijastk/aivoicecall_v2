"""
Carrier-neutral session behaviour, plus the Twilio adapter.

The defensive rules exercised here (media-before-start buffering,
call_id keying, track filtering) live in CarrierSession precisely because
they are the parts every carrier gets wrong in the same way -- so they
are tested once, against the base class, through a concrete subclass.
"""

import json

import pytest

from shuo.types import (
    StreamStartEvent, StreamStopEvent, MediaEvent,
    PlaybackMarkEvent, DtmfEvent,
)
from shuo.carrier.base import MAX_PENDING_MEDIA_FRAMES
from shuo.carrier.twilio import TwilioSession, TwilioCarrier


class FakeWebSocket:
    """Records what the session writes to the wire."""

    def __init__(self):
        self.sent = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def media_frame(payload: bytes = b"\xff" * 160, track: str = "inbound") -> dict:
    return {
        "event": "media",
        "streamSid": "MZ-stream-1",
        "media": {"track": track, "chunk": "1", "timestamp": "20", "payload": b64(payload)},
    }


def start_frame(stream_sid="MZ-stream-1", call_sid="CA-call-1") -> dict:
    return {
        "event": "start",
        "streamSid": stream_sid,
        "start": {
            "streamSid": stream_sid,
            "callSid": call_sid,
            "accountSid": "AC-1",
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }


@pytest.fixture
def session():
    return TwilioSession(FakeWebSocket(), "twilio")


# =============================================================================
# MEDIA BEFORE START
# =============================================================================

class TestMediaBeforeStart:
    def test_media_before_start_is_buffered_not_dropped(self, session):
        """
        Vobiz has been observed sending `media` before `start`. Those
        frames are the top of the caller's first utterance -- losing them
        truncates the greeting.
        """
        assert session.parse_message(media_frame(b"\x01" * 160)) == []
        assert session.parse_message(media_frame(b"\x02" * 160)) == []
        assert session.started is False

    def test_start_drains_buffer_in_arrival_order(self, session):
        session.parse_message(media_frame(b"\x01" * 160))
        session.parse_message(media_frame(b"\x02" * 160))

        events = session.parse_message(start_frame())

        assert len(events) == 3
        assert isinstance(events[0], StreamStartEvent)
        assert [e.audio_bytes[0] for e in events[1:]] == [0x01, 0x02]

    def test_media_after_start_passes_straight_through(self, session):
        session.parse_message(start_frame())
        events = session.parse_message(media_frame())
        assert len(events) == 1
        assert isinstance(events[0], MediaEvent)

    def test_pending_buffer_is_bounded(self, session):
        """A `start` that never arrives must not grow memory without limit."""
        for _ in range(MAX_PENDING_MEDIA_FRAMES + 50):
            session.parse_message(media_frame())
        assert len(session._pending_media) == MAX_PENDING_MEDIA_FRAMES

        events = session.parse_message(start_frame())
        assert len(events) == 1 + MAX_PENDING_MEDIA_FRAMES


# =============================================================================
# IDENTIFIERS
# =============================================================================

class TestIdentifiers:
    def test_start_populates_both_ids(self, session):
        session.parse_message(start_frame())
        assert session.stream_id == "MZ-stream-1"
        assert session.call_id == "CA-call-1"

    def test_reconnect_changes_stream_id_but_not_call_id(self, session):
        session.parse_message(start_frame(stream_sid="MZ-1", call_sid="CA-1"))
        session.parse_message(start_frame(stream_sid="MZ-2", call_sid="CA-1"))
        assert session.stream_id == "MZ-2"
        assert session.call_id == "CA-1"

    def test_outbound_frames_use_the_current_stream_id(self, session):
        session.parse_message(start_frame(stream_sid="MZ-1"))
        session.parse_message(start_frame(stream_sid="MZ-2"))
        frame = session._play_audio_frame("abcd")
        assert frame["streamSid"] == "MZ-2"


# =============================================================================
# TRACK FILTERING
# =============================================================================

class TestTrackFiltering:
    def test_track_is_carried_through(self, session):
        session.parse_message(start_frame())
        events = session.parse_message(media_frame(track="outbound"))
        assert events[0].track == "outbound"

    def test_missing_track_defaults_to_inbound(self, session):
        session.parse_message(start_frame())
        frame = media_frame()
        del frame["media"]["track"]
        events = session.parse_message(frame)
        assert events[0].track == "inbound"


# =============================================================================
# TWILIO PARSING
# =============================================================================

class TestTwilioParsing:
    def test_connected_yields_no_event(self, session):
        assert session.parse_message({"event": "connected", "protocol": "Call"}) == []

    def test_stop(self, session):
        events = session.parse_message({"event": "stop", "streamSid": "MZ-1"})
        assert isinstance(events[0], StreamStopEvent)

    def test_mark_becomes_playback_mark(self, session):
        session.parse_message(start_frame())
        events = session.parse_message(
            {"event": "mark", "streamSid": "MZ-1", "mark": {"name": "turn-3"}}
        )
        assert events == [PlaybackMarkEvent(name="turn-3")]

    def test_dtmf(self, session):
        session.parse_message(start_frame())
        events = session.parse_message(
            {"event": "dtmf", "streamSid": "MZ-1", "dtmf": {"track": "inbound", "digit": "7"}}
        )
        assert events == [DtmfEvent(digit="7")]

    def test_unknown_event_is_ignored(self, session):
        assert session.parse_message({"event": "someNewThing"}) == []

    def test_empty_media_payload_is_ignored(self, session):
        session.parse_message(start_frame())
        frame = media_frame()
        frame["media"]["payload"] = ""
        assert session.parse_message(frame) == []

    def test_audio_is_decoded_from_base64(self, session):
        session.parse_message(start_frame())
        events = session.parse_message(media_frame(b"\xde\xad\xbe\xef"))
        assert events[0].audio_bytes == b"\xde\xad\xbe\xef"


# =============================================================================
# TWILIO OUTBOUND
# =============================================================================

class TestTwilioOutbound:
    @pytest.mark.asyncio
    async def test_play_audio_frame(self):
        ws = FakeWebSocket()
        s = TwilioSession(ws, "twilio")
        s.parse_message(start_frame())
        await s.play_audio("QUJD")
        assert ws.sent[-1] == {
            "event": "media",
            "streamSid": "MZ-stream-1",
            "media": {"payload": "QUJD"},
        }

    @pytest.mark.asyncio
    async def test_clear_audio_frame(self):
        ws = FakeWebSocket()
        s = TwilioSession(ws, "twilio")
        s.parse_message(start_frame())
        await s.clear_audio()
        assert ws.sent[-1] == {"event": "clear", "streamSid": "MZ-stream-1"}

    @pytest.mark.asyncio
    async def test_checkpoint_frame(self):
        ws = FakeWebSocket()
        s = TwilioSession(ws, "twilio")
        s.parse_message(start_frame())
        await s.checkpoint("turn-1")
        assert ws.sent[-1] == {
            "event": "mark",
            "streamSid": "MZ-stream-1",
            "mark": {"name": "turn-1"},
        }

    @pytest.mark.asyncio
    async def test_twilio_has_no_server_initiated_stop(self):
        """Twilio has no stop frame; send_stop must be a silent no-op."""
        ws = FakeWebSocket()
        s = TwilioSession(ws, "twilio")
        s.parse_message(start_frame())
        await s.send_stop()
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_send_survives_a_dead_socket(self):
        """A carrier that hung up mid-write must not raise into the loop."""
        class DeadSocket:
            async def send_text(self, text):
                raise RuntimeError("socket closed")

        s = TwilioSession(DeadSocket(), "twilio")
        s.parse_message(start_frame())
        await s.play_audio("QUJD")  # must not raise


# =============================================================================
# TWILIO ANSWER XML
# =============================================================================

class TestTwilioAnswerXml:
    def test_records_dual_channel_when_asked(self):
        xml = TwilioCarrier().answer_xml("wss://x.test/ws?persona=candidate", record=True)
        assert 'record="record-from-answer-dual"' in xml
        assert "<Stream" in xml

    def test_recording_off(self):
        xml = TwilioCarrier().answer_xml("wss://x.test/ws", record=False)
        assert 'record="do-not-record"' in xml

    def test_url_is_xml_attribute_escaped(self):
        """A query string carries & -- unescaped it makes the XML invalid."""
        import xml.etree.ElementTree as ET

        xml = TwilioCarrier().answer_xml(
            "wss://x.test/ws?persona=candidate&direction=outbound", record=True
        )
        root = ET.fromstring(xml)  # raises if malformed
        stream = root.find(".//Stream")
        assert stream.get("url") == "wss://x.test/ws?persona=candidate&direction=outbound"

    def test_only_inbound_track_is_forked(self):
        """Forking our own audio back would make the agent hear itself."""
        xml = TwilioCarrier().answer_xml("wss://x.test/ws", record=True)
        assert 'track="inbound_track"' in xml
