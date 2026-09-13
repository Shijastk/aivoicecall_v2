# Phase 8 — call management, recording, live logs, notifications

> Historical carrier call-management Phase 8, not Bluetooth Phase 8. Retained results below are historical, not rerun by the 2026-09-13 knowledge setup. See [current architecture](CURRENT_ARCHITECTURE.md) for inspected behavior (including player pacing differences), [testing](TESTING.md) for baseline provenance, and [Bluetooth roadmap](ROADMAP.md) for the separate future phases.

**Status: W5a, W5b, W5c and W5d shipped. Phase 8 backend is complete.**

**W5d shipped 2026-07-27** — decision 50, 757 tests green (+52), 14/14 mutations
caught, and wire-tested against a loopback stand-in for ntfy (16/16). One
**correction to §5 below**: the poller watches the **call log**, not
`/calls/active`. That route answers "what is in flight" and therefore excludes
terminal rows by design, so `missed` and `failed` — two of the three triggers —
would only ever have appeared there as a call quietly vanishing. Polling the log
also means the notifier needs nothing from :3040, so it keeps working while the
call server restarts, which is a window in which calls genuinely go missed.
Two other things worth carrying forward. **Priming is not optional**: without a
silent first tick a restart pages the operator about every call already in the
log. And 🔴 **a pre-existing durability bug surfaced** — a torn final line has no
newline, so the *next* `call_history.append` fused to it and both records were
lost, against the file format's own guarantee; healed in `append` with one seek.

**Decided 2026-07-27:** local µ-law tee for recording (§2.1a), the full
seven-state lifecycle (§1.2), ntfy.sh for notifications (§5), **polling only —
no SSE** (§3.2).

**W5c shipped 2026-07-27** — decision 49, 705 tests green (+49), 6/6 mutations
caught. Measured: **1.43µs per publish in the worst case** (ring full, evicting
on every append), 0.007% of the player's 20ms budget; `summaries()` 8µs over 8
calls. Three things worth carrying forward. `missed` had to become per call and
that is the subtle half of the change — the old `oldest_seq - since - 1` counts
lost events only while the buffer's sequence numbers are contiguous, and with
one ring per call they are not, so every concurrent call would have accused the
others of losing its events. A non-terminal row on disk is *evidence* of a call
in flight rather than proof of one, so the merge needs the 600s staleness window
in §3.2 — otherwise one `kill -9` leaves a phantom ringing call in the panel
forever. And `expect` on `/calls/current/hangup` now **selects** the call rather
than only asserting about it, because "the current call" stopped being
unambiguous the moment `MAX_CALLS` went to 8.

**W5b shipped 2026-07-27** — decision 48, 656 tests green (+50), 4/4
mutations caught. Measured cost of the tee: **4.5µs per 20ms tick, 0.02% of
the player's frame budget**, with the real pacing gate (50 fps, <1 frame
drift) passing with the tape attached. A 10-minute call converts to WAV in
0.54s on the spool's worker thread. Two things worth carrying forward:
`audioop` is gone in Python 3.13 so the µ-law decode is a table, and the
suite left WAVs in the developer's `var/` on its first run — the same trap
decision 44 found, now isolated autouse in `conftest.py`.

**W5a shipped 2026-07-27** — decisions 46–47, 606 tests green (+58), 4/4
mutations caught. Two things worth carrying forward: `call_status.py` was
split out of `call_history.py` because `call_monitor` is pinned to import
nothing that can reach a disk, and `main.py`'s CLI turned out to be a second
untracked origination site. §1.5 (ring/hangup webhooks) is **still
unverified** and is the one thing here that needs a live call.

Four asks, one architectural constraint that shapes all of them: nothing may
put disk or network I/O on :3040's event loop, because the player emits a
160-byte frame every 20ms against a deadline that no longer catches up when
it slips (decision 24). Everything below is designed around that single fact.

---

## 0. The thing that makes this hard, and the one primitive that solves it

The ask says "write a pending record at origination". `trigger_call` lives in
`shuo/server.py` — **the same process and the same event loop as the media
socket**. A `open(...).write()` there is exactly the hazard decision 32 split
the config API into a second process to avoid. Moving the write to :3041 does
not work either: :3041 never sees an inbound call, and it never sees an
outbound call placed by `curl` from the runbook.

So the call process has to write to disk *during* a call, and it has to do it
without touching its loop.

### 0.1 `shuo/spool.py` — the off-loop write spool (new)

```
submit(job)      ->  queue.put_nowait      O(1), never blocks, never raises
consumer task    ->  await asyncio.to_thread(job)
```

- **The producer never touches the filesystem.** `submit` is a `put_nowait`
  on a bounded queue — the same cost class as `call_monitor`'s `deque.append`,
  which is the standard this repo already holds hot-path publishers to.
- **One consumer, so revisions to one call's row land in file order.** N
  independent `to_thread` calls would race and could write `completed` before
  `ringing`.
- **Bounded, drop-oldest, counted.** A stuck disk must cost rows in a table,
  not unbounded heap on the audio process. Drops are logged once, and surface
  on `/health`.
- **`await drain(timeout)` on shutdown** so a SIGTERM does not lose the last
  call's row.

Everything else in this plan is a producer on this spool.

### 0.2 A bug this exposes, worth fixing while we are here

`conversation.py` teardown calls `call_history.append` **synchronously**. Its
docstring justifies this with "the socket is closed, the pool is stopped, no
player is pacing frames" — true of *this* call, false of the process. With two
concurrent calls, call A's teardown fsync blocks call B's player. Routing the
teardown write through the spool closes it at no cost.

---

## 1. Comprehensive call history — every attempt, not just answered ones

### 1.1 The correlation problem

To update a record as a call progresses we need one id that is stable from
before the phone rings until after it hangs up. Nothing existing is:

| candidate | why not |
|---|---|
| `request_uuid` from `originate` | **Not reliably `CallUUID`** — `server.py`'s own `/hangup` docstring says so |
| `CallUUID` | Only known once the media `start` frame lands — i.e. only on answered calls |
| `_Call.id` (`call-3`) | Minted when the socket opens; per-process counter, resets on restart |

**Fix: an attempt id, minted before anything else happens, threaded through
every URL the carrier will call back on.**

```
att-<12 hex>
  outbound: minted in trigger_call, before carrier.originate
  inbound:  minted in /answer, when no ?attempt= is present
```

Threaded via `?attempt=` on `answer_url`, `websocket_url`, and the ring /
hangup / recording callback URLs. `/ws` reads it, `CallContext` carries it,
`MONITOR.begin(attempt=...)` adopts it as `_Call.id` — so the live view, the
history row and the recording file all key on the same string, and the panel's
cursor still keys on a value that never changes mid-call.

`attempt` joins the `/ws` HMAC message in `config.mint_stream_token`, so it
cannot be forged into a row it does not own. TTL is already 300s, so a restart
invalidates minted URLs either way — no new failure mode.

### 1.2 Status vocabulary

Today: `completed` / `missed` / `failed`, derived at teardown, deliberately
three (`call-log.ts` renders exactly three). Full lifecycle:

| status | set when |
|---|---|
| `pending` | carrier accepted origination, nothing has rung |
| `ringing` | ring webhook fired |
| `in_progress` | media `start` frame — the call is answered and live |
| `completed` | answered, at least one transcript line |
| `missed` | rang and was never answered, or answered and nobody spoke |
| `cancelled` | ended before answer — operator hangup, or carrier `CANCEL` |
| `failed` | origination refused, transport failure, or the loop raised |

**Decided: ship all seven.** This is a contract change the frontend must
match — `call-log.ts` will render four statuses it has never seen. Backend
ships them; the panel needs a matching change or a `default` badge fallback.

### 1.3 Revisions, not mutation — JSONL stays append-only

A JSONL row cannot be edited in place without rewriting the file, and
rewriting is exactly what makes the current design crash-safe. So an update is
**a new line with the same `id`**, and `load()` folds:

```
walk backwards, newest first
  first revision seen for an id  ->  the row
  older revisions                ->  merged underneath (newest field wins)
  stop at `limit` distinct ids
```

Field-level merge, because the hangup webhook and the loop teardown race and
either may land first. Status specifically is resolved by rank
(`terminal > in_progress > ringing > pending`, and `completed > missed`), so a
late webhook cannot demote a finished call. `_trim_if_large` compacts to one
line per id, which is also what stops the file growing with revisions.

Corrupt-line tolerance and the never-raises posture are unchanged.

### 1.4 Write sites

All spooled. None blocks.

| moment | file | writes |
|---|---|---|
| before `originate` | `server.py` | `id, startedAt, direction, to, from, persona, status=pending` |
| originate result | `server.py` | `carrierCallId` / `status=failed` + reason |
| `/answer` (inbound) | `server.py` | creates the row: `status=ringing, from, to, persona` |
| `/ring` | `server.py` | `status=ringing, ringingAt` |
| media `start` | `call_monitor` → `conversation` | `status=in_progress, callId, answeredAt` |
| teardown | `conversation.py` | terminal status, duration, transcript, milestones, recording |
| `/hangup` | `server.py` | `hangupCause, hangupSource`; terminal status if the row never reached `in_progress` |

**`to` / `from` are new and are a real gap** — the current call log cannot
answer "who was called". No new privacy class: the rows already hold
transcripts.

### 1.5 🔴 Unverified — needs one live call

`vobiz.originate` sends only `answer_url`. It does **not** send `ring_url` or
`hangup_url`, so `/ring` and `/hangup` may currently never fire at all (they
may be wired app-side in the Vobiz console, or not at all). Plivo's API — which
Vobiz mirrors — accepts both. Adding them is a two-line change behind the
carrier interface, but *whether they fire, and with which parameter names*,
is a live-call question in the same category as the three unknowns already
documented on `vobiz.start_recording`. Until it is answered, `ringing` and
`cancelled` degrade gracefully: the row simply stays `pending` until teardown
or the next signal.

---

## 2. Recording — local capture, no carrier dependency

### 2.1 Two candidate sources

**(a) Carrier REST stereo recording** — already half-built (`start_recording`,
`/recording-status`). But the file lives on the carrier, retrieving it needs
carrier credentials in whichever process fetches it, carrier-side recording
and storage are typically billable, and three documented unknowns about
whether it captures `playAudio` audio at all remain open.

**(b) Local tee of the µ-law we already have** — **decided.** Free,
carrier-agnostic, no credentials anywhere new, and it captures exactly what
the pipeline sent and received, which is what a review of a character break
actually needs.

Both tee points already hold decoded µ-law bytes, so there is no extra decode:

| track | tee point | cost per 20ms |
|---|---|---|
| caller | `FeedFluxAction` dispatch in `conversation.py` | one `bytearray.extend`, one int add |
| agent | `AudioPlayer._send_frame`, before the base64 encode | same, plus one compare |

That is the dispatch-boundary observer pattern the monitor and tracer already
use. The state machine learns nothing (rule 1); the streaming chain is
untouched (rule 2).

### 2.2 Alignment, memory, format

- **Caller byte count is the master clock.** The carrier streams inbound
  continuously, so the caller track length *is* the call duration. Each agent
  frame is written at `offset = caller_bytes_so_far`; gaps are 0xFF-filled
  (µ-law digital silence, already a constant in `player.py`).
- **Memory is bounded by flushing, not by a cap.** Every ~5s of audio (40 KB)
  the buffer is handed to the spool and a fresh one started. Peak resident is
  ~80 KB per call regardless of call length.
- **Stereo WAV, caller left / agent right, 8 kHz PCM16.** Written by the
  stdlib `wave` module in the spool thread at teardown. Stereo because
  reviewing barge-in with the two sides separated is the entire point.
  µ-law → PCM16 via a 256-entry lookup table computed at import — **not
  `audioop`**, which is removed in Python 3.13.
- ~1.9 MB/minute. Retention is a count + total-bytes budget, pruned in the
  spool thread, oldest first.
- Off switch: `SHUO_LOCAL_RECORDING` (default on), independent of
`RECORD_CALLS`. The two are separate switches on purpose — the existing
carrier-side REST recording is **left exactly as it is**, still governed by
`RECORD_CALLS`, because it is the instrument that answers the three open
unknowns on `vobiz.start_recording`. Set `RECORD_CALLS=false` to stop paying
the carrier for a second copy; the panel's player reads the local file either
way.

`var/recordings/<attempt-id>.wav`, alongside `var/call_history.jsonl` — same
directory, same reasoning, same gitignore.

### 2.3 Retrieval

`GET /v1/calls/{id}/recording` on **:3041**, never :3040 — serving a 10 MB WAV
from the process that paces 20ms frames is the exact failure decision 32
exists to prevent.

- Path is derived from the id and resolved under the recordings root; ids are
  validated `^[a-z0-9][a-z0-9-]{0,63}$` and the resolved path is re-checked to
  be inside the root. Path traversal gets a 404, not a file.
- `FileResponse` with Range support, so `<audio>` seeking works.
- Same auth posture as the rest of :3041 (loopback, or the shared token). The
  Next server proxies it; the browser never holds the token.
- The history row carries `recording: {available, seconds, bytes, url}`.

---

## 3. Real-time logs for every call, not just the test call

### 3.1 What is wrong today

- `call_monitor` holds **one global 200-event ring across all calls**. Two
  concurrent calls evict each other's transcripts.
- `/calls/live` reports on **one** call — `latest()`, or one named ref. There
  is no way to ask "what is happening right now" in the plural.
- `/v1/test-call/status` is named as if the feature were the test call. It is
  not; it is the live view.

### 3.2 Changes

`call_monitor.py`
- Per-call event deques (`MAX_EVENTS` each) instead of one shared ring. The
  `_seq` counter stays global, so panel cursors stay valid across calls and
  `missed` still reports honestly.
- `MAX_CALLS` 3 → 8.
- `summaries()` — every live and recent call as one small dict (`id, callId,
  direction, persona, to, state, startedAt, durationMs, turns`).

`server.py`
- `GET /calls/active` (operator-gated) → `{"calls": [...summaries]}`. A list
  comprehension over ≤8 dicts; nothing that can block.

`config_api.py`
- `GET /v1/calls/active` — **the union of live and pending**: summaries proxied
  from :3040, merged with non-terminal history rows from disk. That merge is
  the point — a call that is `pending` or `ringing` has no media socket and
  exists *only* in the history file, so a live-only view would show nothing
  while the phone was ringing.
- `GET /v1/calls/live?call=&since=` — the general form. `/v1/test-call/status`
  stays as an alias so the panel does not break.

**Polling only — no SSE.** The panel keeps its 1Hz timer against
`/v1/calls/active` and `/v1/calls/live?call=`. `since=` already makes a
steady-state poll carry nothing, so the cost of the decision is feed latency
bounded by the panel's own interval and one fewer resident task. If a push
channel is wanted later it belongs *here*, on :3041, and nowhere near :3040 —
the reasoning is already written on `/calls/live` in `server.py`.

---

## 4. Storage — no database, and why

Nothing here needs one. JSONL + folding gives append-only O(1) writes, a
crash-tolerant reader, and human-readable rows (`cat` answers "why does the
table say that"). SQLite would put a driver, a lock and an fsync in the
process that paces 20ms frames — decision 33 already rejected it for config
for exactly that reason, and a call log is a weaker case, not a stronger one.

Recordings are files on disk. Total footprint is bounded by the retention
budget in §2.2.

---

## 5. SMS / notifications

**No free permanent SMS to an Indian number exists.** A2P SMS to Indian
handsets requires DLT principal-entity registration, which CLAUDE.md §2.1 puts
explicitly out of scope; and every provider trial (Twilio, Plivo, MSG91) is
credit-limited, expiring, and usually card-gated. Anything built on one would
stop working without warning, or start billing. **Skip SMS.**

**Decided: ntfy.sh.** `POST https://ntfy.sh/<topic>` — no account, no key, no
card, permanently free, and the free Android/iOS app turns it into a real
push on the operator's handset. **The topic name is the entire secret**, so it
is generated long and random and treated like one: it lives in `.env`, never
in the panel, and never in a log line. A generic `SHUO_NOTIFY_URL` means the
same code also drives Slack, Discord or Telegram if that changes.

`shuo/notify.py`, **in :3041, off by default**, `SHUO_NOTIFY_URL`
(+ optional `SHUO_NOTIFY_TOKEN` for a bearer header). Fires on inbound call
started, call missed, call failed.

**It carries its own poller**, because there is no SSE fan-out to piggyback
on and driving it from the panel's polls would mean notifications only arrive
when somebody is already looking at the screen — the opposite of the point. A
1Hz background task in :3041's lifespan, **started only when
`SHUO_NOTIFY_URL` is set**, polls ~~`/calls/active`~~ **the call log** (see the
correction at the top — `/calls/active` cannot show a terminal status) and fires
on transitions. Never from :3040: an outbound HTTP POST does not belong in the
audio process, and a notifier that is down must never be able to affect a call.

Rate-limited (one notification per call per transition, and a floor on total
sends per minute) so a carrier flapping cannot turn the operator's phone into
an alarm. Failures are logged and swallowed.

The panel's own Web Notification API is complementary and is frontend work.

---

## 6. Sequencing

| package | contents | touches |
|---|---|---|
| ~~**W5a**~~ ✅ | spool, history revisions, attempt-id threading, status vocabulary, `to`/`from` | `spool.py`+`call_status.py`(new), `call_history.py`, `call_monitor.py`, `server.py`, `config.py`, `conversation.py`, `carrier/*`, `main.py` |
| ~~**W5b**~~ ✅ | local recording + retrieval endpoint | `recording.py`(new), `player.py`, `agent.py`, `conversation.py`, `config_api.py`, `conftest.py` |
| ~~**W5c**~~ ✅ | multi-call monitor, `/calls/active`, `/v1/calls/*` (polling) | `call_monitor.py`, `server.py`, `config_api.py`, `call_client.py` |
| ~~**W5d**~~ ✅ | opt-in ntfy notifier + its poller | `notify.py`(new), `config_api.py`, `call_history.py` |

Each package ends green — `python -m pytest tests/ -v` — before the next
starts. W5a is the one with an unverified dependency (§1.5); W5b/c/d do not
depend on it.

## 7. Tests

- `test_spool.py` — ordering under concurrent submits, non-blocking submit,
  bounded drop-oldest, never raises on a failing job, drain on shutdown.
- `test_call_history.py` (extend) — revision folding, field merge, status
  rank, compaction to one line per id, torn-line tolerance preserved.
- `test_recording.py` — µ-law table vs a known-good reference, gap padding,
  WAV header geometry, retention pruning, path-traversal refusal, Range.
- `test_call_monitor.py` (extend) — per-call rings, no cross-call eviction,
  `summaries()`, cursor validity across a call boundary.
- `test_config_api.py` (extend) — new routes; **`TestIsolation` must stay
  green** — no import crosses the seam.
- **Hot-path guard**: monkeypatch `open` to raise, then drive a full turn
  through the tee points and the spool producers. Any real I/O on the loop
  fails the test rather than being found on a live call.
- `tests/test_update.py` — untouched, 307 lines, must stay green (rule 1).

## 8. Risks

1. **Ring/hangup webhooks unverified** (§1.5). Degrades gracefully; needs one
   live call to confirm.
2. **Frontend contract change** — four new statuses, new `to`/`from`,
   `recording` block, renamed live endpoints. Backend keeps the old routes as
   aliases; the panel still needs work.
3. **Recording is audio of a real conversation.** Operator-only, loopback by
   default, retention-bounded. Consent is a deployment matter, not a code one.
4. **Disk.** ~1.9 MB/min of recording plus the log. Budget-pruned, and the
   budget must be set before this runs anywhere unattended.
