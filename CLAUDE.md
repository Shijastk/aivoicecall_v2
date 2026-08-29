# CLAUDE.md — shuo

> **Naming note:** `.claude/` in this repo is a *directory* holding `settings.json` (Entire's hooks — do not edit). `CLAUDE.md` is the file Claude Code auto-loads into context every session. This file is the "`.claude` file" referred to in project conversations.

**Read order at session start:** this file → [context.md](context.md) (where we are) → [rules.md](rules.md) (how to build) → [plan.md](plan.md) (deep research, *read the corrections below first*).

---

## 1. What we are building

A **multi-persona, emotionally intelligent voice Digital Twin** for Indian telephony — Indian-accented English over a standard 10-digit Vobiz DID, running the existing `shuo` streaming loop with sub-second turn-taking.

The persona is **runtime configuration, not code**. One pipeline, many roles, selected at call setup:

| Phase | Persona | Direction | Status |
|---|---|---|---|
| **Test Phase 1** | **JOB CANDIDATE being interviewed** | outbound | **current focus** |
| Future | Technical Interviewer | outbound | planned |
| Future | HR Recruiter | outbound | planned |
| Future | Inbound Receptionist | **inbound** | planned |

### Why "job candidate" first

It is deliberately the **hardest** persona, chosen as an adversarial stress test. A candidate does not control the conversation — the human does. That maximally exercises the four failure modes we are hunting:

1. **Character break** — drops the persona, says "as an AI language model", answers meta-questions about itself.
2. **Hallucination** — invents résumé facts, employers, dates, salary figures not in its grounded profile.
3. **Context loss** — forgets what it said three turns ago; contradicts an earlier answer under follow-up.
4. **Robotic delivery** — uniform response gaps, no hesitation, no self-correction, over-structured "firstly/secondly" answers.

If the pipeline survives an unscripted interview, every other persona is easier.

---

## 2. 🔴 CORRECTIONS TO `plan.md` — these override the document

`plan.md` was researched against a *promotional telecalling* brief. That brief is **wrong**. When `plan.md` conflicts with this section, **this section wins**.

### 2.1 We are NOT building a promotional dialer

**Ignore entirely:**

- **§0.1** in full — the 140xx CLI mandate, TCCCPR/TRAI promotional analysis, Reg 4 auto-dialer declaration, Reg 25 sanction ladder, abandoned/silent-call ratio caps, the 10:00–21:00 IST regulatory calling window.
- **DLT registration** — Principal Entity registration, physical verification, biometric auth, content/consent templates, VILPOWER fees. All out of scope.
- **DND scrubbing** — no pre-dial Scrubbing Function, no tokenised virtual identities, no CCAF/OTP consent capture.
- **Phase 0** (compliance & procurement) — deleted, except one surviving item: confirm Vobiz can provision a working **standard 10-digit Indian DID** and give us trunk credentials.
- **Phase 8** (dialer & compliance gates) — deleted. No calling-window gate, no abandoned-call counters, no scrub-validity checks. A thin call-outcome classifier from SIP response codes may return later as plain observability, not compliance.
- **Part 7 → "Compliance"** verification block — deleted.

**We use a standard 10-digit Vobiz DID.** Calls are consented, relationship-based, low-volume, one-to-one (interviews, reception, recruiting conversations) — not cold promotional broadcast.

**Still true and still load-bearing** from §0.1: there is no Indian rule requiring AI-caller disclosure, and DPDP 2023 permits US-hosted processing. So **ap-south-1 remains a pure latency decision.**

### 2.2 It is a Digital Twin, not a receptionist

`plan.md` assumes a single fixed outbound sales script. Replace that assumption with:

- **Persona is data.** A persona = system prompt + grounded fact block + voice ID + turn-taking profile + filler inventory. Swappable without touching the loop.
- **Inbound is a first-class path**, not an afterthought — the Receptionist persona needs it. `plan.md` Phase 1 only specifies outbound origination; the `<Stream>` answer-URL must serve both.
- **§7 "Replace the placeholder SYSTEM_PROMPT with a telecalling script"** → replace with the **persona layer** (`shuo/persona/`).
- **Emotional intelligence is a requirement, not polish.** Part 5 (fillers, response-gap sampling, backchannel suppression) is promoted from "human-likeness nice-to-have" to core acceptance criteria.

### 2.3 What survives from `plan.md` unchanged

Everything technical. Specifically: the re-specified latency targets (§0.2), Vobiz transport and its trap list (§2.1), Sarvam `saaras:v3` STT (§2.2), in-process Silero VAD v5 + Smart Turn v3.1 (§2.3), the LLM decision-by-measurement (§2.4), Cartesia Sonic 3.5 + voice cloning (§2.5), the cost model (Part 3), the latency budget and Bugs A/B/C (Parts 1.3 & 4), human-likeness (Part 5), and Phases 1–7 + 9.

---

## 3. Non-negotiable rules

1. **The state machine stays pure.** [shuo/state.py](shuo/state.py) `process_event` is `(State, Event) -> (State, [Action])` with zero I/O. Every new capability enters as new **events** and **actions**, never as a side effect inside the machine. All 307 lines of [tests/test_update.py](tests/test_update.py) must keep passing.
2. **Never break the streaming chain.** Token-level LLM → TTS → Player streaming ([shuo/agent.py:158-193](shuo/agent.py#L158-L193)) is the entire latency advantage. No buffering a full response anywhere.
3. **Vendor code goes behind a provider interface.** No vendor SDK imported outside its provider module. Swapping Cartesia↔ElevenLabs↔Inworld must be a config change.
4. **Measure, don't trust.** Every vendor latency claim gets benchmarked from ap-south-1 before it earns a place in the pipeline. [shuo/server.py:200-283](shuo/server.py#L200-L283) and [scripts/bench_sarvam.py](scripts/bench_sarvam.py) exist for this.
5. **µ-law 8kHz end to end.** No L16 path may be reachable — see the endianness trap in [rules.md](rules.md).
6. **Ask before scope-expanding.** If work implies a change to the architecture recorded in [context.md](context.md), stop and confirm.
7. **Do not write Phase code before the phase is approved.** Plan → approval → code.

---

## 4. Persona rules (Digital Twin)

1. **Grounded facts only.** Each persona carries an explicit fact block (for the candidate: résumé, skills, dates, employers, expected CTC, notice period). The prompt must forbid inventing anything outside it, and must give an explicit *"I don't have that detail"* escape hatch — hallucination is worse than a gap.
2. **Never break character.** No meta-references to being an AI, a model, or a system prompt. Deflect identity probes in-persona.
3. **Register: Indian English.** Natural Hinglish code-mixing where a real speaker would use it (`ji`, `haan`, `theek hai`, `matlab`, `achha`). Lakh/crore, Indian number grouping.
4. **Short conversational turns.** The candidate persona is the exception — it must sustain 20–45s answers without becoming a monologue. That makes barge-in handling and played-ms truncation (Bug A) *critical*, not optional.
5. **Turn-taking profile is per persona.** An interviewer pausing mid-question ("So… tell me about… your Python experience") must **not** trigger a premature end-of-turn. The candidate persona needs a *patient* EOT threshold; the receptionist needs an eager one.

---

## 5. Working agreements

- **Update [context.md](context.md) at every milestone** — phase completion, architectural decision, vendor confirmed or rejected, benchmark result. It is the recovery point if conversation context is lost.
- Record decisions in the context.md **Decision Log** with date, choice, and reason. Reversals get a new entry, not an edit.
- Never delete a `plan.md` section — it is the research archive. Corrections live here.
- Dev machine is **Windows**; deploy target is **Linux ap-south-1**. Do not introduce POSIX-only paths (see Bug C).

---

## 6. Commands

```bash
python -m pytest tests/ -v            # state machine — must stay green
python main.py                        # server-only (inbound)              :3040
python main.py +91XXXXXXXXXX          # outbound call
python config_api.py                  # operator config API (separate!)    :3041
python scripts/bench_sarvam.py        # full-pipeline latency, no telephony needed
curl localhost:3040/bench/ttft        # LLM TTFT comparison
curl localhost:3041/v1/config         # what the agent would run with
curl localhost:3041/v1/voices         # voices that can actually be synthesised
python scripts/getfreevocies.py       # re-measure which voices the plan allows

# W4 — what each panel screen loads on page load, and the call log.
curl localhost:3041/v1/agent/config     # /agent      prompt + voice
curl localhost:3041/v1/agent/persona    # /persona    rules (the default when unset)
curl localhost:3041/v1/agent/knowledge  # /knowledge  fact block
curl "localhost:3041/v1/calls/history?limit=20"   # /call-logs  real records
# Read from disk here, so it still loads with the call server stopped. Path is
# on /health.

# W5a (Phase 8) — every *attempt* is a row, not just the answered calls.
# One call is one row with a stable `id` (the attempt id), written several
# times as it progresses:
#   pending -> ringing -> in_progress -> completed | missed | cancelled | failed
# The file is still append-only: an update is a new line with the same id, and
# `call_history.load` folds them. `cat var/call_history.jsonl` shows the
# revisions; the API shows the folded rows.
curl localhost:3040/health              # writes.dropped / writes.failed — the
                                        # only signal that the log is lying by
                                        # omission (the spool swallows errors)

# W5b (Phase 8) — every call is recorded locally, for free, by teeing the
# µ-law the pipeline already holds. Stereo: caller left, agent right, which is
# what makes a barge-in audible as one. var/recordings/<id>.wav
curl -o call.wav localhost:3041/v1/calls/<id>/recording   # Range-capable
curl localhost:3041/health              # recordings_path / recordings_stored
# SHUO_LOCAL_RECORDING=false turns it off. It is *separate* from RECORD_CALLS,
# which governs the carrier's billable recording — turning that off to stop
# paying for a second copy does not take the free local one with it.

# W5c (Phase 8) — the live view, in the plural. Two polls at ~1Hz feed the
# whole monitor screen, and there is deliberately no SSE or WebSocket on
# either hop: :3040's loop paces a 160-byte frame every 20ms, and a resident
# task with keepalives on it is the one thing decision 24 makes unrecoverable.
#
# `active` is the *union* of two sources, because neither knows every call: an
# answered call is in :3040's memory, and a phone that is still ringing has no
# media socket at all, so it exists only as a row in the log. A live-only view
# is blank for exactly the window you sit watching.
curl localhost:3041/v1/calls/active            # every call in flight
curl "localhost:3041/v1/calls/live?call=<id>&since=42"   # one call, incremental
curl localhost:3040/calls/active               # the live half only (needs the token)
# `callServer: "unreachable"` is a *field*, not an error — with `main.py`
# stopped, the ringing/pending rows still come back off disk.

# W5e (Phase 8) — a call that ends says so. Three things the panel needs, and
# none of them existed before: `/v1/calls/live` **falls back to the call log**
# when :3040 has never heard of the call, which is the entire life of one that
# is ringing and the whole life of one that is declined (neither opens a media
# socket, so the live monitor never sees either);  a finished call stays in
# `/v1/calls/active` for 20s with `live: false` so the transition is *seen*
# rather than raced past;  and `declined` is a status of its own, split out of
# `missed`.
#
# 🔴 Branch on `endedCode`, never on `endedReason`. The code is a closed set
# (`remote_declined`, `remote_busy`, `no_answer`, `cancelled_by_us`,
# `origination_refused`, `no_carrier_response`, `carrier_error`, `completed`,
# `call_failed`); `endedReason` beside it is a sentence for a human and is not
# a stable string.
curl "localhost:3041/v1/calls/live?call=<id>"   # status/endedCode/endedAt/live
#   -> {"status":"declined","endedCode":"remote_declined","live":false, ...}
#      "source" says which half answered: "live" | "log" | "none"
#
# A carrier that never posts /hangup (unverified — see carrier/vobiz.py) is
# covered too: after RING_TIMEOUT_SECONDS a still-ringing call is *reported*
# as failed/no_carrier_response. Derived, never written — nothing on disk is
# rewritten on a guess, so a late webhook still folds normally.
#
# `/v1/test-call/status` still answers identically; it is an alias now. And
# pass `expect=<id>` when hanging up: with 8 concurrent calls "the current
# call" is not something a panel can safely mean.
curl -X POST "localhost:3041/v1/test-call/hangup?expect=<id>"

# W5d (Phase 8) — push notifications, and they are OFF unless you set a URL.
# Inbound call started / missed / failed, pushed from :3041 by a ~1Hz task that
# watches the call log. Never from :3040 — an outbound TLS handshake has no
# business on the loop that paces 20ms frames.
#
# 🔴 The topic name IS the credential: anyone who knows it reads every
# notification. Generate it long and random, keep it in .env, never in the
# panel, never in a log line. Nothing here ever prints it — /health shows
# https://ntfy.sh/(redacted).
#
#   SHUO_NOTIFY_URL=https://ntfy.sh/shuo-$(openssl rand -hex 12)
#   SHUO_NOTIFY_TOKEN=...        # optional bearer, not needed on a public topic
#
# Install the free ntfy app, subscribe to the topic, and a missed call rings
# the operator's handset. Test the wiring without a real call by pointing
# SHUO_NOTIFY_URL at any local HTTP server first.
curl localhost:3041/health      # notifications.{enabled,running,sent,dropped,failed}
                                # dropped/failed are the only signal the
                                # operator is not being told something — the
                                # notifier swallows its own failures on purpose.
# It carries metadata (direction, number, persona, duration) and *never* a
# transcript. It is the only thing in this system that sends call data off the
# machine, which is why it defaults to off.

# W3 — test call. Needs BOTH processes, and SHUO_ADMIN_TOKEN set for both.
curl -X POST localhost:3041/v1/test-call \
     -H 'content-type: application/json' \
     -d '{"phoneNumber":"+919876543210"}'   # rings a real phone, spends money
curl localhost:3041/v1/test-call/status     # live state, transcript, latency
curl "localhost:3041/v1/test-call/status?since=42"   # only what is new
curl -X POST localhost:3041/v1/test-call/hangup
curl localhost:3041/health                  # test_call_ready — is the token wired?
```

`config_api.py` is a **second process, and must stay one** — see context.md
decision 32. It serves the Next.js control panel; `main.py` serves calls on a
20ms player deadline. Never mount config routes on `shuo/server.py`.

W3 is the first feature that needs the two to talk, and it does so **over
HTTP on loopback** ([call_client.py](shuo/call_client.py)) — never by
importing across the seam. `SHUO_ADMIN_TOKEN` lives in both processes'
environments and never in the browser: the panel can *ask* for a call, only
the host can place one.
