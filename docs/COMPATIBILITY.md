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
| SHUO integration / release status | Partial evidence; digital E2E and release support untested |
| Quirks/limits | Numeric IDs unstable; laptop acoustic route remained; other codecs not validated |
| Provenance | Task-owner Phase 1 evidence; date/raw artifact/version details not supplied |

Exact node/address fixtures live only in [BLUETOOTH_ARCHITECTURE](BLUETOOTH_ARCHITECTURE.md).
No other phone has a validated row. WSL and other host environments are untested;
being outside the reference environment is not evidence of technical impossibility.

## Platform boundary

Existing SHUO core/carrier Windows/Linux portability remains mandatory. Only the
future isolated, optional PipeWire adapter may be Linux-specific, with OS-specific
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
