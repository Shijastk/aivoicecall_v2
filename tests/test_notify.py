"""
Tests for W5d's push notifier.

Four things are being defended here, in descending order of how badly they
would hurt if they broke:

1. **It is off by default, and silent when off.** An unconfigured install must
   create no task, make no request, and say nothing. `TestOffByDefault`.
2. **The topic never reaches a log line or `/health`.** On ntfy the topic in
   the URL *is* the credential, so a redaction bug is a credential leak.
   `TestTheTopicIsASecret`.
3. **It cannot become an alarm.** One notification per call per transition, a
   global ceiling per minute, and nothing announced about calls that were
   already in the log when the process started. `TestItCannotBecomeAnAlarm`.
4. **It cannot break anything else.** A dead vendor, a 403, a torn log: every
   one is swallowed, counted, and costs one tick. `TestItCannotCostAnything`.
"""

import asyncio

import httpx
import pytest

import shuo.call_history as call_history
import shuo.notify as notify
from shuo.notify import (
    CALL_FAILED,
    CALL_MISSED,
    INBOUND_STARTED,
    Notification,
    Notifier,
    notification_for,
)


TOPIC_URL = "https://ntfy.sh/shuo-secret-topic-9f3a2b1c"


@pytest.fixture
def sink(monkeypatch):
    """
    A fake ntfy, mounted under the notifier's own client.

    Mounted rather than stubbing `_send`, so the request goes through the real
    header-building and rate-limiting code. A test that stubbed the send would
    prove only that the tick calls the function it calls.
    """
    class Sink:
        def __init__(self):
            self.requests = []
            self.status_code = 200

        def handler(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status_code)

        @property
        def last(self):
            return self.requests[-1]

        @property
        def bodies(self):
            return [r.content.decode() for r in self.requests]

    fake = Sink()
    monkeypatch.setenv("SHUO_NOTIFY_URL", TOPIC_URL)
    monkeypatch.delenv("SHUO_NOTIFY_TOKEN", raising=False)
    yield fake


@pytest.fixture
def notifier(sink):
    """A primed notifier wired to the sink. Primed, because a fresh one is not."""
    instance = Notifier(clock=lambda: 1000.0)
    instance._client = httpx.AsyncClient(
        transport=httpx.MockTransport(sink.handler)
    )
    return instance


def row(call_ref: str, **fields) -> dict:
    """One folded log row, as `call_history.load` would return it."""
    record = {
        "id": call_ref,
        "direction": "inbound",
        "persona": "candidate",
        "from": "+919876543210",
        "to": "+911234567890",
        "startedAt": call_history.now_iso(),
    }
    record.update(fields)
    return record


def write(call_ref: str, **fields) -> None:
    """Write one revision to the (tmp_path-isolated) call log."""
    call_history.append(call_history.revision(call_ref, **fields))


async def prime(notifier) -> None:
    """One tick, whose only job is to record what is already on disk."""
    await notifier.tick()


# =============================================================================
# WHAT IS WORTH A NOTIFICATION
# =============================================================================

class TestThePolicy:
    """
    `notification_for` is pure and is the only place the policy lives, so this
    is where "what fires, and on what" is decided once rather than per test.
    """

    def test_an_inbound_call_starting_is_news(self):
        result = notification_for(row("att-1", status="ringing"), "", frozenset())

        assert result is not None
        assert result.kind == INBOUND_STARTED
        assert "+919876543210" in result.message

    def test_an_inbound_call_answered_straight_away_is_still_news(self):
        """
        The `ringing` revision can be missed entirely -- the row may first be
        read at `in_progress` if the tick landed between the two writes.
        """
        result = notification_for(
            row("att-1", status="in_progress"), "", frozenset()
        )

        assert result is not None and result.kind == INBOUND_STARTED

    def test_an_outbound_call_starting_is_not(self):
        """The operator placed it. Telling them about it is noise."""
        result = notification_for(
            row("att-1", direction="outbound", status="ringing"), "", frozenset()
        )

        assert result is None

    def test_a_missed_call_is_news(self):
        result = notification_for(
            row("att-1", status="missed", durationSeconds=18), "ringing", frozenset()
        )

        assert result is not None
        assert result.kind == CALL_MISSED
        assert result.priority == "high"
        assert "18s" in result.message

    def test_a_missed_outbound_call_is_news_too(self):
        """
        Unlike a *starting* outbound call: the operator asked for this one and
        it did not connect, which is the outcome they are waiting on.
        """
        result = notification_for(
            row("att-1", direction="outbound", status="missed"),
            "ringing",
            frozenset(),
        )

        assert result is not None and result.kind == CALL_MISSED

    def test_a_failed_call_carries_the_reason(self):
        result = notification_for(
            row("att-1", status="failed", endedReason="origination refused"),
            "pending",
            frozenset(),
        )

        assert result is not None
        assert result.kind == CALL_FAILED
        assert "origination refused" in result.message

    def test_a_completed_call_is_not_news(self):
        """The system working is not an event. A notifier that fires on
        everything is one that gets muted."""
        assert notification_for(
            row("att-1", status="completed"), "in_progress", frozenset()
        ) is None

    def test_a_cancelled_call_is_not_news(self):
        """The operator stopped it themselves."""
        assert notification_for(
            row("att-1", status="cancelled"), "ringing", frozenset()
        ) is None

    def test_an_unchanged_status_is_never_news(self):
        assert notification_for(
            row("att-1", status="missed"), "missed", frozenset()
        ) is None

    def test_a_transition_already_sent_does_not_repeat(self):
        assert notification_for(
            row("att-1", status="ringing"), "pending", frozenset({INBOUND_STARTED})
        ) is None

    def test_a_row_with_no_status_says_nothing(self):
        assert notification_for(row("att-1"), "", frozenset()) is None

    def test_no_notification_carries_a_transcript(self):
        """
        🔴 This is the only thing in the system that sends call data off the
        machine. Metadata is the deliberate limit -- the difference between "you
        missed a call" and the contents of a conversation.
        """
        loaded = row(
            "att-1",
            status="missed",
            transcript=[{"speaker": "caller", "text": "So, tell me about yourself."}],
        )

        result = notification_for(loaded, "ringing", frozenset())

        assert result is not None
        assert "tell me about yourself" not in result.message
        assert "tell me about yourself" not in result.title


# =============================================================================
# OFF BY DEFAULT
# =============================================================================

class TestOffByDefault:
    def test_it_is_disabled_with_no_url(self, monkeypatch):
        monkeypatch.delenv("SHUO_NOTIFY_URL", raising=False)

        assert notify.enabled() is False

    def test_start_creates_no_task(self, monkeypatch):
        monkeypatch.delenv("SHUO_NOTIFY_URL", raising=False)
        instance = Notifier()

        async def go():
            assert instance.start() is False
            assert instance._task is None
            await instance.stop()  # must be safe anyway

        asyncio.run(go())

    def test_a_blank_url_is_off_not_a_crash(self, monkeypatch):
        monkeypatch.setenv("SHUO_NOTIFY_URL", "   ")

        assert notify.enabled() is False

    @pytest.mark.asyncio
    async def test_nothing_is_sent_when_off(self, monkeypatch, sink, notifier):
        monkeypatch.delenv("SHUO_NOTIFY_URL", raising=False)
        write("att-1", status="ringing", direction="inbound")
        await prime(notifier)
        write("att-1", status="missed")

        await notifier.tick()

        assert sink.requests == []

    def test_stats_report_the_ordinary_state(self, monkeypatch):
        monkeypatch.delenv("SHUO_NOTIFY_URL", raising=False)

        stats = Notifier().stats()

        assert stats["enabled"] is False
        assert stats["running"] is False
        assert stats["target"] == ""
        assert stats["sent"] == 0


# =============================================================================
# THE TOPIC IS A SECRET
# =============================================================================

class TestTheTopicIsASecret:
    """
    🔴 On ntfy the topic in the URL *is* the credential -- anyone who knows it
    can read every notification. A redaction bug here is a credential leak into
    a log file, which is exactly the place secrets outlive the process.
    """

    def test_the_topic_is_not_in_the_redacted_target(self, sink):
        redacted = notify.redacted_target()

        assert "shuo-secret-topic-9f3a2b1c" not in redacted
        assert "ntfy.sh" in redacted, "the host is the part worth keeping"

    def test_not_even_a_prefix_of_the_topic_survives(self, sink):
        """A prefix of a topic is a prefix of a password."""
        assert "shuo-secret" not in notify.redacted_target()

    def test_health_never_carries_the_topic(self, sink):
        assert "shuo-secret-topic-9f3a2b1c" not in str(Notifier().stats())

    def test_a_send_failure_is_logged_without_the_topic(self, monkeypatch, caplog):
        monkeypatch.setenv("SHUO_NOTIFY_URL", TOPIC_URL)
        instance = Notifier()
        instance._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))
            )
        )

        async def go():
            with caplog.at_level("WARNING"):
                await instance._send(
                    Notification(kind="missed", call_ref="att-1",
                                 title="Missed call", message="x")
                )

        asyncio.run(go())

        assert caplog.records, "a failure the operator can fix must be logged"
        for record in caplog.records:
            assert "shuo-secret-topic-9f3a2b1c" not in record.getMessage()

    def test_a_refusal_is_logged_without_the_topic(self, monkeypatch, caplog):
        monkeypatch.setenv("SHUO_NOTIFY_URL", TOPIC_URL)
        instance = Notifier()
        instance._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(403))
        )

        async def go():
            with caplog.at_level("WARNING"):
                await instance._send(
                    Notification(kind="missed", call_ref="att-1",
                                 title="Missed call", message="x")
                )

        asyncio.run(go())

        assert caplog.records
        for record in caplog.records:
            assert "shuo-secret-topic-9f3a2b1c" not in record.getMessage()

    def test_starting_the_poller_does_not_log_the_topic(self, sink, caplog):
        instance = Notifier()

        async def go():
            with caplog.at_level("INFO"):
                instance.start()
            await instance.stop()

        asyncio.run(go())

        for record in caplog.records:
            assert "shuo-secret-topic-9f3a2b1c" not in record.getMessage()

    def test_an_unparseable_url_still_redacts(self, monkeypatch):
        monkeypatch.setenv("SHUO_NOTIFY_URL", "not a url at all")

        assert "not a url at all" not in notify.redacted_target()


# =============================================================================
# THE POLL
# =============================================================================

class TestThePoll:
    @pytest.mark.asyncio
    async def test_a_missed_call_reaches_the_sink(self, notifier, sink):
        write("att-1", status="ringing", direction="inbound",
              **{"from": "+919876543210"})
        await prime(notifier)

        write("att-1", status="missed")
        sent = await notifier.tick()

        assert [n.kind for n in sent] == [CALL_MISSED]
        assert len(sink.requests) == 1
        assert "+919876543210" in sink.bodies[0]

    @pytest.mark.asyncio
    async def test_the_request_is_a_post_with_ntfys_headers(self, notifier, sink):
        await prime(notifier)
        write("att-1", status="ringing", direction="inbound")
        await notifier.tick()

        assert sink.last.method == "POST"
        assert sink.last.headers["Title"] == "Incoming call"
        assert sink.last.headers["Tags"]
        assert sink.last.headers["Content-Type"].startswith("text/plain")

    @pytest.mark.asyncio
    async def test_the_bearer_token_is_sent_when_set(
        self, notifier, sink, monkeypatch
    ):
        monkeypatch.setenv("SHUO_NOTIFY_TOKEN", "tk_live_123")
        await prime(notifier)
        write("att-1", status="failed", direction="outbound")

        await notifier.tick()

        assert sink.last.headers["Authorization"] == "Bearer tk_live_123"

    @pytest.mark.asyncio
    async def test_no_token_means_no_auth_header(self, notifier, sink):
        await prime(notifier)
        write("att-1", status="missed", direction="inbound")

        await notifier.tick()

        assert "authorization" not in {k.lower() for k in sink.last.headers}

    @pytest.mark.asyncio
    async def test_a_steady_state_tick_sends_nothing(self, notifier, sink):
        write("att-1", status="in_progress", direction="inbound")
        await prime(notifier)

        assert await notifier.tick() == []
        assert await notifier.tick() == []
        assert sink.requests == []

    @pytest.mark.asyncio
    async def test_an_empty_log_is_not_an_error(self, notifier, sink):
        assert await notifier.tick() == []
        assert await notifier.tick() == []

    @pytest.mark.asyncio
    async def test_two_calls_are_both_reported(self, notifier, sink):
        await prime(notifier)
        write("att-1", status="ringing", direction="inbound")
        write("att-2", status="failed", direction="outbound")

        sent = await notifier.tick()

        assert {n.call_ref for n in sent} == {"att-1", "att-2"}

    @pytest.mark.asyncio
    async def test_a_call_that_rings_then_is_missed_reports_both(
        self, notifier, sink
    ):
        await prime(notifier)

        write("att-1", status="ringing", direction="inbound")
        first = await notifier.tick()

        write("att-1", status="missed")
        second = await notifier.tick()

        assert [n.kind for n in first] == [INBOUND_STARTED]
        assert [n.kind for n in second] == [CALL_MISSED]


# =============================================================================
# IT CANNOT BECOME AN ALARM
# =============================================================================

class TestItCannotBecomeAnAlarm:
    @pytest.mark.asyncio
    async def test_the_calls_already_in_the_log_are_not_announced(
        self, notifier, sink
    ):
        """
        🔴 A restart must not page the operator about forty old calls. The first
        tick records; only changes after it are notified.
        """
        for index in range(5):
            write(f"att-{index}", status="missed", direction="inbound")

        sent = await notifier.tick()

        assert sent == []
        assert sink.requests == []

    @pytest.mark.asyncio
    async def test_a_flapping_carrier_reports_one_ringing_phone(
        self, notifier, sink
    ):
        """
        `ringing` -> `pending` -> `ringing` -> `in_progress` describes one
        ringing phone, not four.

        Two guards stack here, and it is worth knowing which does what. The
        log's own status *rank* absorbs the backwards steps before this module
        sees them -- a folded row never demotes from `ringing` to `pending`. The
        per-call, per-transition set is what absorbs the rest, including the
        genuine `ringing` -> `in_progress` promotion, which is a real status
        change and still the same ringing phone.
        """
        write("att-1", direction="inbound", status="pending")
        await prime(notifier)

        for status in ("ringing", "pending", "ringing", "in_progress", "ringing"):
            write("att-1", status=status)
            await notifier.tick()

        assert len(sink.requests) == 1
        assert sink.last.headers["Title"] == "Incoming call"

    @pytest.mark.asyncio
    async def test_a_flood_is_dropped_and_counted(self, notifier, sink):
        await prime(notifier)

        for index in range(notify.MAX_SENDS_PER_MINUTE + 8):
            write(f"att-{index}", status="missed", direction="inbound")

        await notifier.tick()

        assert len(sink.requests) == notify.MAX_SENDS_PER_MINUTE
        assert notifier.stats()["dropped"] == 8

    @pytest.mark.asyncio
    async def test_the_ceiling_is_a_window_not_a_lifetime_cap(self, sink):
        """A quiet hour must restore the budget, or the notifier silently
        stops working for the life of the process."""
        clock = {"now": 1000.0}
        instance = Notifier(clock=lambda: clock["now"])
        instance._client = httpx.AsyncClient(
            transport=httpx.MockTransport(sink.handler)
        )

        await prime(instance)
        for index in range(notify.MAX_SENDS_PER_MINUTE):
            write(f"att-{index}", status="missed", direction="inbound")
        await instance.tick()
        assert len(sink.requests) == notify.MAX_SENDS_PER_MINUTE

        clock["now"] += 120.0
        write("att-later", status="missed", direction="inbound")
        await instance.tick()

        assert len(sink.requests) == notify.MAX_SENDS_PER_MINUTE + 1

    @pytest.mark.asyncio
    async def test_the_flood_warning_is_not_itself_a_flood(
        self, notifier, sink, caplog
    ):
        await prime(notifier)
        for index in range(notify.MAX_SENDS_PER_MINUTE + 20):
            write(f"att-{index}", status="missed", direction="inbound")

        with caplog.at_level("WARNING"):
            await notifier.tick()

        floods = [r for r in caplog.records if "not become an alarm" in r.getMessage()]
        assert len(floods) == 1

    @pytest.mark.asyncio
    async def test_a_failed_send_is_not_retried_every_second(
        self, monkeypatch, sink
    ):
        """
        One attempt per transition. A vendor outage must not become a request
        per second for the life of the process -- `failed` on /health is how it
        surfaces instead.

        Two guards could produce this and only one of them is being tested here,
        so the statuses deliberately keep *changing*: `pending` -> `ringing` ->
        `in_progress` are three different statuses that all mean the same one
        notification, so the memo of the last status cannot be what suppresses
        the retry. Only marking the transition as attempted -- before the
        network, not after it -- can.
        """
        monkeypatch.setenv("SHUO_NOTIFY_URL", TOPIC_URL)
        attempts = []

        def refuse(request):
            attempts.append(request)
            raise httpx.ConnectError("down")

        instance = Notifier()
        instance._client = httpx.AsyncClient(
            transport=httpx.MockTransport(refuse)
        )

        write("att-1", direction="inbound", status="pending")
        await prime(instance)

        for status in ("ringing", "in_progress", "ringing", "in_progress"):
            write("att-1", status=status)
            await instance.tick()

        assert len(attempts) == 1, "a failing send was retried"
        assert instance.stats()["failed"] == 1

    @pytest.mark.asyncio
    async def test_tracking_is_bounded(self, notifier, sink, monkeypatch):
        monkeypatch.setattr(notify, "MAX_TRACKED_CALLS", 5)
        monkeypatch.setattr(notify, "SCAN_CALLS", 40)

        for index in range(30):
            write(f"att-{index}", status="completed", direction="outbound")
        await notifier.tick()

        assert len(notifier._tracked) <= 5


# =============================================================================
# IT CANNOT COST ANYTHING
# =============================================================================

class TestItCannotCostAnything:
    @pytest.mark.asyncio
    async def test_a_dead_vendor_does_not_raise(self, monkeypatch, sink):
        monkeypatch.setenv("SHUO_NOTIFY_URL", TOPIC_URL)
        instance = Notifier()
        instance._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))
            )
        )

        await prime(instance)
        write("att-1", status="missed", direction="inbound")

        assert await instance.tick() == []  # must not raise
        assert instance.stats()["failed"] == 1

    @pytest.mark.asyncio
    async def test_a_refusal_is_counted_not_raised(self, notifier, sink):
        sink.status_code = 403
        await prime(notifier)
        write("att-1", status="missed", direction="inbound")

        assert await notifier.tick() == []
        assert notifier.stats()["failed"] == 1

    @pytest.mark.asyncio
    async def test_a_torn_log_line_does_not_stop_the_poll(self, notifier, sink):
        """
        The expected corrupt state: the writer killed mid-append. The reader
        already tolerates it, and the notifier must not be the thing that turns
        a lost revision into a dead poller.
        """
        write("att-1", status="ringing", direction="inbound")
        await prime(notifier)

        path = call_history.default_history_path()
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"id": "att-2", "status": "mis')

        write("att-1", status="missed")
        sent = await notifier.tick()

        assert [n.kind for n in sent] == [CALL_MISSED]

    @pytest.mark.asyncio
    async def test_a_missing_log_is_not_an_error(self, notifier, sink):
        """No calls have been made on this install. Not a warning, not a crash."""
        call_history.default_history_path().unlink(missing_ok=True)

        assert await notifier.tick() == []

    @pytest.mark.asyncio
    async def test_the_loop_outlives_a_failing_tick(self, notifier, monkeypatch):
        """
        The contract is that the poller survives its own bugs: a tick that
        raises costs one tick, not the notifier.
        """
        calls = {"n": 0}

        async def sometimes_explode():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("something in the tick broke")
            return []

        monkeypatch.setattr(notifier, "tick", sometimes_explode)
        monkeypatch.setattr(notifier, "_poll_seconds", 0.001)

        notifier._task = asyncio.create_task(notifier._run())
        await asyncio.sleep(0.05)
        still_running = not notifier._task.done()
        await notifier.stop()

        assert calls["n"] > 1, "the loop stopped at the first exception"
        assert still_running

    @pytest.mark.asyncio
    async def test_stop_is_safe_before_start_and_twice(self, notifier):
        await notifier.stop()
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_starting_twice_does_not_double_the_poller(self, sink):
        instance = Notifier()
        try:
            assert instance.start() is True
            first = instance._task
            assert instance.start() is True
            assert instance._task is first
        finally:
            await instance.stop()

    @pytest.mark.asyncio
    async def test_the_log_read_is_off_the_event_loop(self, notifier, monkeypatch):
        """
        A file read, however small, on the loop that also serves the operator's
        panel. `asyncio.to_thread` is not decoration.
        """
        threaded = []
        original = asyncio.to_thread

        async def watched(func, *args, **kwargs):
            threaded.append(func)
            return await original(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", watched)
        await notifier.tick()

        assert call_history.load in threaded
