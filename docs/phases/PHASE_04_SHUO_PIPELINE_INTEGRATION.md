# Bluetooth Phase 4 — SHUO conversation pipeline integration

**Status: PLANNED / PENDING APPROVAL.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Future phase scope and gates are PROPOSED pending review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Connect the optional Bluetooth adapter to existing conversation services with unchanged carrier/browser behavior.

Review minimal injected transport seam; optional entrypoint; pure event/state reuse; carrier-format audio conversions; barge-in, completion and failure mapping; monitor/history/recording integration policy; bounded media lifecycle.

## Explicit exclusions

Controlled cellular E2E campaign, implicit provider contacts, automatic answer/hangup, carrier refactoring unrelated to the seam and browser provider changes.

## Dependencies and prerequisites

Phase 3 targeting/cleanup evidence accepted; public seam approved; identify which local completion events are real observations versus dispatch-only signals. Providers stay mocked unless separately authorized.

## Files and boundaries

PROPOSED Bluetooth runner/session integration; likely review shuo/conversation.py::run_conversation, Agent, types/state, player/Flux/TTS, runtime_config, recording, monitor/history. Preserve shuo/server.py and v2 route contracts; any touched core file must be justified.

## Tests and measurable acceptance gate

Injected integration demonstrates downlink-only STT, streaming token/audio delivery, interruption and stale-completion rejection, zero-audio completion, start failure and deterministic teardown. Preserve config snapshots and record/history IDs without falsely claiming handset playback acknowledgment. Focused transport/state/player/integration tests then authorized full regression; feature-off has no device import/access.

## Risks

CarrierSession assumes JSON sockets and checkpoint acknowledgment; conversation owns carrier recording; accidental V2 PCM reuse; widening production failure domain.

## Rollback

Disable optional entrypoint/feature and restore the reviewed seam change if needed; carrier/browser defaults continue. Stop owned streams and drain only owned resources; validate regression identity/signature baseline.

## Artifacts and documentation to update

Current architecture/interface changes, requirements traceability, integration test inventory, recording/completion decision and phase evidence.

## Approval and stop boundary

Explicit Phase 5 authorization must name controlled calls/providers/devices, approved measurements and manual emergency exit.

Stop after the authorized phase, report evidence and unresolved gates. Do not
mark a later phase implemented or start its work from this document alone.
