# Bluetooth Phase 4 realtime latency implementation plan

**Status: IN PROGRESS — Phase 4A completed on the reference path; Phase 4B shadow mechanism passed offline regression and controlled live validation on 2026-09-14, but the current eager trigger showed insufficient pre-final readiness to advance to Phase 4C.**

This plan is subordinate to `AGENTS.md`, `CLAUDE.md`, `rules.md`, the Bluetooth
roadmap and the existing Phase 4 contract. It does not authorize Phase 5 live-call
acceptance, Phase 6 call control, Phase 7 resilience qualification or Phase 8
release work.

## Why this plan exists

The remote `main` branch contains the first Bluetooth-to-SHUO integration commit
`ae9d981eff861deb364bb8a34691d6008877860b`, while older Bluetooth documents still
describe Phase 4 as wholly planned. That is documentation drift, not evidence that
Phase 4 is accepted.

Task-owner supplied runtime evidence from 2026-09-14 also shows a real Bluetooth
conversation path with approximately 424–458 ms warm LLM first-token latency on
Groq Qwen 3.6 non-thinking, plus roughly 272–462 ms from first token to first TTS
audio in the observed turns. Those measurements were not reproduced by this
branch and some local runtime patches used for them are not present on remote
`main`. Do not infer that this branch already contains those local changes.

The current serial path waits for final `EndOfTurn` before starting the agent. The
approved optimization strategy is to first measure Deepgram Flux eager-turn lead,
then add speculative generation only if the evidence justifies it.

## Permanent invariants

- Keep `process_event` pure and keep side effects outside `shuo/state.py`.
- Preserve Vobiz, Twilio, browser/V2 and default `main.py` behavior.
- Keep Bluetooth optional and isolated; no default import/start from production
  carrier entrypoints.
- Keep shared/carrier audio as G.711 mu-law 8 kHz; Bluetooth S16LE/16 kHz remains
  boundary-only.
- Never feed TTS/uplink audio to STT.
- Keep realtime queues bounded and cleanup deterministic.
- No new dependency, secret access, live provider call, live device action or
  automatic call control from this plan.
- No full-response LLM or TTS buffering.
- A speculative draft must never mutate committed conversation history.
- Performance optimization may degrade to the existing final-EOT path; it must not
  degrade correctness, cleanup or call availability.

## Phase 4 implementation slices

### Phase 4A — eager-turn measurement only

Goal: establish whether Flux can provide enough useful lead before final
`EndOfTurn` to justify speculative generation.

Implementation:

- Add an optional `eager_eot_threshold` to `FluxService`; default is disabled.
- Enable it only from the explicit Bluetooth runner when the operator supplies a
  CLI value.
- Restrict the first slice to 0.3–0.7 so the existing Flux final-EOT default
  threshold is not silently changed.
- Log only sanitized timing metadata: eager candidate, `TurnResumed`, and
  eager-to-final elapsed milliseconds plus transcript-match boolean/character
  counts. Do not log call text.
- Do not start LLM/TTS early and do not add new state-machine events in 4A.

Gate before 4B:

- Focused unit/integration tests pass with feature disabled and enabled.
- Default carrier/browser behavior is unchanged by code inspection/regression.
- Runtime evidence records eager-to-final lead distribution and resume rate on an
  explicitly authorized controlled call.
- No private transcript content is written to measurement logs.

### Phase 4B — shadow speculative LLM

Goal: measure real overlap and cancellation behavior without changing caller audio.

Planned design, not implemented by 4A:

- Introduce a separate speculative-turn object/task keyed by generation/turn id.
- Start at `EagerEndOfTurn`; never reuse `Agent.cancel_turn` as the speculative
  cancellation primitive because the current LLM cancellation path can preserve
  partial generated history.
- `TurnResumed` cancels and destroys the speculative result/history completely.
- Final `EndOfTurn` may validate/promote the prepared result only for the matching
  turn/transcript.
- At most one speculative LLM request per call; bounded global concurrency with a
  graceful fallback to normal final-EOT generation when capacity is unavailable.
- No speculative TTS in this slice.

Gate before 4C:

- Zero stale-generation speech/history mutation in race tests.
- Cancellation and cleanup leave no task/resource leak.
- Shadow metrics prove useful post-EOT latency reduction and acceptable extra LLM
  request rate.
- Existing non-speculative path remains an immediate feature-flag rollback.

#### Controlled Phase 4B live shadow result — 2026-09-14

Executed on the validated Bluetooth reference path with:

- `LLM_MODEL=qwen/qwen3.6-27b`
- `eager_eot_threshold=0.3`
- `shadow_speculation=true`
- `pw-cat latency=120ms`

Observed final-turn shadow outcomes:

- final turns observed: **10**
- `ready_before_final`: **0**
- `not_ready_by_final`: **10**
- retracted/resumed eager candidates observed: **4**
- one retracted candidate reached first token at **500.8ms**, but the caller
  resumed speaking, so discarding it was correct.

Normal final-EOT Qwen first-token latency remained approximately in the
**412–461ms** class for most observed turns.

Important interpretation:

- Phase 4B cancellation/history-isolation behavior worked as intended.
- The current Deepgram EagerEndOfTurn trigger did **not** produce a reusable
  shadow first token before final EOT on any of the 10 final turns.
- Therefore this evidence does **not** justify enabling Phase 4C prepared-response
  reuse yet.
- The next latency work should address the already-measured TTS warm-connection
  churn and investigate an earlier, separately validated transcript/turn signal
  before revisiting Phase 4C.

Status: **Phase 4B shadow mechanism runtime-validated; latency benefit from the
current eager trigger is insufficient to advance to Phase 4C.**

### Phase 4C — commit-on-final response reuse

Goal: use the prepared LLM result after final EOT while retaining correctness.

Planned design:

- Promote a matching speculative result exactly once at final EOT.
- If no valid draft exists, use the existing final-EOT path.
- Phrase-aware streaming may emit short stable clauses to TTS after final commit;
  never wait for a full response and never send stale draft speech.
- Add generation ids so late provider chunks from a cancelled draft are ignored.

Gate before 4D:

- Same committed answer/history semantics as the non-speculative path.
- Barge-in, zero-audio completion, teardown and stale-completion tests pass.
- Measured latency improvement survives warm/cold trials without audio regression.

### Phase 4D — TTS/context/startup hardening

Goal: remove avoidable variance after speculative overlap works.

Planned work, each separately measured:

- Replace fixed 8-second TTS connection churn with a liveness/keepalive policy
  supported by the selected provider; no silent dead-socket reuse.
- Replace token-by-token forced generation with bounded phrase-aware streaming if
  it improves naturalness without violating latency gates.
- Instrument prompt/input size, provider queue/prompt timing where available and
  bound/summarize old conversation context without losing digital-twin facts.
- Instrument startup spans and parallelize only independent warmups after proving
  lifecycle/cancellation safety.
- Tune Bluetooth prebuffer only after upstream improvements and audio/XRUN tests.

## TTS warm-pool follow-up — 2026-09-14

The task owner explicitly authorized this narrow TTS fix ahead of the original
4D sequence, consistent with the later Phase 4B decision to prioritize warm
connection churn. Phase 4C remains deferred; other 4D work remains planned.

The initial offline implementation removed age-only eviction and retained
liveness checks. That unlimited-age policy passed offline tests but was disproven
by the following **task-owner supplied controlled live evidence**, not reproduced
by the coding agent:

| Warm idle age | Reported outcome | Reported TTS latency |
|---|---|---|
| 10,544ms | Reused successfully | 255ms |
| 11,078ms | Reused successfully | 260ms |
| 17,451ms | Reused successfully | 258ms |
| 19,819ms | Dispensed, then rejected; caller-visible silent turn | No audio |

The last socket received `input_timeout_exceeded` with the provider message:
`Have not received a new text input within the timeout of 20 seconds.`
Successful reuse beyond eight seconds proves the old cutoff was too aggressive.
The failure proves that local `TTSService.is_active` alone cannot determine safe
reuse: first real text may arrive after the provider's input deadline.

The revised production design in `shuo/services/tts_pool.py::TTSPool` uses
`max_idle_age=15.0`, leaving roughly five seconds below the observed 20-second
timeout. Checkout rejects at or above the maximum, even if locally active.
Maintenance evicts/refills proactively and wakes at the earliest expiry rather
than waiting past it for the next health poll. After review, age starts immediately
before sending initialization text, after the handshake, via
`TTSService.warm_idle_started_at`. This replaces the earlier pre-handshake origin
and retains send/backpressure time in the idle budget. The timestamp is recorded
only after successful send; it is not a provider receipt acknowledgement.
Injected services without the timestamp retain the conservative pre-start origin.
A long first-token
delay can still consume the remaining margin; this is not a delivery guarantee.

The separate `health_check_interval` controls liveness polling. Existing
positional/keyword callers still work: absent that new argument, `ttl / 2`
supplies the polling interval only, not the safe lifetime. Dead/inactive sockets
are rejected on observation regardless of age. Cleanup, callback and per-call
voice binding are preserved. No provider keepalive text or synthetic TTS is used.

Offline coverage and executed results are recorded in
[TESTING](../TESTING.md#tts-warm-pool-regression--2026-09-14).
At this implementation stage, the maximum-idle choice and startup behavior still
required controlled live validation. The subsequent
[final controlled validation](#final-controlled-tts-warm-pool-validation--2026-09-14)
below records the supplied results. Phase 4C, Phase 4 acceptance and later phases
are unchanged.

For a later explicitly authorized controlled measurement, manually establish the
call first, load the existing provider environment, and set `BT_ADDRESS` to the
explicitly selected phone. Run from the repository root:

```bash
LLM_MODEL=qwen/qwen3.6-27b SHUO_LOG_LEVEL=INFO PYTHONPATH=. \
  .venv/bin/python -c 'from shuo.log import setup_logging; setup_logging(); import runpy; runpy.run_path("scripts/run_bluetooth_ai.py", run_name="__main__")' \
  --bluetooth-address "${BT_ADDRESS:?Set BT_ADDRESS to the selected phone}" \
  --latency 120ms --call-id bt-tts-safe-idle-15s
```

Keep model, voice and audio settings fixed across before/after trials. Include
turn gaps of 10–14 seconds and gaps beyond 15 and 20 seconds. Verify proactive
replacement and absence of over-limit checkout/silent turns; correlate warm
checkout age and over-age eviction/cold reconnect logs
with the existing `tts_pool` trace span, `llm_first_token` and `tts_first_audio`
markers. Provider idle closures can still require reconnects. Compare warm/cold
distributions; these timings alone do not measure caller mouth-to-ear latency.
Ctrl+C stops the AI session; call answer/hangup remains manual. Speculation is
disabled in this command to isolate the TTS pool change.

## First-turn startup race correction — 2026-09-14

Owner-supplied controlled live evidence reported Flux forwarding caller audio
before initial TTS readiness, then `Pool empty, connecting fresh...` and
`TTS 4132ms setup`; the background warm socket finished almost simultaneously.
This observation was not reproduced with live providers by the coding agent.

Source root cause: the manual runner builds the Bluetooth session, production
wiring delegates lifecycle to `run_bluetooth_conversation`, and the orchestrator
starts the session and Flux before awaiting the Agent factory. That factory
awaited `TTSPool.start()`, but start merely scheduled `_fill_loop()`. Agent
creation therefore completed before initial TTS readiness. The reader/event loop
could deliver final EOT to `Agent.start_turn()` → `TTSPool.get()`, whose empty-pool
path started a second connection rather than joining warmup. Carrier setup also
awaits the same nonblocking start before constructing its Agent.

Chosen minimal design: retain nonblocking `start()` for compatibility, add an
explicit `wait_ready(timeout=10.0)` barrier in Bluetooth Agent creation, and
make `get()` join initial warmup instead of racing it. Readiness requires an
active socket below the 15-second limit, is not a reservation, and is rechecked
on checkout. Following review, transient preconnection failures are retried within
the original readiness timeout instead of aborting immediately. At timeout the
most recent preconnection error is retained as the cause. Stop fails the waiter;
cancelling one waiter does not cancel the pool-owned fill task. Production
records pool ownership before awaiting readiness, so teardown still cancels
warmup on failure/abort.

This gates only initial Bluetooth reader/event dispatch, after Flux has started;
the shared media/codec/state paths and call-control architecture are unchanged.
It does not block the event loop, add per-frame checks, or wait on TTS before
every turn. The startup bound remains ten seconds, including retry backoff;
exhausting that budget ends startup rather than leaving a stalled session.
Other shared callers acquire the initial
socket through `get()` without opening a duplicate cold handshake.

Offline tests cover delayed warmup/first checkout, production Agent creation,
failure, timeout, cancellation, stop, voice binding, and the existing idle-age
policy. Removing the competing handshake does not prove faster provider setup
or caller-heard latency: startup waiting is moved ahead of turn dispatch. At this
stage, startup order and first-turn timing still needed live verification; the
final controlled validation below supplies that evidence. Phase 4C and later
phases remain deferred. The coding agent did not run live calls.

## Final controlled TTS warm-pool validation — 2026-09-14

**VERIFIED AT RUNTIME — task-owner supplied controlled live results**, not
independently reproduced by the coding agent. These validate the corrected
startup-readiness barrier and 15-second safe warm-idle policy on the tested path:

- The initial TTS warm connection was ready before caller audio forwarding.
- The first turn used warm TTS with **0ms setup**, compared with the earlier
  **4132ms cold setup** observed during the startup race.
- Successful warm reuse was observed at **10.164s, 12.995s and 14.629s idle**.
- Over-age sockets were proactively evicted at **15.000–15.001s** and replaced.
- No `input_timeout_exceeded` occurred.
- No socket older than the **15s safe maximum** was checked out.

A later `quota_exceeded` failure was reported as **provider-account quota
exhaustion**, unrelated to the pool design. It does not negate the observed
readiness, warm reuse, expiry or replacement results, and this run must not be
described as free of all provider failures.

The proven result is **warm-connection/setup behavior**, including elimination
of the observed first-turn cold setup. This does **not** establish lower
ElevenLabs synthesis latency, faster provider handshakes, lower mouth-to-ear
latency, or universal reliability under every provider/account condition.
Retain the 15-second maximum and startup barrier. This validates the narrow TTS
work only: it does not complete Phase 4/4D, authorize Phase 4C, or advance later
phases. Earlier failed runs and offline-only evidence above remain historical.

## Phase 4A verification workflow

The working tree may contain local Qwen/latency experiments that are not on remote
`main`. Do not reset, overwrite or silently stash them. Verify this branch in a
separate worktree instead:

```bash
git fetch origin phase4-realtime-latency
git worktree add --detach ../aivoicecall_v2_phase4 origin/phase4-realtime-latency
cd ../aivoicecall_v2_phase4
```

Use the existing project Python environment if available; do not install a new
dependency merely to run this slice. Focused tests first:

```bash
../aivoicecall_v2/.venv/bin/python -m pytest -q \
  tests/test_flux.py \
  tests/test_bluetooth_production.py \
  tests/test_bluetooth_conversation.py \
  -p no:cacheprovider
```

Then the Bluetooth-focused selection:

```bash
../aivoicecall_v2/.venv/bin/python -m pytest -q \
  tests/test_bluetooth_*.py \
  -p no:cacheprovider
```

Only after those pass should an authorized full root regression be run. Compare
failures by identity/signature against the four recorded historical failures; do
not treat an equal failure count as proof of no regression.

### Phase 4A executed offline evidence — 2026-09-14

Task-owner executed the verification from the detached worktree at branch commit
`630e2f65ff0b2be5aa798f3f026e7697d8848c7d` using the existing project Python 3.12
virtual environment. No live provider/device/call action was part of these runs.

Focused selection:

```text
26 passed, 3 warnings in 0.96s
```

Bluetooth-focused selection:

```text
77 passed, 3 warnings in 1.33s
```

Warnings are the already-known deprecations only:

```text
websockets.client.WebSocketClientProtocol is deprecated
websockets.legacy is deprecated
audioop is deprecated and slated for removal in Python 3.13
```

No new test failure identity was observed in these Phase 4A offline selections.
This passes the offline portion of the 4A gate only. It does **not** prove eager
lead, resume rate, provider behavior, caller mouth-to-ear latency or Phase 4
acceptance. Those remain pending controlled runtime evidence.

A live measurement is **not** part of offline verification and must not be run
implicitly. When a controlled active call/provider/device exercise is explicitly
authorized, the Bluetooth runner can opt in with for example:

```text
--eager-eot-threshold 0.4
```

Without that flag, eager mode is disabled and Flux keeps the existing final-EOT
provider request behavior.

## Phase 5 — controlled cellular E2E validation

No Phase 5 implementation is authorized by this plan. When separately approved,
measure the physical path with valid clocks/correlation:

- caller speech-stop to final/eager turn events
- final EOT to first committed LLM phrase
- TTS request to first audio
- first Bluetooth uplink write and caller mouth-to-ear proxy/measurement
- p50/p95/p99 warm and cold distributions
- echo/self-audio, interruption and manual abort

The working product target is sub-500 ms p50 mouth-to-ear on normal turns with a
strong p95, not a claim that every cloud/cellular turn can be deterministically
below 500 ms.

## Phase 6 — lifecycle ownership

Separate authorization. Own answer/hangup, remote disconnect, provider
cancellation, idempotent teardown and route restoration. Phase 4 must not smuggle
call-control behavior into the latency work.

## Phase 7 — resilience and scale

Separate authorization. Validate staged concurrency and overload behavior at
1, 5, 10, 25 and 50 simultaneous sessions (or the approved production target):
provider limits, memory, event-loop lag, queue residence, cancellations, route
isolation, resource leaks and graceful degradation. The design target is that
speculation can be skipped under pressure while the normal final-EOT call path
continues correctly.

## Phase 8 — release and operations

Separate authorization. Package supported startup, health/metrics, feature flags,
capacity limits, rollback runbook, supported environment and release evidence.

## Change-control rule

If implementation evidence requires changing this plan, update this file and the
owning Phase 4/ROADMAP/DECISIONS documentation in the same change. Preserve the
old rationale/evidence; record reversals as new decisions instead of rewriting
history. A plan change is not authorization to cross a later phase boundary.

## Earlier shadow transcript experiment — 2026-09-15

**IMPLEMENTED / OFFLINE VERIFIED; live benefit UNKNOWN.** The task owner
explicitly authorized this next 4B step. This supersedes the eager-only trigger
choice for the new opt-in experiment, not the historical evidence or the 4C gate.
The earlier planned 4B phrase “validate/promote” is not an implemented promotion
path: this slice and the existing implementation only observe and discard.

### Signal and wiring

[Deepgram's Flux state contract](https://developers.deepgram.com/docs/flux/state)
(inspected 2026-09-15) documents `Update` approximately every 250 ms of transcribed
audio, including unchanged transcripts. Unlike final/eager turn events, an
Update has no documented immutable-transcript guarantee. Nova-style `is_final`
and word confidence are not a stable-turn contract here. Repeated whole Update
text is therefore a local admission heuristic, never proof the caller stopped.
StartOfTurn can carry earlier text, but a single onset snapshot supplies no
repeat-stability evidence; it resets shadow turn state rather than starting LLM.

`FluxService._on_message` already forwarded nonempty Updates to `on_interim`.
`run_bluetooth_conversation` previously ignored them. It now passes them to
`SpeculativeTurnCoordinator.on_interim`; that method is inert unless early mode
is enabled. Optional `include_empty_interims=True` forwards empty replacement
Updates too, only in this Bluetooth experiment. Default Flux/carrier/browser
callbacks and provider connection parameters remain unchanged.

### Exact admission and invalidation rule

Current rule incorporates the [100ms revision](#phase-4b1-admission-revision--2026-09-15);
the original experiment used 200ms, retained in the historical evidence below.

- Enable `--shadow-speculation --shadow-early-transcripts` together with an
  explicit `--eager-eot-threshold` in the existing 0.3–0.7 range. Early mode alone
  is rejected before resource setup. Omitting the early flag retains eager-only
  measurement; omitting both shadow flags disables speculative requests.
- A stripped full transcript must contain at least **3 whitespace-separated
  words**, at most **2000 characters**, and recur unchanged on at least **two
  Update callbacks spanning 100 ms** of local monotonic receive time. Case,
  punctuation and internal whitespace remain significant. No timer fires when
  updates stop; a new callback must confirm stability.
- At most **2 admission attempts per turn**, including capacity failures, errors
  and eager fallback; at least **1 second between admissions**, including across
  turns. These are experimental bounds, not tuned live thresholds.
- Any extension, replacement, deletion, or empty Update invalidates the active
  generation immediately. A different eager transcript also invalidates it.
  Matching eager text leaves an earlier request intact. Eager fallback does not
  require the 3-word/repeat rule but shares the size, attempt and cooldown bounds.
- `TurnResumed` cancels/discards and clears stability evidence without resetting
  the turn's attempt budget. Fresh Updates must establish stability again.
  `StartOfTurn` discards prior candidates and resets the per-turn budget.
  Final EOT closes admission until the next StartOfTurn, including empty finals.
- Never admit while a prior task is still acquiring capacity, requesting tokens,
  or closing its stream, even after its generation was invalidated. No pending
  replacement queue or delayed automatic retry is created; subsequent qualifying
  events may retry within budget. The existing gate waits at most **25 ms** and
  falls back on exhaustion. Provider probing has a **2-second cooperative
  timeout** after admission. Cleanup cancels and awaits owned tasks, without
  re-cancelling a stream already closing.
- Exact final transcript equality only informs telemetry. Final EOT always enters
  the normal pure-state-machine/Agent path, with no speculative TTS, text reuse,
  Agent cancellation or committed-history mutation from shadow callbacks.

### Telemetry and interpretation

`BTShadow` records generation, trigger (`interim`/`eager`), character count and:

- `trigger_to_first_token_ms` (includes capacity wait; also logged when ready);
- `trigger_to_final_ms` and `speculative_lead_ms = final - first_token`;
- `ready_before_final` / `not_ready_by_final` for matching active candidates;
- `transcript_mismatch`, `replaced`, `resumed`, `new_turn`, `cancelled` (cleanup),
  `capacity_skip` and `probe_error:<exception class>`;
- `discarded_by_final` for the most recent invalidated/skipped candidate and
  `no_candidate_by_final` when no attempt exists;
- `transcript_match` and the boolean eligibility/readiness outcome at final.

The legacy `eager_to_final_ms` field remains populated only for eager triggers.
A missing first token/lead is unknown, not zero. Discarded candidates may have a
positive historical first-token lead and matching final text but remain
ineligible. Do not count their lead as useful readiness. For repeated admissions,
final metrics describe the latest candidate; earlier cancellation records remain
in logs. In-memory observations retain only the latest 256 records; all records
are logged without transcript, generated text, hashes, prompts or exception body.

The existing provider probe returns only after stream closure. Readiness therefore
requires both first-token receipt and completed probe cleanup by final; a token
received while close is pending does not count. A first token is not a completed
answer, and no answer is retained. Provider cancellation is cooperative: closure
may briefly overlap the ordinary final request, which must not wait for it.
Cancellation-hostile providers cannot be forcibly terminated by asyncio; no new
shadow work starts while their task remains alive.

### Architecture limits and next gate

Source inspection found the production capacity gate is **per invocation/call**
(`run_production_bluetooth_conversation`), despite older plan language promising
bounded global concurrency. That historical implementation claim is not supported
by source. This change preserves that boundary; a shared multi-call gate and load
validation remain future work. The optional manual runner is the current scope.
Ordered Flux socket callbacks and StartOfTurn/EndOfTurn boundaries are assumed;
this is not an out-of-order provider-event replay protocol. Generation identity
protects against late asynchronous probe completion.

Focused race tests, Bluetooth and full regression results are recorded in
[TESTING](../TESTING.md#earlier-shadow-transcript-regression--2026-09-15).
Offline evidence supports requesting a controlled **shadow-only** live experiment
on the already validated reference setup; no calls/devices/providers were run by
the coding agent. Keep the warm TTS policy/model/voice/audio settings fixed and
compare eager-only and early mode. Report all final denominators, trigger/lead
and TTFT distributions, match/mismatch, cancellations, skips/errors, requests per
turn and normal Agent TTFT/contention. Include short turns, extensions, corrections,
mid-sentence pauses and resumes. Tune the admission heuristic only against those
measurements. No sub-500ms claim, Phase 4C readiness, answer reuse, or later-phase
acceptance follows from these offline tests.

## Phase 4B.1 admission diagnosis — 2026-09-15

**REVIEWED LIVE METADATA; exact rejection cause UNKNOWN.** The task owner
reports the corrected model `qwen/qwen3.8-27b` returned HTTP 200. Read-only,
allowlisted metadata extraction from `/tmp/bt-phase4b1-shadow.log` found 9
StartOfTurn events, 12 eager events, 9 admitted generations (all `trigger=eager`),
6 `not_ready_by_final`, 3 resumed/discarded finals and zero interim triggers.
No live call/provider/device action was performed during this review.

The log contains neither Update receipt/repetition timing nor word counts,
admission rejection reasons or the early-mode setting. Flux heartbeat totals
(178 messages after 250 input frames; 333 after 500) include all message kinds
and cannot establish Update cadence or identical-text stability. Character
counts cannot establish the whitespace-separated word count. A valid Groq model
only affects the probe after admission; it does not select the trigger.

Source evidence:

- `FluxService._on_message` awaits the Update callback; Bluetooth
  `on_flux_interim` forwards to `SpeculativeTurnCoordinator.on_interim` when the
  coordinator exists. Injected real-dispatch tests demonstrate this wiring,
  including ordinary final Agent execution, but do not prove this run's delivery.
- Admission requires a confirming identical stripped whole Update after >=200ms,
  with >=3 words and <=2000 characters. A pause without another Update never
  fires a timer. Every whole-text change restarts stability.
- Eager may admit first and block a later qualifying Update as `active_candidate`;
  replacement/resume can also leave cancellation, cooldown or budget gates.
  Cancelling an already admitted interim would not erase its original trigger log.
- `scripts/dev/08_run_bt_shadow.sh` currently omits `--shadow-early-transcripts`.
  Running that helper unchanged disables early admission. The inspected run log
  does not establish the launch command, so this is conditional, not the proven
  runtime cause. The helper and speculative settings remain unchanged.

### Diagnostic contract (no admission/threshold change)

The shadow coordinator logs `BTShadowAdmission: configured early_enabled=...`.
Early mode also enables the optional `FluxService.diagnose_updates` locally:

- `BTShadowFlux` logs each recognized turn event and a service-lifetime Update
  count, callback presence, successful Update callback return or empty filtering.
  Receipt without return requires checking callback failure/teardown; receipt
  without coordinator admission rows indicates a delivery/configuration gap.
- `BTShadowAdmission` logs each early-mode Update with a local turn ordinal,
  per-turn Update count, consecutive same-transcript repetitions **after the
  first observation**, stable-span ms, word count/eligibility, rejection/admission
  reason and attempt count. Empty Updates are counted too. Stability is measured
  between received Updates, never extrapolated from a pause.
- Reasons distinguish changed text, too few words, insufficient stable span,
  oversized text, active candidate, pending prior task, cooldown, attempt budget,
  closed turn/coordinator and admission. Reasons follow existing branch priority;
  word eligibility is independent, so a changed two-word Update reports both.
- `eager_arrived_first=True` means eager was observed before the first successful
  interim admission in this turn, not necessarily that eager was admitted or that
  no eligible Update existed. Eager rows show their own admission result.
- Final, resume, start-boundary and cleanup rows retain the last Update snapshot;
  word count and stable span on these rows describe that Update, **not** the
  eager/final transcript or elapsed time since the last Update. Resume clears
  stability; StartOfTurn resets turn counters. Final rows include turns with zero
  Updates. Updates arriving before coordinator construction log
  `reason=speculator_not_ready` at the Bluetooth callback boundary.

Only counters/booleans/timings/closed reason strings are added: no text, hashes,
prompts, generated output or provider bodies. State is constant-size; no timer,
queue, retry, request, audio, history or cancellation behavior is added. Default
Flux callers have diagnostics disabled. Per-Update logging overhead remains
unmeasured; use the same instrumentation for controlled comparisons.

### Next controlled test (proposal only; not executed)

After separate live authorization, use the existing validated phone/audio/voice,
15-second TTS policy, threshold 0.3 and corrected model. Explicitly pass **both**
shadow flags; the eager-only helper is unsuitable for this early-mode test.
With the existing provider environment and explicit `BT_ADDRESS` already set:

```bash
PYTHON_DOTENV_DISABLED=1 LLM_MODEL=qwen/qwen3.8-27b SHUO_LOG_LEVEL=INFO PYTHONPATH=. \
  .venv/bin/python -c 'from shuo.log import setup_logging; setup_logging(); import runpy; runpy.run_path("scripts/run_bluetooth_ai.py", run_name="__main__")' \
  --bluetooth-address "${BT_ADDRESS:?Set BT_ADDRESS to the selected phone}" \
  --latency 120ms --eager-eot-threshold 0.3 \
  --shadow-speculation --shadow-early-transcripts \
  --call-id bt-phase4b1-admission \
  2>&1 | rg --line-buffered 'BTShadow|Eager EOT measurement|BTLatency' \
  | tee /tmp/bt-phase4b1-admission.log
```

First verify `configured early_enabled=True`. Exercise one/two-word controls,
3+-word complete utterances, longer clauses with 300–600ms pauses, extensions,
corrections and resumes; repeat each several times. Pauses are a test stimulus,
not a guarantee of repeated Updates. Match receipt/return counts to coordinator
rows and classify each turn by the first blocking rule and eager ordering.
Record zero-Update turns as well as all admissions/cancellations/final outcomes.
If repeats never arrive, measure that before proposing a different heuristic;
if words or eager ordering block admission, retain the thresholds until the
observations justify a separately reviewed change. Preserve manual call control,
shadow-only output and normal final Agent responses. Phase 4C remains deferred.

## Phase 4B.1 admission revision — 2026-09-15

**REVIEWED LIVE METADATA / IMPLEMENTED; revised live benefit UNKNOWN.** The
current task authorizes this narrow behavior revision and offline tests. No live
calls, devices or provider probes were started. Read-only allowlisted metadata
extraction from `/tmp/bt-phase4b1-diagnostics.log` (local log clock) found early mode enabled, 409 Update receipts, 409 callback returns,
409 coordinator Update rows, 6 start/final turns, 12 eager events, 6 resumes,
and 8 admitted generations, all eager. Text/credentials were not extracted.

### Measured opportunities and limits

Times below are log receive times; identical spans are coordinator monotonic
measurements. The millisecond log clock makes event differences approximate.

| Turn / sequence | Measurements | Implication |
|---|---|---|
| 3 | 3-word text changes at 07.302 and 07.545; eager 07.682; first identical repeat at 07.847, span 301.975ms | No repeat threshold can beat this eager; word eligibility alone is insufficient |
| 5 initial (12:39) | 4 words at 55.038, changed to 5 at 55.076; eager 55.164; repeat 55.201, span 125.447ms; next repeat 55.441, span 365.335ms | Eager is 126ms after first word-eligible Update, 88ms after latest text; even a 100ms repeat rule arrives 37ms too late |
| 5 after resume 56.205 | 6 words at 56.480; identical repeat 56.592, span 111.629ms; changed text 56.710; eager/final 56.748 | A 100ms rule can admit about 156ms before eager, but this example would be invalidated about 118ms later |
| 6 after eager 12:40:20.556 and resume 20.807 | 3 words 21.069; repeat 21.300, span 230.892ms; eager 21.335 | Repeat is before eager but only 744ms after the prior admission; shared cooldown intentionally rejects it |
| 6 later | New text 21.559; eager 21.676; repeat 21.711, span 151.807ms | Repeat is 35ms after eager; a smaller span alone cannot beat this eager |
| 6 token/discard | Token ready 22.354; resume 22.449; final 22.613; trigger-to-token 678.244ms, trigger-to-final 937.144ms | 258.899ms historical lead at final remains ineligible after resume and final mismatch; token precedes resume by only about 95ms |

This resolves the newer run's delivery/admission uncertainty; it does not fill
missing telemetry in the older `bt-phase4b1-shadow.log`. Nor does this six-turn
sample establish an optimal threshold or a false-speculation rate.

### Options and selected rule

| Option | Evidence-based tradeoff / decision |
|---|---|
| Lower 200ms to 150ms | Still rejects the 111.629ms pre-eager opportunity; 151.807ms repeat is already after eager |
| One identical repeat plus 100ms minimum | Selected: admits the measured 111.629ms repeat while rejecting bursts below 100ms; simple local change, no new task/timer |
| Zero minimum / first eligible Update | Weaker evidence; 4→5 words changed in 38ms in turn 5. No measured benefit justifies removing repeat confirmation |
| Give early priority over eager | Matching eager already retains an admitted early generation in `on_eager`; test it. Delaying eager to wait for a future repeat adds scheduling and may miss eager-only opportunities. Relabeling an eager admission is not an earlier start |
| Separate early/eager cooldowns | Would allow the turn-6 request at +744ms but permits extra close-spaced work. Keep the shared 1s bound to isolate this experiment's risk |
| Timer after one Update | Could start before some initial eager events, but assumes stability without another provider observation and adds cancellation races. Defer |

`SpeculativeTurnCoordinator.on_interim` now requires **two or more consecutive
identical stripped whole Updates spanning at least 100ms**, **3+ words** and
**<=2000 characters**. No timer or elapsed-time admission in `on_eager`.
An admitted matching early candidate wins over eager without a restart or extra
attempt. Different eager text invalidates it immediately; admission of any
replacement still requires the existing gates.

All prior bounds remain: **two attempts per turn shared by early/eager**, including
capacity/error attempts; **1s between all admissions including across turns**;
no admission while any prior task is still closing; 25ms capacity wait, 2s probe
timeout, bounded observations. Mutation/empty Update/resume invalidate; resume
clears repeat evidence without replenishing budget. Final closes the turn and
requires exact text match for readiness telemetry, while the normal Agent always
generates independently. No speculative TTS, output reuse or history mutation.
Startup diagnostics now include `min_stable_span_ms=100` to identify the rule.

Expected risk increase: more short-lived prefix requests and earlier consumption
of the two-attempt budget, potentially suppressing a better later eager request.
The measured post-resume opportunity itself changes shortly afterward: this is
evidence of likely wasted work, not a demonstrated useful latency win. Request
rate/overlap bounds are unchanged; normal Agent provider contention can still
increase within those bounds and must be measured. No Phase 4C inference follows.

### Exact next controlled shadow test (not executed)

After separate live authorization, keep the validated phone/audio selection,
voice, normal model settings and 15s TTS policy fixed. Manually establish/control
the call and retain the validated manual abort procedure. With the existing
provider environment and explicit selected `BT_ADDRESS` already set:

```bash
PYTHON_DOTENV_DISABLED=1 LLM_MODEL=qwen/qwen3.8-27b SHUO_LOG_LEVEL=INFO PYTHONPATH=. \
  .venv/bin/python -c 'from shuo.log import setup_logging; setup_logging(); import runpy; runpy.run_path("scripts/run_bluetooth_ai.py", run_name="__main__")' \
  --bluetooth-address "${BT_ADDRESS:?Set BT_ADDRESS to the selected phone}" \
  --latency 120ms --eager-eot-threshold 0.3 \
  --shadow-speculation --shadow-early-transcripts \
  --call-id bt-phase4b1-repeat100 \
  2>&1 | rg --line-buffered 'BTShadow|Eager EOT measurement|BTLatency' \
  | tee /tmp/bt-phase4b1-repeat100.log
```

Verify `early_enabled=True min_stable_span_ms=100`. Exercise 20 synthetic turns:
four each of 1–2-word controls, complete 4–6-word questions, longer questions with
300–600ms pauses, extensions/corrections after a brief pause, and resume/repeat
sequences. Record actual provider events; spoken pauses do not guarantee repeats
or TurnResumed. Repeat the same sequence in an eager-only comparison by omitting
`--shadow-early-transcripts` and using call ID/log suffix `eager-control`.

For all finals (including zero-Update/no-candidate turns), report interim/eager
admissions and first admission ordering, repeat spans and rejection reasons,
requests per turn, match/mismatch/resume/cancel/skip/error outcomes, valid
ready-before-final fraction, trigger-to-token/final/lead distributions, and
normal Agent TTFT from BTLatency. Separate positive discarded lead from useful
matching readiness. Require <=2 attempts/turn, >=1s admission separation and no
shadow request overlap. Compare normal Agent TTFT/contention and cancellation
rates with eager-only, and explicitly report if no repeat beats eager. Stop and
manually abort for stale speech, request overlap/budget violation or provider
failure; retain metadata. Roll back by removing the early flag; remove both
shadow flags to disable all shadow work. No automatic live run or Phase 4C.

## Separate barge-in lifecycle investigation — 2026-09-15

The [investigation record](../BLUETOOTH_BARGE_IN_INVESTIGATION.md) reviews the
latest 100ms-shadow log without changing its thresholds. No normal interruption
is logged; all nine final responses dispatch. Added lifecycle diagnostics and
hardware-free real Agent/player interruption/restart tests. The reported audible
failure remains unlocalized and warrants a separately authorized controlled test.
This is not Phase 4C, and no phase status or call-control capability advances.

## Phase 4C automated implementation evidence — 2026-09-15

The task owner explicitly authorized Phase 4C implementation while deferring all
new real-call/provider/device validation until repository-level automation is
clean. The implementation is opt-in and does **not** reverse the earlier evidence
that the measured Phase 4B triggers had insufficient live ready-before-final
frequency. It therefore makes no latency-improvement or caller-heard claim.

Implemented contract:

- a speculative provider stream may be retained only after its first content token
  is ready; the remainder stays unread, so no complete response is buffered;
- final `EndOfTurn` may promote that stream exactly once only when the final
  transcript, system prompt and committed history snapshot still match;
- mismatch, `TurnResumed`, timeout, capacity pressure, teardown or invalid history
  discard/cancel the speculative stream and preserve the ordinary final-EOT path;
- promoted tokens still flow through the existing `Agent` token -> TTS -> Player
  callbacks; TTS remains non-speculative;
- the feature is disabled by default and requires the explicit Bluetooth shadow
  path plus `--prepared-response-reuse`; omitting the flag is the rollback;
- carrier/browser/default startup and the pure state machine are unchanged.

Automated verification was executed by GitHub Actions run `34966008520` on Python
3.12 with no provider secrets, Bluetooth device or cellular call. The run passed:
source application, `py_compile`, the focused Phase 4 regression, the complete
Bluetooth regression, the full root regression compared to the documented four
baseline failures by exact identity/signature, and final `git diff --check`.
The verified implementation was committed as `bc9996f727bf4ff7870d6f112e73200d82a49b87`.

Remaining gate: a controlled real call/provider/device exercise is still required
to establish actual latency benefit, audio correctness and caller-heard behavior.
Phase 4C code is repository-verified; Phase 4 overall acceptance is not yet
claimed.
