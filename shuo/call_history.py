"""
Completed calls, kept after the ring buffer forgets them.

`call_monitor.py` is a **live** view: 200 events across the last 3 calls,
in memory, gone on restart. That is deliberate and it is the right shape for
"what is the twin doing right now". It is the wrong shape for a call log,
which has to survive a restart and answer questions about a call from
yesterday.

So this module is the durable half. One append-only JSONL file, one row per
call *attempt* -- including the ones that rang and were never answered, which
is what Phase 8 added and what the live view could never have told you.

Three rules, and they are the whole design:

- **Every write goes through `shuo/spool.py`, never straight from the loop.**
  Phase 8 records a call *while it is happening* -- a pending row when it is
  placed, `ringing` when the carrier says so, a terminal row at teardown -- and
  several of those sites are HTTP handlers on the same event loop as the media
  socket. The functions here are ordinary blocking file I/O; it is the spool
  that keeps them off the loop, and the call sites are the ones that must
  remember to use it. Nothing in this module starts a thread or knows a loop
  exists.
- **It never raises.** A call log that can end a call is worse than no call
  log. Every entry point swallows and logs; the worst outcome of a full disk
  is a missing row in a table.
- **JSONL, not one JSON document.** Appending a line is O(1) and cannot
  rewrite what is already there, so a crash mid-write costs the record being
  written and nothing else. `load` skips a line it cannot parse for exactly
  that reason: a torn final line is an expected state, not a corrupt file.

### A row is written many times, and the file is still append-only

A call's status changes from `pending` to `ringing` to `in_progress` to a
terminal value, and editing a line in place would mean rewriting the file --
which is the one thing the format above exists to avoid. So **an update is a
new line carrying the same `id`**, holding only the fields that changed, and
`load` folds the revisions back into one row on the way out:

    {"id": "att-9f3…", "status": "pending",  "to": "+9198…"}
    {"id": "att-9f3…", "status": "ringing",  "ringingAt": "…"}
    {"id": "att-9f3…", "status": "completed", "transcript": [ … ]}
        ->  one row: completed, with the number it dialled and when it rang

The fold is field-level rather than last-line-wins because the revisions
genuinely race: the carrier's hangup webhook and the call loop's teardown are
two processes' worth of latency apart and either can land first. Status
specifically is resolved by **rank** (`_STATUS_RANK`), so a late webhook can
never demote a call that has already finished. A few fields are *first*-write
wins (`_FIRST_WINS`) because the earliest revision is the authoritative one:
an outbound call's `startedAt` is when it was placed, not when the media
socket happened to open.

Compaction rides on the existing trim: it rewrites one folded line per call,
which is also what stops revisions growing the file without bound.

### Why the config API reads this file directly

W3's live view goes over HTTP from :3041 to :3040 (`call_client.py`) because
live state exists only in the call process's memory -- there is nothing else
to read. History is different: it is on disk, and the config process is
already the process that does disk-bound work (decision 32). Reading it
directly means the call log still loads when `main.py` is stopped, which is
the ordinary state of a machine between test calls, and it adds no route to
the app that shares an event loop with the media socket.

It is the same arrangement as `config_store` with the direction reversed:
one writer, one reader, a file between them, and no import across the seam.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .call_status import (  # noqa: F401  (re-exported for the log's readers)
    ALL_ENDED_CODES,
    ALL_STATUSES,
    CALL_COMPLETED,
    CALL_FAILED,
    CANCELLED,
    COMPLETED,
    DECLINED,
    FAILED,
    IN_PROGRESS,
    MISSED,
    ORIGINATION_REFUSED,
    PENDING,
    RINGING,
    TERMINAL_STATUSES,
    best_status,
    classify_hangup,
    code_for_status,
    is_terminal,
    sentence_for,
)
from .log import get_logger

logger = get_logger("shuo.call_history")


# =============================================================================
# THE LIFECYCLE
# =============================================================================
#
# The vocabulary is `call_status.py`, re-exported here so a reader of the log
# has one import rather than two. It lives in its own module because
# `call_monitor` needs the same seven names and is pinned to import nothing
# that can touch a disk -- which this module very much can.

# Fields where the *first* revision is the authoritative one. Everything else
# is last-write-wins.
#
# `startedAt` is the load-bearing member: an outbound attempt starts when the
# operator places it, and the teardown record's idea of "started" is when the
# media socket opened -- seconds later, and after the part of the call an
# operator reviewing "why did nobody answer" actually cares about. `to`/`from`
# are here because only the origination site knows them; teardown does not
# carry them at all, and this is what stops a later revision blanking them.
_FIRST_WINS = frozenset({"startedAt", "to", "from"})


def now_iso() -> str:
    """A UTC stamp in the format every record in this file uses."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def revision(call_ref: str, **fields: Any) -> Dict[str, Any]:
    """
    One update to a call's row, ready for `append`.

    Deliberately a helper rather than a dict literal at seven call sites: the
    `id` key and the `revisedAt` stamp are what make the fold work, and a site
    that forgot either would write a row that quietly never merges.

    Empty values are dropped, because a revision saying `"to": ""` would
    otherwise be indistinguishable from one that means it.
    """
    record: Dict[str, Any] = {"id": call_ref, "revisedAt": now_iso()}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        record[key] = value
    return record


# Why the call ended, in prose and in a code. Neither is last-write-wins:
# **they follow the status**, and `_resolve_ended` below is what enforces it.
#
# The two revisions that can carry them are the call loop's teardown and the
# carrier's hangup webhook, and those are two processes' worth of latency apart
# in either order. Plain last-write-wins would let a hangup webhook landing
# after teardown restate a forty-minute conversation as "the far end declined
# the call" -- while `best_status` correctly kept it `completed`, because the
# rank protects the status and, until this existed, nothing protected the
# sentence next to it. A row reading `completed` / "they declined it" is worse
# than either half alone.
_ENDED_FIELDS = ("endedReason", "endedCode")


def merge(older: Dict[str, Any], newer: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fold one revision into an earlier one. `newer` was written later.

    Not `{**older, **newer}`: an empty string in the newer revision means
    "this write had nothing to say about that field", not "clear it",
    `_FIRST_WINS` fields mean the opposite of last-write-wins entirely, and the
    ended pair means neither -- see `_ENDED_FIELDS`.
    """
    merged = dict(older)

    for key, value in newer.items():
        if value is None or value == "":
            continue
        if key in _FIRST_WINS and older.get(key):
            continue
        merged[key] = value

    older_status = str(older.get("status") or "")
    newer_status = str(newer.get("status") or "")
    status = best_status(older_status, newer_status)
    if status:
        merged["status"] = status

    _resolve_ended(merged, older, older_status, newer, newer_status, status)

    return merged


def _resolve_ended(
    merged: Dict[str, Any],
    older: Dict[str, Any],
    older_status: str,
    newer: Dict[str, Any],
    newer_status: str,
    status: str,
) -> None:
    """
    Make the ended pair describe the outcome that won, in place.

    The losing revision's sentence is dropped rather than kept as a fallback:
    it explains a status this row does not have, and a reason that contradicts
    the status is the one shape of row an operator cannot reason about. If the
    winner said nothing about why, the honest answer is that we do not know,
    and the panel derives a code from the status instead
    (`call_status.code_for_status`).

    Nothing happens at all when only one side speaks to the status, which is
    the ordinary case -- the loop writes `in_progress` and later a terminal
    row, and no webhook ever lands.
    """
    if not status or older_status == newer_status:
        return
    if not (older_status and newer_status):
        return

    winner = newer if status == newer_status else older
    for key in _ENDED_FIELDS:
        value = winner.get(key)
        if value:
            merged[key] = value
        else:
            merged.pop(key, None)


# =============================================================================
# LIMITS
# =============================================================================

# `parents[1]` from shuo/call_history.py is the repo root -- the same
# resolution `config_store.store.default_config_path` uses, and for the same
# reason: a literal POSIX path here would be Bug C.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# How many records survive a trim. Enough that an operator reviewing a day of
# test calls never hits it, small enough that the read path stays a single
# `read_text`.
MAX_RECORDS = 200

# When the file grows past this, the next append trims it back to
# MAX_RECORDS. Checked with `stat().st_size`, which is O(1) -- counting lines
# would mean reading the whole file on every call end to discover that
# nothing needs doing.
TRIM_ABOVE_BYTES = 8 * 1024 * 1024

# Ceiling on one poll of `load`. The panel renders a table; a request for ten
# thousand rows is a mistake, not a requirement.
MAX_LIMIT = MAX_RECORDS

# How far back from the end of the file `load` reads while folding revisions.
#
# It has to be more than MAX_RECORDS, because a record is several lines until
# the next compaction, and it has to be bounded, because a file just under the
# trim threshold is 8MB. Twelve lines per call is roughly double the seven
# revisions a call can currently produce, so a row's earliest fields survive
# well past the point compaction would have folded them anyway. Revisions
# older than this window are lost, which costs a field like `ringingAt` on a
# very old row -- never the newest revision, which is always the one carrying
# the transcript.
MAX_SCAN_LINES = MAX_RECORDS * 12


def default_history_path() -> Path:
    """
    Where the call log lives.

    Next to `agent_config.json` in `var/`, for the same reason: it has to
    survive a restart, so it is not disposable the way a trace is. Overridable
    for a deploy that mounts a volume.
    """
    override = os.getenv("SHUO_CALL_HISTORY_PATH")
    if override:
        return Path(override)
    return _REPO_ROOT / "var" / "call_history.jsonl"


# =============================================================================
# WRITE (call process, always through the spool)
# =============================================================================

def append(record: Optional[Dict[str, Any]], path: Optional[Path] = None) -> None:
    """
    Persist one revision of a call's row. Never raises. **Blocking.**

    Called several times per call -- once when it is placed, again as the
    carrier reports progress, last at teardown -- and every one of those sites
    is on the call process's event loop, so **every one of them submits this
    to `shuo/spool.py` rather than calling it directly**. It is written as
    plain blocking I/O on purpose: this module is imported by :3041 across the
    process seam (decision 44), and it stays importable there precisely
    because it knows nothing about threads or event loops.

    One `open(..., "a")` and one `write` of a few KB: the kernel serialises an
    append on both Linux and Windows, so a concurrent second call writing at
    the same moment interleaves whole lines rather than bytes.

    No fsync. A record lost to a machine that lost power is an acceptable
    trade for not holding the spool's worker on the disk; the config store
    fsyncs because a torn config is read on the next call, and a torn call log
    is read by nobody until an operator opens a table.

    `None` is accepted and ignored, so a call site holding a disabled
    recorder's `record()` needs no guard of its own.
    """
    if not record:
        return

    target = Path(path) if path is not None else default_history_path()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)

        # 🔴 Heal a torn tail before appending after it.
        #
        # The promise above is that a crash mid-write costs the record being
        # written "and nothing else", and without this it costs *two*: a process
        # killed mid-append leaves a line with no terminating newline, so the
        # next append lands on the same line and fuses the two into one
        # unparseable record. `load` then skips the pair, and the record lost is
        # the one written *after* the crash -- which is the live one somebody is
        # waiting on, not the historical one.
        #
        # Found by W5d's notifier tests: a lost revision is a notification that
        # never arrives, which is a failure with no symptom at all.
        prefix = "\n" if _has_torn_tail(target) else ""

        # newline="\n" so the file is byte-identical on the Windows dev
        # machine and the Linux target (CLAUDE.md §5).
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + line + "\n")

        _trim_if_large(target)

    except Exception as exc:
        logger.warning(
            f"Could not record the call in {target} ({exc}). The call itself "
            f"is unaffected; it will be missing from the call log."
        )


def _has_torn_tail(path: Path) -> bool:
    """
    Whether the file's last line was never terminated -- i.e. a write was cut off.

    One seek and one byte, which is why this can sit on every append rather than
    being a repair pass somebody has to remember to run. A file that does not
    exist yet, or cannot be read, is not torn: the append is about to find that
    out for itself and has a better error to report.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with open(path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except OSError:
        return False


def _trim_if_large(path: Path) -> None:
    """
    Compact to the newest `MAX_RECORDS` **calls** when the file gets big.

    Two jobs in one pass, because both need the same expensive read. It bounds
    the file, as it always did; and it *folds*, collapsing a call's revisions
    into the single line `load` would have produced anyway. Without the fold a
    log of 200 calls would keep growing at a line per status change forever,
    and the trim would start throwing away whole calls to make room for the
    revision history of the newest one.

    Rare by construction -- 8MB is hundreds of long calls -- so the cost of
    reading and rewriting lands on one write in a thousand rather than on
    every one.
    """
    try:
        if path.stat().st_size <= TRIM_ABOVE_BYTES:
            return
    except OSError:
        return

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        rows, _ = _fold(raw.splitlines(), limit=MAX_RECORDS)

        # Oldest first on disk: the file's order is its history, and `load`
        # reads it backwards.
        lines = [json.dumps(row, ensure_ascii=False) for row in reversed(rows)]

        # Same atomic swap as the config store: a reader must never observe a
        # half-written file, and here the reader is a different process.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(tmp, path)

        logger.info(f"Compacted the call log to the newest {len(lines)} calls")
    except Exception as exc:
        logger.warning(f"Could not compact the call log at {path}: {exc}")


# =============================================================================
# THE FOLD
# =============================================================================

def _fold(lines: Iterable[str], *, limit: int) -> tuple[List[Dict[str, Any]], int]:
    """
    Revisions in file order -> whole rows, newest call first.

    Walks backwards so the newest revision of each call is the one that starts
    its row and `limit` can stop the scan early, and so `merge` is always
    called with the arguments the right way round: the line being read is
    always *older* than what has been folded so far.

    Returns the rows and the number of lines that could not be parsed.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    skipped = 0
    scanned = 0

    for line in reversed(list(lines)):
        if not line.strip():
            continue

        scanned += 1
        if scanned > MAX_SCAN_LINES:
            break

        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(parsed, dict):
            continue

        # A row with no id cannot be revised and cannot collide: give it one
        # that is unique to its position, so a legacy line still shows up.
        key = str(parsed.get("id") or "") or f"line-{scanned}"

        if key in rows:
            rows[key] = merge(parsed, rows[key])
        elif len(order) < limit:
            order.append(key)
            rows[key] = parsed
        # Past the limit an unseen call is dropped, but a revision of one
        # already collected is still folded -- which is the branch above.

    return [rows[key] for key in order], skipped


# =============================================================================
# READ (config process)
# =============================================================================


# =============================================================================
# READ (config process)
# =============================================================================

def load(limit: int = MAX_LIMIT, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    The newest calls first. Never raises; an unreadable log is an empty one.

    Each call is one row, folded from however many revisions were written for
    it -- see the module docstring. `limit` counts *calls*, not lines, which
    is what the panel means when it asks for twenty.

    A line that does not parse is skipped rather than fatal. The expected
    cause is the writer having been killed mid-append, which costs that one
    revision -- refusing to serve the other 199 calls because of it would turn
    a lost row into a broken screen.
    """
    raw = _read(path)
    limit = max(0, min(int(limit), MAX_LIMIT))
    if limit == 0 or raw is None:
        return []

    records, skipped = _fold(raw.splitlines(), limit=limit)

    if skipped:
        logger.warning(
            f"Skipped {skipped} unreadable record(s) in "
            f"{path or default_history_path()} — most likely a write "
            f"interrupted by a restart"
        )

    return records


def find(call_ref: str, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    One folded row by `id`, or by the carrier's `callId`. `None` if unknown.

    The two are both accepted for the same reason `CallMonitor._select`
    accepts both: a panel that has only ever seen one of them must still be
    able to ask for the call.
    """
    if not call_ref:
        return None
    for row in load(limit=MAX_LIMIT, path=path):
        if row.get("id") == call_ref or row.get("callId") == call_ref:
            return row
    return None


def count(path: Optional[Path] = None) -> int:
    """
    How many calls are on file. For `/health`, and for nothing hot.

    Folded, so it counts calls rather than revisions -- an unfolded count
    would climb every time a ringing phone changed state and read as though
    the machine had placed four times as many calls as it had.
    """
    raw = _read(path)
    if raw is None:
        return 0
    return len(_fold(raw.splitlines(), limit=MAX_LIMIT)[0])


def _read(path: Optional[Path]) -> Optional[str]:
    """The whole log as text, or None when there is nothing to read."""
    target = Path(path) if path is not None else default_history_path()
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No calls have been made on this install. Not a warning.
        return None
    except OSError as exc:
        logger.warning(f"Could not read the call log at {target}: {exc}")
        return None
