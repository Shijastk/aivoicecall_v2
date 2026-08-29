"""
What a call attempt can be, as seven strings and an ordering.

A vocabulary module and nothing else: **no imports, no I/O, no state.** That
is the entire reason it exists as its own file rather than living in
`call_history.py`, which is the module that owns the durable log.

Two modules need these names and neither may import the other:

- `call_history.py` writes them to disk and folds conflicting revisions.
- `call_monitor.py` derives three of them at teardown, and is pinned by
  `test_the_monitor_imports_nothing_that_does_io` to import nothing capable of
  touching a disk. Importing the log writer to get a string constant would
  hand the hot-path observer the ability to block on an fsync -- which is the
  one thing that test exists to prevent, and it would have prevented it
  correctly.

So the constants live here, where importing them proves nothing and costs
nothing, and the two modules that disagree about everything else agree about
these.

### The eight, and why they are eight

Before Phase 8 there were three -- `completed`, `missed`, `failed` -- all
*derived* at teardown from what had happened on an answered call. Nothing
described a call that was never answered, so an unanswered call produced no
row at all: the log recorded conversations, not attempts.

    pending      the carrier accepted the origination; nothing has rung yet
    ringing      the carrier says the far end is ringing
    in_progress  the media `start` frame landed -- answered, and live

    completed    answered, and somebody actually spoke
    missed       rang out, or answered in silence
    declined     the far end actively refused it -- rejected, or busy
    cancelled    ended before it was answered -- by us, not by them
    failed       origination refused, transport dead, or the loop raised

`cancelled` and `missed` are deliberately distinct. Missed is the callee not
picking up; cancelled is the operator or the carrier stopping a call that was
still ringing. Collapsing them would make "did I hang up on a ringing phone"
unanswerable, which on a test-call log is one of the two questions anybody
asks.

`declined` was split out of `missed` for the same reason, and it is the one a
*panel* needs rather than the one a log needs. A ringing screen has to stop
ringing, and "they pressed decline" is the fastest and commonest way a test
call ends -- it was previously indistinguishable from "it rang out for sixty
seconds", so the operator watching could not tell whether to try again now or
later. A busy signal lands here too: it never rang, so it was never missed,
and the device refused it on the callee's behalf. The distinction between the
two is carried by `endedCode` (`remote_declined` vs `remote_busy`), not by a
ninth status.

### Ended codes

`endedReason` is, and stays, a human sentence -- `conversation.py` writes
prose into it and `notify.py` renders it into a notification. A panel cannot
branch on prose, so the *machine-readable* half is a separate field,
`endedCode`, drawn from the closed set below. One meaning per field: nothing
here is ever shown to an operator, and nothing in `endedReason` is ever
matched against.
"""

PENDING = "pending"
RINGING = "ringing"
IN_PROGRESS = "in_progress"

COMPLETED = "completed"
MISSED = "missed"
DECLINED = "declined"
CANCELLED = "cancelled"
FAILED = "failed"


# Higher wins when two revisions of the same row disagree, which they will:
# the carrier's hangup webhook and the call loop's teardown are two processes'
# worth of latency apart and either can land first.
#
# The ordering encodes two rules. A terminal status always outranks a
# transient one, so a late `ringing` cannot resurrect a finished call. And
# among the terminals the ones resting on **positive evidence** outrank the
# ones inferred from an absence: `completed` wins outright because somebody
# actually spoke, and `declined` outranks `missed` because the far end did
# something we were told about, where "missed" is only ever a conclusion drawn
# from nothing having happened. Without that, a hangup webhook arriving after
# teardown would rewrite a real conversation as `missed`, and a decline
# arriving alongside a ring-out timeout would read as a call nobody was near.
STATUS_RANK = {
    "": 0,
    PENDING: 1,
    RINGING: 2,
    IN_PROGRESS: 3,
    CANCELLED: 4,
    FAILED: 5,
    MISSED: 6,
    DECLINED: 7,
    COMPLETED: 8,
}

# The attempt is over. `/v1/calls/active` uses this to tell a phone that is
# still ringing from one that stopped a minute ago.
TERMINAL_STATUSES = frozenset({COMPLETED, MISSED, DECLINED, CANCELLED, FAILED})

# Every status, for validation and for tests that assert the panel and the
# backend know the same words.
ALL_STATUSES = frozenset(
    {PENDING, RINGING, IN_PROGRESS, COMPLETED, MISSED, DECLINED, CANCELLED, FAILED}
)


def best_status(*candidates: str) -> str:
    """
    The most authoritative of several statuses. Empty ones are ignored.

    An unrecognised value outranks nothing but the empty string, so a
    document written by a future build with a vocabulary this one has never
    heard of degrades to "whatever this build does understand" rather than to
    a status it cannot render.
    """
    best = ""
    for candidate in candidates:
        if not candidate:
            continue
        if not best or STATUS_RANK.get(candidate, 0) > STATUS_RANK.get(best, 0):
            best = candidate
    return best


def is_terminal(status: str) -> bool:
    """Whether the attempt is over and nothing more will be written about it."""
    return status in TERMINAL_STATUSES


# =============================================================================
# WHY IT ENDED
# =============================================================================
#
# `endedCode` is the machine-readable companion to `endedReason`'s prose. A
# panel branches on this; an operator reads that. Keeping them apart is what
# lets `conversation.py` keep writing sentences without a frontend having to
# string-match one, and it is why adding a code never changes what anybody
# sees.

REMOTE_DECLINED = "remote_declined"      # they pressed decline
REMOTE_BUSY = "remote_busy"              # their line or handset refused it
NO_ANSWER = "no_answer"                  # rang out, nobody picked up
CANCELLED_BY_US = "cancelled_by_us"      # we, or the operator, stopped it
ORIGINATION_REFUSED = "origination_refused"  # the carrier would not place it
NO_CARRIER_RESPONSE = "no_carrier_response"  # see below -- a derived code
CARRIER_ERROR = "carrier_error"          # it ended for a reason we cannot name
CALL_COMPLETED = "completed"             # answered, and it ran to the end
CALL_FAILED = "call_failed"              # answered, then the loop raised

ALL_ENDED_CODES = frozenset(
    {
        REMOTE_DECLINED,
        REMOTE_BUSY,
        NO_ANSWER,
        CANCELLED_BY_US,
        ORIGINATION_REFUSED,
        NO_CARRIER_RESPONSE,
        CARRIER_ERROR,
        CALL_COMPLETED,
        CALL_FAILED,
    }
)


# How a carrier's hangup cause maps onto this vocabulary. Consulted only for a
# call that **never reached `in_progress`** -- an answered call's outcome is
# decided by what happened on it, and `best_status` refuses the demotion
# anyway.
#
# Matched as a substring against an upper-cased cause, because carriers spell
# these several ways (`NO_ANSWER`, `NOANSWER`, `NO ANSWER`) and the exact
# vocabulary Vobiz emits is unverified. Order matters: the first match wins, so
# the more specific spelling has to come first -- `ORIGINATOR_CANCEL` before
# `CANCEL`, and `CALL_REJECTED` before anything that merely contains `CALL`.
_HANGUP_CAUSES = (
    ("ORIGINATOR_CANCEL", CANCELLED, CANCELLED_BY_US),
    ("CALL_REJECTED", DECLINED, REMOTE_DECLINED),
    ("REJECTED", DECLINED, REMOTE_DECLINED),
    ("REJECT", DECLINED, REMOTE_DECLINED),
    ("DECLINE", DECLINED, REMOTE_DECLINED),
    ("USER_BUSY", DECLINED, REMOTE_BUSY),
    ("BUSY", DECLINED, REMOTE_BUSY),
    ("NO_ANSWER", MISSED, NO_ANSWER),
    ("NOANSWER", MISSED, NO_ANSWER),
    ("NO ANSWER", MISSED, NO_ANSWER),
    ("UNALLOCATED", FAILED, CARRIER_ERROR),
    ("TIMEOUT", MISSED, NO_ANSWER),
    ("CANCEL", CANCELLED, CANCELLED_BY_US),
)

# Hangup sources that mean the call was cleared from our side.
_OUR_SOURCES = frozenset({"caller", "api", "callflow", "originator"})


def classify_hangup(cause: str, source: str = "") -> tuple[str, str]:
    """
    A carrier's hangup cause -> `(status, endedCode)` for an unanswered call.

    Returns two values rather than one because the panel needs both and they
    are not derivable from each other: `declined` covers a handset that
    rejected the call *and* one that was busy, and an operator staring at a
    ringing screen is entitled to know which. Deriving the code from the status
    afterwards would flatten exactly that distinction.

    Anything unrecognised falls to `failed`/`carrier_error`: a call that ended
    for a reason we cannot name did not succeed, and saying so is more useful
    than guessing which flavour of failure it was.
    """
    upper = (cause or "").upper()
    for needle, status, code in _HANGUP_CAUSES:
        if needle in upper:
            return status, code

    # A clean clearing on a call that was never answered is us giving up on it,
    # not the callee refusing -- and if the carrier says the hangup came from
    # our side, that is a cancellation however it was spelled.
    if "NORMAL_CLEARING" in upper or (source or "").strip().lower() in _OUR_SOURCES:
        return CANCELLED, CANCELLED_BY_US

    return FAILED, CARRIER_ERROR


# The sentence an operator reads for each code, for the sites that have no
# better one of their own. `conversation.py` writes its own prose and keeps it;
# this is for the webhook path, where until now `endedReason` was simply empty
# and a declined call reported no reason at all.
_SENTENCES = {
    REMOTE_DECLINED: "the far end declined the call",
    REMOTE_BUSY: "the far end was busy",
    NO_ANSWER: "nobody answered",
    CANCELLED_BY_US: "the call was cancelled before it was answered",
    ORIGINATION_REFUSED: "the carrier refused the call",
    NO_CARRIER_RESPONSE: (
        "the carrier never reported what happened to this call — it was still "
        "ringing when we stopped waiting"
    ),
    CARRIER_ERROR: "the call ended for a reason the carrier did not name",
    CALL_COMPLETED: "the call ended",
    CALL_FAILED: "the call loop failed — see the server log",
}


def sentence_for(code: str) -> str:
    """Prose for an ended code, or `""` for one this build does not know."""
    return _SENTENCES.get(code, "")


# The code to assume when a row carries a terminal status and no code -- every
# row written before `endedCode` existed, which is every row already on disk.
# A panel that branches on the code must not see `""` on a call the log is
# perfectly clear about.
_CODE_FOR_STATUS = {
    COMPLETED: CALL_COMPLETED,
    MISSED: NO_ANSWER,
    DECLINED: REMOTE_DECLINED,
    CANCELLED: CANCELLED_BY_US,
    FAILED: CARRIER_ERROR,
}


def code_for_status(status: str) -> str:
    """The best ended code for a status alone. `""` while the call is live."""
    return _CODE_FOR_STATUS.get(status, "")
