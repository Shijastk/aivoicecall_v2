# Architecture decision log

Recorded 2026-09-13. These decisions record task-owner direction and proposed
engineering choices, not Phase 2 approval. Historical decisions remain in
[../context.md](../context.md); do not renumber or rewrite that log.

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D01 — Accepted product direction | Task brief; `shuo/carrier/__init__.py::get_carrier` has Vobiz/Twilio | Bluetooth is additive, not replacement | Preserve carrier/browser behavior and defaults | Explicit product scope change approved |
| BT-D02 — Accepted design constraint | Call server owns realtime loop (`server.py`, `conversation.py`); no Bluetooth module implemented | Production server must not import device code by default | Optional isolated startup; process/seam implementation PROPOSED for Phase 4 | Measured isolation and startup portability justify an approved alternative |
| BT-D03 — Accepted runtime constraint | Supplied Phase 1: numeric IDs unstable | Numeric PipeWire IDs are not stable identifiers | Discover validated properties; explicit selector on ambiguity | Runtime identity guarantees demonstrated and design change reviewed |
| BT-D04 — Accepted product constraint | Only supplied reference combination has evidence | itel P40+ is reference hardware, not a hard-coded dependency | General capability matrix; exact identifiers only fixtures | A separately approved product scope change, never convenience |
| BT-D05 — Accepted boundary placement; codec implementation PROPOSED | Flux/TTS/player µ-law/8 kHz versus supplied S16LE/16 kHz | Convert at Bluetooth boundary | Preserve core format; independent directional state; codec library TBD | Core audio contract change separately approved with carrier/browser regressions |
| BT-D06 — Accepted safety constraint | `state.py::process_event` drops non-inbound tracks; target duplex topology | Isolate downlink and uplink structurally | Distinct queues/process streams/sinks; no TTS-to-STT route | Replacement proves equal isolation with explicit review |
| BT-D07 — Accepted execution constraint | Task permissions distinguish mocks/devices/calls | Device and real-call work is phase-gated | Approval per scope; manual abort before first E2E; stop at phase boundary | Only an explicit task authorization changes executable scope |
| BT-D08 — Accepted evidence limit | Supplied mSBC/16 kHz session only | No universal compatibility inference | Reject unvalidated formats; qualify each matrix capability independently | New reproducible compatibility/codec evidence and approval |
| BT-D09 — PROPOSED sequencing | Existing conversation assumes carrier playback ack/recording; no automated SHUO Bluetooth control | Keep phases 1–8, add minimum cleanup early and manual exit before Phase 5 | Full automated call lifecycle remains Phase 6; no unsafe dependency on unfinished control | Phase 3 shows safe manual control impossible; reorder with documented approval |
| BT-D10 — Accepted documentation policy | Legacy plans conflict with current code and historical counts lack provenance | Separate source snapshot, supplied runtime evidence, requirements and plans | Keep archives; link current owners; report conflicts instead of retroactive compliance edits | Better evidence changes facts, logged without erasing history |

## Task-owner clarification addendum

These clarify the existing decisions without removing restrictions or approving
implementation. Phase 1 evidence is supplied, not re-executed in this task.

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D11 — Accepted evidence correction | Reference environment exposed `org.pipewire.Telephony.Call1` and `org.ofono.VoiceCall`; manual D-Bus Answer and disconnect/hangup succeeded | Manual answer/hangup capability was validated on the reference environment | Automated SHUO call-control integration, lifecycle reconciliation, reconnect and general-device compatibility remain unimplemented/unverified; Phase 6 still required | Separately authorized integration and compatibility evidence passes the relevant gates |
| BT-D12 — Accepted scope clarification of BT-D05 | rules.md C1/C2 protect carrier/core µ-law; reference HFP/mSBC exposes S16LE 16 kHz mono | Permit PCM only inside the isolated Bluetooth boundary, converted to/from SHUO µ-law 8 kHz | C1/C2 remain mandatory for current carrier/shared core; no PCM/L16 route in Vobiz/Twilio/shared carrier path and no PCM leakage into carrier interfaces; protections not removed or weakened | A separately approved contract change with preservation evidence |
| BT-D13 — Accepted platform clarification of BT-D02 | Existing Windows/Linux portability requirements; reference runtime is native Ubuntu/PipeWire | Only the future injected, isolated, optional PipeWire adapter may be Linux-specific | No default production import/start; unchanged Windows development/carrier startup; unsupported platforms fail clearly without side effects | An explicitly approved adapter/platform design preserves those guarantees |
| BT-D14 — Candidate scope, pending Phase 2 approval | Task-owner candidate file list in Phase 2 document | Inspect source before finalizing candidate files; Phase 2 telephony.py is interfaces/fakes only | No live D-Bus access/control, real PipeWire streams or implementation in this correction task | Explicit phase approval permits its scoped work |

Detailed rationale for BT-D05/06 is in [BLUETOOTH_ARCHITECTURE](BLUETOOTH_ARCHITECTURE.md).
Phase boundaries/gates are in [ROADMAP](ROADMAP.md); known differences between
legacy instruction wording and current code are in [KNOWN_ISSUES](KNOWN_ISSUES.md).
