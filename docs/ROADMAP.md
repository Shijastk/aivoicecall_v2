# Bluetooth roadmap

This is the Bluetooth roadmap, independent of the older carrier/provider phase
numbers in context.md, plan.md and phase8-plan.md. Baseline implementation is
[CURRENT_ARCHITECTURE](CURRENT_ARCHITECTURE.md).

Phase 2 hardware-free Bluetooth boundary code and tests have now been implemented.
No live PipeWire, D-Bus, active-call, provider, or SHUO pipeline integration has
been added by Phase 2.

## Phase status

| Phase | Scope and complete phase contract | Status |
|---|---|---|
| 1 | [Hardware/runtime contract](phases/PHASE_01_RUNTIME_CONTRACT.md) | Complete: supplied runtime evidence |
| 2 | [Isolated adapter and codec](phases/PHASE_02_ADAPTER_AND_CODEC.md) | Complete for Phase 2 scope; production numeric queue/latency budget intentionally deferred to integrated measurement |
| 3 | [PipeWire capture/playback integration](phases/PHASE_03_PIPEWIRE_INTEGRATION.md) | Complete on reference hardware; Phase 4 SHUO media integration and Phase 6 lifecycle ownership remain pending |
| 4 | [SHUO conversation pipeline integration](phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md) | Planned / pending approval |
| 5 | [Controlled cellular end-to-end validation](phases/PHASE_05_CELLULAR_E2E.md) | Planned / pending approval |
| 6 | [Call control and lifecycle ownership](phases/PHASE_06_CALL_CONTROL_AND_LIFECYCLE.md) | Planned / pending approval |
| 7 | [Resilience and coexistence](phases/PHASE_07_RESILIENCE_AND_COEXISTENCE.md) | Planned / pending approval |
| 8 | [Release and operations](phases/PHASE_08_RELEASE_AND_OPERATIONS.md) | Planned / pending approval |

## Technical validation of sequencing

Retain all eight boundaries.

Phase 2 now separates deterministic adapter/codec work from Linux process/device
variability with hardware-free code and tests. It implements Bluetooth format
contracts, stateful conversion, capability-based target selection, explicit
bounded-queue APIs, telephony fakes and minimum lifecycle rollback/cleanup.

Phase 3 has implemented real PipeWire discovery, explicit capture/playback process
ownership, AI-only physical-route isolation/restoration and bounded cleanup on the
reference hardware. The 20-second lifecycle run was a validation harness, not a
production duration. Default application startup still does not attach Bluetooth to
the SHUO conversation/call lifecycle; that remains a later integration boundary.

Phase 4 must address the existing carrier-specific session, checkpoint, recording
and history assumptions (`CarrierSession`, `run_conversation`) without copying the
browser PCM path. Phase 5 measures the actual cellular path.

Phase 1 supplied evidence includes exposed `org.pipewire.Telephony.Call1` and
`org.ofono.VoiceCall` interfaces and successfully exercised manual D-Bus Answer
and disconnect/hangup on the reference environment. Automated SHUO control,
lifecycle reconciliation, reconnect and general-device compatibility remain
unimplemented/unverified. Phase 6 is still required; manual control success does
not complete it.

Phase 7 validates faults/coexistence before Phase 8 packages a supported release.

Minimum lifecycle ownership and partial-start rollback began in Phase 2. Phase 5
still requires a tested manual phone hangup/abort path. Phase 6 remains the full
automated call-control and lifecycle phase.

If Phase 3 HFP streams only exist during calls, separate call authorization is
required for a narrowly controlled stream test; device-only scope does not imply
call permission.

The future live PipeWire adapter alone may be Linux-specific, isolated and optional
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
Phase 3 must measure and approve that runtime budget.

## Per-phase contracts and acceptance

1. Phase 1 accepts only supplied runtime observations, no code or integrated E2E.
2. Phase 2 accepts deterministic hardware-free directional contracts/conversion,
   bounded queue semantics, lifecycle rollback and hardware-free tests.
3. Phase 3 accepts explicit negotiated stream targeting, real PipeWire I/O and
   cleanup evidence.
4. Phase 4 accepts injected pipeline integration and preserved regression behavior.
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
changes, live device/provider access, phone control, real calls, deployment,
commit/push, or later-phase implementation.

Rollback normally means disabling optional Bluetooth, stopping owned resources
and restoring previous routes/config; phase-specific procedures are linked above.
Default carrier and browser operation must survive that rollback.

## Phase 3 evidence summary

Base implementation revision:

```text
97076cd1739e2456843ced239eeebeedcfefa70b
```

Closeout changes after that base add AI-only route isolation/session resources,
capture shutdown draining, bounded route-restore retry, and safe handling when
selected BlueZ call ports disappear during hangup. Record the final closeout
revision after this code/docs change set is committed.

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

Phase 3 is complete for its defined reference-hardware gate. The next integration
boundary is Phase 4: connect Bluetooth media to the existing SHUO conversation
pipeline without changing carrier/browser defaults. Phase 6 still owns complete
automated call-control and lifecycle behavior, while reconnect/soak/coexistence
qualification remains in the resilience phases.
