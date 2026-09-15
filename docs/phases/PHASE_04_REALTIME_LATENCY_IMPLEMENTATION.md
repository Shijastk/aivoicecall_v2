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
