# Bluetooth roadmap

This is the Bluetooth roadmap, independent of the older carrier/provider phase
numbers in context.md, plan.md and phase8-plan.md. Baseline implementation is
[CURRENT_ARCHITECTURE](CURRENT_ARCHITECTURE.md).

Phase 2 hardware-free Bluetooth boundary code and tests have now been implemented.
Phase 3 real PipeWire capture/playback and AI-only route isolation were reference-
validated. Remote revision `ae9d981eff861deb364bb8a34691d6008877860b` then
added the first explicit Bluetooth→SHUO Phase 4 integration seam. Phase 4 is not
accepted yet; realtime latency/correctness hardening and gate evidence remain.

## Phase status

| Phase | Scope and complete phase contract | Status |
|---|---|---|
| 1 | [Hardware/runtime contract](phases/PHASE_01_RUNTIME_CONTRACT.md) | Complete: supplied runtime evidence |
| 2 | [Isolated adapter and codec](phases/PHASE_02_ADAPTER_AND_CODEC.md) | Complete for Phase 2 scope; production numeric queue/latency budget intentionally deferred to integrated measurement |
| 3 | [PipeWire capture/playback integration](phases/PHASE_03_PIPEWIRE_INTEGRATION.md) | Complete on reference hardware; Phase 6 lifecycle ownership remains pending |
| 4 | [SHUO conversation pipeline integration](phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md) | **In progress:** 4A/4B reference evidence plus default-off 4C/4D repository implementation are present; controlled provider/device/cellular validation and Phase 4 acceptance remain pending |
| 5 | [Controlled cellular end-to-end validation](phases/PHASE_05_CELLULAR_E2E.md) | Planned / requires separate approval |
| 6 | [Call control and lifecycle ownership](phases/PHASE_06_CALL_CONTROL_AND_LIFECYCLE.md) | Planned / requires separate approval |
| 7 | [Resilience and coexistence](phases/PHASE_07_RESILIENCE_AND_COEXISTENCE.md) | Planned / requires separate approval |
| 8 | [Release and operations](phases/PHASE_08_RELEASE_AND_OPERATIONS.md) | Planned / requires separate approval |

## Technical validation of sequencing

Retain all eight boundaries.

Phase 2 separates deterministic adapter/codec work from Linux process/device
variability with hardware-free code and tests. It implements Bluetooth format
contracts, stateful conversion, capability-based target selection, explicit
bounded-queue APIs, telephony fakes and minimum lifecycle rollback/cleanup.

Phase 3 implemented real PipeWire discovery, explicit capture/playback process
ownership, AI-only physical-route isolation/restoration and bounded cleanup on the
reference hardware. The 20-second lifecycle run was a validation harness, not a
production duration.

Phase 4 now has a remote base integration seam: the explicit manual runner builds
the Phase-3 session and wires Bluetooth media to the SHUO state/Agent/Flux/TTS
pipeline without changing default `main.py`. Phase 4 still owns correctness and
latency hardening around that seam. The realtime implementation plan is
[PHASE_04_REALTIME_LATENCY_IMPLEMENTATION](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md).

Phase 4A is measurement only: optional Flux eager-turn telemetry is disabled by
default and must not start LLM/TTS early. Later speculative generation remains
gated on measured eager lead/resume behavior and tests proving no stale history or
speech. Phase 5 remains the owner of actual caller mouth-to-ear acceptance data.

Phase 1 supplied evidence includes exposed `org.pipewire.Telephony.Call1` and
`org.ofono.VoiceCall` interfaces and successfully exercised manual D-Bus Answer
and disconnect/hangup on the reference environment. Automated SHUO control,
lifecycle reconciliation, reconnect and general-device compatibility remain
unimplemented/unverified. Phase 6 is still required; manual control success does
not complete it.

Phase 7 validates faults, concurrency, overload and coexistence before Phase 8
packages a supported release. Load-safety mechanisms can be designed earlier,
but 1/5/10/25/50-session qualification belongs to Phase 7.

Minimum lifecycle ownership and partial-start rollback began in Phase 2. Phase 5
still requires a tested manual phone hangup/abort path. Phase 6 remains the full
automated call-control and lifecycle phase.

If Phase 3 HFP streams only exist during calls, separate call authorization is
required for a narrowly controlled stream test; device-only scope does not imply
call permission.

The live PipeWire adapter alone may be Linux-specific, isolated and optional
behind an injected OS boundary. Core/carrier Windows/Linux portability and Windows
development/startup remain unchanged; default production entrypoints must neither
import nor start the adapter.

## Phase 2 evidence summary

Implementation revision:

```text
6f4c8c423a0d2741d033439c510fc21a1052a91e
```

Executed evidence supplied by the task owner:

- Bluetooth-focused suite: **27 passed, 1 warning**
- Relevant SHUO regression: **99 passed, 2 warnings**
- Full root suite: **802 passed, 4 failed, 4 warnings**
- The four full-suite failure identities match the known pre-existing baseline.
- No new failure identity was observed from Phase 2.

Known Phase 2 limitation: codec conversion currently relies on Python 3.12 stdlib
`audioop`, which is deprecated and removed in Python 3.13. No replacement
dependency has been approved.

The queue API is bounded and requires an explicit capacity/overflow policy, but
Phase 2 intentionally does not hard-code a production numeric queue budget.
Phase 3/integrated measurement owns runtime calibration.

## Per-phase contracts and acceptance

1. Phase 1 accepts only supplied runtime observations, no code or integrated E2E.
2. Phase 2 accepts deterministic hardware-free directional contracts/conversion,
   bounded queue semantics, lifecycle rollback and hardware-free tests.
3. Phase 3 accepts explicit negotiated stream targeting, real PipeWire I/O and
   cleanup evidence.
4. Phase 4 accepts injected pipeline integration, low-latency turn correctness and
   preserved regression behavior. Code existence alone is insufficient.
5. Phase 5 accepts authorized digital cellular duplex, latency/echo and abort evidence.
6. Phase 6 accepts verified control capability and complete lifecycle fault coverage.
7. Phase 7 accepts resilience, concurrency, privacy/security and coexistence evidence.
8. Phase 8 accepts supported operations, release evidence and rehearsed rollback.

## Common advancement and rollback policy

Complete only the currently authorized phase. Record revision, environment,
commands, test identities/signatures, sanitized evidence, limitations and rollback
result using TESTING.md. An unexplained new regression blocks acceptance;
historical failures remain individually identified rather than “same count.”

Update the owning phase, this status table, compatibility, known issues and
decisions together when phase evidence/status changes.

Every move to a later phase requires explicit task authorization after reviewing
the preceding gate. Phase planning/completion is not approval for dependency
changes, live device/provider access, phone control, real calls, deployment or
later-phase implementation.

Rollback normally means disabling optional Bluetooth/eager/speculative features,
stopping owned resources and restoring previous routes/config; carrier and browser
operation must survive that rollback.

## Phase 3 evidence summary

Base implementation revision:

```text
97076cd1739e2456843ced239eeebeedcfefa70b
```

Closeout changes after that base add AI-only route isolation/session resources,
capture shutdown draining, bounded route-restore retry, and safe handling when
selected BlueZ call ports disappear during hangup.

Executed evidence:

- Phase 3/route/cleanup focused selection before disappearance hardening:
  **21 passed, 1 warning**
- Full Bluetooth-focused selection after capture cleanup fix:
  **51 passed, 1 warning**
- Final focused route/session/process cleanup selection:
  **25 passed, 1 warning**
- Real active-call AI-only lifecycle: **20 seconds, clean start/stop**
- Five consecutive fresh start/stop cycles: **all 5 completed cleanly**
- During active session: physical laptop mic -> Bluetooth uplink absent
- During active session: Bluetooth downlink -> physical laptop speaker absent
- After normal stop: prior routes restored
- After normal and call-cut teardown: no `pw-cat` process remained
- Prior unread-capture shutdown timeout reproduced and resolved
- Real call-cut route-restore race reproduced, fixed and retested successfully
- Latest full root suite: **826 passed, 4 failed, 4 warnings**
- The four failures match the documented pre-existing baseline identities

Phase 3 is complete for its defined reference-hardware gate.

## Phase 4 current evidence/status

Remote base integration revision:

```text
ae9d981eff861deb364bb8a34691d6008877860b
```

That revision added an explicit manual Bluetooth-AI runner, Bluetooth conversation
orchestration/production wiring and focused tests. Its commit message explicitly
noted remaining issues, and the older docs were not advanced at the time.

The Phase 4A measurement slice and Phase 4B shadow mechanism are now present on
the current main-line work and have controlled reference-path evidence recorded
below. Phase 4 remains incomplete, and no sub-500 ms caller-heard latency claim
is supported by the current evidence.

## Phase 4 status update — 2026-09-14

Phase 4B shadow speculation has now passed focused offline/Bluetooth regression
testing and a controlled reference-path live measurement.

Current Phase 4 position:

- Phase 4A eager measurement: completed for the current reference path
- Phase 4B shadow coordinator: implemented and runtime exercised
- Phase 4B correctness/cancellation path: observed working
- current eager-trigger latency benefit: insufficient
- Phase 4C prepared-response reuse: **not authorized by current evidence**
- next work: measured TTS warm-connection latency issue, then investigation of an
  earlier safe speculative trigger
- Phase 5 and later phases remain unchanged

2026-09-14 TTS follow-up: owner-supplied live evidence proved the old eight-second
cutoff too aggressive, but disproved unlimited reuse with a silent turn at
19,819ms near the provider's 20-second input timeout. The revised implementation
uses a separate 15-second safe idle maximum plus liveness checks and proactive
expiry/refill. At that stage controlled validation was pending; the final result
is recorded below. This narrow, explicitly authorized warm-pool fix
does not advance Phase 4C or complete Phase 4D. See the
[implementation record](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#tts-warm-pool-follow-up--2026-09-14).

Further 2026-09-14 startup correction: owner-reported 4132ms first-turn setup
exposed a duplicate cold connection racing initial warmup. An explicit Bluetooth
readiness barrier and shared initial-checkout wait prevented that race offline;
controlled startup verification subsequently passed as recorded below.

Final controlled TTS validation (owner-supplied, 2026-09-14): initial readiness
preceded caller audio forwarding; first-turn setup was 0ms instead of the earlier
4132ms cold setup. Warm reuse below 15s and proactive expiry/refill at
15.000–15.001s were observed, with no over-limit checkout or input timeout.
Later account quota exhaustion was unrelated to the pool. Only connection/setup
behavior is proven, not lower synthesis latency. See the
[final evidence](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#final-controlled-tts-warm-pool-validation--2026-09-14).
This closes the narrow TTS live-validation item, not Phase 4/4D; no Phase 4C or
later-phase advancement is implied.

## Earlier shadow follow-up — 2026-09-15

The separately authorized repeated-Flux-Update shadow experiment is implemented
and offline tested behind an additional opt-in. It retains normal final-EOT
Agent generation and discards all speculative output. A controlled shadow live
comparison is the next evidence step, not an automatic action. Phase 4C and
Phase 4 acceptance remain deferred. See the
[admission rule and limits](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#earlier-shadow-transcript-experiment--2026-09-15).

### Phase 4B.1 admission revision — 2026-09-15

The current authorized slice reduces the confirming-Update minimum from 200ms
to 100ms with unchanged shadow-only bounds. Offline results are in
[TESTING](TESTING.md#phase-4b1-100ms-admission-regression--2026-09-15);
the [next controlled comparison](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#exact-next-controlled-shadow-test-not-executed)
is proposed, not executed. Useful live readiness remains unknown; Phase 4C and
later phases remain deferred.

### Phase 4C repository status — 2026-09-15

Phase 4C prepared-response reuse is implemented behind an explicit default-off
Bluetooth flag and has passed repository-level automated verification. This is an
implementation milestone, not Phase 4 acceptance: real provider/device/cellular
validation is still required before enabling the feature by default or claiming a
latency/caller-heard improvement. Phase 5 remains unchanged.

### Phase 4D repository status — 2026-09-15

Phase 4D hardening controls are implemented at revision `b56a38b132951b322ed5d63059a42f0a7600f829` and
passed the repository-level automated gates recorded in [TESTING](TESTING.md).
The new controls remain explicit Bluetooth opt-ins: bounded incremental TTS phrase
batching, provider-visible conversation-history budgeting, content-free Groq
usage/timing capture, parallel Flux/TTS startup, and a rules-C5 2/3-frame player
pre-roll A/B control. Existing carrier/browser/default startup behavior and the
validated 15-second TTS warm-idle policy are unchanged.

This does **not** complete Phase 4. No provider/device/cellular call was executed
for this milestone, no 2-frame pre-roll or context budget was promoted to a
default, and no caller-heard latency improvement is claimed. The remaining Phase
4 gate is controlled real-path validation of the enabled 4C/4D candidates and
rollback behavior. Phase 5 and later phases remain unchanged and separately
authorized.
