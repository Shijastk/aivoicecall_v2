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

**Candidate new files, subject to Phase 2 source inspection; not implemented or
approved by this document correction:**

```text
bluetooth_main.py
shuo/bluetooth/__init__.py
shuo/bluetooth/codec.py
shuo/bluetooth/pipewire.py
shuo/bluetooth/transport.py
shuo/bluetooth/telephony.py
shuo/bluetooth/runtime.py
tests/test_bluetooth_codec.py
tests/test_bluetooth_pipewire.py
tests/test_bluetooth_transport.py
tests/test_bluetooth_telephony.py
```

Phase 2 `telephony.py` contains interfaces/fakes only: no live D-Bus access or
call control. `pipewire.py` scope remains hardware-free contracts and injected
process/discovery behavior; real stream integration belongs to Phase 3. Candidate
entrypoint/runtime names do not authorize starting a service or connecting the
live SHUO pipeline. Reference `shuo/carrier/base.py`, `shuo/types.py` and
`shuo/services/player.py`, `flux.py`, `tts.py` without changing their contracts.

S16LE 16 kHz mono is allowed only inside the isolated Bluetooth boundary, with
conversion to/from SHUO µ-law 8 kHz. rules.md C1/C2 remain mandatory for the
carrier/shared core; no PCM/L16 carrier route or PCM leakage is permitted.
The future PipeWire adapter may be Linux-specific only behind an injected,
isolated, optional boundary: no default production import/start, no change to
Windows development/carrier startup, and clear side-effect-free rejection on
unsupported platforms. Core/carrier Windows/Linux portability remains mandatory.

## Tests and measurable acceptance gate

Hardware-free known vectors and synthetic signals verify endian/rate/channel validation, duration/sample counts, fragmented reads, per-direction resampler continuity, loss policy, cross-session isolation and bounded cancellation. Assert unsupported formats rejected, no outbound/uplink audio reaches STT, no default device fallback and no import-time hardware/provider access. Planned checks must also cover unsupported-platform rejection without side effects, preserved Windows/core/carrier startup, PCM exclusion from carrier interfaces, and telephony fakes with no live D-Bus access. Approve numeric budgets/tolerances before accepting. Focused tests then authorized existing regression; compare identities/signatures.

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
