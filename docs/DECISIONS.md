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

## Phase 3 decision addendum — 2026-09-13

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D17 — Implemented Phase 3 explicit targeting | Base revision `97076cd1739e2456843ced239eeebeedcfefa70b`; real active-call discovery selected the reference downlink/uplink by properties and explicit node name | Own `pw-cat` capture/playback processes only with an explicitly validated Bluetooth target; never rely on PipeWire default target fallback | Linux-only adapter remains optional/injected; transient numeric IDs are not persisted | A reviewed PipeWire API replacement preserves equal explicit targeting and fail-closed behavior |
| BT-D18 — Implemented AI-only route isolation | Live graph inspection showed physical `Mic1 -> Bluetooth uplink` and `Bluetooth downlink -> Speaker`; injected speech became clear after unlinking the physical mic route | At Bluetooth AI-session start, snapshot/remove only conflicting physical mic/uplink and downlink/speaker links; restore only session-removed links on stop | Laptop mic/speaker are not globally disabled; unrelated routes remain; monitor/headset mode is deferred | Product explicitly adds monitor/human-takeover routing with separate echo/mic isolation validation |
| BT-D19 — Accepted session-lifetime semantics | A 20-second harness validated isolation/restore; the timer came from the harness, not the session implementation | Production isolation lifetime must equal Bluetooth AI-session lifetime, not a fixed timeout | Future integration starts isolation with the Bluetooth media session and restores on teardown/abort | Lifecycle architecture changes with equivalent cleanup guarantees |
| BT-D20 — Implemented capture shutdown cleanup | First real lifecycle stop hit `ProcessError` while later `pgrep -a pw-cat` was empty; unread capture PIPE stdout could delay asyncio subprocess reaping | Drain capture stdout during shutdown only while retaining direct streaming `read()` during normal operation | No new production capture queue/budget is invented; clean 20-second hardware stop and no orphan process validated | A future process transport removes PIPE reaping behavior or changes capture ownership |

## Phase 4 realtime-latency addendum — 2026-09-14

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D21 — Accepted Phase 4A measurement strategy | Remote Phase 4 base integration exists; owner-supplied live timings show the serial final-EOT→LLM→TTS path is too slow for the sub-500 ms target; Deepgram Flux supports optional EagerEndOfTurn/TurnResumed | Add eager turn detection first as an explicit Bluetooth-only measurement mode, disabled by default. Measure candidate→final lead/resume behavior before speculative LLM/TTS work. Do not start AI early in 4A. | Carrier/browser/default Flux behavior remains unchanged when the flag is absent. Logs contain timing/boolean/character-count metadata, not transcript text. Later speculation must use isolated draft state because committed LLM cancellation can preserve partial history. | Sanitized runtime evidence shows insufficient lead, unacceptable false starts/load, or another measured turn strategy is superior; record a new decision rather than rewriting this one |

## Phase 4B shadow-speculation addendum — 2026-09-14

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D22 — Accepted Phase 4B shadow-only strategy | Threshold 0.3 produced near-zero short-turn lead and 336–490 ms long-turn lead with repeated TurnResumed; Qwen median first token was 455 ms | Implement an opt-in first-token shadow probe only. Speculative text is discarded, never sent to TTS and never written to committed history. Final EOT records readiness and cancels unfinished shadow work; actual response reuse remains Phase 4C. | Produces overlap/cancellation/load evidence without changing normal answer semantics. Adds provider requests/cost only when explicitly enabled. | 4B evidence shows insufficient benefit, provider contention/cost, cancellation leaks/history mutation, or a superior measured strategy |

## Phase 4B live-evidence decision — 2026-09-14

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D23 — Accepted: do not enable Phase 4C yet | Controlled Phase 4B live shadow run observed 10 final turns, 0 ready-before-final, 10 not-ready-by-final and 4 resumed eager candidates. One resumed candidate reached a first token at 500.8ms and was correctly discarded. | Keep Phase 4B shadow-only. Do not reuse speculative output in caller-visible responses yet. Prioritize the measured TTS warm-connection issue and investigate an earlier safe turn/transcript signal before another Phase 4C gate. | Correctness and committed history remain protected; no latency claim is inflated from insufficient evidence. | A later controlled shadow run shows repeatable useful pre-final readiness with acceptable cancellation, provider load and history isolation. |

## TTS idle-policy reversal — 2026-09-14

Owner-supplied controlled live evidence showed successful warm reuse at 10,544,
11,078 and 17,451ms, followed by a caller-visible silent turn after checkout at
19,819ms: ElevenLabs rejected text with its 20-second input inactivity timeout.
The old eight-second cutoff was too aggressive, but unlimited reuse is unsafe.

Decision: retain liveness checks and use a separate `max_idle_age=15.0` with
proactive expiry/refill and independent checkout enforcement. Keep legacy `ttl`
as a fallback for the separately named health-check interval; add no keepalive
text or synthetic speech. The roughly five-second margin and latency benefit
need another controlled live validation. This reverses the initial unlimited-age
offline implementation only; Phase 4C remains deferred.

## TTS startup readiness decision — 2026-09-14

Owner-reported first-turn setup of 4132ms exposed competing initial warm/cold
handshakes. Source confirms `start()` schedules background filling without
awaiting readiness. Keep that API nonblocking; use explicit bounded
`wait_ready()` before Bluetooth Agent creation and let initial `get()` join
warmup for shared callers. Changing every start into a blocking operation would
silently alter existing callers; gating every media frame is unnecessary.
Pool ownership precedes the barrier so abort/failure cleans up deterministically.
This changes startup sequencing only, preserves the 15-second idle policy and
does not implement Phase 4C. Live latency improvement remains unvalidated.

Readiness review correction (2026-09-14): allow the existing preconnect retry
loop to continue until the original readiness timeout. A transient failure must
not immediately abort startup; timeout chains the latest error. Align warm idle
age with the local initialization-send boundary after the handshake, retaining
send/backpressure time. The former pre-handshake timestamp unnecessarily spent
the 15-second budget on connection establishment. The five-second provider
safety margin, cancellation ownership and phase boundaries are unchanged.

Final controlled validation addendum (2026-09-14, task-owner supplied): retain
the 15-second safe idle maximum and readiness barrier. Initial warmth preceded
caller audio forwarding, first-turn warm setup was 0ms rather than the previous
4132ms cold setup, and expiry/replacement occurred at 15.000–15.001s without
over-limit checkout or input timeout. Later provider-account quota exhaustion
was unrelated. This validates connection/setup behavior only, not lower
ElevenLabs synthesis latency. Detailed evidence remains in the
[Phase 4 record](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#final-controlled-tts-warm-pool-validation--2026-09-14);
no phase advancement follows from this narrow result.

## Earlier shadow transcript decision — 2026-09-15

BT-D24 — Task-owner authorized an earlier shadow-only Flux Update experiment
following the 0/10 eager readiness result and separately validated TTS warm pool.
Use repeated identical whole transcripts spanning 200ms (3+ words, <=2000 chars),
not an assumed provider stability bit. Bound early mode to two attempts per turn,
1s between attempts, existing capacity wait and no overlapping requests/closure.
Cancel on mutation/resume and always use normal final-EOT Agent generation.
Keep a separate early opt-in for comparison/rollback. This is an empirical
admission rule requiring controlled live measurement, not Phase 4C approval.
The old global-capacity plan is not implemented: production creates a per-call
gate. Preserve that scope and document it rather than claiming global protection.
See the [Phase 4 experiment](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#earlier-shadow-transcript-experiment--2026-09-15).

## BT-D25 — Phase 4B.1 repeat threshold revision — 2026-09-15

Supersede only BT-D24's 200ms minimum with 100ms, retaining a confirming identical
Update and all attempt/cooldown/invalidation bounds. The diagnostic run includes
a 111.629ms repeat about 156ms before the next eager after resume; 150ms would
still miss it. Other initial repeats follow eager, so this cannot solve every
turn. The same text changed about 118ms later: earlier wasted requests are an
explicit risk. Keep the shared cooldown and existing matching-eager priority;
do not add timer admission or separate cooldown buckets. The current task
authorizes implementation/tests, not a live run or Phase 4C. See the
[option comparison and next controlled test](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#phase-4b1-admission-revision--2026-09-15).

## Bluetooth barge-in investigation decision — 2026-09-15

Preserve lifecycle behavior and speculation thresholds pending a captured normal
interruption failure. The newest matching log shows nine complete normal responses
and zero normal cancellations; its four resumes occur before final EOT. Add
content-free lifecycle diagnostics and offline real Agent/player restart tests,
including slowly closing shadow work. Do not infer Agent restart failure from
shadow `turn_closed` telemetry or long dispatch totals. The
[investigation record](BLUETOOTH_BARGE_IN_INVESTIGATION.md) owns evidence, competing
hypotheses and the next controlled test. No Phase 4C/6 advancement.

## Phase 4C implementation authorization — 2026-09-15

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D26 — Accepted: implement Phase 4C as default-off exact-match prepared-stream reuse | Earlier BT-D23 evidence remains valid: the then-current trigger had insufficient live ready-before-final frequency. The task owner later explicitly authorized implementation while postponing new real-call/provider validation. Automated run `34966008520` passed focused, Bluetooth and full baseline-aware repository gates. | Permit Phase 4C only as an explicit Bluetooth opt-in. Reuse only a first-token-ready provider stream whose final transcript, prompt and committed history still match; otherwise use the ordinary final-EOT path. Keep TTS non-speculative and leave carrier/browser/default startup unchanged. | Code correctness can be exercised without spending provider quota or requiring a phone. No caller-latency benefit is claimed from offline automation. Rollback is omission of the Phase 4C flag. | Controlled real-call evidence demonstrates benefit/regression, or a correctness/cost/provider issue requires changing the promotion contract. |

## BT-D27 — Phase 4D hardening remains default-off pending live evidence — 2026-09-15

Revision `b56a38b132951b322ed5d63059a42f0a7600f829` passed focused, complete Bluetooth and baseline-aware
full-root automation. Keep Phase 4D as explicit Bluetooth controls rather than new
production defaults: bounded incremental TTS phrase grouping, provider-visible
history budgeting that preserves full system/digital-twin facts and canonical
history, content-free provider timing, fail-clean parallel startup, and rules-C5
2/3-frame pre-roll selection. Retain the previously live-validated 15-second TTS
warm-idle/readiness policy and the three-frame pre-roll default. The first full
regression exposed and then verified the fix for an Agent constructor-bypass
compatibility issue.

Decision consequence: repository implementation is ready for controlled real-path
comparison, but there is not enough evidence to choose phrase/history/pre-roll
values, enable parallel startup or prepared reuse by default, or claim lower
caller-heard latency. Rollback is omission of the explicit controls. Phase 4
acceptance and Phase 5 advancement remain separate gates.
