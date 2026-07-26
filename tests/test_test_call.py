"""
Tests for W3's test-call feature -- both halves of the process split.

    :3041  POST /v1/test-call          -> :3040  POST /call/{number}
    :3041  GET  /v1/test-call/status   -> :3040  GET  /calls/live
    :3041  POST /v1/test-call/hangup   -> :3040  POST /calls/current/hangup

The most important class here is `TestTheTokenBoundary`. The whole security
argument for this feature is that `SHUO_ADMIN_TOKEN` lives on the server side
of the seam: reaching the panel must not be the same thing as being able to
place calls. If that header stops being sent, or starts being optional, the
feature still works perfectly in a demo and is wrong.

`TestTheErrorEnvelope` is second, for the reason `TestErrorEnvelope` is the
most valuable class in test_config_api.py: the panel renders `message`
verbatim, so a lost sentence is a lost diagnosis. On these routes it must
also say "No call was placed" rather than "Nothing was saved", or the panel
reports on the wrong thing entirely.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import shuo.call_client as call_client
import shuo.config_api as config_api
from shuo.call_monitor import MONITOR
from shuo.config_store import ConfigStore


ADMIN_TOKEN = "admin-token-for-tests"


# =============================================================================
# A FAKE :3040
# =============================================================================

class FakeCallServer:
    """
    Stands in for `python main.py`, recording what :3041 sent it.

    Mounted under `call_client`'s pooled client, so the request goes through
    the real client code -- headers, timeouts, query string and all. A test
    that stubbed `place_call` would prove only that the route calls the
    function it calls.
    """

    def __init__(self):
        self.requests = []
        self.status_code = 200
        self.body = {"status": "calling", "to": "+919876543210",
                     "carrier": "vobiz", "persona": "candidate",
                     "call_id": "MZ-1"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def call_server(monkeypatch):
    fake = FakeCallServer()
    monkeypatch.setenv("SHUO_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(
        call_client,
        "_client",
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    call_client.reset_cooldown()
    yield fake
    call_client.reset_cooldown()


@pytest.fixture
def client(call_server, tmp_path, monkeypatch):
    monkeypatch.delenv("SHUO_CONFIG_API_TOKEN", raising=False)
    monkeypatch.setattr(config_api, "store", ConfigStore(tmp_path / "config.json"))
    with TestClient(config_api.app) as test_client:
        yield test_client


# =============================================================================
# PLACING A CALL
# =============================================================================

class TestPlacingACall:
    def test_a_valid_number_reaches_the_call_server(self, client, call_server):
        response = client.post(
            "/v1/test-call", json={"phoneNumber": "+919876543210"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "calling"
        assert call_server.last.url.path == "/call/+919876543210"

    def test_it_is_a_POST_to_the_call_server(self, client, call_server):
        """
        Placing a call is neither safe nor idempotent. The GET form survives
        for the curl runbook; this path uses the honest verb.
        """
        client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})

        assert call_server.last.method == "POST"

    def test_the_persona_travels_with_it(self, client, call_server):
        client.post(
            "/v1/test-call",
            json={"phoneNumber": "+919876543210", "persona": "receptionist"},
        )

        assert "persona=receptionist" in str(call_server.last.url)

    def test_no_persona_means_the_call_servers_default(self, client, call_server):
        client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})

        assert "persona=" not in str(call_server.last.url)

    def test_the_carriers_call_id_comes_back(self, client, call_server):
        response = client.post(
            "/v1/test-call", json={"phoneNumber": "+919876543210"}
        )

        assert response.json()["callId"] == "MZ-1"


class TestTheTokenBoundary:
    """
    The security argument for the whole feature, pinned.

    Mutation to try: delete the header in `call_client._request`. Every other
    test in this file still passes.
    """

    def test_the_admin_token_is_sent_to_the_call_server(self, client, call_server):
        client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})

        assert call_server.last.headers.get("x-shuo-admin-token") == ADMIN_TOKEN

    def test_the_token_is_never_returned_to_the_caller(self, client, call_server):
        response = client.post(
            "/v1/test-call", json={"phoneNumber": "+919876543210"}
        )

        assert ADMIN_TOKEN not in response.text

    def test_without_a_token_it_refuses_rather_than_calling_unauthenticated(
        self, client, call_server, monkeypatch
    ):
        """
        Fails closed, like every other operator path (decision 16). An
        unauthenticated attempt would be refused by :3040 anyway; refusing
        here means the operator gets the real reason instead of "forbidden".
        """
        monkeypatch.delenv("SHUO_ADMIN_TOKEN", raising=False)

        response = client.post(
            "/v1/test-call", json={"phoneNumber": "+919876543210"}
        )

        assert response.status_code == 503
        assert "SHUO_ADMIN_TOKEN" in response.json()["message"]
        assert call_server.requests == [], "it called out anyway"

    def test_health_reports_whether_a_call_could_be_placed(self, client):
        assert client.get("/health").json()["test_call_ready"] is True


class TestNumberValidation:
    """
    `/call/{n:path}` prepends a `+` to whatever it is handed, so an
    unvalidated typo becomes an opaque 502 with the carrier's reason
    deliberately swallowed. These turn it into a sentence instead.
    """

    @pytest.mark.parametrize(
        "number",
        ["919876543210", "0919876543210", "+91 98765", "not-a-number", "",
         "+919876543210/../../call/+1900", "+0119876543210"],
    )
    def test_a_bad_number_is_refused_before_anything_is_dialled(
        self, client, call_server, number
    ):
        response = client.post("/v1/test-call", json={"phoneNumber": number})

        assert response.status_code == 400
        assert call_server.requests == [], f"{number!r} reached the carrier"

    def test_spaces_and_hyphens_are_forgiven(self, client, call_server):
        response = client.post(
            "/v1/test-call", json={"phoneNumber": "+91 98765-43210"}
        )

        assert response.status_code == 200
        assert call_server.last.url.path == "/call/+919876543210"

    def test_a_bad_persona_is_refused(self, client, call_server):
        response = client.post(
            "/v1/test-call",
            json={"phoneNumber": "+919876543210", "persona": "../../etc"},
        )

        assert response.status_code == 400
        assert call_server.requests == []

    def test_an_unknown_field_is_refused_by_name(self, client):
        """Decision 36: silently dropping a request field is the worst
        failure available -- the panel says it worked."""
        response = client.post(
            "/v1/test-call",
            json={"phoneNumber": "+919876543210", "recordCall": True},
        )

        assert response.status_code == 400
        assert "recordCall" in response.json()["message"]


class TestTheCooldown:
    def test_a_second_call_straight_away_is_refused(self, client, call_server):
        first = client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})
        second = client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert len(call_server.requests) == 1, "a double-click placed two calls"

    def test_the_refusal_says_no_second_call_was_made(self, client, call_server):
        client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})
        second = client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})

        assert "no second call was made" in second.json()["message"].lower()

    def test_a_rejected_call_does_not_start_the_cooldown(self, client, call_server):
        """
        A typo costs nothing, so it must not lock the operator out for ten
        seconds while they fix it.
        """
        call_server.status_code = 502
        call_server.body = {"error": "origination failed"}
        client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})

        call_server.status_code = 200
        call_server.body = {"status": "calling"}
        again = client.post("/v1/test-call", json={"phoneNumber": "+919876543210"})

        assert again.status_code == 200


# =============================================================================
# THE ERROR ENVELOPE
# =============================================================================

class TestTheErrorEnvelope:
    """
    Every failure is `{"message": ...}` and every message ends by saying what
    happened to the *call* -- the counterpart of "Nothing was saved."
    """

    @pytest.mark.parametrize(
        "status,error,expected",
        [
            (403, "operator endpoints disabled (SHUO_ADMIN_TOKEN unset)",
             "switched off"),
            (403, "forbidden", "does not match"),
            (503, "server is draining", "shutting down"),
            (502, "origination failed", "carrier refused"),
        ],
    )
    def test_the_call_servers_terse_error_becomes_a_sentence(
        self, client, call_server, status, error, expected
    ):
        call_server.status_code = status
        call_server.body = {"error": error}

        response = client.post(
            "/v1/test-call", json={"phoneNumber": "+919876543210"}
        )

        message = response.json()["message"]
        assert expected in message.lower()
        assert "no call was placed" in message.lower()

    def test_an_unreachable_call_server_says_which_one(self, client, monkeypatch):
        monkeypatch.setattr(
            call_client,
            "_client",
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: (_ for _ in ()).throw(
                        httpx.ConnectError("refused")
                    )
                )
            ),
        )

        response = client.post(
            "/v1/test-call", json={"phoneNumber": "+919876543210"}
        )

        message = response.json()["message"]
        assert "main.py" in message
        assert "no call was placed" in message.lower()

    def test_a_validation_failure_does_not_say_nothing_was_saved(self, client):
        """
        The route saves nothing, so "Nothing was saved" would be reporting on
        something the operator never asked for. It has to report on the call.
        """
        response = client.post("/v1/test-call", json={})

        message = response.json()["message"]
        assert "nothing was saved" not in message.lower()
        assert "no call was placed" in message.lower()

    def test_saving_config_still_says_nothing_was_saved(self, client):
        """The other routes are unchanged by the wording split."""
        response = client.put("/v1/agent/persona", json={})

        assert "Nothing was saved" in response.json()["message"]

    def test_never_the_frameworks_detail_shape(self, client):
        body = client.post("/v1/test-call", json={}).json()

        assert "detail" not in body
        assert "message" in body


# =============================================================================
# STATUS
# =============================================================================

class TestStatus:
    def test_it_proxies_the_call_servers_snapshot(self, client, call_server):
        call_server.body = {
            "id": "call-1", "callId": "MZ-1", "live": True, "state": "speaking",
            "events": [{"seq": 1, "kind": "caller", "text": "hello"}],
            "nextSeq": 1, "missed": 0,
        }

        response = client.get("/v1/test-call/status")

        body = response.json()
        assert body["state"] == "speaking"
        assert body["events"][0]["text"] == "hello"
        assert body["callServer"] == "ok"

    def test_the_cursor_is_forwarded(self, client, call_server):
        call_server.body = {"events": [], "nextSeq": 41}

        client.get("/v1/test-call/status?since=41")

        assert "since=41" in str(call_server.last.url)

    def test_it_is_a_GET_and_carries_the_admin_token(self, client, call_server):
        call_server.body = {"events": [], "nextSeq": 0}

        client.get("/v1/test-call/status")

        assert call_server.last.method == "GET"
        assert call_server.last.headers.get("x-shuo-admin-token") == ADMIN_TOKEN

    def test_an_unreachable_call_server_is_a_field_not_an_error(
        self, client, monkeypatch
    ):
        """
        The panel polls this on a timer. A red banner on every tick because
        the call server restarted is noise, and it would bury the one poll
        that matters.
        """
        monkeypatch.setattr(
            call_client,
            "_client",
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: (_ for _ in ()).throw(
                        httpx.ConnectError("refused")
                    )
                )
            ),
        )

        response = client.get("/v1/test-call/status?since=7")

        assert response.status_code == 200
        body = response.json()
        assert body["callServer"] == "unreachable"
        assert body["live"] is False
        assert body["nextSeq"] == 7, "a failed poll must not rewind the cursor"

    def test_no_audio_crosses_the_boundary(self, client, call_server):
        """
        W3 carries text, states and integers. Not a sample, not an amplitude
        envelope -- so µ-law-end-to-end has nothing to say about it, and the
        player is never asked for a copy of what it is pacing.
        """
        call_server.body = {
            "id": "call-1", "state": "speaking",
            "events": [{"seq": 1, "kind": "timing", "name": "tts_first_audio",
                        "ms": 498}],
            "nextSeq": 1,
        }

        body = client.get("/v1/test-call/status").json()

        for event in body["events"]:
            assert "payload" not in event
            assert "audio" not in event


# =============================================================================
# HANGUP
# =============================================================================

class TestHangup:
    def test_it_ends_the_call_in_progress(self, client, call_server):
        call_server.body = {"status": "hangup", "id": "call-1", "call_id": "MZ-1"}

        response = client.post("/v1/test-call/hangup")

        assert response.status_code == 200
        assert response.json()["callId"] == "MZ-1"
        assert call_server.last.url.path == "/calls/current/hangup"

    def test_the_panels_expected_call_is_forwarded(self, client, call_server):
        call_server.body = {"status": "hangup", "call_id": "MZ-1"}

        client.post("/v1/test-call/hangup?expect=call-1")

        assert "expect=call-1" in str(call_server.last.url)

    def test_a_mismatch_is_reported_as_a_refusal_to_hang_up(
        self, client, call_server
    ):
        call_server.status_code = 409
        call_server.body = {"error": "call mismatch"}

        response = client.post("/v1/test-call/hangup?expect=call-1")

        assert response.status_code == 409
        assert "nothing was hung up" in response.json()["message"].lower()

    def test_no_call_in_progress_is_not_an_alarming_error(self, client, call_server):
        call_server.status_code = 409
        call_server.body = {"error": "no call in progress"}

        response = client.post("/v1/test-call/hangup")

        assert response.json()["message"] == (
            "There is no call in progress to hang up."
        )


# =============================================================================
# THE CALL SERVER'S OWN ROUTES
# =============================================================================

@pytest.fixture
def call_app(monkeypatch):
    monkeypatch.setenv("SHUO_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("CARRIER", "vobiz")
    monkeypatch.setenv("VOBIZ_AUTH_ID", "MA_TEST")
    monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("VOBIZ_PHONE_NUMBER", "+911234567890")
    monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")

    from shuo.carrier import reset_carrier_cache
    from shuo.server import app

    reset_carrier_cache()
    MONITOR.reset()
    yield TestClient(app)
    reset_carrier_cache()
    MONITOR.reset()


class TestTheLiveViewIsOperatorOnly:
    """
    Decision 16. `/calls/live` carries the per-turn transcript of what the
    caller said, which is the same reason `/trace/latest` is gated.
    """

    def test_it_refuses_without_the_admin_token(self, call_app):
        assert call_app.get("/calls/live").status_code == 403

    def test_it_refuses_a_wrong_token(self, call_app):
        response = call_app.get(
            "/calls/live", headers={"X-Shuo-Admin-Token": "wrong"}
        )
        assert response.status_code == 403

    def test_hangup_refuses_without_the_admin_token(self, call_app):
        assert call_app.post("/calls/current/hangup").status_code == 403

    def test_with_the_token_it_answers_even_with_no_calls(self, call_app):
        response = call_app.get(
            "/calls/live", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 200
        assert response.json()["live"] is False

    def test_it_serves_what_the_monitor_holds(self, call_app):
        recorder = MONITOR.begin(persona="candidate", direction="outbound")
        recorder.identify("MZ-live")
        recorder.caller_said("So, tell me about yourself.")

        body = call_app.get(
            "/calls/live", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        ).json()

        assert body["callId"] == "MZ-live"
        assert body["events"][-1]["text"] == "So, tell me about yourself."

    def test_hangup_with_no_call_is_a_409_not_a_500(self, call_app):
        response = call_app.post(
            "/calls/current/hangup", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 409
        assert response.json()["error"] == "no call in progress"

    def test_hangup_refuses_when_the_panel_means_a_different_call(self, call_app):
        recorder = MONITOR.begin(persona="candidate", direction="outbound")
        recorder.identify("MZ-live")

        response = call_app.post(
            "/calls/current/hangup?expect=call-999",
            headers={"X-Shuo-Admin-Token": ADMIN_TOKEN},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "call mismatch"

    def test_hangup_waits_for_the_carriers_call_id(self, call_app):
        """
        🔴 The `request_uuid` from `originate` is not reliably the
        `CallUUID`, so a call whose `start` frame has not landed has no id to
        hang up by. Guessing here would end a different call.
        """
        MONITOR.begin(persona="candidate", direction="outbound")

        response = call_app.post(
            "/calls/current/hangup", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 409
        assert response.json()["error"] == "call not yet identified"

    def test_hangup_reaches_the_carrier(self, call_app, monkeypatch):
        hung_up = []

        async def fake_hangup(self, call_id):
            hung_up.append(call_id)

        from shuo.carrier.vobiz import VobizCarrier

        monkeypatch.setattr(VobizCarrier, "hangup", fake_hangup)

        recorder = MONITOR.begin(persona="candidate", direction="outbound")
        recorder.identify("MZ-live")

        response = call_app.post(
            "/calls/current/hangup", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 200
        assert hung_up == ["MZ-live"]


class TestTheCallRouteAcceptsBothVerbs:
    def test_GET_still_works_for_the_runbook(self, call_app, monkeypatch):
        placed = []

        async def fake_originate(self, number, **kwargs):
            placed.append(number)
            from shuo.carrier.base import OriginateResult
            return OriginateResult(call_id="MZ-1", raw={})

        from shuo.carrier.vobiz import VobizCarrier

        monkeypatch.setattr(VobizCarrier, "originate", fake_originate)

        response = call_app.get(
            "/call/+919876543210", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 200
        assert placed == ["+919876543210"]

    def test_POST_is_what_the_config_api_uses(self, call_app, monkeypatch):
        placed = []

        async def fake_originate(self, number, **kwargs):
            placed.append(number)
            from shuo.carrier.base import OriginateResult
            return OriginateResult(call_id="MZ-1", raw={})

        from shuo.carrier.vobiz import VobizCarrier

        monkeypatch.setattr(VobizCarrier, "originate", fake_originate)

        response = call_app.post(
            "/call/+919876543210", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 200
        assert placed == ["+919876543210"]


# =============================================================================
# THE SPLIT IS STILL A SPLIT
# =============================================================================

class TestTheProcessSplitSurvives:
    """
    W3 is the first feature that needs the two processes to talk. It has to
    do that without either of them learning about the other's internals --
    otherwise the split becomes a merge with extra steps, and decision 32's
    protection (a Save can never stall a live call) goes with it.
    """

    def test_the_config_api_reaches_the_call_server_only_over_http(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(call_client))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        forbidden = {"conversation", "server", "agent", "state", "carrier",
                     "call_monitor"}
        assert not (imported & forbidden), (
            f"{imported & forbidden} reached call_client.py — the config API "
            f"must talk to the call server over HTTP, not by importing it"
        )

    def test_the_call_server_has_no_test_call_routes(self):
        """
        The panel's entry point stays on :3041. A `/v1/test-call` mounted on
        the call server would be an operator endpoint on the app that shares
        an event loop with the player.
        """
        from shuo.server import app as call_app

        paths = {route.path for route in call_app.routes}
        assert "/v1/test-call" not in paths
        assert "/v1/test-call/status" not in paths

    def test_the_config_api_has_no_media_routes(self):
        paths = {route.path for route in config_api.app.routes}
        assert "/ws" not in paths
        assert "/answer" not in paths
        assert "/calls/live" not in paths
