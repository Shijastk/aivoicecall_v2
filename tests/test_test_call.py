"""
Tests for W3's test-call feature -- both halves of the process split.

    :3041  POST /v1/test-call          -> :3040  POST /call/{number}
    :3041  GET  /v1/calls/live         -> :3040  GET  /calls/live
    :3041  GET  /v1/test-call/status   -> the same handler (alias)
    :3041  POST /v1/test-call/hangup   -> :3040  POST /calls/current/hangup
    :3041  GET  /v1/calls/active       -> :3040  GET  /calls/active
                                         **plus the call log on disk**

That last one is the only route in the repo that reads both sides of the
split, and `TestActiveCallsMerge` is where it is held to account: neither side
knows about all the calls, because a ringing phone has no media socket and so
exists nowhere in :3040's memory.

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

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import shuo.call_client as call_client
import shuo.call_history as call_history
import shuo.config_api as config_api
from shuo.call_monitor import MONITOR
from shuo.call_status import IN_PROGRESS
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
# ACTIVE CALLS -- THE MERGE (W5c)
# =============================================================================

def live_summary(call_ref: str, **fields) -> dict:
    """One row as :3040's `/calls/active` would return it."""
    summary = {
        "id": call_ref,
        "callId": f"MZ-{call_ref}",
        "direction": "outbound",
        "persona": "candidate",
        "to": "",
        "from": "",
        "state": "listening",
        "live": True,
        "status": IN_PROGRESS,
        "startedAt": call_history.now_iso(),
        "durationMs": 4200,
        "turns": 3,
        "endedReason": "",
    }
    summary.update(fields)
    return summary


def _ago(seconds: float) -> str:
    """An ISO stamp `seconds` in the past, in the log's own format."""
    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return stamp.isoformat(timespec="milliseconds")


def log_row(call_ref: str, *, status: str, age_seconds: float = 0.0, **fields) -> None:
    """Write one revision to the (tmp_path-isolated) call log."""
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    iso = stamp.isoformat(timespec="milliseconds")
    record = call_history.revision(
        call_ref, status=status, startedAt=iso, **fields
    )
    record["revisedAt"] = iso
    call_history.append(record)


class TestActiveCallsMerge:
    """
    `GET /v1/calls/active` -- the one route that reads both sides of the split.

    Neither side knows about all the calls, and that is the whole reason this
    exists rather than being a proxy:

        :3040's memory   answered calls -- there is a media socket for each
        the log on disk  every attempt, including the ones still ringing, which
                         have no socket to be in memory *of*

    A live-only view is blank for the entire time the phone is ringing, which
    is exactly the window an operator sits watching.
    """

    def test_a_live_call_is_reported(self, client, call_server):
        call_server.body = {"calls": [live_summary("att-one")]}

        body = client.get("/v1/calls/active").json()

        assert body["callServer"] == "ok"
        assert body["count"] == 1
        assert body["calls"][0]["id"] == "att-one"
        assert body["calls"][0]["live"] is True
        assert body["calls"][0]["source"] == "live"

    def test_a_ringing_call_with_no_socket_still_appears(self, client, call_server):
        """
        🔴 The reason the merge exists. A ringing phone is in the log and
        nowhere else, so this is the only route that can show it.
        """
        call_server.body = {"calls": []}
        log_row("att-ringing", status="ringing", to="+919876543210")

        body = client.get("/v1/calls/active").json()

        assert [call["id"] for call in body["calls"]] == ["att-ringing"]
        row = body["calls"][0]
        assert row["status"] == "ringing"
        assert row["to"] == "+919876543210"
        assert row["live"] is False
        assert row["state"] is None, "a ringing call has not got as far as a state"
        assert row["source"] == "log"

    def test_a_pending_call_appears_too(self, client, call_server):
        call_server.body = {"calls": []}
        log_row("att-pending", status="pending", to="+919876543210")

        body = client.get("/v1/calls/active").json()

        assert body["calls"][0]["status"] == "pending"

    def test_long_finished_calls_are_not_active(self, client, call_server):
        call_server.body = {"calls": []}
        stale = config_api.TERMINAL_GRACE_SECONDS + 30
        log_row("att-done", status="completed", age_seconds=stale)
        log_row("att-missed", status="missed", age_seconds=stale)
        log_row("att-failed", status="failed", age_seconds=stale)
        log_row("att-cancelled", status="cancelled", age_seconds=stale)
        log_row("att-declined", status="declined", age_seconds=stale)
        log_row("att-live", status="in_progress")

        body = client.get("/v1/calls/active").json()

        assert [call["id"] for call in body["calls"]] == ["att-live"]

    def test_a_call_that_just_ended_is_still_listed_once(self, client, call_server):
        """
        🔴 The W5e fix, and the reason the panel froze.

        A poll-driven client learns a call ended by *seeing it end*. Dropping
        the row the instant it went terminal meant the decline was never in any
        response: between two polls the call simply stopped existing, which is
        indistinguishable from a failed request, so the ringing screen had
        nothing to close on and stayed up.
        """
        call_server.body = {"calls": []}
        log_row("att-declined", status="declined", endedCode="remote_declined")

        [row] = client.get("/v1/calls/active").json()["calls"]

        assert row["id"] == "att-declined"
        assert row["status"] == "declined"
        assert row["live"] is False
        assert row["endedCode"] == "remote_declined"

    def test_an_ended_call_in_the_monitor_lingers_then_goes(
        self, client, call_server
    ):
        """
        Same grace window from the *live* side. :3040 keeps the last few ended
        calls, and the one that ended a moment ago is the one a panel polling
        across a hangup needs; the one that ended a minute ago belongs in the
        history table, which is a different screen reading a different route.
        """
        call_server.body = {
            "calls": [
                live_summary("att-one"),
                live_summary(
                    "att-just-ended",
                    live=False,
                    state="ended",
                    status="completed",
                    endedAt=call_history.now_iso(),
                ),
                live_summary(
                    "att-old",
                    live=False,
                    state="ended",
                    status="completed",
                    endedAt=_ago(config_api.TERMINAL_GRACE_SECONDS + 30),
                ),
            ]
        }

        body = client.get("/v1/calls/active").json()

        assert [call["id"] for call in body["calls"]] == [
            "att-one",
            "att-just-ended",
        ]

    def test_a_ringing_call_the_carrier_went_quiet_on_is_reported_as_failed(
        self, client, call_server
    ):
        """
        🔴 The backstop for an unverified webhook.

        `originate` only began sending a `hangup_url` in Phase 8 and whether
        Vobiz honours it is unverified, so "the far end declined and nothing
        told us" is a live possibility. Without this the row stays `ringing`
        until `ACTIVE_WINDOW_SECONDS` hides it -- ten minutes of ringing screen
        for a call that ended in three seconds.
        """
        call_server.body = {"calls": []}
        log_row(
            "att-quiet",
            status="ringing",
            age_seconds=config_api.RING_TIMEOUT_SECONDS + 5,
        )

        [row] = client.get("/v1/calls/active").json()["calls"]

        assert row["status"] == "failed"
        assert row["endedCode"] == "no_carrier_response"
        assert row["live"] is False

    def test_a_call_still_within_the_ring_timeout_is_still_ringing(
        self, client, call_server
    ):
        """
        The margin matters more than the cut-off. Reporting a live call as dead
        while the phone is still in someone's hand is a worse failure than the
        phantom the timeout replaces, so a call inside the window is untouched.
        """
        call_server.body = {"calls": []}
        log_row(
            "att-ringing",
            status="ringing",
            age_seconds=config_api.RING_TIMEOUT_SECONDS - 30,
        )

        [row] = client.get("/v1/calls/active").json()["calls"]

        assert row["status"] == "ringing"
        assert row["endedCode"] == ""

    def test_the_timed_out_call_is_reported_then_released(self, client, call_server):
        """
        The timeout is dated to when it was *crossed*, not to the last revision
        and not to now.

        Dated to the last revision, the call would already be three minutes
        stale the moment it turned terminal, so it would fall outside the grace
        window immediately and vanish without ever having been reported as
        failed -- the same silent disappearance this whole change exists to
        stop, reached by a different route. Dated to `now`, every poll would
        re-date it and it would linger forever.
        """
        call_server.body = {"calls": []}
        log_row(
            "att-released",
            status="ringing",
            age_seconds=(
                config_api.RING_TIMEOUT_SECONDS
                + config_api.TERMINAL_GRACE_SECONDS
                + 30
            ),
        )

        assert client.get("/v1/calls/active").json()["calls"] == []

    def test_the_log_supplies_the_number_the_monitor_never_knew(
        self, client, call_server
    ):
        """
        `to`/`from` are known at origination. A socket opened without them --
        which is every inbound call before the carrier's form is parsed -- has
        an empty pair in memory, and blanking the panel's only record of who
        was called would be a regression from W5a.
        """
        call_server.body = {"calls": [live_summary("att-one", to="", **{"from": ""})]}
        log_row(
            "att-one",
            status="in_progress",
            to="+919876543210",
            **{"from": "+911234567890"},
        )

        row = client.get("/v1/calls/active").json()["calls"][0]

        assert row["to"] == "+919876543210"
        assert row["from"] == "+911234567890"
        # Live still wins where it has something to say.
        assert row["state"] == "listening"
        assert row["turns"] == 3

    def test_a_terminal_log_row_cannot_be_demoted_by_a_live_one(
        self, client, call_server
    ):
        """
        The carrier's hangup webhook and the loop's teardown race, and either
        can land first: for a moment the log says `cancelled` while the socket
        is still open. `best_status` is what stops this list reporting
        `in_progress` over the top of it -- the same rank the log folds
        revisions with.
        """
        call_server.body = {"calls": [live_summary("att-one")]}
        log_row("att-one", status="cancelled")

        row = client.get("/v1/calls/active").json()["calls"][0]

        assert row["status"] == "cancelled"
        # Still listed, because the socket is genuinely still open. It clears
        # itself on the next poll, once teardown has run.
        assert row["live"] is True

    def test_a_stale_pending_row_is_not_a_call_in_flight(self, client, call_server):
        """
        A row stays non-terminal forever if the process that wrote it was
        killed between origination and teardown -- the revision that would have
        closed it was never written. Without the window, one `kill -9` leaves a
        phantom ringing call in the panel for the life of the log file.
        """
        call_server.body = {"calls": []}
        log_row(
            "att-phantom",
            status="ringing",
            age_seconds=config_api.ACTIVE_WINDOW_SECONDS + 60,
        )
        log_row("att-fresh", status="ringing", age_seconds=5)

        body = client.get("/v1/calls/active").json()

        assert [call["id"] for call in body["calls"]] == ["att-fresh"]

    def test_a_stale_row_the_call_server_says_is_live_is_still_merged(
        self, client, call_server
    ):
        """
        A very long call is not a phantom. :3040 asserting the socket is open
        outranks the clock.

        The call is listed either way -- the live summary alone is enough for
        that -- so what the exemption actually protects is the *merge*: an
        hour-long call whose log row predates the window would otherwise lose
        the number it dialled, because `to`/`from` exist nowhere else.
        """
        call_server.body = {"calls": [live_summary("att-long")]}
        log_row(
            "att-long",
            status="in_progress",
            to="+919876543210",
            age_seconds=config_api.ACTIVE_WINDOW_SECONDS + 600,
        )

        body = client.get("/v1/calls/active").json()

        assert [call["id"] for call in body["calls"]] == ["att-long"]
        assert body["calls"][0]["to"] == "+919876543210"

    def test_the_newest_attempt_comes_first(self, client, call_server):
        call_server.body = {"calls": []}
        log_row("att-older", status="ringing", age_seconds=120)
        log_row("att-newer", status="ringing", age_seconds=5)

        body = client.get("/v1/calls/active").json()

        assert [call["id"] for call in body["calls"]] == ["att-newer", "att-older"]

    def test_a_call_is_listed_once_not_twice(self, client, call_server):
        call_server.body = {"calls": [live_summary("att-one")]}
        log_row("att-one", status="in_progress")

        body = client.get("/v1/calls/active").json()

        assert body["count"] == 1

    def test_an_unreachable_call_server_still_serves_the_disk(
        self, client, monkeypatch
    ):
        """
        The half of this answer that lives on disk is still correct without
        :3040, and it is the half worth seeing: a call that is ringing *right
        now*. Refusing to serve it because the call server is down would hide
        exactly the calls an operator is waiting on.
        """
        log_row("att-ringing", status="ringing", to="+919876543210")
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

        response = client.get("/v1/calls/active")

        assert response.status_code == 200
        body = response.json()
        assert body["callServer"] == "unreachable"
        assert body["message"]
        assert [call["id"] for call in body["calls"]] == ["att-ringing"]

    def test_a_missing_admin_token_reads_as_unreachable(
        self, client, call_server, monkeypatch
    ):
        """
        This process cannot authenticate to :3040, which from the panel's side
        is the same situation as :3040 not being there -- and the disk rows are
        unaffected either way.
        """
        monkeypatch.delenv("SHUO_ADMIN_TOKEN", raising=False)
        log_row("att-ringing", status="ringing")

        body = client.get("/v1/calls/active").json()

        assert body["callServer"] == "unreachable"
        assert [call["id"] for call in body["calls"]] == ["att-ringing"]

    def test_a_call_server_talking_nonsense_does_not_break_the_poll(
        self, client, call_server
    ):
        """A 1Hz poll must degrade, not 500."""
        call_server.body = {"calls": "not a list"}
        log_row("att-ringing", status="ringing")

        body = client.get("/v1/calls/active").json()

        assert [call["id"] for call in body["calls"]] == ["att-ringing"]

    def test_an_empty_everything_is_an_empty_list(self, client, call_server):
        call_server.body = {"calls": []}

        body = client.get("/v1/calls/active").json()

        assert body == {"calls": [], "count": 0, "callServer": "ok"}

    def test_it_carries_no_transcript(self, client, call_server):
        """
        The list is polled at 1Hz for every call at once. Transcripts come from
        `/v1/calls/live`, for the one call an operator has open.
        """
        call_server.body = {"calls": [live_summary("att-one")]}
        log_row(
            "att-one",
            status="in_progress",
            transcript=[{"speaker": "caller", "text": "So, tell me about yourself."}],
        )

        response = client.get("/v1/calls/active")

        assert "tell me about yourself" not in response.text

    def test_it_is_a_GET_and_carries_the_admin_token(self, client, call_server):
        call_server.body = {"calls": []}

        client.get("/v1/calls/active")

        assert call_server.last.url.path == "/calls/active"
        assert call_server.last.method == "GET"
        assert call_server.last.headers.get("x-shuo-admin-token") == ADMIN_TOKEN


class TestTheLiveRouteAndItsAlias:
    """
    `/v1/calls/live` is the name; `/v1/test-call/status` is the same handler.

    The old name was wrong from the start and W5c is where it became visible:
    this was never about the test call, it is *the live view*, and a route
    saying `test-call` reads as though watching a real inbound call needed a
    different endpoint. The alias stays until the panel moves.
    """

    def test_the_general_form_serves_the_snapshot(self, client, call_server):
        call_server.body = {
            "id": "att-one", "callId": "MZ-1", "live": True, "state": "speaking",
            "events": [{"seq": 1, "kind": "caller", "text": "hello"}],
            "nextSeq": 1, "missed": 0,
        }

        body = client.get("/v1/calls/live?call=att-one").json()

        assert body["state"] == "speaking"
        assert body["callServer"] == "ok"
        assert "call=att-one" in str(call_server.last.url)

    def test_the_alias_answers_identically(self, client, call_server):
        call_server.body = {
            "id": "att-one", "live": True, "state": "listening",
            "events": [], "nextSeq": 9, "missed": 0,
        }

        general = client.get("/v1/calls/live?since=3&call=att-one").json()
        alias = client.get("/v1/test-call/status?since=3&call=att-one").json()

        assert general == alias

    def test_the_alias_still_reports_unreachability_as_a_field(
        self, client, monkeypatch
    ):
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

        for path in ("/v1/calls/live?since=7", "/v1/test-call/status?since=7"):
            body = client.get(path).json()
            assert body["callServer"] == "unreachable"
            assert body["nextSeq"] == 7, "a failed poll must not rewind the cursor"


# =============================================================================
# THE LIVE ROUTE FALLS BACK TO THE LOG (W5e)
# =============================================================================

# `/calls/live` as :3040 answers it for a call its monitor has never heard of.
# That is not an edge case: it is the *entire life* of a call that is ringing,
# and the whole life of one that is declined. Neither opens a media socket, so
# `call_monitor` never sees either.
UNKNOWN_TO_THE_CALL_SERVER = {
    "id": None,
    "callId": "",
    "live": False,
    "state": None,
    "status": "",
    "endedReason": "",
    "endedCode": "",
    "endedAt": "",
    "events": [],
    "nextSeq": 12,
    "missed": 0,
}


class TestTheLiveRouteFallsBackToTheLog:
    """
    🔴 The bug that froze the panel, and the fix for it.

    Before this, a declined call polled here forever and got the same payload
    every time -- `{live: false, state: null, events: []}` -- both while the
    phone rang and after the far end hung up on it. Nothing in the response
    ever changed, so a poll-driven client had no transition to see and no
    reason to stop ringing.

    The call exists on disk the whole time. This route now reads it.
    """

    def test_a_declined_call_reports_a_terminal_status(self, client, call_server):
        call_server.body = dict(UNKNOWN_TO_THE_CALL_SERVER)
        log_row(
            "att-declined",
            status="declined",
            endedCode="remote_declined",
            endedReason="the far end declined the call",
            endedAt=call_history.now_iso(),
        )

        body = client.get("/v1/calls/live?call=att-declined").json()

        assert body["status"] == "declined"
        assert body["endedCode"] == "remote_declined"
        assert body["live"] is False
        assert body["endedAt"], "the transition needs a time, not just a value"
        assert body["source"] == "log"
        assert body["callServer"] == "ok"

    def test_a_ringing_call_reports_ringing_rather_than_nothing(
        self, client, call_server
    ):
        """
        The other half, and the reason the fallback is not only about endings:
        while the phone rings this route used to say nothing at all, so the
        panel could not distinguish "ringing" from "the call server forgot".
        """
        call_server.body = dict(UNKNOWN_TO_THE_CALL_SERVER)
        log_row("att-ringing", status="ringing", to="+919876543210")

        body = client.get("/v1/calls/live?call=att-ringing").json()

        assert body["status"] == "ringing"
        assert body["live"] is False
        assert body["state"] is None, "a ringing call has not got as far as a state"
        assert body["endedCode"] == "", "it has not ended"
        assert body["to"] == "+919876543210"

    def test_the_cursor_survives_the_fallback(self, client, call_server):
        """
        A panel that polls through a ringing call and then reaches the live view
        must keep its place. Answering with a smaller `nextSeq` would replay the
        whole transcript the moment the call was answered.
        """
        call_server.body = dict(UNKNOWN_TO_THE_CALL_SERVER)
        log_row("att-ringing", status="ringing")

        body = client.get("/v1/calls/live?since=41&call=att-ringing").json()

        assert body["nextSeq"] == 41
        assert body["events"] == []

    def test_a_live_call_is_not_second_guessed(self, client, call_server):
        """
        The fallback is for calls :3040 has never heard of. A call it *is*
        running answers from memory, transcript and all -- reading the log over
        the top of it would replace a live transcript with a stale row.
        """
        call_server.body = {
            "id": "att-one",
            "callId": "MZ-1",
            "live": True,
            "state": "speaking",
            "status": "in_progress",
            "events": [{"seq": 1, "kind": "caller", "text": "hello"}],
            "nextSeq": 1,
            "missed": 0,
        }
        log_row("att-one", status="ringing")

        body = client.get("/v1/calls/live?call=att-one").json()

        assert body["state"] == "speaking"
        assert body["events"][0]["text"] == "hello"
        assert body["source"] == "live"

    def test_a_call_nobody_has_heard_of_is_not_an_error(self, client, call_server):
        """
        An id from a stale browser tab, or one trimmed out of the log. The panel
        polls this on a timer; a 404 per tick is noise, and the honest answer is
        that there is nothing to report.
        """
        call_server.body = dict(UNKNOWN_TO_THE_CALL_SERVER)

        response = client.get("/v1/calls/live?call=att-ghost")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == ""
        assert body["live"] is False
        assert body["source"] == "none"

    def test_a_stopped_call_server_still_reports_a_decline(
        self, client, monkeypatch
    ):
        """
        The disk half is correct without :3040, and `main.py` being stopped is
        the ordinary state of the machine between test calls. Refusing to
        answer would hide the outcome the operator is waiting for.
        """
        log_row(
            "att-declined",
            status="declined",
            endedCode="remote_declined",
            endedAt=call_history.now_iso(),
        )
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

        body = client.get("/v1/calls/live?since=7&call=att-declined").json()

        assert body["callServer"] == "unreachable"
        assert body["status"] == "declined"
        assert body["endedCode"] == "remote_declined"
        assert body["nextSeq"] == 7, "a failed poll must not rewind the cursor"

    def test_an_old_row_without_a_code_still_gets_one(self, client, call_server):
        """
        Every row already on disk predates `endedCode`. A panel branching on it
        must not need a branch for "terminal, but we did not say why".
        """
        call_server.body = dict(UNKNOWN_TO_THE_CALL_SERVER)
        log_row("att-legacy", status="missed")

        body = client.get("/v1/calls/live?call=att-legacy").json()

        assert body["status"] == "missed"
        assert body["endedCode"] == "no_answer"

    def test_the_alias_falls_back_identically(self, client, call_server):
        call_server.body = dict(UNKNOWN_TO_THE_CALL_SERVER)
        log_row("att-declined", status="declined", endedCode="remote_declined")

        general = client.get("/v1/calls/live?call=att-declined").json()
        alias = client.get("/v1/test-call/status?call=att-declined").json()

        # `durationMs` is derived from `startedAt` against the clock, so it
        # advances by the milliseconds between the two requests. Everything
        # else must be identical -- the alias is a delegation, not a copy.
        assert general.pop("durationMs") >= 0
        assert alias.pop("durationMs") >= 0
        assert general == alias
        assert alias["status"] == "declined"


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


class TestActiveCallsOnTheCallServer:
    """
    `GET /calls/active` (W5c) -- the plural of `/calls/live`.

    Operator-gated for the same reason everything here is: it names who was
    called, and `to`/`from` on a list of calls is the same class of thing as a
    transcript.
    """

    def test_it_refuses_without_the_admin_token(self, call_app):
        assert call_app.get("/calls/active").status_code == 403

    def test_it_refuses_a_wrong_token(self, call_app):
        response = call_app.get(
            "/calls/active", headers={"X-Shuo-Admin-Token": "wrong"}
        )
        assert response.status_code == 403

    def test_with_no_calls_it_answers_an_empty_list(self, call_app):
        response = call_app.get(
            "/calls/active", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 200
        assert response.json() == {"calls": []}

    def test_it_reports_several_calls_at_once(self, call_app):
        first = MONITOR.begin(
            persona="candidate", direction="outbound", attempt="att-one",
            to_number="+919876543210",
        )
        first.identify("MZ-one")
        second = MONITOR.begin(
            persona="receptionist", direction="inbound", attempt="att-two"
        )
        second.identify("MZ-two")

        body = call_app.get(
            "/calls/active", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        ).json()

        assert [call["id"] for call in body["calls"]] == ["att-two", "att-one"]
        by_id = {call["id"]: call for call in body["calls"]}
        assert by_id["att-one"]["to"] == "+919876543210"
        assert by_id["att-one"]["callId"] == "MZ-one"
        assert by_id["att-two"]["direction"] == "inbound"
        assert all(call["live"] is True for call in body["calls"])

    def test_it_carries_no_transcript(self, call_app):
        """
        This is the list, not the reading. Eight transcripts encoded on the
        loop that paces 20ms frames is precisely the work that must not happen
        here -- `/calls/live` serves one call, with a cursor.
        """
        recorder = MONITOR.begin(persona="candidate", direction="outbound")
        recorder.caller_said("So, tell me about yourself.")

        response = call_app.get(
            "/calls/active", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert "So, tell me about yourself." not in response.text


class TestHangupNamesItsCall:
    """
    W5c: `expect` selects the call, it does not merely assert about it.

    `MONITOR.current()` means "the most recently opened live call", which was
    unambiguous while the monitor held one call and is not while it holds
    eight. An operator ending the interview they are watching, on a box that
    has since answered the receptionist line, would have hung up the
    receptionist.
    """

    def test_it_hangs_up_the_named_call_not_the_newest(self, call_app, monkeypatch):
        hung_up = []

        watched = MONITOR.begin(
            persona="candidate", direction="outbound", attempt="att-watched"
        )
        watched.identify("MZ-watched")
        newer = MONITOR.begin(
            persona="receptionist", direction="inbound", attempt="att-newer"
        )
        newer.identify("MZ-newer")

        from shuo.carrier import get_carrier

        async def fake_hangup(call_id):
            hung_up.append(call_id)

        monkeypatch.setattr(get_carrier(), "hangup", fake_hangup)

        response = call_app.post(
            "/calls/current/hangup?expect=att-watched",
            headers={"X-Shuo-Admin-Token": ADMIN_TOKEN},
        )

        assert response.status_code == 200
        assert hung_up == ["MZ-watched"], "it must not end the newer call"

    def test_a_call_that_has_already_ended_is_a_refusal(self, call_app):
        recorder = MONITOR.begin(
            persona="candidate", direction="outbound", attempt="att-done"
        )
        recorder.identify("MZ-done")
        recorder.ended("caller hung up")

        MONITOR.begin(persona="receptionist", direction="inbound")

        response = call_app.post(
            "/calls/current/hangup?expect=att-done",
            headers={"X-Shuo-Admin-Token": ADMIN_TOKEN},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "call mismatch"

    def test_without_expect_it_still_means_the_current_call(self, call_app, monkeypatch):
        """The runbook's `curl`, with one call up. Unchanged by W5c."""
        hung_up = []

        recorder = MONITOR.begin(persona="candidate", direction="outbound")
        recorder.identify("MZ-only")

        from shuo.carrier import get_carrier

        async def fake_hangup(call_id):
            hung_up.append(call_id)

        monkeypatch.setattr(get_carrier(), "hangup", fake_hangup)

        response = call_app.post(
            "/calls/current/hangup", headers={"X-Shuo-Admin-Token": ADMIN_TOKEN}
        )

        assert response.status_code == 200
        assert hung_up == ["MZ-only"]


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
