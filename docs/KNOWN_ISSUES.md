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

## Phase 4D live-evidence limits — 2026-09-15

The Phase 4D controls at `b56a38b132951b322ed5d63059a42f0a7600f829` are repository-verified but are not
live-qualified. In particular:

- `tts_phrase_chars` has no selected production value; batching too aggressively
  can increase first-speech delay and batching too little may not improve TTS
  synthesis behavior;
- `llm_history_max_chars` has no default budget; any production value must preserve
  digital-twin conversational fidelity even though system facts/rules and
  canonical history are structurally retained;
- Groq usage/timing fields are optional provider telemetry and do not by themselves
  identify client/network versus provider causes unless compared with client spans;
- `parallel_startup` is default-off pending live startup comparison;
- two-frame playback pre-roll is default-off pending Bluetooth audio quality/XRUN
  evidence; the current three-frame default remains;
- Phase 4C prepared-stream reuse and these Phase 4D controls have not yet been
  jointly validated on a controlled real call.

No sub-500ms or other caller-heard latency claim follows from the offline pass.
## Android ADB synthetic-caller TX limits — 2026-09-23

- The validated itel USB ADB connection was intermittently absent from
  `adb devices`; the harness therefore requires an explicit connected
  `state=device` target and never silently selects an unavailable/unauthorized
  device.
- An early Telephony-Tx helper consumed all PCM and printed `STREAM_DONE` but
  could remain blocked in Android `AudioTrack.stop()/release()`. The dedicated
  one-shot helper now exits after bounded drain and the host retains bounded
  terminate/kill fallback. This is reference-device behavior, not a universal
  Android claim.
- Caller-phone cellular downlink capture back to Ubuntu is not yet
  reference-runtime validated. Therefore the new path proves synthetic caller
  **transmit** only; fully automated listen/decide/respond caller simulation
  remains a later evidence gate.
- The local Pocket timing markers do not establish caller-heard or mouth-to-ear
  latency.
## Android ADB synthetic-caller RX candidate — 2026-09-23

- The reference itel downlink capability is proven with upstream scrcpy 4.1,
  but the SHUO-owned `TelephonyRxBridge` still requires its own live reference
  probe before merge/qualification.
- The candidate deliberately captures the same proven PCM16/48 kHz/stereo shape
  as scrcpy, then converts in memory to SHUO mu-law/8 kHz. Do not assume a
  lower-rate Android capture configuration is supported without evidence.
- Laptop-speaker monitoring can create an acoustic echo path back into the
  handset microphone. The reference verification used headphones and then
  reported clear audio with no echo. This is a test-monitoring issue, not a
  reason to enable any acoustic production route.
- No receive-side latency value has been measured.
### RX doctor false negative on RECORD_AUDIO — corrected 2026-09-23

The first repository-owned RX doctor treated both `CAPTURE_AUDIO_OUTPUT` and
`RECORD_AUDIO` as privapp-allowlist permissions. On the reference itel this
caused a false-negative stop at `RECORD_AUDIO`.

AOSP permission definitions show the distinction: `RECORD_AUDIO` is
dangerous/runtime, whereas `CAPTURE_AUDIO_OUTPUT` is privileged. The preflight
now verifies the privileged capture permission in the privapp allowlist and
separately requires a concrete package grant for `RECORD_AUDIO`. The live
helper remains unqualified until the corrected doctor and bounded probe pass.
### Android RX live gate closed on reference device — 2026-09-23

The earlier issue "SHUO-owned TelephonyRxBridge still requires its own live
reference probe" is now closed for the itel P683L reference runtime. The bounded
probe sustained 7.915 s of PCM, produced non-zero content-free energy metrics
and converted into the SHUO mu-law boundary without raw-audio persistence.

Remaining limitations are narrower:

- other Android devices remain unqualified;
- no caller-heard RX latency has been measured;
- a full closed-loop synthetic-human controller using RX + TX together is not
  yet promoted by this evidence alone;
- call establishment and hangup remain manual.
### Android RX live gate closed on reference device — 2026-09-23

The earlier issue that the SHUO-owned `TelephonyRxBridge` still required its
own live reference probe is now closed for the itel P683L runtime. The bounded
probe sustained 7.915 s of PCM, produced non-zero content-free energy metrics
and converted into the SHUO mu-law boundary without raw-audio persistence.

Remaining limits: other Android devices are unqualified; no caller-heard RX
latency has been measured; a complete closed-loop synthetic-human controller is
a separate gate; and call establishment/hangup remain manual.
## Closed-loop Android cellular controller live gate pending — 2026-09-23

TX and RX are independently qualified, but simultaneous use by the new
closed-loop controller is not inferred from those independent tests.

The controller remains unqualified until one controlled reference run proves:

- TX and RX helpers remain active together on the itel P683L;
- observer Deepgram Flux receives real SHUO downlink turns;
- the 650 ms prepared-audio thinking pause does not trigger a premature SHUO
  response;
- two interruption stimuli are sent while a remote response is still active and
  each reaches a replacement response;
- the deterministic continuity checks complete;
- no raw audio/transcript artifact is written;
- manual hangup/cleanup remains bounded.

No latency threshold is invented for this gate.
### Galaxy A10 connected but compatible SCO downlink absent in first closed-loop attempt — 2026-09-23

During the first combined live attempt, `bluetoothctl` reported the Galaxy A10
paired, trusted and connected, but fresh PipeWire discovery returned no
compatible downlink target for its address. The SHUO runner correctly failed
closed before opening any default source.

Root cause is not yet established. Possibilities such as an HFP SCO node not
being active or a live property/codec/format mismatch must be distinguished from
the actual `pw-dump` graph; they are not treated as conclusions. The validated
selector contract remains unchanged pending that evidence.
## Phase 5 closed-loop continuity failures and latency evidence — 2026-09-24

A real two-device itel P683L <-> Galaxy A10/SHUO cellular run completed the
deterministic controller with 10 observed remote response EOTs, but the overall
result remained FAIL because `barge_in_2_continuity_codeword` and
`late_session_continuity_fruit` failed. The first barge-in fruit continuity
check passed. Do not attribute the two failures to STT, LLM history, cancellation,
or transport until new evidence localizes the cause.

The earlier controller exposed only interruption-start-to-observer-start and
scenario-duration values; those are not caller-heard response latency. A new
per-response host-correlated measurement seam has been added on the feature
branch. It measures caller paced-TX completion to the itel downlink observer
StartOfTurn for every deterministic response and keeps
`CALLER_HEARD_LATENCY=NOT_MEASURED`. The new seam is not reference-runtime
qualified yet.

## Closed-loop response overlap observed in latency run — 2026-09-26

One of ten host-correlated latency samples was negative
(`-6238.519 ms`) during the first barge-in setup. This means the downlink
observer registered response speech before the synthetic caller's paced TX
boundary completed. The same run reported
`barge_in_1_interrupt_sent_while_remote_speaking=FAIL`.

This evidence is compatible with more than one cause, including premature SHUO
turn finalization, observer-side classification of unintended downlink audio, or
another turn-ordering problem. Do not choose a root cause without additional
content-free timing/audio-path evidence.

Negative samples are now retained and explicitly classified as
`OVERLAP_RESPONSE_STARTED_BEFORE_TX_END` but excluded from response-latency
summary statistics. This is a reporting correction only; it does not hide the
overlap event or convert the supplemental run to PASS.

## First-request LLM cold path and separate Flux fragmentation — 2026-09-26

Reference-path content-free logs show first-turn Agent-start to TTS-first-audio
at about 984 ms versus roughly 339-421 ms on several later turns. The first turn
contained about 247 ms of post-first-token TTS time, indicating that most of the
extra local delay occurred before the first LLM token. Exact provider-internal
root cause remains unknown.

A default-off Bluetooth `--llm-warmup` candidate now primes the same LLM client
with static non-conversation input before caller processing. Live improvement is
not yet proven. Do not describe this as a confirmed DNS/TLS or model-loading bug.

A separate fresh silent closed-loop run still produced 11 observed response EOTs
for 10 expected responses and three response-start-before-caller-TX-end samples.
The SHUO-side log itself reached Agent turn 11 during the scenario, with rapid
Flux turn/cancel sequences, so this cannot be dismissed as only an itel observer
counting issue. The exact cause—premature Flux EOT, speech fragmentation/merge,
transport behavior or another turn-ordering effect—remains unresolved. LLM
warmup must not hide or reclassify this correctness problem.

## Closed-loop call-connected startup delay was harness preparation — 2026-09-26

The owner observed that the first synthetic question began about 5-10 seconds
after call connection. Source review found the legacy closed-loop controller did
all 11 Pocket pre-syntheses plus Android bridge compile/push after confirming the
call was already active. This delay is outside `scenario_duration_ms` and is
not evidence of slow SHUO conversational response.

An explicit `--prepare-before-call` path moves that work before call
establishment and waits for manual operator confirmation before strict active-call
preflight and scenario start. Live reduction of call-connected-to-seed delay is
not yet measured.
## Malayalam STT scope clarification — 2026-10-06

The older statement that Malayalam speech recognition is not established remains
true for the existing carrier/V2 `FluxService`, which still uses
`flux-general-en`.

A narrower Bluetooth-only exception now exists behind explicit
`speech_provider=local-malayalam`. On the reference development machine, a
controlled Malayalam utterance passed both direct IndicConformer recognition and
the complete SHUO local worker path with matching transcript and one start/end
turn pair.

This does not resolve:

- carrier/V2 Malayalam STT;
- live cellular Malayalam accuracy;
- code-mixed Malayalam/English accuracy;
- broad speaker/device/noise robustness;
- caller-heard latency;
- the previously observed real-call turn fragmentation problem.

Treat the Bluetooth local path as reference-offline validated and live-cellular
pending, not as a global Malayalam-STT resolution.\n## Live local-Malayalam segmentation and code-mixed accuracy — 2026-10-06

The first real Bluetooth/cellular run with the opt-in local Malayalam provider
produced transcript-bearing local trace evidence showing both useful full
Malayalam recognition and repeated premature conversational segmentation.

Representative local-only trace results included a complete turn such as
`നാളെ ഒരു മീറ്റിങ് ഉണ്ട് അത് വൈകുന്നേരത്തേക്ക് മാറ്റണം`, while the same
conversation also produced adjacent partial turns such as `നാളെ` followed by
`ഒരു മീറ്റിഗ ഉണ്ട് അത് വൈകുന്നേരത്തേക്ക് മാറ്റണം`. Several one-character or
very short turns were also promoted to Agent turns. This supports a turn-boundary
problem in the local VAD/worker path; it does not prove that every short turn was
noise or that character-count filtering would be safe.

A separate accuracy limitation remains visible on Malayalam/English code-mixed
speech. IndicConformer can return understandable but degraded phonetic Malayalam
for English words such as meeting/reschedule/evening. No model/provider swap is
made in the current candidate because the available evidence does not establish
a better replacement on the actual HFP/cellular input.

Candidate branch `feat/local-malayalam-stt-tts-latency` therefore keeps the
existing model but stops promoting the first Silero acoustic end directly to a
conversational EndOfTurn. After the existing 200 ms Silero silence decision, the
worker holds a bounded 320 ms commit window; speech resuming inside that window
continues the same buffered turn without a second START. The worker also emits
content-free buffered-segment duration, ASR time, peak, RMS and end-reason metadata. No
raw audio is persisted and transcript text remains local-only.

The 320 ms commit window is a controlled candidate, not a proven optimal value.
Live cellular validation is still required. Phase 5 remains unaccepted and no
caller-heard latency claim follows.
\n
## Local STT END-frame NameError escaped repository CI — 2026-10-06

The first live run of the segmentation/latency candidate exposed a worker-only
runtime defect that the initial repository gate did not execute. The worker
assigned the buffered segment duration to `audio_ms` but formatted the emitted
END protocol frame using `buffered_audio_ms`. Python compilation succeeds with
an unresolved local name, so `py_compile` did not detect the defect. Existing
unit coverage exercised `SpeechTurnBuffer` and the parent metadata parser but
did not execute the worker END-frame construction path.

Observed live sequence: the worker reached READY and emitted START for real HFP
speech, then exited at the first attempted committed END frame. The parent
correctly failed closed on the next send with
`LocalMalayalamSpeechError: Local Malayalam STT is not active`.

The candidate now centralizes END-frame construction in a pure
`format_end_frame` helper and executes that exact path in
`tests/test_local_malayalam_speech.py`. The prior green CI run is retained as
historical repository evidence but is not sufficient for the corrected head.
## Pocket TTS audible-response diagnosis lacked audio-derived transcript — 2026-10-06

The manual Bluetooth trace historically persisted caller ASR and timings but not
an audio-derived transcript of the generated response. This made it impossible
to distinguish a semantically correct LLM response from a Pocket TTS
pronunciation/rendering problem after the call had ended.

The current candidate adds an explicit post-call verifier over the exact
post-Player mu-law frames dispatched toward Bluetooth. It stores no raw audio.
The resulting transcript is useful evidence about Pocket/Player output, but
because it is captured before Bluetooth codec/HFP/cellular transport it cannot
by itself localize corruption introduced after that boundary. The verifier also
uses the same IndicConformer family as the local STT path, so code-mixed
recognition limitations must be considered when interpreting its transcript.
## 2026-10-06 live Galaxy A10 noisy-uplink diagnosis narrowed

During an active-call observation on the Galaxy A10 HFP path, the repository
`pw-link` parser reported `AI_ONLY_FORBIDDEN_LINKS=NONE`. That single live
sample did not show the previously documented physical ALSA microphone -> selected
Bluetooth uplink or selected Bluetooth downlink -> physical speaker contamination.
It does not prove those links can never reappear later in a call.

The preceding outbound TTS verification had already produced an intelligible
post-Player/pre-codec transcript for at least one complete response while the
remote handset audio was reported as noisy/unclear. The candidate therefore now
captures the same dispatched utterance at two bounded in-memory boundaries:
post-Player G.711 mu-law/8 kHz and the exact S16LE/16 kHz bytes accepted by
`Phase3AiOnlySession.write()` after `BluetoothOutboundCodec`. Post-call ASR
plus peak/RMS/near-full-scale metrics are persisted as text/metadata only.

Interpretation remains evidence-bound: matching clean pre/post codec transcripts
with sane levels would move suspicion downstream toward PipeWire/BlueZ/HFP/cellular
transport; divergence or clipping after the codec would localize the defect before
that boundary.


## Pocket-only 24 kHz downsampling clarity defect localized — 2026-10-06

The earlier noisy/unclear remote Pocket voice was not reproduced by a known-clean
WAV or by Pocket native PCM when either was sent directly as S16LE/16 kHz through
the same pw-cat/BlueZ/HFP/cellular path. The known-clean WAV also remained clear
after an 8 kHz G.711 mu-law round trip. Pocket alone became clear when an explicit
anti-aliased resampler was used before the existing 8 kHz mu-law boundary.

This evidence rules out a general HFP transport failure, a general G.711
mu-law/8 kHz intelligibility failure, clipping, and the previously documented
physical-route contamination as sufficient explanations for this reproduced
defect. It localizes the fix to Pocket's native-to-8 kHz conversion path.

The candidate now applies a provider-local streaming FIR anti-alias filter before
`audioop.ratecv`. Live end-to-end SHUO validation of the corrected repository
head is still required; repository CI alone does not establish caller-heard
quality.


## Local Malayalam false barge-in confirmed — 2026-10-06

Live call `phase5-local-ml-stt-tts-fix-5` confirmed that acoustic VAD starts
were being promoted too early to conversational interruption:

- turn 1 was cancelled about 8 ms after Agent start by a new local START;
- turn 2 was cancelled about 26 ms after Agent start by another START;
- a later Agent turn was cancelled by a START whose following EndOfTurn had an
  empty transcript and very low RMS;
- a low-energy one-character caller segment also became a full Agent turn.

The local provider now treats Silero START as tentative. A conversational START
is emitted only after a bounded 128 ms rolling voiced window reaches RMS 500.
The worker records `start_qualified` and `start_rms` in content-free END
metadata. Unqualified turns are not promoted to the SHUO state machine.

A separate local-only 200 ms post-EndOfTurn guard suppresses a newly-qualified
residual START immediately following a committed local turn and suppresses its
matching END. This addresses the measured 8/26 ms residual-start pattern without
changing `state.py`, Deepgram Flux behavior, carrier behavior or shared barge-in
semantics.

These thresholds are evidence-driven candidates for the reference HFP path, not
universal hardware claims. A controlled live retest is required before declaring
voice-cut behavior fixed.
