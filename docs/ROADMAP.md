# Bluetooth roadmap

This is the Bluetooth roadmap, independent of the older carrier/provider phase
numbers in context.md, plan.md and phase8-plan.md. Baseline implementation is
[CURRENT_ARCHITECTURE](CURRENT_ARCHITECTURE.md). No Bluetooth code was added or
later phase approved by the documentation task.

## Phase status

| Phase | Scope and complete phase contract | Status |
|---|---|---|
| 1 | [Hardware/runtime contract](phases/PHASE_01_RUNTIME_CONTRACT.md) | Complete: supplied runtime evidence |
| 2 | [Isolated adapter and codec](phases/PHASE_02_ADAPTER_AND_CODEC.md) | Planned / pending approval |
| 3 | [PipeWire capture/playback integration](phases/PHASE_03_PIPEWIRE_INTEGRATION.md) | Planned / pending approval |
| 4 | [SHUO conversation pipeline integration](phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md) | Planned / pending approval |
| 5 | [Controlled cellular end-to-end validation](phases/PHASE_05_CELLULAR_E2E.md) | Planned / pending approval |
| 6 | [Call control and lifecycle ownership](phases/PHASE_06_CALL_CONTROL_AND_LIFECYCLE.md) | Planned / pending approval |
| 7 | [Resilience and coexistence](phases/PHASE_07_RESILIENCE_AND_COEXISTENCE.md) | Planned / pending approval |
| 8 | [Release and operations](phases/PHASE_08_RELEASE_AND_OPERATIONS.md) | Planned / pending approval |

## Technical validation of sequencing

**PROPOSED:** retain all eight boundaries. Phase 2 separates deterministic adapter/
codec work from Linux process/device variability. Phase 3 validates targeting before
provider integration. Phase 4 must address the existing carrier-specific session,
checkpoint, recording and history assumptions (`CarrierSession`, `run_conversation`)
without copying the browser PCM path. Phase 5 measures the actual cellular path.
Phase 6 validates control capabilities, which cannot be inferred from audio nodes.
Phase 7 validates faults/coexistence before Phase 8 packages a supported release.

One safety clarification is necessary: minimum lifecycle ownership, partial-start
rollback and media stop begin in Phase 2, not Phase 6. Phase 5 requires a tested
manual phone hangup/abort path. Phase 6 remains the full automated call-control and
lifecycle phase. If hardware lacks a safe manual path, Phase 5 is blocked and
Phase 6 control work must be reordered with explicit review. Also, if Phase 3 HFP
streams only exist during calls, obtain separate call authorization for a narrowly
controlled stream test; never treat device-only scope as implicit call permission.
No risky phase is compressed or described as already implemented.

## Per-phase contracts and acceptance

The linked phase documents are part of this roadmap: each owns its goal, included
scope, exclusions, prerequisites, likely files, tests/measurable gate, risks,
rollback, artifacts and approval boundary. Keeping those details there prevents two
independent acceptance checklists from diverging. All future numeric latency,
queue, echo, duration and load budgets remain TBD until reviewed before the gate;
passing a vague “works” test cannot complete a phase.

1. Phase 1 accepts only supplied runtime observations, no code or integrated E2E.
2. Phase 2 accepts deterministic hardware-free directional contracts/conversion.
3. Phase 3 accepts explicit negotiated stream targeting and cleanup evidence.
4. Phase 4 accepts injected pipeline integration and preserved regression behavior.
5. Phase 5 accepts authorized digital cellular duplex, latency/echo and abort evidence.
6. Phase 6 accepts verified control capability and complete lifecycle fault coverage.
7. Phase 7 accepts resilience, concurrency, privacy/security and coexistence evidence.
8. Phase 8 accepts supported operations, release evidence and rehearsed rollback.

## Common advancement and rollback policy

Complete only the currently authorized phase. Record revision, environment,
commands, test identities/signatures, sanitized evidence, limitations and rollback
result using TESTING.md. An unexplained new regression blocks acceptance; historical
failures remain individually identified rather than “same count.” Update the owning
phase, this status table, compatibility, known issues and decisions together.

Every move to a later phase requires explicit task authorization after reviewing
the preceding gate. Phase planning is not approval for code, dependency changes,
device/provider access, phone control, real calls, deployment or commit/push.
Rollback normally means disabling optional Bluetooth, stopping owned resources
and restoring previous routes/config; phase-specific procedures are linked above.
Default carrier and browser operation must survive that rollback.
