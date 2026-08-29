"""
Push notifications for the calls nobody was watching -- Phase 8 W5d.

The panel already shows everything this sends. That is exactly the problem it
solves: a missed inbound call is only visible to somebody who is *looking*, and
the whole point of a twin answering the phone is that nobody has to be.

**Off by default.** Nothing here runs, and nothing is sent, unless
`SHUO_NOTIFY_URL` is set. An unconfigured install does not start a task.

    SHUO_NOTIFY_URL     e.g. https://ntfy.sh/<long-random-topic>
    SHUO_NOTIFY_TOKEN   optional, sent as `Authorization: Bearer …`

### Why ntfy.sh, and what it costs

There is **no free permanent SMS to an Indian handset.** A2P SMS needs DLT
principal-entity registration, which CLAUDE.md §2.1 puts out of scope, and every
provider trial is credit-limited, expiring and usually card-gated -- so anything
built on one would stop working without warning or start billing. ntfy needs no
account, no key and no card, and its free app turns a `POST` into a real push on
the operator's phone.

🔴 **The topic name is the entire secret.** Anyone who knows it can read every
notification. So it lives in `.env`, never in the panel, and **never in a log
line** -- every message this module logs goes through `redacted_target()`.

🔴 **This is the only thing in the system that sends call data off the
machine.** It carries metadata -- direction, number, persona, duration -- and
deliberately **no transcript**: the difference between "you missed a call from
+9198…" and the contents of a conversation is the difference between a
notification and a data export. Whether even that is acceptable is a deployment
decision, which is why the default is off.

### Where it runs, and why not on :3040

In the config process, and it could not be anywhere else. An outbound HTTPS
request with a DNS lookup and a TLS handshake in front of it has no business on
the loop that paces a 160-byte frame every 20ms (decision 32), and a notifier
whose vendor is having an outage must never be able to affect a call. Sending
from :3041 makes that structural rather than careful.

### It carries its own poller, and polls the *log*

There is no SSE fan-out to piggyback on (decision 49, and the reasoning is on
`/calls/live` in `server.py`), and driving this from the panel's polls would
mean notifications arriving only when somebody is already looking at the
screen -- the opposite of the point. So it owns a ~1Hz task, started from
:3041's lifespan only when a URL is configured.

It polls **the call log on disk**, not `/v1/calls/active`, which is a
correction to `docs/phase8-plan.md` §5. That route answers "what is in flight"
and therefore *excludes* terminal rows by design, so `missed` and `failed` --
two of the three things worth notifying about -- would only ever have appeared
there as a call quietly vanishing. The log is where a terminal status is
actually written. Reading it instead also means the notifier needs nothing from
:3040 at all: it keeps working while the call server is restarting, which is a
window in which calls genuinely go missed.

The read is a small file, but it is a *file*, so it goes through
`asyncio.to_thread` -- this loop also serves the operator's panel.

### Three postures, all the same as the rest of Phase 8

- **It never raises into anything.** A notifier that can break a poll, or a
  lifespan, is worse than no notifier. Every failure is logged and swallowed.
- **It is rate-limited twice**: once per call per transition, so a carrier
  flapping between `ringing` and `pending` cannot ring the operator's phone
  repeatedly about one call; and once globally per minute, so nothing else can
  either.
- **It primes silently on the first tick.** A restart must not announce the
  forty calls already in the log -- the state is seeded from what is on disk
  and only *changes* after that are notified.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from . import call_history
from .call_status import DECLINED, FAILED, IN_PROGRESS, MISSED, RINGING
from .log import get_logger

logger = get_logger("shuo.notify")


# =============================================================================
# POLICY
# =============================================================================

# How often the log is checked. The same 1Hz as the panel's polls, for the same
# reason: a notification that is a second late is indistinguishable from one
# that is not, and this is a file read either way.
POLL_SECONDS = 1.0

# How many calls' statuses are remembered. Bounded because this process is
# long-lived and the log is not: a machine running for a month must not hold a
# dict entry per call it ever made. Oldest out first, which can at worst
# re-notify about a call that fell out of the window and then changed status --
# hundreds of calls later, which never happens.
MAX_TRACKED_CALLS = 500

# How many calls are folded out of the log per tick. A transition worth
# notifying about is always among the newest, so this is the same reasoning as
# `config_api.ACTIVE_SCAN_CALLS`, one size smaller because this runs unattended.
SCAN_CALLS = 40

# The global ceiling. A carrier flapping, or a bug in a write site, must not be
# able to turn the operator's handset into an alarm -- so beyond this the sends
# are dropped and counted rather than queued, because a notification that
# arrives ten minutes late is noise with a timestamp on it.
MAX_SENDS_PER_MINUTE = 10
_RATE_WINDOW_SECONDS = 60.0

# Connect fast, then allow a slow third party a moment. Bounded tightly because
# nothing waits on this and nothing retries it: the next tick is a second away.
SEND_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)

# ntfy reads the message from the body and the rest from headers. Sent as plain
# text rather than JSON because that is ntfy's documented contract for a
# topic URL.
#
# `_headers_for` is the one seam a different target would need: Slack and
# Discord want a JSON body with their own key names, so pointing
# `SHUO_NOTIFY_URL` at one of those needs a shape adapter here and nothing
# else. Left undone rather than guessed at -- an untested adapter for a service
# nobody has configured is a liability, not genericity.
_CONTENT_TYPE = "text/plain; charset=utf-8"


def notify_url() -> str:
    """
    Where notifications go, or `""` for off.

    Read from the environment on every use rather than captured at import, so
    the tests -- and a `.env` fix -- do not need a restart to take effect.
    """
    return os.getenv("SHUO_NOTIFY_URL", "").strip()


def notify_token() -> str:
    """Optional bearer credential. ntfy does not need one on a public topic."""
    return os.getenv("SHUO_NOTIFY_TOKEN", "").strip()


def enabled() -> bool:
    """Whether anything at all should happen. False on a fresh install."""
    return bool(notify_url())


def redacted_target(url: Optional[str] = None) -> str:
    """
    The target with the secret taken out, safe for a log line or `/health`.

    🔴 The path *is* the credential on ntfy, so it is replaced wholesale rather
    than truncated -- a prefix of a topic is a prefix of a password. The host
    survives because "is it even pointed at ntfy" is the question an operator
    asks first, and that is answerable without the secret.
    """
    raw = notify_url() if url is None else url
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "(unparseable)"
    if not parts.netloc:
        return "(set)"
    return f"{parts.scheme or 'https'}://{parts.netloc}/(redacted)"


# =============================================================================
# WHAT IS WORTH A NOTIFICATION
# =============================================================================

# The three transitions, as the plan's §5 names them. Not `cancelled`, and not
# `completed`: a cancelled call was stopped by the operator, and a completed one
# is the system working -- neither is news, and a notifier that fires on
# everything is one that gets muted.
INBOUND_STARTED = "inbound_started"
CALL_MISSED = "missed"
CALL_FAILED = "failed"


@dataclass(frozen=True)
class Notification:
    """One thing to tell the operator. A value; sending is somebody else's job."""

    kind: str
    call_ref: str
    title: str
    message: str
    tags: str = ""
    priority: str = ""


def _describe(row: Dict[str, Any]) -> str:
    """The far end of a call, as a phone number or an honest blank."""
    number = str(row.get("from") or "") or str(row.get("to") or "")
    return number or "an unknown number"


def _seconds(row: Dict[str, Any]) -> str:
    raw = row.get("durationSeconds")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return ""
    return f" after {seconds}s" if seconds > 0 else ""


def notification_for(
    row: Dict[str, Any], previous_status: str, already_sent: frozenset
) -> Optional[Notification]:
    """
    What this row's new status is worth telling the operator, if anything.

    **Pure**, and deliberately the only place the policy lives: what fires, on
    which transition, with what wording. A loop that decided this inline could
    only be tested by running it for a minute.

    `already_sent` is the transitions this call has produced before, which is
    the per-call half of the rate limit: a carrier that flaps `ringing` ->
    `pending` -> `ringing` describes one ringing phone, not three.
    """
    status = str(row.get("status") or "")
    if not status or status == previous_status:
        return None

    call_ref = str(row.get("id") or "")
    direction = str(row.get("direction") or "")
    persona = str(row.get("persona") or "")

    if (
        direction == "inbound"
        and status in (RINGING, IN_PROGRESS)
        and INBOUND_STARTED not in already_sent
    ):
        answered = " — the twin is answering" if status == IN_PROGRESS else ""
        return Notification(
            kind=INBOUND_STARTED,
            call_ref=call_ref,
            title="Incoming call",
            message=(
                f"{_describe(row)} is calling"
                f"{f' ({persona})' if persona else ''}{answered}."
            ),
            tags="telephone_receiver",
        )

    # `declined` shares this transition rather than getting one of its own.
    # It was carved out of `missed` in W5e for the *panel*, which has to stop a
    # ringing screen and needs to know which happened; a notification is answering
    # a different question -- "nobody took that call" -- and two push kinds for
    # one answer would double the rate-limit bookkeeping to say the same thing
    # twice. The wording still distinguishes them, because "they declined it" and
    # "it rang out" are not the same thing to act on.
    if status in (MISSED, DECLINED) and CALL_MISSED not in already_sent:
        body = (
            f"{_describe(row)} declined the call{_seconds(row)}."
            if status == DECLINED
            else (
                f"{_describe(row)} was not answered{_seconds(row)}. "
                f"Nothing was said on the call."
            )
        )
        return Notification(
            kind=CALL_MISSED,
            call_ref=call_ref,
            title="Declined call" if status == DECLINED else "Missed call",
            message=body,
            tags="warning",
            priority="high",
        )

    if status == FAILED and CALL_FAILED not in already_sent:
        reason = str(row.get("endedReason") or "").strip()
        return Notification(
            kind=CALL_FAILED,
            call_ref=call_ref,
            title="Call failed",
            message=(
                f"The call to {_describe(row)} failed"
                f"{f': {reason}' if reason else '.'}"
            ),
            tags="rotating_light",
            priority="high",
        )

    return None


# =============================================================================
# THE NOTIFIER
# =============================================================================

@dataclass
class _Tracked:
    """What is remembered about one call between ticks."""

    status: str = ""
    sent: set = field(default_factory=set)


class Notifier:
    """
    The 1Hz poller and its sender. One per process; `config_api` owns it.

    Constructed unconditionally and cheap to hold -- `start()` is what checks
    whether it is configured, so the lifespan has no branch to get wrong.
    """

    def __init__(
        self,
        *,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

        self._tracked: "OrderedDict[str, _Tracked]" = OrderedDict()
        self._primed = False
        self._sends: Deque[float] = deque()

        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._rate_limit_warned_at = 0.0

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Begin polling, if a URL is configured. Returns whether it started.

        The check lives here rather than in the caller so that "off by default"
        is a property of this module and not of one line in a lifespan.
        """
        if self._task is not None:
            return True
        if not enabled():
            logger.info(
                "Notifications are off (SHUO_NOTIFY_URL is not set). Nothing "
                "will be sent and no poller is running."
            )
            return False

        self._task = asyncio.create_task(self._run(), name="shuo-notifier")
        logger.info(
            f"Notifications on, every {self._poll_seconds:g}s -> "
            f"{redacted_target()}"
        )
        return True

    async def stop(self) -> None:
        """Cancel the poller and release the client. Never raises."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                pass

    def stats(self) -> Dict[str, Any]:
        """
        For `/health`. `dropped` and `failed` are the two worth watching.

        Both mean the operator is not being told something, and neither is
        visible any other way -- this module swallows its failures on purpose,
        because a notifier that can break a poll is worse than a silent one.
        """
        return {
            "enabled": enabled(),
            "running": self._task is not None and not self._task.done(),
            # Redacted, always. The topic is the credential.
            "target": redacted_target(),
            "sent": self._sent,
            "dropped": self._dropped,
            "failed": self._failed,
            "tracking": len(self._tracked),
        }

    # ── The loop ────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                # The loop outliving its own bugs is the whole contract: a tick
                # that raises must cost one tick, not the notifier.
                logger.warning(f"A notification poll failed ({exc!r}); continuing")
            await asyncio.sleep(self._poll_seconds)

    async def tick(self) -> List[Notification]:
        """
        One pass: read the log, decide, send. Returns what was sent.

        Returned rather than logged so the tests can assert on the *decision*
        without a fake HTTP server in front of every case.
        """
        rows = await asyncio.to_thread(call_history.load, SCAN_CALLS)

        # Oldest first, so a call that changed twice between ticks is judged in
        # the order it happened.
        pending = self._decide(list(reversed(rows)))

        sent: List[Notification] = []
        for notification in pending:
            if await self._send(notification):
                sent.append(notification)
        return sent

    def _decide(self, rows: List[Dict[str, Any]]) -> List[Notification]:
        """
        The transitions this tick found. Pure apart from the state it updates.

        On the **first** call it only records: a restart must not announce every
        call already in the log, and the operator has already seen those --
        possibly weeks ago.
        """
        out: List[Notification] = []

        for row in rows:
            call_ref = str(row.get("id") or "")
            if not call_ref:
                continue

            tracked = self._tracked.get(call_ref)
            if tracked is None:
                tracked = _Tracked()
                self._tracked[call_ref] = tracked
                self._evict()

            status = str(row.get("status") or "")

            if self._primed:
                notification = notification_for(
                    row, tracked.status, frozenset(tracked.sent)
                )
                if notification is not None:
                    # Marked as sent *here*, before the network is involved, and
                    # that is deliberate: a send that fails must not be retried
                    # every second for the life of the process. One attempt per
                    # transition, and `failed` on /health is how it surfaces.
                    tracked.sent.add(notification.kind)
                    out.append(notification)

            tracked.status = status

        self._primed = True
        return out

    def _evict(self) -> None:
        while len(self._tracked) > MAX_TRACKED_CALLS:
            self._tracked.popitem(last=False)

    # ── Sending ─────────────────────────────────────────────────────

    def _within_rate_limit(self) -> bool:
        now = self._clock()
        while self._sends and now - self._sends[0] > _RATE_WINDOW_SECONDS:
            self._sends.popleft()

        if len(self._sends) >= MAX_SENDS_PER_MINUTE:
            self._dropped += 1
            # One warning a minute, not one per drop: the situation this guards
            # against is a flood, and a log line per dropped notification would
            # be the same flood in a different file.
            if now - self._rate_limit_warned_at > _RATE_WINDOW_SECONDS:
                self._rate_limit_warned_at = now
                logger.warning(
                    f"More than {MAX_SENDS_PER_MINUTE} notifications in a "
                    f"minute — dropping the rest so the operator's phone does "
                    f"not become an alarm. Check the call log for what is "
                    f"flapping."
                )
            return False

        self._sends.append(now)
        return True

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def _send(self, notification: Notification) -> bool:
        """
        POST one notification. Never raises; returns whether it went.

        The URL is never logged, only `redacted_target()`. A 4xx from ntfy is
        logged at warning because it is almost always a bad topic or a stale
        token -- something the operator can fix -- and it is exactly the class
        of failure that is otherwise invisible, since nothing waits on this.
        """
        url = notify_url()
        if not url:
            return False
        if not self._within_rate_limit():
            return False

        headers = {"Content-Type": _CONTENT_TYPE, "Title": notification.title}
        if notification.tags:
            headers["Tags"] = notification.tags
        if notification.priority:
            headers["Priority"] = notification.priority
        token = notify_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = await self._http().post(
                url,
                content=notification.message.encode("utf-8"),
                headers=headers,
                timeout=SEND_TIMEOUT,
            )
        except Exception as exc:
            self._failed += 1
            logger.warning(
                f"Could not send the {notification.kind!r} notification to "
                f"{redacted_target()} ({exc!r}). The call is unaffected."
            )
            return False

        if response.status_code >= 400:
            self._failed += 1
            logger.warning(
                f"{redacted_target()} refused the {notification.kind!r} "
                f"notification with {response.status_code}. Check "
                f"SHUO_NOTIFY_URL and SHUO_NOTIFY_TOKEN."
            )
            return False

        self._sent += 1
        logger.info(
            f"Notified: {notification.title} (call {notification.call_ref})"
        )
        return True

    # ── Testing seam ────────────────────────────────────────────────

    def reset(self) -> None:
        """Forget every call and every count. For tests, and nothing else."""
        self._tracked.clear()
        self._sends.clear()
        self._primed = False
        self._sent = self._dropped = self._failed = 0
        self._rate_limit_warned_at = 0.0


# One per process. `config_api`'s lifespan starts and stops it; it does nothing
# at all unless `SHUO_NOTIFY_URL` is set.
NOTIFIER = Notifier()
