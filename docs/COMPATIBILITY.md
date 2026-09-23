# Capability-based compatibility

**PLANNED product policy:** qualify a phone/host/runtime combination by actual
capabilities. A phone model alone is neither sufficient nor required for support.
No universal Android/Linux support is promised.

## Initial matrix

| Field | Only recorded reference combination |
|---|---|
| Hardware label | **VALIDATED REFERENCE HARDWARE** |
| Computer / phone | MSI GF63 Thin 11SC / itel P40+ |
| OS | Native Ubuntu (not WSL); exact release/kernel UNKNOWN |
| Android version/build | UNKNOWN |
| PipeWire / WirePlumber / BlueZ versions | UNKNOWN / UNKNOWN / UNKNOWN |
| Bluetooth controller/firmware | UNKNOWN |
| Profile / phone role | HFP / Audio Gateway |
| Codec | mSBC in supplied active-call session |
| Format / sample rate / channels | S16LE / 16,000 Hz / mono |
| Capture discovery result | Explicit downlink target found; fixture name in BLUETOOTH_ARCHITECTURE; automated discovery untested |
| Playback discovery result | Explicit uplink target found; fixture name in BLUETOOTH_ARCHITECTURE; automated discovery untested |
| Exposed telephony interfaces | `org.pipewire.Telephony.Call1` and `org.ofono.VoiceCall` on the reference environment |
| Manual call-control support | Validated: manual D-Bus Answer and manual disconnect/hangup successfully exercised; task-owner evidence, not reproduced here |
| Automated SHUO call control | Unimplemented/unverified; lifecycle reconciliation, reconnect behavior and general-device compatibility also unimplemented/unverified; Phase 6 still required |
| Audio status | Validated runtime stream availability/explicit targeting only |
| Bluetooth software status | Phase 2 hardware-free codec/selection/lifecycle contracts implemented and unit-tested; real PipeWire capture/playback remains Phase 3 and digital E2E/release support remain untested |
| Quirks/limits | Numeric IDs unstable; laptop acoustic route remained; other codecs not validated |
| Provenance | Task-owner Phase 1 evidence; date/raw artifact/version details not supplied |

Exact node/address fixtures live only in [BLUETOOTH_ARCHITECTURE](BLUETOOTH_ARCHITECTURE.md).
No other phone has a validated row. WSL and other host environments are untested;
being outside the reference environment is not evidence of technical impossibility.

## Platform boundary

Existing SHUO core/carrier Windows/Linux portability remains mandatory. Only the
live isolated, optional PipeWire adapter implemented in a later phase may be Linux-specific, with OS-specific
behavior behind an injected boundary. Default production entrypoints must neither
import nor start it; Windows development and carrier startup remain unchanged.
Unsupported platforms must fail clearly without side effects. This permission
does not validate any additional Linux environment or promise Windows Bluetooth
support. Manual answer/hangup evidence applies only to the reference combination.
## Status rules

- **Validated:** named capability demonstrated with reproducible evidence on the
  specific combination; label the capability (audio, call control, or E2E).
- **Partial:** some prerequisites shown; required capabilities remain unknown.
- **Untested:** no relevant evidence. Do not advertise as supported.
- **Unsupported:** fails an explicit supported-contract requirement; record reason.
  Non-mSBC/non-16-kHz formats are outside the initial software contract, pending
  separate design and testing, rather than a universal phone incompatibility.

## Adding a combination — PLANNED procedure

1. Obtain authorization for device access; record non-secret versions, controller,
   OS, phone build, profile/role and sanitized selected stream properties.
2. Validate capture/playback direction, unique selection, negotiated format and
   explicit targeting. Show missing/ambiguous devices fail without default routing.
3. Run authorized device-only synthetic duplex/cleanup tests (Phase 3), then
   separately authorized E2E calls (Phase 5). Record latency, integrity/echo and
   device-loss results; leave untested columns UNKNOWN.
4. Validate answer, hangup and remote end independently in Phase 6; test reconnect
   and coexistence in Phase 7. Do not infer call control from stream availability.
5. Attach sanitized commands/results, session date, repository revision and scope;
   add a matrix row and update audio contracts, known issues and phase evidence.
   Only promote a capability after its gate passes and review approves it.


## Phase 2 software evidence

Phase 2 implementation revision:

```text
6f4c8c423a0d2741d033439c510fc21a1052a91e
```

Hardware-free test evidence supplied by the task owner:

- Bluetooth-focused suite: 27 passed, 1 warning
- Relevant SHUO regression selection: 99 passed, 2 warnings
- Full root suite: 802 passed, 4 failed, 4 warnings
- No new failure identity was observed from Phase 2.

This software evidence does **not** promote any additional phone/host combination
to validated hardware support. Real PipeWire capture/playback, broader device
compatibility, digital E2E and release support remain unvalidated.

## Phase 4D compatibility note — 2026-09-15

The Phase 4D implementation at `b56a38b132951b322ed5d63059a42f0a7600f829` does not expand the supported
hardware matrix. All new tuning is attached to the explicit Bluetooth runner and
is default-off; carrier/browser/default production startup remains unchanged.
Player pre-roll is constrained to the already permitted two/three-frame range,
with three retained as default. No claim is made that another phone, PipeWire
version, codec/profile, provider region or operating system supports the reference
Bluetooth path until independently qualified.
## Supplemental Android ADB synthetic-caller TX — 2026-09-23

Reference-only compatibility evidence now includes an itel P683L running Android
13/API 33 as a **caller-side synthetic-audio injector** over USB ADB. During an
active manually controlled cellular call, shell UID 2000 could enumerate a unique
`TYPE_TELEPHONY` output and an `AudioTrack` explicitly routed to it returned
actual `ROUTED_TYPE=18`. Generated tone, Pocket speech and live ADB-stdin PCM
were heard at the remote Galaxy A10.

This does not qualify arbitrary Android phones. Required capabilities remain
runtime gates: connected authorized ADB target, API >=23, `app_process`, shell
privapp routing grants, active `MODE_IN_CALL`, unique telephony output and actual
telephony routing after playback starts. Any failure is fail-closed. USB UAC
bidirectional audio is not part of this supported reference path.

Receive/downlink capture on the itel remains unvalidated and must not be inferred
from the transmit result.
## itel P683L cellular downlink capture evidence — 2026-09-23

On the reference itel P683L / Android 13, upstream scrcpy 4.1 successfully
started `voice-call-downlink` capture while Android reported `MODE_IN_CALL`.
The remote Galaxy A10 caller was heard clearly on Ubuntu. With Ubuntu playback
moved to headphones, the reported result was **clear, no echo**.

This validates the phone/runtime capability needed for caller-side receive over
ADB. The repository-owned `TelephonyRxBridge` remains a separate compatibility
gate until its own bounded probe passes on the same reference call. No other
Android model is qualified from this result.
## itel P683L SHUO receive qualification — 2026-09-23

The repository-owned `TelephonyRxBridge` has now passed on the same reference
itel P683L / Android 13 runtime previously qualified with scrcpy.

Bounded probe evidence:

- PCM bytes: 1,519,616;
- observed PCM duration: 7.915 s in an 8-second probe;
- chunks: 371;
- peak RMS: 4300;
- average RMS: 1541.0;
- in-memory SHUO mu-law bytes: 63,318;
- raw-audio persistence: none.

This qualifies the reference phone/runtime for the SHUO-owned caller-side
downlink bridge. It does not qualify arbitrary Android devices.
## itel P683L SHUO receive qualification — 2026-09-23

The repository-owned `TelephonyRxBridge` has passed on the same reference itel
P683L / Android 13 runtime previously qualified with scrcpy.

Bounded probe evidence: 1,519,616 PCM bytes, 7.915 s observed PCM duration,
371 chunks, peak RMS 4300, average RMS 1541.0, and 63,318 in-memory SHUO
mu-law bytes. No raw audio was persisted.

This qualifies the reference phone/runtime for the SHUO-owned caller-side
downlink bridge. It does not qualify arbitrary Android devices.
