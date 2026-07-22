"""
End-to-end transport test: a fake Vobiz drives the real FastAPI app.

This is the test that proves Phase 1 actually works. It exercises the
answer URL, signature validation, the WebSocket reader, the session
parser, the pure state machine and the dispatch loop together -- with no
Vobiz account and no phone call.

The STT and TTS stages are stubbed because they reach the network; the
transport under test does not.
"""

import json
import asyncio

import pytest
from fastapi.testclient import TestClient

from starlette.websockets import WebSocketDisconnect
from fake_vobiz import VobizProtocol, sign_headers, extract_ws_url, MULAW_SILENCE_FRAME


AUTH_ID = "MA-test-auth-id"
AUTH_TOKEN = "test-auth-token"
PUBLIC_URL = "https://shuo.test"


# =============================================================================
# STUBS FOR THE NETWORK-BOUND STAGES
# =============================================================================

class StubFlux:
    """Stands in for Deepgram Flux. Records the audio it was fed."""

    instances = []

    def __init__(self, on_end_of_turn=None, on_start_of_turn=None):
        self.fed = []
        self.started = False
        self.stopped = False
        self.on_end_of_turn = on_end_of_turn
        StubFlux.instances.append(self)

    async def start(self):
        self.started = True

    async def send(self, audio_bytes):
        self.fed.append(audio_bytes)

    async def stop(self):
        self.started = False
        self.stopped = True


class StubTTSPool:
    instances = []

    def __init__(self, *a, **kw):
        self.stopped = False
        StubTTSPool.instances.append(self)

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    async def get(self, **kw):
        raise AssertionError("TTS should not be reached in a transport test")


def ws_path(persona="candidate", direction="outbound", ttl=300):
    """
    A signed /ws URL, as config.websocket_url would mint into the answer
    XML. /ws rejects anything unsigned, so tests must sign too.
    """
    import time
    from shuo import config

    exp = int(time.time()) + ttl
    token = config.mint_stream_token(
        persona_id=persona, direction=direction, expires_at=exp
    )
    return f"/ws?persona={persona}&direction={direction}&exp={exp}&token={token}"


def wait_for(predicate, timeout=3.0, interval=0.02):
    """
    Poll until `predicate()` is true or the timeout expires.

    Teardown happens on the server's event loop, which runs in the
    TestClient's portal thread, so it is not complete the instant the
    `with` block exits.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("CARRIER", "vobiz")
    monkeypatch.setenv("VOBIZ_AUTH_ID", AUTH_ID)
    monkeypatch.setenv("VOBIZ_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("VOBIZ_PHONE_NUMBER", "+911234567890")
    monkeypatch.setenv("PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("PERSONA", "candidate")
    # The LLM client is constructed eagerly when the Agent is built; it
    # needs a key to exist, but is never called in a transport test.
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
    # Recording would make a live HTTP call to Vobiz.
    monkeypatch.setenv("RECORD_CALLS", "false")
    monkeypatch.setenv("VALIDATE_WEBHOOK_SIGNATURES", "true")

    import shuo.conversation as conv
    from shuo.carrier import reset_carrier_cache

    reset_carrier_cache()
    StubFlux.instances.clear()
    StubTTSPool.instances.clear()
    monkeypatch.setattr(conv, "FluxService", StubFlux)
    monkeypatch.setattr(conv, "TTSPool", StubTTSPool)

    from shuo.server import app
    yield app
    reset_carrier_cache()


@pytest.fixture
def client(app_env):
    return TestClient(app_env)


# =============================================================================
# ANSWER URL
# =============================================================================

class TestAnswerEndpoint:
    def test_signed_request_returns_stream_xml(self, client):
        url = f"{PUBLIC_URL}/answer?persona=candidate&direction=outbound"
        resp = client.post(
            "/answer?persona=candidate&direction=outbound",
            data={"From": "+911234567890", "To": "+919876543210", "CallUUID": "c1"},
            headers=sign_headers(url, AUTH_TOKEN, version="v3"),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/xml")

        ws_url = extract_ws_url(resp.text)
        assert ws_url.startswith("wss://shuo.test/ws")
        assert "persona=candidate" in ws_url

    def test_unsigned_request_is_rejected(self, client):
        """A missing signature is a failure, not a reason to skip the check."""
        resp = client.post("/answer", data={"From": "+91", "To": "+91"})
        assert resp.status_code == 403

    def test_badly_signed_request_is_rejected(self, client):
        url = f"{PUBLIC_URL}/answer"
        resp = client.post(
            "/answer",
            data={"From": "+91"},
            headers=sign_headers(url, "wrong-token", version="v3"),
        )
        assert resp.status_code == 403

    def test_inbound_persona_routes_on_dialled_number(self, client, monkeypatch):
        """One DID per Digital Twin role, resolved without a code change."""
        monkeypatch.setenv("PERSONA_ROUTES", "+919876543210:receptionist")
        url = f"{PUBLIC_URL}/answer?direction=inbound"
        resp = client.post(
            "/answer?direction=inbound",
            data={"From": "+911111111111", "To": "+919876543210", "Direction": "inbound"},
            headers=sign_headers(url, AUTH_TOKEN, version="v3"),
        )
        assert resp.status_code == 200
        assert "persona=receptionist" in extract_ws_url(resp.text)

    def test_explicit_persona_query_wins(self, client, monkeypatch):
        monkeypatch.setenv("PERSONA_ROUTES", "+919876543210:receptionist")
        url = f"{PUBLIC_URL}/answer?persona=candidate&direction=outbound"
        resp = client.post(
            "/answer?persona=candidate&direction=outbound",
            data={"To": "+919876543210"},
            headers=sign_headers(url, AUTH_TOKEN, version="v3"),
        )
        assert "persona=candidate" in extract_ws_url(resp.text)

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["carrier"] == "vobiz"


# =============================================================================
# MEDIA STREAM
# =============================================================================

class TestMediaStream:
    def test_caller_audio_reaches_the_stt_stage(self, client):
        with client.websocket_connect(ws_path("candidate", "outbound")) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))
            for chunk in range(5):
                ws.send_text(json.dumps(
                    VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=chunk + 1, chunk=chunk)
                ))

        flux = StubFlux.instances[-1]
        assert len(flux.fed) == 5
        assert all(f == MULAW_SILENCE_FRAME for f in flux.fed)

    def test_media_before_start_is_not_lost(self, client):
        """
        The frames ahead of `start` are the top of the caller's first
        utterance. Dropping them truncates the greeting.
        """
        with client.websocket_connect(ws_path()) as ws:
            for chunk in range(3):
                ws.send_text(json.dumps(
                    VobizProtocol.media("s1", bytes([chunk]) * 160, seq=chunk, chunk=chunk)
                ))
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", b"\x09" * 160, seq=9, chunk=9)
            ))

        flux = StubFlux.instances[-1]
        assert [f[0] for f in flux.fed] == [0, 1, 2, 9]

    def test_agent_audio_is_never_transcribed(self, client):
        """
        If the carrier ever forks our own leg back to us, feeding it to
        STT makes the agent transcribe itself and reply to itself.
        """
        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", b"\x01" * 160, seq=1, chunk=1, track="outbound")
            ))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", b"\x02" * 160, seq=2, chunk=2, track="inbound")
            ))

        flux = StubFlux.instances[-1]
        assert [f[0] for f in flux.fed] == [0x02]

    def test_reconnect_keeps_feeding_audio(self, client):
        """A maxRetries retry replays `start` with a new streamId mid-call."""
        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", b"\x01" * 160, seq=1, chunk=1)))
            ws.send_text(json.dumps(VobizProtocol.start("s2", "c1")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s2", b"\x02" * 160, seq=2, chunk=2)))

        flux = StubFlux.instances[-1]
        assert [f[0] for f in flux.fed] == [0x01, 0x02]
        # One Flux instance for the whole call -- a reconnect must not
        # restart the pipeline or drop conversation history.
        assert len(StubFlux.instances) == 1

    def test_playback_ack_and_dtmf_do_not_break_the_loop(self, client):
        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))
            ws.send_text(json.dumps(VobizProtocol.played_stream("turn-1")))
            ws.send_text(json.dumps(VobizProtocol.cleared_audio("s1", seq=2)))
            ws.send_text(json.dumps(VobizProtocol.dtmf("s1", "5", seq=3)))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=4, chunk=1)))

        flux = StubFlux.instances[-1]
        assert len(flux.fed) == 1

    def test_cleanup_runs_after_the_socket_closes(self, client):
        """
        Cleanup ordering check.

        NOTE this does NOT prove the loop terminates on its own: TestClient
        cancels the app task when the `with` block exits, which runs the
        `finally` regardless. The termination invariant itself is tested
        harness-independently in TestCallTermination below.
        """
        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))

        flux = StubFlux.instances[-1]
        pool = StubTTSPool.instances[-1]
        assert wait_for(lambda: flux.stopped), "STT connection was never released"
        assert wait_for(lambda: pool.stopped), "TTS pool was never released"

    def test_active_call_count_returns_to_zero(self, client):
        """Otherwise graceful drain never completes and deploys hang."""
        import shuo.server as server_module

        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))

        assert wait_for(lambda: server_module._active_calls == 0), (
            f"active call count stuck at {server_module._active_calls}"
        )

    def test_malformed_frame_does_not_kill_the_call(self, client):
        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))
            ws.send_text("this is not json")
            ws.send_text(json.dumps({"event": "unknownThing"}))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=2, chunk=1)))

        flux = StubFlux.instances[-1]
        assert len(flux.fed) == 1


# =============================================================================
# RECORDING
# =============================================================================

class TestRecording:
    def test_stereo_recording_starts_when_the_call_id_is_known(self, client, monkeypatch):
        """
        Dual-channel recording cannot be expressed in Vobiz's answer XML,
        so it fires over REST the moment `start` gives us the call id.
        """
        monkeypatch.setenv("RECORD_CALLS", "true")

        calls = []

        async def fake_start_recording(self, call_id, *, callback_url=None):
            calls.append((call_id, callback_url))
            return "rec-1"

        from shuo.carrier.vobiz import VobizCarrier
        monkeypatch.setattr(VobizCarrier, "start_recording", fake_start_recording)

        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "call-xyz")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1)))

        assert calls and calls[0][0] == "call-xyz"
        assert calls[0][1] == f"{PUBLIC_URL}/recording-status"

    def test_recording_is_started_once_across_a_reconnect(self, client, monkeypatch):
        monkeypatch.setenv("RECORD_CALLS", "true")

        calls = []

        async def fake_start_recording(self, call_id, *, callback_url=None):
            calls.append(call_id)
            return "rec-1"

        from shuo.carrier.vobiz import VobizCarrier
        monkeypatch.setattr(VobizCarrier, "start_recording", fake_start_recording)

        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "call-xyz")))
            ws.send_text(json.dumps(VobizProtocol.start("s2", "call-xyz")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s2", MULAW_SILENCE_FRAME, seq=1, chunk=1)))

        assert calls == ["call-xyz"]

    def test_recording_failure_does_not_drop_the_call(self, client, monkeypatch):
        monkeypatch.setenv("RECORD_CALLS", "true")

        raised = []

        async def boom(self, call_id, *, callback_url=None):
            raised.append(call_id)
            raise RuntimeError("vobiz is down")

        from shuo.carrier.vobiz import VobizCarrier
        monkeypatch.setattr(VobizCarrier, "start_recording", boom)

        with client.websocket_connect(ws_path()) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "call-xyz")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1)))

        # Without this the test passes even if recording is never attempted
        # at all, so it would not distinguish "failed and contained" from
        # "feature silently removed".
        assert raised == ["call-xyz"], "the failure path was never entered"
        flux = StubFlux.instances[-1]
        assert len(flux.fed) == 1


# =============================================================================
# CALL TERMINATION (harness-independent)
# =============================================================================

class ClosingWebSocket:
    """
    A WebSocket that yields a fixed set of frames and then disconnects,
    exactly as a carrier does when the caller hangs up.

    Deliberately NOT a TestClient socket: TestClient cancels the app task
    when its context exits, which runs the loop's `finally` even if the
    loop itself is wedged. That masks the failure this class exists to
    expose.
    """

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def receive_text(self):
        if self._frames:
            return json.dumps(self._frames.pop(0))
        raise WebSocketDisconnect(code=1006)

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class TestCallTermination:
    """
    Vobiz NEVER sends an inbound `stop` event -- the WebSocket close is the
    only in-band end-of-stream signal.

    If the disconnect handler fails to queue a StreamStopEvent, the loop
    blocks forever on `event_queue.get()`. Its `finally` never runs, so the
    call leaks its STT socket, its TTS pool and its active-call slot, and
    graceful drain can never complete. Every call on the box leaks.
    """

    @pytest.mark.asyncio
    async def test_loop_exits_when_the_carrier_disconnects(self, app_env):
        import shuo.conversation as conv
        from shuo.carrier import get_carrier
        from shuo.types import CallContext, CallDirection

        ws = ClosingWebSocket([
            VobizProtocol.start("s1", "c1"),
            VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1),
        ])
        ctx = CallContext(call_id="", direction=CallDirection.INBOUND,
                          persona_id="candidate", carrier="vobiz")

        # If the loop wedges, this raises TimeoutError instead of hanging
        # the whole suite.
        await asyncio.wait_for(
            conv.run_conversation(ws, ctx, get_carrier("vobiz")),
            timeout=5.0,
        )

        flux = StubFlux.instances[-1]
        assert flux.fed, "caller audio never reached STT"
        assert flux.stopped, "STT connection was not released"

    @pytest.mark.asyncio
    async def test_disconnect_before_start_also_terminates(self, app_env):
        """A call that drops before `start` must not wedge either."""
        import shuo.conversation as conv
        from shuo.carrier import get_carrier
        from shuo.types import CallContext, CallDirection

        ws = ClosingWebSocket([])
        ctx = CallContext(call_id="", direction=CallDirection.OUTBOUND,
                          persona_id="candidate", carrier="vobiz")

        await asyncio.wait_for(
            conv.run_conversation(ws, ctx, get_carrier("vobiz")),
            timeout=5.0,
        )


# =============================================================================
# OPERATOR ENDPOINT AUTHENTICATION
# =============================================================================

class TestAdminEndpoints:
    """
    PUBLIC_URL is internet-reachable by definition and the server binds
    0.0.0.0. An ungated endpoint here is an ungated endpoint on the
    internet: /call spends money on PSTN minutes, /trace/latest hands out
    the transcript of what the caller said, /bench/ttft burns paid LLM
    credits amplified by `runs`.
    """

    ADMIN = "s3cret-admin-token"

    @pytest.fixture(autouse=True)
    def _admin(self, monkeypatch):
        monkeypatch.setenv("SHUO_ADMIN_TOKEN", self.ADMIN)

    @pytest.mark.parametrize("path", [
        "/call/+919876543210",
        "/trace/latest",
        "/bench/ttft?runs=1",
    ])
    def test_rejected_without_token(self, client, path):
        assert client.get(path).status_code == 403

    @pytest.mark.parametrize("path", [
        "/call/+919876543210",
        "/trace/latest",
        "/bench/ttft?runs=1",
    ])
    def test_rejected_with_wrong_token(self, client, path):
        resp = client.get(path, headers={"X-Shuo-Admin-Token": "wrong"})
        assert resp.status_code == 403

    def test_disabled_when_no_token_is_configured(self, client, monkeypatch):
        """Fails closed: an unset secret must not mean 'no gate'."""
        monkeypatch.delenv("SHUO_ADMIN_TOKEN", raising=False)
        assert client.get("/trace/latest").status_code == 403

    def test_correct_token_passes_the_gate(self, client):
        # No traces exist in a fresh test run, so 404 proves we got past
        # the gate and into the handler.
        resp = client.get("/trace/latest", headers={"X-Shuo-Admin-Token": self.ADMIN})
        assert resp.status_code in (200, 404)

    def test_originate_errors_are_not_echoed_to_the_client(self, client, monkeypatch):
        """The carrier's raw response body must not reach an API caller."""
        async def boom(self, to_number, **kw):
            raise RuntimeError("Vobiz originate failed (401): SECRET-INTERNAL-DETAIL")

        from shuo.carrier.vobiz import VobizCarrier
        monkeypatch.setattr(VobizCarrier, "originate", boom)

        resp = client.get(
            "/call/+919876543210", headers={"X-Shuo-Admin-Token": self.ADMIN}
        )
        assert resp.status_code == 502
        assert "SECRET-INTERNAL-DETAIL" not in resp.text


class TestStreamTokenGate:
    """
    The signature gate on /answer proves nothing about who dials /ws.
    Without a token the media socket is an open door onto the agent, and
    persona is attacker-chosen.
    """

    def test_unsigned_connection_is_refused(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/ws?persona=candidate&direction=outbound"):
                pass

    def test_expired_token_is_refused(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect(ws_path(ttl=-10)):
                pass

    def test_token_does_not_transfer_to_another_persona(self, client):
        """A token minted for one twin must not select a different one."""
        signed = ws_path("candidate", "outbound")
        swapped = signed.replace("persona=candidate", "persona=receptionist")
        with pytest.raises(Exception):
            with client.websocket_connect(swapped):
                pass

    def test_token_does_not_transfer_to_another_direction(self, client):
        signed = ws_path("candidate", "outbound")
        swapped = signed.replace("direction=outbound", "direction=inbound")
        with pytest.raises(Exception):
            with client.websocket_connect(swapped):
                pass

    def test_answer_xml_mints_a_usable_token(self, client):
        """The token in the <Stream> URL must actually open the socket."""
        url = f"{PUBLIC_URL}/answer?persona=candidate&direction=outbound"
        resp = client.post(
            "/answer?persona=candidate&direction=outbound",
            data={"To": "+919876543210"},
            headers=sign_headers(url, AUTH_TOKEN, version="v3"),
        )
        ws_url = extract_ws_url(resp.text)
        path = ws_url.split("shuo.test", 1)[1]

        with client.websocket_connect(path) as ws:
            ws.send_text(json.dumps(VobizProtocol.start("s1", "c1")))
            ws.send_text(json.dumps(
                VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1)))

        assert StubFlux.instances[-1].fed


class TriggeringFlux(StubFlux):
    """Fires an end-of-turn as soon as the first audio frame arrives."""

    async def send(self, audio_bytes):
        await super().send(audio_bytes)
        if len(self.fed) == 1 and self.on_end_of_turn:
            await self.on_end_of_turn("so, tell me about yourself")


class ExplodingAgent:
    """An Agent whose every turn fails, as a dead vendor would."""

    def __init__(self, **kwargs):
        self.cancelled = 0

    async def start_turn(self, transcript):
        raise RuntimeError("LLM provider is down")

    async def cancel_turn(self):
        self.cancelled += 1

    async def cleanup(self):
        pass


class TestTurnFailureContainment:
    @pytest.mark.asyncio
    async def test_a_failing_turn_does_not_end_the_call(self, app_env, monkeypatch):
        """
        A vendor hiccup mid-turn should cost one turn, not hang up on the
        caller. Before the fix, any exception from `agent.start_turn`
        propagated out of the dispatch loop and terminated the call.
        """
        import shuo.conversation as conv
        from shuo.carrier import get_carrier
        from shuo.types import CallContext, CallDirection

        monkeypatch.setattr(conv, "FluxService", TriggeringFlux)
        monkeypatch.setattr(conv, "Agent", ExplodingAgent)

        ws = ClosingWebSocket([
            VobizProtocol.start("s1", "c1"),
            VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1),
            VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=2, chunk=2),
        ])
        ctx = CallContext(call_id="", direction=CallDirection.INBOUND,
                          persona_id="candidate", carrier="vobiz")

        await asyncio.wait_for(
            conv.run_conversation(ws, ctx, get_carrier("vobiz")), timeout=5.0
        )

        flux = StubFlux.instances[-1]
        # The second frame only arrives if the loop survived the failure.
        assert len(flux.fed) == 2, "call died after a single failed turn"
        assert flux.stopped
