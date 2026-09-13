# Bluetooth Phase 3 — PipeWire capture/playback integration

**Status: PLANNED / PENDING APPROVAL.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Future phase scope and gates are PROPOSED pending review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Prove explicit digital capture and playback with controlled signals before attaching the SHUO conversation pipeline.

Injected subprocess runner implementation; property-based discovery and explicit selector; negotiated-format checks; independent pw-cat capture/playback or reviewed equivalent; fragmented I/O, exit/error handling, bounded stop and route restoration.

## Explicit exclusions

Live SHUO STT/LLM/TTS pipeline, unapproved real calls, automated telephony control and undocumented default routing.

## Dependencies and prerequisites

Phase 2 accepted. Explicit device authorization; versioned environment, known original routes and cleanup procedure. If HFP streams need an active call, obtain separate controlled-call authorization or record that gate blocked; device-only approval is insufficient.

## Files and boundaries

PROPOSED shuo/bluetooth/ discovery and process/stream modules, injected integration tests and opt-in device test harness. No default production entrypoint may import or start the adapter. Linux-specific
PipeWire behavior is permitted only inside the isolated, optional, injected
adapter boundary; core/carrier Windows/Linux portability and Windows development/
carrier startup remain unchanged. Unsupported platforms must fail clearly without
side effects.

## Tests and measurable acceptance gate

Record selected properties/role/profile/format and versions. Feed distinct synthetic uplink/downlink signals; prove targeting, no default-route fallback and correct frame duration/byte order. Exercise missing/ambiguous nodes, process exit, device disappearance and repeated start/stop; no owned processes/handles or stale queues remain. Also verify the injected platform guard rejects unsupported platforms without side effects and preserves default Windows/core/carrier startup. Approve duration and timing budgets before acceptance.

## Risks

Profile visible only during a call; node recreation; ambiguous devices; default fallback; subprocess pipe deadlock or route leakage.

## Rollback

Stop owned capture/playback processes, close pipes, discard queued audio and restore recorded prior links/routes. Confirm no residual device stream before ending authorized session.

## Artifacts and documentation to update

Versioned capability matrix, sanitized property fixtures, device validation procedure/results, process cleanup evidence and updated architecture/decisions.

## Approval and stop boundary

Explicit Phase 4 approval for pipeline seam changes; device approval is not provider/live-call approval.

Stop after the authorized phase, report evidence and unresolved gates. Do not
mark a later phase implemented or start its work from this document alone.
