# Bluetooth runtime contract and proposed architecture

## Phase 1 evidence ledger

**VERIFIED AT RUNTIME — supplied by the task owner; not reproduced in this task.**
Phase 1 is complete on that basis alone. No application code was implemented.
Raw logs, exact OS/package versions, session date and independent measurements
were not supplied. Do not invent them or use the documentation date as test date.

| Observation | Supplied evidence scope |
|---|---|
| Host | Native Ubuntu, not WSL; MSI GF63 Thin 11SC |
| Phone | itel P40+ — VALIDATED REFERENCE HARDWARE only |
| Role | Phone operated as HFP Audio Gateway |
| Runtime | PipeWire/WirePlumber exposed stable call streams during an active cellular call |
| Codec/audio | Negotiated mSBC; S16LE, 16,000 Hz, mono |
| Targeting | Explicit pw-cat stream targeting was possible; numeric PipeWire IDs were unstable |
| Physical route | Laptop speaker and microphone were still in the route |
| Feasibility | Direct digital bridge technically possible; integrated bridge not demonstrated |
| Dependencies | No new dependency was proven necessary in Phase 1 |
| Telephony interfaces | Reference environment exposed `org.pipewire.Telephony.Call1` and `org.ofono.VoiceCall` |
| Manual call control | Manual D-Bus Answer and manual disconnect/hangup were successfully exercised on the reference environment |

Reference fixture identifiers (not product constants or selection defaults):

```text
Bluetooth address: 00:C7:11:7B:84:21
Caller downlink capture node: bluez_input.00_C7_11_7B_84_21.0
Phone uplink playback node:   bluez_output.00_C7_11_7B_84_21.1
```

These node names support the recorded session only. mSBC/16 kHz is the sole
validated contract, not a promise that subsequent sessions or phones negotiate it.
**Manual answer/hangup capability was validated on the reference environment.**
This corrected evidence is supplied by the task owner, not reproduced here.
Automated SHUO call-control integration, lifecycle reconciliation, reconnect
behavior and general-device compatibility remain unimplemented and unverified.
Phase 6 is still required. The exposed interface names do not establish a
universal backend or method-to-interface mapping for other systems.

## General design — Phase 2 hardware-free boundary implemented; live adapter pending

Keep Bluetooth optional and isolated from default `shuo/server.py` imports.

Phase 2 now implements the hardware-free boundary under `shuo/bluetooth/`:
codec contracts/conversion, target description/selection, bounded queue APIs,
telephony interfaces/fakes and minimum lifecycle ownership. No live PipeWire
capture/playback, live discovery, D-Bus access/control, provider traffic or SHUO
conversation integration is implemented by Phase 2.

`bluetooth_main.py` was intentionally not added in Phase 2. A live opt-in
entrypoint and process model remain later-phase work. Hardware/process/provider
objects must continue to be injected. Phase 2 `telephony.py` remains
interfaces/fakes only: no live D-Bus access or call control.

Define a device descriptor, negotiated audio contract and distinct capture and
playback interfaces. Discovery should inspect validated PipeWire/BlueZ properties,
direction, profile, role and capabilities; the exact property schema must be
recorded and tested in Phase 3. An explicit selector resolves ambiguity. Never
silently route to system defaults, match only a phone model, or persist numeric
object IDs. Re-discover after restarts and validate again before opening streams.

Proposed responsibilities:

| Boundary | Responsibility |
|---|---|
| Discovery/selection | Enumerate candidates; enforce capability/profile/direction predicates; require unambiguous selection |
| Audio contract | Validate format, rate, channels, HFP profile and mSBC codec; reject unsupported negotiation with clear errors |
| Downlink capture | Own capture process/stream and bounded inbound queue; caller audio only |
| Uplink playback | Own playback process/stream and separate bounded outbound queue; generated audio only |
| Codec boundary | Stateful conversion/reframing; separate state for each direction/session; reset on end |
| Session/lifecycle | Own cancellation, startup rollback, failure propagation, stop deadlines and later call control |
| Observation | Report selected capabilities, loss, queue delay and errors without payloads or secrets |

## Codec and platform scope — task-owner clarification

Existing rules.md C1/C2 remain mandatory for the current carrier and shared
SHUO core pipeline. For Bluetooth only, S16LE PCM is permitted inside the isolated
boundary because the validated HFP/mSBC runtime exposes S16LE 16 kHz mono. It must
be converted to/from the existing SHUO µ-law 8 kHz boundary. No PCM/L16 route may
be added to Vobiz, Twilio or the existing shared carrier path; Bluetooth PCM must
never leak into existing carrier interfaces. This narrows the scope of C1/C2
without removing or weakening carrier protections.

Existing core and carrier code retain their Windows/Linux portability requirements.
The future PipeWire adapter may be Linux-specific only as an isolated, optional
component behind an injected adapter boundary. It must not be imported or started
by default production entrypoints; existing Windows development and carrier
startup remain unchanged. Unsupported platforms must fail clearly without side
effects. This is a future adapter constraint, not a claim of implemented support.
## Audio direction contract

```text
Caller downlink → negotiated Bluetooth PCM → inbound boundary conversion
               → SHUO-compatible inbound audio → STT

SHUO-generated outbound audio → outbound boundary conversion
               → negotiated Bluetooth PCM → phone uplink
```

The verified carrier/STT/player contract is µ-law/8 kHz
(`shuo/services/flux.py::FluxService.start`, `player.py::FRAME_BYTES`,
`tts.py::TTSService.start`). **IMPLEMENTED in the Phase 2 hardware-free codec boundary:** downlink S16LE
16 kHz mono → resample 8 kHz → G.711 µ-law; outbound µ-law 8 kHz → decode to
linear PCM → resample 16 kHz → S16LE mono. Twenty milliseconds represents 640
Bluetooth PCM bytes or 160 SHUO µ-law bytes; these are format arithmetic, not a
validated PipeWire chunk size. Handle fragmented reads and sample boundaries.
Do not assume OS/provider chunks align with either frame size.

Phase 2 implements the conversion with Python 3.12 stdlib `audioop`, using
separate stateful converters per direction/session. No new dependency was added.
`audioop` is deprecated and removed in Python 3.13, so Python 3.13+ requires a
separately reviewed replacement.

Queue capacity is not hard-coded by Phase 2: bounded queues require explicit
capacity and overflow policy from the caller. The production numeric queue/latency
budget remains a Phase 3 measurement/approval item. Keep future codec selection
and any replacement dependency at this isolated boundary. Narrowband
HFP and all other formats remain unsupported by the initial design until
explicitly designed/tested; reject them, never reinterpret bytes.

Shared input/output buffers or process pipes could send generated speech back
to STT, producing self-triggered turns. Use separate ownership, conversion state,
queues and sinks; prove no path exists from uplink to STT in mocks, then verify
with distinct test signals on hardware. Digital isolation still requires tests
for acoustic/phone/network echo during a controlled call.

## Integrating without carrier-shaped assumptions

`CarrierSession` currently assumes WebSocket JSON and carrier acknowledgments;
`run_conversation` additionally owns carrier recording and call history behavior.
Do not simply subclass a carrier and invent playedStream acknowledgments. Phase 4
must review an injected transport seam, preserve real carrier completion and
define what local playback completion can actually establish. Retain pure
`process_event` and existing provider streaming. Browser direct PCM output is
not the verified carrier pipeline and is not a shortcut to Bluetooth integration.

Minimum resource ownership/rollback is implemented in Phase 2 for hardware-free resources. Automated telephony
control is Phase 6; any Phase 5 call needs independently confirmed manual phone
control and bounded media stop. Device removal invalidates selection; fail closed
instead of capturing a laptop microphone. See [ROADMAP](ROADMAP.md),
[REQUIREMENTS](REQUIREMENTS.md), [TESTING](TESTING.md) and [COMPATIBILITY](COMPATIBILITY.md).


## Phase 2 implementation evidence

Implementation revision:

```text
6f4c8c423a0d2741d033439c510fc21a1052a91e
```

Phase 2 added only isolated hardware-free Bluetooth modules/tests. Executed
evidence supplied by the task owner:

- Bluetooth-focused suite: 27 passed, 1 warning
- Relevant SHUO regression selection: 99 passed, 2 warnings
- Full root suite: 802 passed, 4 failed, 4 warnings
- The four failure identities match the known pre-existing baseline identities;
  no new failure identity was observed from Phase 2.

These results do not validate real PipeWire I/O, cellular duplex, echo isolation,
device-loss handling or live SHUO integration. Those remain later-phase gates.
