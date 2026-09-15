# Known issues, evidence limits and documentation conflicts

The historical issues below remain outside the Bluetooth Phase 2 implementation
scope unless explicitly noted. Phase 2 added isolated hardware-free Bluetooth
modules/tests but did not authorize unrelated fixes in the existing carrier/V2
code. Existing issues are not permission for unrelated refactoring.

## Recorded historical failures

Historical 775 passed / 4 failed remains preserved task-owner evidence.

A later Phase 2 root-suite execution reported **802 passed / 4 failed / 4 warnings**.
The four failing test identities were the same four identities recorded historically.
See [TESTING](TESTING.md) for the executed commands and current signatures.

| Issue / affected test | Evidence/status | Predates Bluetooth? | Scope / fix authorization |
|---|---|---|---|
| `scripts/test_v2_keys.py::test_shunya_key` | Previously recorded failure; current cause UNKNOWN | Yes, supplied baseline | Document only / no |
| `scripts/test_v2_keys.py::test_azure_key` | Previously recorded failure; current cause UNKNOWN | Yes, supplied baseline | Document only / no |
| `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes` | Previously recorded failure; no current traceback | Yes, supplied baseline | Document only / no |
| `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes` | Previously recorded failure; no current traceback | Yes, supplied baseline | Document only / no |

The latter two assertions forbid config/test-call paths on the call app. Current
`shuo/server.py` includes the V2 router, not the config app. Import/dependency
errors or past code differences cannot be ruled out; do not invent a diagnosis.

## Source findings

| Finding | Evidence and affected module | Predates Bluetooth? | Scope / fix authorization |
|---|---|---|---|
| Carrier format mismatch only logged | `shuo/carrier/vobiz.py::VobizSession._check_media_format` logs mismatch and returns; it does not reject incompatible audio | Yes | Document only / no |
| Generated history is not heard-audio history | `shuo/agent.py::cancel_turn`, `shuo/services/llm.py::_generate` preserve generated text; no alignment-based truncation | Yes | Document only / no |
| Unbounded realtime buffers remain | `run_conversation` and `v2_browser_call` create asyncio.Queue without maxsize; AudioPlayer uses growing bytearray | Yes | Future seam must be bounded; existing refactor not authorized |
| Synchronous trace write | `shuo/tracer.py::Tracer.save` calls mkdir/write_text from conversation teardown; concurrent-call latency impact unmeasured | Yes | Document only / no |
| Clear acknowledgment is not a playback barrier | `run_conversation` handles AudioClearedEvent by voiding completion; no wait-for-cleared gate before next turn | Yes | Document only / no |
| V2 buffers text and has a different audio contract | Shunya/Azure `send` accumulates until `flush`; AgentV2 sends provider chunks directly | Yes | Preserve pending separate review / no |
| V2 browser playback interruption/completion unproven | BrowserSession lacks clear_audio/checkpoint; direct path completes at TTS done and does not consume playback marks | Yes | Document only / no |
| V2 route does not share carrier auth/drain accounting | `shuo/v2/api_v2.py::v2_browser_call` accepts directly, no stream-token gate or `_active_calls` update | Yes | Document only / no |
| V2 silence timeout is event-dependent | `v2_browser_call` checks elapsed silence after `event_queue.get`; no independent timeout task | Yes | Document only / no |
| Cleanup limits | Carrier recording tasks wait with timeout but pending set is not cancelled afterward; V2 reader is cancelled but not awaited | Yes | Document only / no |
| Direct V2 dependency declaration gap | V2 services import aiohttp; `requirements.txt` does not directly declare it; installed/transitive availability UNKNOWN | Yes | Document only / dependency change needs approval |
| Browser input / Shunya PCM contract incomplete | BrowserSession forwards decoded bytes to mulaw/8000 Flux without negotiation; Shunya flush specifies only pcm, fixed 44-byte RIFF stripping | Yes | Document only / no |
| Malayalam speech recognition not established | V2 language route selects Azure TTS but FluxService.start remains flux-general-en | Yes | Document only / no |
| Multiple V2 implementations can drift | api_v2 owns active loop; conversation_v2 contains another; services/tts_router is used while v2/tts_router duplicates it | Yes | Document only / no |


## Bluetooth Phase 2 known limitations

| Finding | Evidence/status | Scope / next owner |
|---|---|---|
| Python 3.12 `audioop` dependency | `shuo/bluetooth/codec.py` uses stdlib `audioop`; focused suite passes, but Python warns that it is deprecated and removed in Python 3.13 | Phase 2 accepted limitation; Python 3.13+ replacement requires separate review/approval |
| Live PipeWire I/O not implemented | Resolved for Phase 3 reference path: real `pw-dump` discovery and explicit `pw-cat` process ownership are implemented and hardware-validated | RESOLVED in Phase 3 reference scope |
| Production queue budget uncalibrated | `BoundedAudioQueue` requires explicit `max_frames` and overflow policy; Phase 3 uses bounded runtime values for reference validation, but the final product budget still needs integrated latency measurement | Phase 4/5 integrated measurement |
| Real digital duplex/echo isolation unverified | Partially resolved: live AI-only route isolation is hardware-validated and physical mic mixing root cause identified; full SHUO cellular E2E/self-audio/echo validation remains pending | Phase 5 for full E2E |
| Broader codec/device compatibility unverified | Only mSBC / S16LE / 16 kHz / mono reference contract is supported by Phase 2 selection rules | Future compatibility qualification |
| Production Bluetooth entrypoint/lifecycle integration not implemented | Phase 3 resources are callable from a validation harness, but default `main.py` does not start them and no fixed-duration test harness is production behavior | Phase 4/6 runtime and lifecycle integration |

## Older documentation versus inspected code

These conflicts are reported, not resolved by changing application code or
weakening `CLAUDE.md`/`rules.md`. Existing restrictions remain intact. The current
user explicitly authorizes Bluetooth design documentation, not a new PCM path in
the carrier implementation; any future implementation must review the boundary.

| Historical statement | Verified source / interpretation |
|---|---|
| Root README/context/phase8 narrative: player never catches up after a late deadline | `AudioPlayer._sleep_until` currently catches up within MAX_LATENESS_SECONDS=0.100 and re-anchors beyond it |
| Blanket µ-law-only and no full-response buffering descriptions | True of intended carrier transport; V2 requests PCM and buffers text. Do not claim the blanket statement describes V2 |
| rules.md turn-detector upsampling/local-model and alternate-provider plans | `FluxService.start` still uses Deepgram mulaw/8000; no in-process Silero/Smart Turn feed here |
| Notifications described as only off-machine call-data flow | Flux sends audio; LLM/TTS send text to external providers. Notification metadata is only one egress path |
| docs/api-plan.md describes unauthenticated calls and proposed `/v1/calls` API | `server._require_admin` gates origination; implemented route inventory is in CURRENT_ARCHITECTURE |
| Older “Phase 1” / “Phase 8” completion statements | Carrier migration/call-management phases, not this Bluetooth roadmap |

These document issues predate this setup; reconciliation is in scope only through
explicit snapshot notes and linked current documentation. No archive sections or
restrictions were removed, and no old runtime/test claims were promoted to current.

## Hardware/product unknowns

The Phase 1 supplied reference lacks versioned runtime artifacts and session date;
its audio availability and manual answer/hangup capability are validated on the
reference environment. The task owner's corrected evidence reports exposed
`org.pipewire.Telephony.Call1` and `org.ofono.VoiceCall` interfaces and successful
manual D-Bus Answer and disconnect/hangup; this was not reproduced here.
Automated SHUO call-control integration, lifecycle reconciliation, reconnect
behavior and general-device compatibility remain unimplemented and unverified;
Phase 6 is still required. Digital E2E and latency/echo also remain unverified. These are roadmap gates, not
current defects with authorized fixes. Discovery property schema for live enumeration, production queue/latency budgets,
integrated manual-abort runbook and release support policy remain TBD. Phase 2
codec implementation is no longer TBD: Python 3.12 `audioop` is used at the
isolated boundary, with Python 3.13+ replacement still unresolved. Do not read live devices to fill gaps during this task.

## Bluetooth Phase 3 remaining limitations

| Finding | Evidence/status | Next owner |
|---|---|---|
| Production lifecycle seam not connected | 20-second manual harness passes, but default application startup does not own the Bluetooth session | Phase 4 for SHUO media integration; Phase 6 for complete call lifecycle |
| Production queue/latency budget still unapproved | `PwCatConfig` has bounded runtime values used for reference validation, but the end-to-end product budget has not been measured/accepted | Phase 4/5 integrated pipeline and E2E latency work |
| Call-end BlueZ disappearance cleanup validated; reconnect still not qualified | Real call-cut teardown now passes cleanly with bounded restore retry and no orphan `pw-cat`; reconnect after a dropped/recreated session is still a separate resilience concern | Phase 7 / resilience |
| EnumFormat capability vs actual negotiation needs review | Current discovery accepts explicit compatible EnumFormat or direct audio props; capability advertisement is not always proof of current negotiated state | Compatibility hardening / broader device qualification |
| Monitor/headset listening mode deferred | AI-only mode intentionally removes local speaker route; no human monitor branch is implemented | Future optional feature after core E2E |
| Python 3.13+ remains unsupported for current codec path | `audioop` warning remains in passing suites | Separate codec replacement review |

## Phase 4 realtime latency findings — 2026-09-14

- Eager lead is turn-shape dependent: several short turns had almost no lead, while longer turns reached 336–490 ms at threshold 0.3.
- TurnResumed is common enough to make cancellation/history isolation load-bearing.
- The TTS pool 8-second TTL is a measured latency source: later turns showed approximately 293–321 ms fresh TTS setup after stale-connection eviction. Keep that fix separate from Phase-4B shadow correctness work.

### Current eager trigger is too late for Phase 4C promotion

The controlled Phase 4B shadow run produced **0 ready-before-final results out
of 10 final turns**. Although earlier Phase 4A measurement showed that some
eager candidates can precede final EOT by hundreds of milliseconds, the actual
shadow Qwen probe did not deliver a reusable first token before final EOT on
the observed final turns.

This means Deepgram `EagerEndOfTurn` at threshold 0.3 is currently useful as a
measurement/cancellation signal but is **not proven sufficient as the sole
speculative trigger** for the latency target.

Do not treat this as a provider failure or as proof that speculation cannot
work. The next investigation should compare a safely earlier transcript/turn
signal while preserving cancellation, history isolation and bounded concurrency.

## Earlier shadow experiment limits — 2026-09-15

- Repeated Flux Update text is an admission heuristic, not guaranteed stability
  or turn completion. The 200ms/3-word/two-attempt rule may spend its budget on
  prefixes or reject useful short turns. Useful live lead remains unknown.
- Older Phase 4 plans describe global speculation capacity, but production
  constructs `AsyncCapacityGate(1)` per call. This experiment preserves the
  existing per-call bound; multi-call global admission is not implemented.
- The provider's first-token probe closes before reporting readiness and retains
  no answer. Positive shadow lead is not full-answer readiness or Phase 4C proof.
  Cancellation/timeout remain cooperative with the provider; late completions
  cannot restore eligibility, and new shadow requests cannot overlap closure.
- See [Phase 4](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#earlier-shadow-transcript-experiment--2026-09-15)
  and [test results](TESTING.md#earlier-shadow-transcript-regression--2026-09-15).

## Phase 4B.1 live admission evidence gap — 2026-09-15

The reviewed `/tmp/bt-phase4b1-shadow.log` has 9 eager-triggered generations and
no interim triggers, but lacks Update cadence, word eligibility, rejection and
early-enabled telemetry. The exact cause cannot be reconstructed. The unchanged
`08_run_bt_shadow.sh` helper omits the early flag; whether it launched this run is
unknown. New content-free diagnostics measure these alternatives without changing
the heuristic. See the [Phase 4B.1 review](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#phase-4b1-admission-diagnosis--2026-09-15).
The REQUIREMENTS document's old task-specific exclusion of code/tests predates
and conflicts with this task's explicit diagnostic/test authorization; permanent
preservation requirements still apply, and the historical wording is retained.

## Phase 4B.1 measured admission window — 2026-09-15

The newer `bt-phase4b1-diagnostics.log` establishes early mode and complete
Update callback delivery (409 receipts/returns/coordinator rows). Repeat spans
125.447ms and 151.807ms were already after eager; lowering the span cannot make
those early. A 111.629ms post-resume repeat precedes the next eager, motivating
the new 100ms minimum, but changes ~118ms later. Useful live lead and added
waste/contention remain unknown. The turn-6 +744ms cooldown rejection remains
intentional. Historical missing telemetry and 200ms results above are preserved;
see the [current rule and evidence](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#phase-4b1-admission-revision--2026-09-15).

## Bluetooth audible barge-in failure not yet localized — 2026-09-15

The newest matching live log has no normal Agent cancellation and no turn boundary
inside a response; all nine final EOTs reach Agent, TTS and dispatch. Its repeated
`turn_closed` telemetry belongs to shadow admission, not the normal state machine.
Resume-only signaling does not interrupt the normal Agent, awaited TTS cleanup can
delay player clearing, and downstream/in-flight playback is not retractable by
local queue clear. These are source observations, not a measured root cause of
the reported audible cut. Diagnostics and real Agent/player regression coverage
were added; see the [evidence and next-test gate](BLUETOOTH_BARGE_IN_INVESTIGATION.md).

## Phase 4C external acceptance pending — 2026-09-15

Phase 4C's default-off prepared-stream reuse has passed repository-level automated
verification, but its live benefit is intentionally unproven. The current task
defers provider/device/real-call exercise until the codebase is clean, and recent
owner evidence also reached ElevenLabs account quota exhaustion. Treat real-call
latency, audio continuity and provider behavior as pending external acceptance,
not as an automated-test failure.
