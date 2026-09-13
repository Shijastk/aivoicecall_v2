# Bluetooth Phase 1 — Hardware/runtime contract

**Status: COMPLETE — supplied Phase 1 runtime evidence only.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Future phase scope and gates are PROPOSED pending review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Identify an active-call Linux Bluetooth audio contract before designing software.

Native Ubuntu reference host/phone; HFP Audio Gateway role; mSBC, S16LE/16 kHz/mono; capture/playback node availability and explicit pw-cat targeting.

## Explicit exclusions

Application code, direct digital SHUO bridge, new dependency claims, automated call control and broad compatibility.

## Dependencies and prerequisites

Task-owner Phase 1 observations, transcribed in BLUETOOTH_ARCHITECTURE. No repeat hardware work authorized by this task.

## Files and boundaries

No application files. BLUETOOTH_ARCHITECTURE.md and COMPATIBILITY.md own the evidence; this phase links to them.

## Tests and measurable acceptance gate

Accepted on the supplied observations only: separate call streams available, explicit targeting possible, numeric IDs unstable. Exact runtime versions/session date/raw logs are UNKNOWN. No runtime command or test was re-executed here.

## Risks

Confusing stream visibility with digital duplex/E2E success; treating one phone as universal compatibility.

## Rollback

No application change to roll back. No hardware route was changed by this task. Restore any future validation-only routing to its recorded prior state under separate authorization.

## Artifacts and documentation to update

Runtime evidence ledger, compatibility reference row, roadmap status and evidence limitations.

## Approval and stop boundary

Phase 2 remains pending explicit authorization for isolated adapter/codec implementation; Phase 1 completion does not authorize it.

Stop after the authorized phase, report evidence and unresolved gates. Do not
mark a later phase implemented or start its work from this document alone.
