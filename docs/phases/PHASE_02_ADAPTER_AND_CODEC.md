# Bluetooth Phase 2 — Isolated adapter and codec

**Status: PLANNED / PENDING APPROVAL.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Future phase scope and gates are PROPOSED pending review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Design and implement hardware-free Bluetooth contracts and directional conversion without disturbing production imports.

Injectable device descriptors and selection model; negotiated audio contract; separate capture/playback interfaces; conversion/reframing state; bounded queues; mock session/process/provider objects; partial-start rollback and idempotent stop.

## Explicit exclusions

Real PipeWire capture/playback, active-call audio, live device discovery, D-Bus control, provider calls and core pipeline integration.

## Dependencies and prerequisites

Phase 1 evidence reviewed; approve format handling, queue bounds/overflow, converter choice and public interfaces. Any new dependency requires explicit approval.

## Files and boundaries

PROPOSED new shuo/bluetooth/ contracts, adapter and codec modules; PROPOSED tests/test_bluetooth_adapter.py and tests/test_bluetooth_codec.py. Reference shuo/carrier/base.py, shuo/types.py and services/player.py/flux.py/tts.py without changing their contracts.

## Tests and measurable acceptance gate

Hardware-free known vectors and synthetic signals verify endian/rate/channel validation, duration/sample counts, fragmented reads, per-direction resampler continuity, loss policy, cross-session isolation and bounded cancellation. Assert unsupported formats rejected, no outbound/uplink audio reaches STT, no default device fallback and no import-time hardware/provider access. Approve numeric budgets/tolerances before accepting. Focused tests then authorized existing regression; compare identities/signatures.

## Risks

Lossy µ-law conversion mistaken for byte-exact PCM round-trip; stateless resampling artifacts; queues masking overload; optional imports changing carrier startup.

## Rollback

Disable/remove only newly introduced optional adapter wiring; preserve carrier and V2 defaults. No system state to restore. Verify existing import/startup contract with mocks.

## Artifacts and documentation to update

New interface/format docs, synthetic fixtures and test commands, decision on codec/queues, known limitations and phase evidence.

## Approval and stop boundary

Explicit Phase 3 authorization must specify permitted device/runtime access; no real streams follow automatically from unit tests.

Stop after the authorized phase, report evidence and unresolved gates. Do not
mark a later phase implemented or start its work from this document alone.
