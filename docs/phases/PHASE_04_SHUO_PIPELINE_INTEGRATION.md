# Bluetooth Phase 4 — SHUO conversation pipeline integration

**Status: IN PROGRESS. Base Bluetooth→SHUO integration exists on remote `main`;
Phase 4 acceptance is still pending. Phase 4A eager-turn measurement is authorized
on `phase4-realtime-latency`.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Realtime latency work is detailed in
[PHASE_04_REALTIME_LATENCY_IMPLEMENTATION](PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md).

Historical note: this file previously said Phase 4 was wholly planned. Remote
revision `ae9d981eff861deb364bb8a34691d6008877860b` added an explicit manual
Bluetooth-AI runner plus Bluetooth conversation/production bridges. That code
existence does not itself complete the phase; runtime gates, regressions and the
remaining latency/safety work still require evidence.

## Goal and included scope

Connect the optional Bluetooth adapter to existing conversation services with
unchanged carrier/browser behavior, then harden that seam for low-latency realtime
turn-taking without weakening correctness or lifecycle isolation.

Included Phase 4 scope:

- injected Bluetooth transport seam and optional entrypoint;
- pure event/state reuse and carrier-format audio conversion;
- barge-in, completion and failure mapping;
- bounded media lifecycle and deterministic cleanup;
- latency telemetry required to identify where time is spent;
- opt-in eager-turn measurement, followed by separately gated speculative LLM
  work only if measurements justify it;
- TTS/context/startup latency hardening that preserves streaming.

## Explicit exclusions

- Phase 5 controlled cellular E2E acceptance campaign;
- implicit provider/live-device contacts from tests or normal startup;
- automatic answer/hangup or lifecycle ownership (Phase 6);
- soak/reconnect/50-call qualification (Phase 7);
- release packaging/operations (Phase 8);
- unrelated carrier or browser provider rewrites.

## Existing Phase 4 base implementation

Remote `main` currently contains, among other Phase 4 files:

- `scripts/run_bluetooth_ai.py` — explicit manual opt-in runner;
- `shuo/bluetooth/conversation.py` — bounded Bluetooth event loop using the pure
  SHUO state machine;
- `shuo/bluetooth/production.py` — injected production service wiring;
- `shuo/bluetooth/shuo_inbound.py` and `shuo_media.py` — directional codec/media
  bridges;
- focused Bluetooth conversation/production tests.

The runner does not answer, originate or hang up a cellular call. Default
`main.py`, Vobiz, Twilio and browser/V2 startup do not import/start the Bluetooth
runner.

## Phase 4A — measurement-only eager turn work

The first authorized realtime-latency slice is deliberately non-behavioral:

- `FluxService` may receive an optional `eager_eot_threshold` only when explicitly
  requested;
- the Bluetooth runner exposes an explicit measurement flag;
- default is `None`/disabled;
- measurement range is restricted to 0.3–0.7 so the existing provider default
  final-EOT threshold is not silently changed;
- EagerEndOfTurn/TurnResumed/final timing metadata is logged without transcript
  text;
- AI still starts only from final `EndOfTurn` in this slice.

This slice does **not** change `process_event`, start speculative LLM/TTS work,
change conversation history, or change carrier/browser behavior.

## Later Phase 4 slices and gates

The detailed plan owns the sequence:

1. **4A measurement:** eager→final lead and resume-rate evidence.
2. **4B shadow speculation:** one cancellable speculative LLM task per call;
   discarded drafts never mutate history or speech.
3. **4C commit-on-final:** only matching, final-confirmed drafts may be reused;
   stale provider chunks are generation-id rejected.
4. **4D TTS/context/startup hardening:** warm-socket policy, phrase-aware streaming,
   prompt/context budget and startup span optimization, each evidence-gated.

Optimization must always be able to degrade to the existing final-EOT path rather
than fail a call.

## Dependencies and prerequisites

Phase 3 targeting/cleanup evidence is accepted on the reference hardware. Any live
call/provider/device exercise still needs explicit current-task authorization.
Providers remain mocked in tests unless separately authorized.

Before speculative work begins, Phase 4A must prove that eager events provide
useful lead and record the false-start/TurnResumed rate. Do not infer those values
from vendor claims.

## Files and boundaries

Review/touch only the smallest justified seam around:

- `shuo/services/flux.py` for opt-in turn-detection telemetry;
- `shuo/bluetooth/production.py` and the explicit Bluetooth runner for opt-in
  configuration;
- later, a dedicated speculative-turn coordinator rather than reusing committed
  `Agent.cancel_turn` semantics;
- focused tests plus owning documentation.

Any shared-core file change must preserve carrier/browser defaults and be justified
against `AGENTS.md`, `CLAUDE.md` and `rules.md`.

## Tests and measurable acceptance gate

Phase 4 base/incremental acceptance requires injected tests demonstrating:

- downlink-only STT and no TTS/self-audio loop;
- streaming token/audio delivery;
- interruption and stale-completion rejection;
- zero-audio completion;
- partial/start failure and deterministic teardown;
- feature-off behavior with no eager configuration/provider change;
- eager measurement events never start an agent turn in 4A;
- no transcript content in eager timing logs;
- config snapshots and monitor/history/recording semantics are not fabricated for
  the manual Bluetooth harness.

Run focused tests before any authorized full regression and compare failures by
identity/signature, not only count.

## Risks and required containment

- Existing `LLMService` cancellation may preserve partial generated history, so it
  must not be used as the speculative-draft storage/cancellation primitive.
- Slow speculative work must run in a separate cancellable task; the main event
  loop must not await it and block TurnResumed/barge-in.
- At most one speculative request per call; later global concurrency limits must
  fail open to the ordinary final-EOT path, not fail the call.
- TTS stays non-speculative until the LLM cancellation/commit contract is proven.
- Provider thresholds/cost/load are measured before production defaults change.

## Rollback

Phase 4A rollback is immediate: omit the explicit eager measurement flag. The
provider connection then uses the existing final-EOT behavior. The optional
Bluetooth entrypoint itself remains removable/disableable without affecting
carrier/browser defaults.

Later speculative slices must retain a feature flag that returns to ordinary
final-EOT generation.

## Artifacts and documentation to update

When behavior/evidence changes, update the realtime implementation plan, this
phase file, ROADMAP, CURRENT_ARCHITECTURE, TESTING, KNOWN_ISSUES and DECISIONS as
applicable. Preserve the append-only recovery/decision history in `context.md`.

## Approval and stop boundary

### TTS controlled validation update — 2026-09-14

Task-owner supplied final live results validate startup readiness before caller
audio forwarding, first-turn warm setup of 0ms (previously 4132ms cold), reuse
below 15 seconds, and proactive expiry/replacement at 15.000–15.001s. No
over-limit checkout or `input_timeout_exceeded` occurred. A later
`quota_exceeded` was provider-account quota exhaustion, unrelated to pool design.
Only warm-connection/setup behavior is proven; lower ElevenLabs synthesis
latency is not claimed. See the
[complete evidence and limits](PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#final-controlled-tts-warm-pool-validation--2026-09-14).
Phase 4 acceptance remains pending; Phase 4C and later phases are unchanged.

Phase 5 still requires explicit authorization naming controlled calls/providers/
devices, approved measurements and manual emergency exit. Phase 4 work does not
authorize automatic call control, concurrency qualification or release work.

## Earlier shadow follow-up — 2026-09-15

The task owner approved a bounded earlier Update-trigger experiment within 4B,
with no response reuse. The
[realtime latency record](PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#earlier-shadow-transcript-experiment--2026-09-15)
owns the exact admission/invalidation rule, opt-in, telemetry and concerns.
This advances offline shadow implementation only; 4C and phase acceptance remain
pending live evidence and separate authorization.

### Phase 4B.1 threshold follow-up — 2026-09-15

The latest diagnostics justify a 100ms confirming-repeat minimum, with shared
cooldown/attempt bounds retained. See the
[admission revision](PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#phase-4b1-admission-revision--2026-09-15)
for measured opportunities, false-speculation risk and the exact next shadow
comparison. This is still 4B.1; no Phase 4C or live exercise was performed.
