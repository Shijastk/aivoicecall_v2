# Bluetooth Phase 6 — Call control and lifecycle ownership

**Status: PLANNED / PENDING APPROVAL.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Future phase scope and gates are PROPOSED pending review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Own answer/hangup/remote-end/cancellation and reconcile telephony state with media state safely.

Validate control capabilities independently; injected control backend; answer/hangup, remote disconnect, duplicate/out-of-order events, cancellation, partial startup and teardown; idempotent bounded cleanup and explicit session/device ownership.

## Explicit exclusions

Unvalidated universal Android control, unattended redial, multi-device release guarantees and unrelated carrier lifecycle redesign.

## Dependencies and prerequisites

Phase 5 accepted; explicit control/device/call authorization. Determine actual available control API and permissions through validated evidence; do not assume BlueZ/PipeWire audio nodes expose answer/hangup.

## Files and boundaries

PROPOSED shuo/bluetooth/ control/lifecycle modules and tests; review types/events, call_status and monitor/history integration. D-Bus backend selection remains TBD.

## Tests and measurable acceptance gate

Mocked lifecycle transition matrix plus separately authorized hardware calls covers local answer/hangup, remote end, cancellation at every startup boundary, duplicate/late events, timeout and process failure. All owned tasks/processes/pipes close within approved deadlines; no unexpected calls, stale audio or double-final history. Record control status separately in compatibility.

## Risks

Control unavailable despite working HFP audio; media and phone lifecycle disagree; repeated actions; missed remote disconnect or orphaned call.

## Rollback

Disable automatic control, use tested manual hangup, close session/media resources and restore prior routes. Retain only explicitly supported manual mode if product review approves; otherwise keep Bluetooth disabled.

## Artifacts and documentation to update

Control capability matrix, lifecycle/state/interface documentation, authorization model, test evidence, operator abort instructions and decisions.

## Approval and stop boundary

Explicit Phase 7 authorization for resilience/load/coexistence and any additional devices/calls.

Stop after the authorized phase, report evidence and unresolved gates. Do not
mark a later phase implemented or start its work from this document alone.
