# Requirements

These are preservation requirements and **PLANNED** Bluetooth requirements, not
claims that the current application already satisfies every item. Proposed gates
need approval before implementation. Evidence: [current architecture](CURRENT_ARCHITECTURE.md).

## Existing behavior to preserve

- **PRES-1:** retain Vobiz/Twilio origination, inbound routing, webhook/stream/admin
  gates, protocol parsing, call identifiers and playback checkpoints.
- **PRES-2:** retain carrier token streaming, µ-law/8 kHz framing, pacing,
  barge-in, no-audio completion and pure state/event dispatch semantics.
- **PRES-3:** preserve browser/V2 routes, messages, provider routing and behavior;
  separately authorize fixes for existing V2 gaps.
- **PRES-4:** retain per-call configuration snapshot, separate config API, catalog,
  recording toggles, monitoring/history/status semantics, notifications and spool.
- **PRES-5:** preserve default startup without importing/starting Bluetooth code;
  no unrelated provider swaps, architecture rewrite or dependency changes.

- **PRES-6:** rules.md C1/C2 remain mandatory for the current carrier and shared
  SHUO core pipeline. No PCM/L16 route may be added to Vobiz, Twilio or the existing
  shared carrier path; Bluetooth PCM must never leak into carrier interfaces.
- **PRES-7:** retain existing Windows/Linux portability of core/carrier code and
  preserve Windows development and carrier startup unchanged.

## Bluetooth functionality and compatibility

- **BT-1:** explicitly opt into the new transport; discover/select paired device
  and both stream directions using validated profile/role/capability properties.
- **BT-2:** require explicit selection on ambiguity; fail clearly if either stream
  is missing or unsuitable. Never silently choose system default mic/speaker.
- **BT-3:** validate negotiated sample format/rate/channels/profile before transfer.
  Initially accept only the validated mSBC, S16LE/16 kHz/mono contract. Reject
  narrowband or other formats until separately designed and tested.
- **BT-4:** do not use numeric PipeWire IDs as stable configuration. Reference
  device names/addresses/node names are fixtures, not universal constants.
- **BT-5:** S16LE PCM is permitted only inside the isolated Bluetooth boundary
  because the validated HFP/mSBC runtime exposes S16LE 16 kHz mono. Convert it
  to/from the existing SHUO µ-law 8 kHz boundary. This scopes C1/C2 without
  removing or weakening carrier protections; future formats must not force a
  rewrite of the core conversation pipeline.
- **BT-6:** advertise only matrix-validated capabilities; audio and call-control
  support are separate. Other phones/systems are untested, not promised.

- **BT-7:** only the future PipeWire adapter may be Linux-specific, provided it
  remains isolated and optional, is not imported or started by default production
  entrypoints, keeps OS-specific behavior behind an injected adapter boundary,
  and fails clearly without side effects on unsupported platforms. PRES-7 applies.

## Realtime, direction and echo isolation

- **RT-1:** maintain streaming and measure conversion cost, queue residence,
  capture-to-STT, EOT-to-first-token/audio, uplink delivery and mouth-to-ear.
  Bluetooth latency thresholds and percentile/sample-size gates are **TBD**;
  approve measurement definitions and thresholds before phase acceptance.
  Existing carrier frame constants/test gates are not Bluetooth latency targets.
- **RT-2:** bounded per-direction queues, explicit backpressure/overflow handling,
  no blocking process or disk work on the realtime path; observable underruns,
  overruns, drops and stalls. Approve queue limits during Phase 2.
- **AUD-1:** physically and logically separate downlink capture and uplink playback;
  no AI/TTS loopback into STT, no default/acoustic routing as a fallback.
- **AUD-2:** test duplex with distinguishable synthetic signals; inspect channels
  separately. Route isolation does not prove absence of phone/network echo;
  measure and define an approved echo threshold before live-call acceptance.

## Lifecycle and cleanup

- **LIFE-1:** explicit ownership of session, selected device, subprocesses, streams,
  queues and conversion state; idempotent bounded stop and cancellation.
- **LIFE-2:** release resources after partial start, end, remote disconnect,
  provider/process failure, device loss and cancellation; no orphan task/process.
- **LIFE-3:** Phase 5 must have a proven manual abort path and bounded media cleanup
  before any live call. Manual answer/hangup was validated on the reference
  environment (supplied Phase 1 evidence); it does not validate automated SHUO
  integration, lifecycle reconciliation, reconnect or general-device compatibility.
  These remain unimplemented/unverified. Phase 6 is still required for automated
  answer/hangup/event reconciliation.
- **LIFE-4:** reconnect must rediscover/revalidate; no replay of stale audio or
  cross-call data. Do not silently redial or resume an uncertain call.

## Security, privacy and operations

- **SEC-1:** no secrets in documentation/logs/fixtures; no private recordings as
  test inputs. Synthetic or explicitly approved sanitized fixtures only.
- **SEC-2:** explicit authorization for device/provider/live-call exercises and
  call control; constrained process arguments and least necessary device access.
- **SEC-3:** define retention, redaction, recording consent and cleanup policy for
  Bluetooth before release; defaults/retention durations are TBD, not invented.
- **OPS-1:** feature-off default, observable failures with actionable diagnostics,
  supported-environment documentation, compatibility records and tested rollback.
- **OPS-2:** release requires coexistence, load/security review, dependency/package
  reproducibility and operator runbooks; notifications must not expose content.

## Testing and non-goals

- **TEST-1:** hardware-free injected unit/integration tests first; compare carrier
  and V2 regressions by failure identity/signature against an authorized baseline.
- **TEST-2:** separate device-only and real-call tests; retain sanitized provenance,
  commands, formats, timing, cleanup evidence and limitations for each gate.
- **TEST-3:** update affected docs alongside implementation and phase evidence.

**OUT OF SCOPE for this task:** all code/test edits, dependency changes, execution
of application/tests/providers/devices/calls, system changes, commit/push and
Phase 2 implementation. **Product non-goals:** replacement of carriers, universal
Android/Linux compatibility, an itel-only product, acoustic bridging, unvalidated
codec fallback, promotional dialer work, unrelated frontend/provider migration.
## Current owner amendment: supplemental Android cellular test transport — 2026-09-23

The earlier task-specific OUT-OF-SCOPE wording above is historical. The task owner
has now explicitly authorized implementation, test, documentation, commit and
push of an isolated Phase-5 Android ADB synthetic-caller **transmit** harness.

Permanent requirements remain unchanged: the shared SHUO/carrier boundary stays
G.711 mu-law/8 kHz; S16LE/16 kHz/mono exists only at the isolated local device
edge; no raw audio is persisted; manual call control remains in force for this
harness; route selection is explicit/fail-closed; and documentation/tests ship
with the implementation. This amendment does not by itself authorize Phase-6
automatic call control or a caller-heard latency claim.
## Current owner amendment: supplemental Android cellular receive candidate — 2026-09-23

After direct reference-device proof that the itel P683L can expose real cellular
`VOICE_DOWNLINK` audio to Ubuntu through shell-UID ADB capture, the owner
explicitly authorized implementation/test/documentation/merge work for the
isolated receive half of the Phase-5 synthetic-caller harness.

Permanent restrictions are unchanged: shared/core audio remains mono G.711
mu-law/8 kHz; raw audio is not persisted; manual call control remains in force;
the Android capture edge is explicit and fail-closed; local/content-free probe
metrics are not caller-heard latency evidence; and Phase 6 is not authorized by
this amendment.
