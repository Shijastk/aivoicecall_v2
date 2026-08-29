"""
Every call attempt reaches the log, not just the answered ones.

This is the gap Phase 8 W5a closes. Before it, the only write was in
`conversation.py`'s teardown, which is reached only by a call that was
*answered* -- so a call that rang out, was declined, or was cancelled while
ringing left no trace anywhere. An operator looking at the panel could not
tell "I never placed that call" from "I placed it and nobody picked up".

Four things are defended:

1. **A row exists before the phone rings**, written at origination, and it
   survives the carrier refusing the call. `TestTheAttemptIsRecorded`.
2. **The row is updated, and the updates fold into one call.** Append-only
   JSONL plus a field-level merge, so a revision cannot blank what an earlier
   one knew. `TestRevisionsFold`.
3. **A late webhook cannot demote a finished call.** The hangup callback and
   the call loop's teardown race, and either can land first.
   `TestStatusRanking`.
4. **The attempt id is the thread that ties them together**, and it is
   treated as untrusted on the way back in. `TestTheAttemptId`.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from shuo import call_history, call_status, config, server
from shuo.call_monitor import MONITOR
from shuo.call_status import (
    CANCELLED,
    COMPLETED,
    DECLINED,
    FAILED,
    IN_PROGRESS,
    MISSED,
    PENDING,
    RINGING,
)
from shuo.spool import SPOOL

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "call_history.jsonl"
    monkeypatch.setenv("SHUO_CALL_HISTORY_PATH", str(path))
    return path


@pytest.fixture
def call_app(monkeypatch, log_path):
    monkeypatch.setenv("SHUO_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("CARRIER", "vobiz")
    monkeypatch.setenv("VOBIZ_AUTH_ID", "MA_TEST")
    monkeypatch.setenv("VOBIZ_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("VOBIZ_PHONE_NUMBER", "+911234567890")
    monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")
    # The webhook handlers are the subject here, not the signature gate that
    # `tests/test_vobiz.py` already covers.
    monkeypatch.setenv("VALIDATE_WEBHOOK_SIGNATURES", "false")

    from shuo.carrier import reset_carrier_cache

    reset_carrier_cache()
    MONITOR.reset()
    SPOOL.reset()
    with TestClient(server.app) as client:
        yield client
    reset_carrier_cache()
    MONITOR.reset()
    SPOOL.reset()


class FakeCarrier:
    """A carrier that records what it was asked to do and never dials."""

    name = "fake"
    records_via_rest = False

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.originated = []

    async def originate(self, to_number, **kwargs):
        self.originated.append({"to": to_number, **kwargs})
        if self.fail:
            raise RuntimeError("carrier said no")

        class Result:
            call_id = "req-uuid-1"
            raw = {}

        return Result()


def rows(path):
    """Folded rows, newest call first."""
    return call_history.load(path=path)


def settle(path, predicate=bool, timeout=2.0):
    """
    Wait for the spool's worker to land what a request queued.

    The waiting is the *point*, not an inconvenience: a handler that had
    already written to disk by the time it answered would be a handler that
    blocked the event loop pacing 20ms frames. These tests drive the app
    through a synchronous client, so the write lands a moment after the
    response, on the portal's loop.

    Polls rather than sleeping a fixed time, so a fast machine is fast.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = rows(path)
        if predicate(found) or time.monotonic() > deadline:
            return found
        time.sleep(0.01)


def raw_lines(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# =============================================================================
# THE ATTEMPT IS RECORDED
# =============================================================================

class TestTheAttemptIsRecorded:
    @pytest.mark.asyncio
    async def test_a_row_exists_before_the_carrier_is_even_called(
        self, log_path, monkeypatch
    ):
        """
        🔴 The ordering is the point.

        Between the pending write and the carrier answering lies a REST round
        trip that can take seconds and can fail. A row written *after* it would
        leave the one case an operator most needs explained -- "I pressed call
        and nothing happened" -- with nothing on disk at all.

        Asserted on the spool rather than on the file, because the whole
        design is that the handler does not wait for a disk: what has to
        happen before the carrier is called is the *submission*.
        """
        monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")
        # A delta, not an absolute: the spool is one per process and every
        # earlier test in the run has already submitted through it.
        before = SPOOL.stats()["submitted"]
        queued_at_originate = []

        class Watching(FakeCarrier):
            async def originate(self, to_number, **kwargs):
                queued_at_originate.append(SPOOL.stats()["submitted"] - before)
                return await super().originate(to_number, **kwargs)

        await server.place_outbound_call(
            "+919876543210", "candidate", carrier=Watching()
        )
        await SPOOL.drain()

        assert queued_at_originate == [1], (
            "the pending row was not queued before the carrier was called"
        )
        [row] = rows(log_path)
        assert row["status"] == PENDING
        assert row["to"] == "+919876543210"

    @pytest.mark.asyncio
    async def test_the_number_dialled_is_recorded(self, log_path, monkeypatch):
        """
        New in Phase 8, and a real gap before it: the call log could not
        answer "who was called".
        """
        monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")
        await server.place_outbound_call(
            "+919876543210", "candidate", carrier=FakeCarrier()
        )
        await SPOOL.drain()

        [row] = rows(log_path)
        assert row["to"] == "+919876543210"
        assert row["persona"] == "candidate"
        assert row["direction"] == "outbound"

    @pytest.mark.asyncio
    async def test_a_refused_origination_is_recorded_as_failed(
        self, log_path, monkeypatch
    ):
        monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")

        with pytest.raises(RuntimeError):
            await server.place_outbound_call(
                "+919876543210", "candidate", carrier=FakeCarrier(fail=True)
            )
        await SPOOL.drain()

        [row] = rows(log_path)
        assert row["status"] == FAILED
        assert row["to"] == "+919876543210", (
            "the failure revision must not blank what the stub knew"
        )

    @pytest.mark.asyncio
    async def test_the_carrier_is_given_ring_and_hangup_urls(
        self, log_path, monkeypatch
    ):
        """
        Without these the carrier reports nothing about a call that is never
        answered, and `missed`/`cancelled` become underivable. Whether Vobiz
        honours them is unverified -- see docs/phase8-plan.md §1.5 -- but not
        sending them guarantees it cannot.
        """
        monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")
        carrier = FakeCarrier()

        result = await server.place_outbound_call(
            "+919876543210", "candidate", carrier=carrier
        )
        await SPOOL.drain()

        [placed] = carrier.originated
        attempt = result["attempt"]
        assert placed["ring_url"].endswith(f"/ring?attempt={attempt}")
        assert placed["hangup_url"].endswith(f"/hangup?attempt={attempt}")
        assert f"attempt={attempt}" in placed["answer_url"]

    @pytest.mark.asyncio
    async def test_the_request_uuid_is_not_stored_as_the_call_id(
        self, log_path, monkeypatch
    ):
        """
        `originate` returns a `request_uuid` which is NOT reliably the
        `CallUUID` the webhooks and the media `start` frame quote. Storing it
        under `callId` would make the two indistinguishable later, on exactly
        the field the hangup path keys on.
        """
        monkeypatch.setenv("PUBLIC_URL", "https://shuo.test")
        await server.place_outbound_call(
            "+919876543210", "candidate", carrier=FakeCarrier()
        )
        await SPOOL.drain()

        [row] = rows(log_path)
        assert row["requestId"] == "req-uuid-1"
        assert not row.get("callId")

    def test_an_inbound_call_is_recorded_when_it_rings(self, call_app, log_path):
        """
        Inbound has no origination moment -- `/answer` is the first the system
        hears of the call -- so the row starts there, and starts at `ringing`,
        which is the truth: a phone is ringing and the carrier is asking what
        to do about it.
        """
        response = call_app.post(
            "/answer?direction=inbound",
            data={"To": "+911234567890", "From": "+919876543210"},
        )
        assert response.status_code == 200

        [row] = settle(log_path)
        assert row["status"] == RINGING
        assert row["direction"] == "inbound"
        assert row["from"] == "+919876543210"
        assert row["to"] == "+911234567890"


# =============================================================================
# THE WEBHOOKS
# =============================================================================

class TestTheWebhooks:
    def test_the_ring_callback_moves_the_row_to_ringing(self, call_app, log_path):
        call_app.post("/ring?attempt=att-abc123", data={"CallUUID": "MZ-1"})

        [row] = settle(log_path)
        assert row["status"] == RINGING
        assert row["ringingAt"]

    def test_an_unanswered_call_is_missed(self, call_app, log_path):
        """
        🔴 The gap Phase 8 exists to close.

        This call never fetched `/answer`, never opened a media socket and
        never reached `conversation.py`. The hangup callback is the *only*
        signal it produces, so if this handler does not write a row, nothing
        anywhere records that the call happened.
        """
        call_app.post(
            "/hangup?attempt=att-abc123",
            data={
                "CallUUID": "MZ-1",
                "HangupCause": "NO_ANSWER",
                "HangupSource": "callee",
                "Duration": "0",
            },
        )

        [row] = settle(log_path)
        assert row["status"] == MISSED
        assert row["callId"] == "MZ-1"
        assert row["hangupCause"] == "NO_ANSWER"

    @pytest.mark.parametrize(
        "cause,source,expected,code",
        [
            ("NO_ANSWER", "callee", MISSED, call_status.NO_ANSWER),
            ("USER_BUSY", "callee", DECLINED, call_status.REMOTE_BUSY),
            ("CALL_REJECTED", "callee", DECLINED, call_status.REMOTE_DECLINED),
            ("DECLINE", "callee", DECLINED, call_status.REMOTE_DECLINED),
            (
                "ORIGINATOR_CANCEL",
                "caller",
                CANCELLED,
                call_status.CANCELLED_BY_US,
            ),
            ("NORMAL_CLEARING", "caller", CANCELLED, call_status.CANCELLED_BY_US),
            ("NETWORK_OUT_OF_ORDER", "", FAILED, call_status.CARRIER_ERROR),
            ("", "", FAILED, call_status.CARRIER_ERROR),
        ],
    )
    def test_hangup_causes_map_to_the_vocabulary(
        self, call_app, log_path, cause, source, expected, code
    ):
        call_app.post(
            "/hangup?attempt=att-abc123",
            data={"HangupCause": cause, "HangupSource": source},
        )

        [row] = settle(log_path)
        assert row["status"] == expected
        # The code is what a panel branches on, and it carries the distinction
        # the status deliberately drops: `declined` covers a handset that
        # rejected the call *and* one that was busy.
        assert row["endedCode"] == code
        assert row["endedReason"], "a terminal row must say why, in prose too"

    def test_a_declined_call_is_declined_not_missed(self, call_app, log_path):
        """
        🔴 The W5e transition. A declined test call is the commonest way one
        ends, and it used to be recorded as `missed` -- indistinguishable from
        a phone that rang out for a minute, which is the difference between
        "try again now" and "try again later".
        """
        call_app.post(
            "/hangup?attempt=att-abc123",
            data={
                "CallUUID": "MZ-1",
                "HangupCause": "CALL_REJECTED",
                "HangupSource": "callee",
                "Duration": "0",
            },
        )

        [row] = settle(log_path)
        assert row["status"] == DECLINED
        assert row["endedCode"] == call_status.REMOTE_DECLINED
        assert row["endedAt"], "the panel needs a time to leave its ringing screen on"

    def test_a_late_hangup_webhook_cannot_restate_a_real_conversation(
        self, call_app, log_path
    ):
        """
        The webhook and teardown are two processes' worth of latency apart and
        either can land first. `best_status` has always protected the *status*;
        until W5e nothing protected the sentence beside it, so a decline
        arriving after a forty-minute call left the row reading `completed` with
        "the far end declined the call" next to it.
        """
        call_history.append(
            call_history.revision(
                "att-abc123",
                status=COMPLETED,
                endedReason="the call ended",
                endedCode=call_status.CALL_COMPLETED,
            )
        )
        call_app.post(
            "/hangup?attempt=att-abc123",
            data={"HangupCause": "CALL_REJECTED", "HangupSource": "callee"},
        )

        [row] = settle(log_path, lambda found: found and "hangupCause" in found[0])
        assert row["status"] == COMPLETED
        assert row["endedCode"] == call_status.CALL_COMPLETED
        assert row["endedReason"] == "the call ended"
        # The cause is still recorded -- it is evidence, under its own key.
        assert row["hangupCause"] == "CALL_REJECTED"

    def test_the_carriers_duration_does_not_overwrite_the_measured_one(
        self, call_app, log_path
    ):
        """
        The authoritative duration is the loop's frozen monotonic measurement.
        A webhook arriving after teardown must not silently replace a measured
        number with a billed one.
        """
        call_history.append(
            call_history.revision("att-abc123", durationSeconds=42, status=COMPLETED)
        )
        call_app.post(
            "/hangup?attempt=att-abc123",
            data={"HangupCause": "NORMAL_CLEARING", "Duration": "45"},
        )

        [row] = settle(log_path, lambda found: found and "carrierDurationSeconds" in found[0])
        assert row["durationSeconds"] == 42
        assert row["carrierDurationSeconds"] == 45

    def test_a_webhook_without_an_attempt_writes_nothing(self, call_app, log_path):
        """
        A carrier configured application-side, or a stray request. It must not
        invent a row it cannot attribute -- an unattributable row is worse
        than a missing one, because it looks like a call that happened.
        """
        call_app.post("/hangup", data={"HangupCause": "NO_ANSWER"})

        # Nothing to wait for, so wait a beat and confirm nothing appeared.
        time.sleep(0.2)
        assert rows(log_path) == []


# =============================================================================
# REVISIONS FOLD
# =============================================================================

class TestRevisionsFold:
    def test_the_file_stays_append_only(self, log_path):
        for status in (PENDING, RINGING, IN_PROGRESS):
            call_history.append(call_history.revision("att-1", status=status))

        assert len(raw_lines(log_path)) == 3, "a revision must not rewrite the file"
        assert len(rows(log_path)) == 1, "the reader must fold them into one call"

    def test_the_newest_revision_wins(self, log_path):
        call_history.append(call_history.revision("att-1", status=PENDING))
        call_history.append(call_history.revision("att-1", status=COMPLETED))

        assert rows(log_path)[0]["status"] == COMPLETED

    def test_an_early_field_survives_a_later_revision(self, log_path):
        """
        🔴 Why the fold is field-level rather than last-line-wins.

        Only the origination stub knows the number dialled; the teardown
        record does not carry it at all. Last-line-wins would blank it.
        """
        call_history.append(
            call_history.revision("att-1", status=PENDING, to="+919876543210")
        )
        call_history.append(
            call_history.revision("att-1", status=COMPLETED, transcript=[{"x": 1}])
        )

        [row] = rows(log_path)
        assert row["to"] == "+919876543210"
        assert row["transcript"] == [{"x": 1}]

    def test_started_at_is_the_first_one(self, log_path):
        """
        An outbound attempt starts when it is placed, not when the media
        socket happens to open -- which is seconds later, and after the part
        an operator asking "why did nobody answer" cares about.
        """
        call_history.append(
            call_history.revision("att-1", startedAt="2026-07-27T10:00:00.000+00:00")
        )
        call_history.append(
            call_history.revision("att-1", startedAt="2026-07-27T10:00:09.000+00:00")
        )

        assert rows(log_path)[0]["startedAt"] == "2026-07-27T10:00:00.000+00:00"

    def test_an_empty_value_never_overwrites_a_known_one(self, log_path):
        call_history.append(call_history.revision("att-1", to="+919876543210"))
        call_history.append({"id": "att-1", "to": "", "status": COMPLETED})

        assert rows(log_path)[0]["to"] == "+919876543210"

    def test_two_calls_stay_two_rows(self, log_path):
        call_history.append(call_history.revision("att-1", status=PENDING))
        call_history.append(call_history.revision("att-2", status=PENDING))
        call_history.append(call_history.revision("att-1", status=COMPLETED))

        assert [row["id"] for row in rows(log_path)] == ["att-1", "att-2"]

    def test_limit_counts_calls_not_lines(self, log_path):
        for index in range(4):
            for status in (PENDING, RINGING, COMPLETED):
                call_history.append(
                    call_history.revision(f"att-{index}", status=status)
                )

        assert len(call_history.load(limit=2, path=log_path)) == 2

    def test_compaction_folds_the_file(self, log_path, monkeypatch):
        monkeypatch.setattr(call_history, "TRIM_ABOVE_BYTES", 1)

        for status in (PENDING, RINGING, IN_PROGRESS, COMPLETED):
            call_history.append(call_history.revision("att-1", status=status))

        assert len(raw_lines(log_path)) == 1, "compaction must collapse revisions"
        assert rows(log_path)[0]["status"] == COMPLETED

    def test_count_counts_calls_not_revisions(self, log_path):
        for status in (PENDING, RINGING, COMPLETED):
            call_history.append(call_history.revision("att-1", status=status))

        assert call_history.count(path=log_path) == 1

    def test_find_accepts_either_id(self, log_path):
        call_history.append(
            call_history.revision("att-1", callId="MZ-9", status=COMPLETED)
        )

        assert call_history.find("att-1", path=log_path)["callId"] == "MZ-9"
        assert call_history.find("MZ-9", path=log_path)["id"] == "att-1"
        assert call_history.find("nope", path=log_path) is None


# =============================================================================
# STATUS RANKING
# =============================================================================

class TestStatusRanking:
    def test_a_late_hangup_cannot_demote_a_finished_call(self, log_path):
        """
        🔴 The race this exists for.

        The carrier's hangup webhook and the call loop's teardown are two
        processes' worth of latency apart, and either can land first. Without
        ranking, a webhook arriving second rewrites a real conversation as
        `missed`.
        """
        call_history.append(call_history.revision("att-1", status=COMPLETED))
        call_history.append(call_history.revision("att-1", status=MISSED))

        assert rows(log_path)[0]["status"] == COMPLETED

    def test_a_terminal_status_beats_a_transient_one(self, log_path):
        call_history.append(call_history.revision("att-1", status=COMPLETED))
        call_history.append(call_history.revision("att-1", status=RINGING))

        assert rows(log_path)[0]["status"] == COMPLETED

    def test_progress_still_moves_forward(self, log_path):
        call_history.append(call_history.revision("att-1", status=PENDING))
        call_history.append(call_history.revision("att-1", status=RINGING))
        call_history.append(call_history.revision("att-1", status=IN_PROGRESS))

        assert rows(log_path)[0]["status"] == IN_PROGRESS

    def test_an_unknown_status_does_not_win(self, log_path):
        """
        A document written by a future build with a vocabulary this one has
        never heard of must degrade to something renderable.
        """
        call_history.append(call_history.revision("att-1", status=COMPLETED))
        call_history.append(call_history.revision("att-1", status="transcribing"))

        assert rows(log_path)[0]["status"] == COMPLETED

    def test_declined_outranks_missed(self, log_path):
        """
        Both describe an unanswered call and they can both be written for one:
        a ring-out timeout and a decline can race. `declined` wins because it
        rests on the far end having *done* something we were told about, where
        `missed` is only ever a conclusion drawn from nothing having happened.
        """
        call_history.append(call_history.revision("att-1", status=MISSED))
        call_history.append(call_history.revision("att-1", status=DECLINED))
        assert rows(log_path)[0]["status"] == DECLINED

        call_history.append(call_history.revision("att-2", status=DECLINED))
        call_history.append(call_history.revision("att-2", status=MISSED))
        assert rows(log_path)[0]["status"] == DECLINED

    def test_completed_still_beats_declined(self, log_path):
        call_history.append(call_history.revision("att-1", status=COMPLETED))
        call_history.append(call_history.revision("att-1", status=DECLINED))

        assert rows(log_path)[0]["status"] == COMPLETED

    @pytest.mark.parametrize("webhook_first", [True, False])
    def test_the_reason_follows_the_status_that_won(self, log_path, webhook_first):
        """
        🔴 The ranking protects the status; until W5e nothing protected the
        sentence beside it.

        A row reading `completed` with "the far end declined the call" next to
        it is worse than either half alone -- it is the one shape an operator
        cannot reason about. So the ended pair belongs to whichever revision
        supplied the winning status, in *either* arrival order.
        """
        teardown = call_history.revision(
            "att-1",
            status=COMPLETED,
            endedReason="the call ended",
            endedCode=call_status.CALL_COMPLETED,
        )
        webhook = call_history.revision(
            "att-1",
            status=DECLINED,
            endedReason="the far end declined the call",
            endedCode=call_status.REMOTE_DECLINED,
        )

        for record in (webhook, teardown) if webhook_first else (teardown, webhook):
            call_history.append(record)

        [row] = rows(log_path)
        assert row["status"] == COMPLETED
        assert row["endedCode"] == call_status.CALL_COMPLETED
        assert row["endedReason"] == "the call ended"

    def test_the_losing_reason_is_dropped_not_kept_as_a_fallback(self, log_path):
        """
        A reason that explains a status this row does not have is worse than no
        reason: the panel derives one from the status
        (`call_status.code_for_status`), which is at least consistent with it.
        """
        call_history.append(call_history.revision("att-1", status=COMPLETED))
        call_history.append(
            call_history.revision(
                "att-1", status=MISSED, endedReason="nobody answered"
            )
        )

        [row] = rows(log_path)
        assert row["status"] == COMPLETED
        assert "endedReason" not in row


# =============================================================================
# THE ATTEMPT ID
# =============================================================================

class TestTheAttemptId:
    def test_it_is_unguessable(self):
        ids = {server.new_attempt_id() for _ in range(50)}
        assert len(ids) == 50
        assert all(value.startswith("att-") for value in ids)

    def test_it_is_signed_into_the_stream_token(self, monkeypatch):
        """
        Unsigned, anyone who could reach `/ws` could write a transcript into
        somebody else's call record -- and a call log the caller can write to
        is not a record of anything.
        """
        monkeypatch.setenv("SHUO_STREAM_SECRET", "s3cret")
        token = config.mint_stream_token(
            persona_id="candidate",
            direction="outbound",
            expires_at=2**31,
            attempt="att-abc123",
        )

        assert config.verify_stream_token(
            token,
            persona_id="candidate",
            direction="outbound",
            expires_at=str(2**31),
            attempt="att-abc123",
        )
        assert not config.verify_stream_token(
            token,
            persona_id="candidate",
            direction="outbound",
            expires_at=str(2**31),
            attempt="att-someone-else",
        ), "a substituted attempt id must fail the signature"

    @pytest.mark.parametrize(
        "supplied",
        ["../../etc/passwd", "att-../x", "att-ABC!", "", "   ", "x" * 200],
    )
    def test_a_malformed_id_is_dropped_not_trusted(self, supplied):
        """
        It arrives through URLs the carrier controls, and in W5b it will name
        a file. Dropped rather than rejected: a malformed id costs a row in a
        table, never a call that does not happen.
        """
        assert server._clean_attempt(supplied) == ""

    def test_a_well_formed_id_survives(self):
        assert server._clean_attempt("att-0123456789ab") == "att-0123456789ab"

    def test_the_monitor_adopts_it_as_the_call_id(self):
        """
        What makes the live view, the history row and (in W5b) the recording
        file all key on one string.
        """
        MONITOR.reset()
        recorder = MONITOR.begin(
            persona="candidate", direction="outbound", attempt="att-abc123"
        )

        assert recorder.id == "att-abc123"
        assert MONITOR.snapshot()["id"] == "att-abc123"
        MONITOR.reset()

    def test_a_socket_without_one_still_works(self):
        """A direct `/ws` connection, and every test that opens one."""
        MONITOR.reset()
        recorder = MONITOR.begin(persona="candidate", direction="outbound")

        assert recorder.id.startswith("call-")
        MONITOR.reset()


# =============================================================================
# END TO END THROUGH THE REAL LOOP
# =============================================================================

class TestOneCallIsOneRow:
    """
    Driven through `run_conversation`, because what breaks is the *wiring*.

    Every piece of this can be right on its own and still produce two rows in
    the panel for one phone call: an origination stub that stays `pending`
    forever, and a completed call that appears from nowhere with no number
    attached. That is the failure this test exists to catch, and nothing
    smaller than the real loop catches it.
    """

    @pytest.mark.asyncio
    async def test_the_stub_and_the_conversation_fold_into_one_call(
        self, log_path, monkeypatch
    ):
        import asyncio

        from starlette.websockets import WebSocketDisconnect

        import shuo.conversation as conv
        from shuo.carrier import get_carrier, reset_carrier_cache
        from shuo.types import CallContext, CallDirection

        from tests.test_integration import (
            MULAW_SILENCE_FRAME,
            StubFlux,
            StubTTSPool,
            VobizProtocol,
        )

        for key, value in {
            "CARRIER": "vobiz",
            "VOBIZ_AUTH_ID": "MA_TEST",
            "VOBIZ_AUTH_TOKEN": "test-auth-token",
            "VOBIZ_PHONE_NUMBER": "+911234567890",
            "PUBLIC_URL": "https://shuo.test",
            "PERSONA": "candidate",
            "GROQ_API_KEY": "test-key-not-used",
            "RECORD_CALLS": "false",
        }.items():
            monkeypatch.setenv(key, value)

        reset_carrier_cache()
        StubFlux.instances.clear()
        StubTTSPool.instances.clear()
        MONITOR.reset()

        # 1. The operator places the call. This is the row that exists while
        #    the phone is still ringing.
        placed = await server.place_outbound_call(
            "+919876543210", "candidate", carrier=FakeCarrier()
        )
        attempt = placed["attempt"]
        await SPOOL.drain()

        assert rows(log_path)[0]["status"] == PENDING

        # 2. The callee answers and the media socket opens carrying the same
        #    attempt id, signed into the <Stream> URL by `config.websocket_url`.
        class DrainingWebSocket:
            def __init__(self, frames):
                self._frames = list(frames)
                self.sent = []

            async def receive_text(self):
                if self._frames:
                    await asyncio.sleep(0)
                    return json.dumps(self._frames.pop(0))
                await asyncio.sleep(0.1)
                raise WebSocketDisconnect(code=1006)

            async def send_text(self, text):
                self.sent.append(json.loads(text))

        class SpeakingFlux(StubFlux):
            async def send(self, audio_bytes):
                await super().send(audio_bytes)
                if len(self.fed) == 1:
                    await self.on_end_of_turn("So, tell me about yourself.")

        class QuietAgent:
            def __init__(self, **kwargs):
                self.recorder = kwargs.get("recorder")

            async def start_turn(self, transcript):
                self.recorder.agent_said("Sure, ji.", turn=1)

            async def cancel_turn(self):
                pass

            async def cleanup(self):
                pass

        monkeypatch.setattr(conv, "FluxService", SpeakingFlux)
        monkeypatch.setattr(conv, "TTSPool", StubTTSPool)
        monkeypatch.setattr(conv, "Agent", QuietAgent)

        ws = DrainingWebSocket([
            VobizProtocol.start("s1", "MZ-live"),
            VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1),
        ])
        context = CallContext(
            call_id="",
            direction=CallDirection.OUTBOUND,
            persona_id="candidate",
            carrier="vobiz",
            attempt_id=attempt,
        )

        try:
            await asyncio.wait_for(
                conv.run_conversation(ws, context, get_carrier("vobiz")), timeout=5.0
            )
        finally:
            reset_carrier_cache()
        await SPOOL.drain()

        # 3. One call, not two.
        stored = rows(log_path)
        assert len(stored) == 1, (
            f"one phone call produced {len(stored)} rows in the panel"
        )

        [row] = stored
        assert row["id"] == attempt
        assert row["status"] == COMPLETED
        # From the origination stub...
        assert row["to"] == "+919876543210"
        # ...and from the conversation.
        assert row["callId"] == "MZ-live"
        assert [turn["text"] for turn in row["transcript"]] == [
            "So, tell me about yourself.",
            "Sure, ji.",
        ]

        # ...and W5b's recording, keyed on the same id, so the panel's play
        # button is a property of the row rather than a second lookup. The
        # stub agent plays no audio, so this is the caller's side only --
        # which is exactly what a missed call's recording looks like, and it
        # still has to be produced.
        from shuo import recording

        assert row["recording"]["available"] is True
        assert row["recording"]["url"] == f"/v1/calls/{attempt}/recording"
        assert recording.recording_path(attempt).exists(), (
            "the row claims a recording that is not on disk"
        )
        MONITOR.reset()
