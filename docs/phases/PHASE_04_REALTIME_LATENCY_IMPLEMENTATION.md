# Bluetooth Phase 4 realtime latency implementation plan

**Status: IN PROGRESS — Phase 4A offline gate passed 2026-09-14; live eager-lead measurement pending separate authorization.**

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
