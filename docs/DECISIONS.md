# Architecture decision log

Recorded 2026-09-13. These decisions record task-owner direction, accepted
constraints and Phase 2 implementation choices. Historical decisions remain in
[../context.md](../context.md); do not renumber or rewrite that log.

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D01 — Accepted product direction | Task brief; `shuo/carrier/__init__.py::get_carrier` has Vobiz/Twilio | Bluetooth is additive, not replacement | Preserve carrier/browser behavior and defaults | Explicit product scope change approved |
| BT-D02 — Accepted design constraint | Call server owns realtime loop (`server.py`, `conversation.py`); no Bluetooth module implemented | Production server must not import device code by default | Optional isolated startup; process/seam implementation PROPOSED for Phase 4 | Measured isolation and startup portability justify an approved alternative |
| BT-D03 — Accepted runtime constraint | Supplied Phase 1: numeric IDs unstable | Numeric PipeWire IDs are not stable identifiers | Discover validated properties; explicit selector on ambiguity | Runtime identity guarantees demonstrated and design change reviewed |
| BT-D04 — Accepted product constraint | Only supplied reference combination has evidence | itel P40+ is reference hardware, not a hard-coded dependency | General capability matrix; exact identifiers only fixtures | A separately approved product scope change, never convenience |
| BT-D05 — Accepted boundary placement; Phase 2 codec implemented | Flux/TTS/player µ-law/8 kHz versus supplied S16LE/16 kHz | Convert at Bluetooth boundary with separate stateful Python 3.12 `audioop` converters | Preserve core format and directional state; no new dependency; `audioop` removal in Python 3.13 requires separate replacement review | Core audio contract change or Python 3.13+ codec replacement separately approved with carrier/browser regressions |
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
| BT-D14 — Implemented Phase 2 scope | Phase 2 implementation revision `6f4c8c423a0d2741d033439c510fc21a1052a91e` | Hardware-free Bluetooth modules/tests implemented; `telephony.py` remains interfaces/fakes only; no `bluetooth_main.py` added | No live D-Bus access/control, real PipeWire streams, provider traffic or SHUO pipeline integration in Phase 2 | Phase 3 explicit authorization permits live PipeWire/device work |
| BT-D15 — Accepted Phase 2 queue API decision | Realtime queues must be bounded; production latency budget is not yet measured | Require explicit `max_frames` and explicit overflow policy; do not hard-code a production numeric queue budget in Phase 2 | Prevents unbounded growth and silent policy; Phase 3 must measure/approve runtime queue sizing | Phase 3 measurements justify a specific production budget |
| BT-D16 — Accepted Phase 2 codec/runtime limitation | Phase 2 focused tests pass on the project Python 3.12 environment; `audioop` emits a one-sample interpolation startup boundary and is deprecated | Keep stateful `audioop` conversion for current Python 3.12 Phase 2 scope; do not fake-pad per chunk; record Python 3.13+ as unsupported until replacement review | No new dependency now; duration test guards against progressive drift | Python runtime support expands or a replacement codec/resampler is approved |

Detailed rationale for BT-D05/06 is in [BLUETOOTH_ARCHITECTURE](BLUETOOTH_ARCHITECTURE.md).
Phase boundaries/gates are in [ROADMAP](ROADMAP.md); known differences between
legacy instruction wording and current code are in [KNOWN_ISSUES](KNOWN_ISSUES.md).
