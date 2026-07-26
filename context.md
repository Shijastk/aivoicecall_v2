# context.md — living project state

**Last updated:** 2026-07-22
**Maintained by:** Claude. Update at every milestone. This is the recovery point if conversation context is lost.

Read with [CLAUDE.md](CLAUDE.md) (scope + rules) and [rules.md](rules.md) (engineering constraints).

---

## 1. Where we are right now

| | |
|---|---|
| **Current phase** | **Phase 1 — Transport swap: Twilio → Vobiz** |
| **Status** | **Steps 1.0–1.8 implemented. 229 tests green.** Research + adversarial review applied. **First live inbound call made 2026-07-22** — transport confirmed working end to end (see decision 28); the pipeline past STT is still unproven live. |
| **Blocking on** | A 10-digit Indian DID (a dummy number works for local testing). Credentials are in hand. |
| **Codebase state** | `shuo/carrier/` holds Vobiz + Twilio behind one interface. Pipeline is Flux + Groq + **ElevenLabs (now permanent — decision 21)**. Player paces at realtime; turns end on the carrier's playback ack. |
| **Last completed** | **First live call debugged** — Deepgram TurnInfo events were being discarded, so the agent never took a turn (decision 28). |

### Phase tracker

| Phase | Scope | Status |
|---|---|---|
| 0 | ~~Compliance & procurement~~ | **DELETED** — not a promotional dialer. Only survivor: get a Vobiz DID. |
| **1** | **Transport swap Twilio → Vobiz** | **code complete, 185 tests green; live call pending** |
| 2 | Provider abstraction (`shuo/providers/`) | not started |
| 3 | STT (Sarvam) + in-process turn detection, 3-phase state machine | not started |
| 4 | ~~TTS → Cartesia~~ **ElevenLabs confirmed, voice selected** | **vendor question closed** (decision 21); founder voice clone deferred |
| 5 | Player rewrite — fixes Bug A + Bug B | **Bug B done** — pacing (decision 22) + authoritative checkpoint (decision 27); **Bug A not started** |
| 6 | Filler engine, response-gap sampler, backchannel suppression | not started |
| 6.5 | **`shuo/persona/` — Digital Twin persona layer** (NEW, not in plan.md) | not started |
| 7 | LLM → Vertex Gemini 2.5 Flash-Lite, decided by measurement | not started |
| 8 | ~~Dialer & compliance gates~~ | **DELETED** |
| 9 | Observability — fix Bug C, export per-turn metrics | Bug C **done** (pulled into Phase 1); metrics not started |

### What Phase 1 delivered

| Area | Files |
|---|---|
| Carrier interface + factory | [carrier/base.py](shuo/carrier/base.py), [carrier/__init__.py](shuo/carrier/__init__.py) |
| Vobiz implementation | [carrier/vobiz.py](shuo/carrier/vobiz.py) |
| Twilio regression path | [carrier/twilio.py](shuo/carrier/twilio.py) |
| Carrier-neutral config | [config.py](shuo/config.py) |
| Types + state machine extensions | [types.py](shuo/types.py), [state.py](shuo/state.py) |
| Loop, endpoints, player seam | [conversation.py](shuo/conversation.py), [server.py](shuo/server.py), [services/player.py](shuo/services/player.py) |
| Protocol stub | [scripts/fake_vobiz.py](scripts/fake_vobiz.py) |
| Tests (146 passing) | [tests/](tests/) — `test_update.py` unchanged, + carrier/vobiz/player/state/integration |

**Test breakdown:** 24 original (unchanged) + 21 state + 25 carrier + 17 player + 56 vobiz + 34 integration + 6 logging = 185. Later additions bring the suite to **218**, including 14 in [tests/test_turn_completion.py](tests/test_turn_completion.py) (decision 27).

**Bugs fixed beyond scope:** Bug C (POSIX-only trace paths); Deepgram SDK 7.x incompatibility in [flux.py](shuo/services/flux.py) that broke every call at connect time; webhook URL reconstruction (see decision 12).

---

## 2. Scope, in one paragraph

A multi-persona voice **Digital Twin** on Indian telephony via a **standard 10-digit Vobiz DID**. No promotional dialing, no 140xx, no DLT/TCCCPR/DND work — see [CLAUDE.md §2](CLAUDE.md). Persona is runtime config over one pipeline. **Test Phase 1 persona: JOB CANDIDATE being interviewed** — chosen because it is adversarial and unscripted, to break the AI on character-break, hallucination, context-loss, and robotic delivery. Later personas: Technical Interviewer, HR Recruiter, Inbound Receptionist.

---

## 3. Target architecture

```
Indian mobile (PSTN)  ── G.711 A-law 20ms
        ▼
Vobiz SBC + media plane        ── AWS ap-south-1 (Mumbai), ~1-5ms
        │  wss:// JSON, base64 audio/x-mulaw;rate=8000
        ▼
┌──────────────────────────────────────────────────────────────┐
│ shuo agent — EC2 c7i.xlarge, ap-south-1                      │
│                                                               │
│  carrier/vobiz.py ──▶ event queue ──▶ process_event() [pure] │
│    ┌──────────────────────┼───────────────────────┐          │
│    ▼                      ▼                       ▼          │
│  Silero VAD v5      Sarvam saaras:v3         Filler engine   │
│  + SmartTurn v3.1   8kHz native, en-IN,      (pre-rendered,  │
│  (in-process CPU)   codemix                   0ms TTFB)      │
│         └──── EOT ────────┬────────────────────┘             │
│                           ▼                                   │
│              persona/  ← system prompt + fact block + voice   │
│                           ▼                                   │
│              Gemini 2.5 Flash-Lite (Vertex asia-south1)       │
│                           ▼ tokens                            │
│              Cartesia Sonic 3.5 — cloned voice                │
│                  raw / pcm_mulaw / 8000 (zero transcode)      │
│                           ▼                                   │
│              Player: 20ms monotonic-deadline pacing           │
└──────────────────────────────────────────────────────────────┘
```

**Latency contract** (re-specified — `<400ms` end-to-end is not achievable in India):

| Metric | Target |
|---|---|
| Server-side turn latency (EOT → first TTS byte queued) | **300–450ms** ← the number we own and optimise |
| Mouth-to-ear | 500–750ms (p50 ≈ 600ms) |
| Perceived (with gated fillers) | ~250ms |

---

## 4. Current codebase map

2,195 lines of Python. Verified against source 2026-07-21.

| File | Lines | Role | Fate |
|---|---|---|---|
| [shuo/state.py](shuo/state.py) | 60 | Pure `process_event`, 2 phases | **Keep**, extend to 3 phases (Phase 3) |
| [shuo/types.py](shuo/types.py) | 109 | Immutable State/Event/Action | Extend (Phase 1 + 3) |
| [shuo/conversation.py](shuo/conversation.py) | 426 | Event loop + `_TurnCompletion` (the ack gate) | Keep structure, add event sources |
| [shuo/agent.py](shuo/agent.py) | 190 | Per-turn LLM→TTS→Player pipeline | Heavy modification |
| [shuo/server.py](shuo/server.py) | 307 | FastAPI + TTFT bench harness | Modify endpoints; **reuse bench** |
| [shuo/log.py](shuo/log.py) | 296 | Structured logging | Keep |
| [shuo/tracer.py](shuo/tracer.py) | 140 | Per-turn span tracing | Keep; fix Bug C, export metrics |
| [shuo/services/flux.py](shuo/services/flux.py) | 160 | Deepgram Flux (STT + turn detect) | **Delete** (Phase 3) |
| [shuo/services/tts.py](shuo/services/tts.py) | 191 | ElevenLabs WS | → `providers/tts/elevenlabs.py` (fallback) |
| [shuo/services/tts_pool.py](shuo/services/tts_pool.py) | 182 | Warm pool, TTL eviction | **Keep pattern**, generalise |
| [shuo/services/player.py](shuo/services/player.py) | 429 | Reframes to 160-byte frames, deadline-paced at 50/s | **Bug B done**; Bug A remains |
| [shuo/services/twilio_client.py](shuo/services/twilio_client.py) | 74 | Outbound call + WS parsing | **Replace** → `carrier/vobiz.py` (Phase 1) |
| [shuo/services/llm.py](shuo/services/llm.py) | 120 | Groq streaming | Modify (Phase 7) |
| [tests/test_update.py](tests/test_update.py) | 307 | State machine tests | **Must stay green** |

### Confirmed geography problems (verified in source)

| Where | Pin |
|---|---|
| [flux.py:57-59](shuo/services/flux.py#L57-L59) | `wss://api.eu.deepgram.com` — Frankfurt |
| [llm.py:35](shuo/services/llm.py#L35) | `api.groq.com` — US |
| [tts.py:60](shuo/services/tts.py#L60) | `api.elevenlabs.io`; code expects region `"Netherlands"` at [tts.py:73](shuo/services/tts.py#L73) |
| [twilio_client.py:36](shuo/services/twilio_client.py#L36) | `edge="frankfurt", region="us1"` |

### Three latent bugs (all confirmed present)

- **Bug A — agent believes it said things the caller never heard.** [llm.py:111](shuo/services/llm.py#L111) appends the *entire generated* text `+ "..."` to history on cancel. Fix needs played-ms accounting. **Severity is higher for the candidate persona** — long answers get interrupted often.
- **Bug B — pacing drifts, turn-completion is guessed.** ***FIXED*** *2026-07-22 (decisions 22 + 27).* Pacing: [player.py](shuo/services/player.py) reframes TTS chunks of any size to 160-byte frames and emits one per 20ms tick on an accumulating monotonic deadline. Pinned by `TestPacing` in [tests/test_player.py](tests/test_player.py) — the literal rules.md gate (3000 frames in 60s, <1 frame drift) passes on Windows; run it with `SHUO_PACING_SECONDS=60`. Completion: `_TurnCompletion` in [conversation.py](shuo/conversation.py) holds the turn open until the carrier's `playedStream`, bounded by a grace window so rules.md V18 is respected. Pinned by [tests/test_turn_completion.py](tests/test_turn_completion.py).
- **Bug C — POSIX-only path.** [tracer.py:26](shuo/tracer.py#L26) `Path("/tmp/shuo")`, same at [server.py:80](shuo/server.py#L80). Breaks the Windows dev machine today.

---

## 5. Decision log

Append-only. Reversals get a new entry.

| # | Date | Decision | Reason |
|---|---|---|---|
| 1 | 2026-07-21 | **Not a promotional dialer.** Drop 140xx, DLT, TCCCPR, DND, calling-window gates. Standard 10-digit Vobiz DID. | User scope correction. Calls are consented, relationship-based, one-to-one. |
| 2 | 2026-07-21 | **Multi-persona Digital Twin**, persona = runtime config, not code. | One pipeline serves candidate / interviewer / recruiter / receptionist. |
| 3 | 2026-07-21 | **Test Phase 1 persona = job candidate being interviewed.** | Hardest, least scripted case. Adversarially stresses character-break, hallucination, context-loss, robotic delivery. |
| 4 | 2026-07-21 | **Inbound is a first-class path**, not deferred. | Receptionist persona requires it; cheap to build in during Phase 1. |
| 5 | 2026-07-21 | Keep the custom loop; do not adopt a managed platform. | Every managed orchestration fee alone (₹4.40–₹8.80/min) exceeds the ₹5/min ceiling. Retell's floor is ₹6.42. |
| 6 | 2026-07-21 | Transport = **Vobiz `<Stream>`**; no FreeSWITCH/Asterisk/jambonz. | Native Twilio-Media-Streams-equivalent WS API, ap-south-1. Byte-identical µ-law payloads → event-name mapping only. |
| 7 | 2026-07-21 | Stay on **µ-law 8kHz**; no L16 path. | Sidesteps the Vobiz big-endian-in / little-endian-out corruption trap. PSTN leg is 8kHz anyway. |
| 8 | 2026-07-21 | Latency target **re-specified** to 300–450ms server-side, not <400ms end-to-end. | Physics. Best published India number is LiveKit's ~1.67s. |
| 9 | 2026-07-21 | New **Phase 6.5 `shuo/persona/`**, replacing plan.md's single telecalling `SYSTEM_PROMPT`. | Direct consequence of decisions 2 & 3. |
| 10 | 2026-07-22 | **Carrier interface modelled on the Plivo shape**, with Twilio kept as a working adapter, not a stub. | Vobiz is a Plivo clone, so Vobiz+Plivo drop in and Twilio is the one that adapts. `CARRIER=twilio` placing a real call is the proof the abstraction has not grown Vobiz-shaped assumptions. |
| 11 | 2026-07-22 | **Persona and direction travel in the WebSocket query string**, not a carrier custom-parameter mechanism. | Every carrier forks to exactly the URL it is given. Twilio has `<Parameter>`; Vobiz's equivalent is unconfirmed. The query string needs neither. |
| 12 | 2026-07-22 | **Webhook signature URL is rebuilt from `PUBLIC_URL` + request path**, never from `request.url` or `X-Forwarded-*`. | Behind ngrok/an ALB `request.url` is the internal `http://localhost:…` form, so validation would fail on *every* live call. Forwarded headers are attacker-controllable unless the proxy overwrites them. Caught by an integration test before it reached a live call. |
| 13 | 2026-07-22 | **`MediaEvent.track` guard** — the state machine drops anything that is not the inbound track. | Vobiz's REST `<Stream>` path defaults `audioTrack` to `"both"`. If that ever leaks through, the agent transcribes its own TTS and replies to itself. |
| 14 | 2026-07-22 | **Dual-channel recording starts over REST on `start`**, not from the answer XML. | Vobiz's XML `<Record>` has no channel attribute — stereo is REST-only, and REST needs the `call_uuid` that `start` provides. See open question 8: whether it actually captures agent audio is unverified. |
| 15 | 2026-07-22 | **Bug C pulled forward from Phase 9**; Deepgram SDK 7.x break fixed opportunistically. | Both blocked local testing on Windows *today*, which is the entire point of Phase 1. |
| 16 | 2026-07-22 | **Operator endpoints gated behind `SHUO_ADMIN_TOKEN`, failing closed.** `/call`, `/trace/latest`, `/bench/ttft`. | `PUBLIC_URL` is internet-reachable and the server binds 0.0.0.0. `/call` spends PSTN money (toll fraud), `/trace/latest` hands out call transcripts, `/bench/ttft` burns paid LLM credits amplified by `runs`. Unset token = endpoints disabled, never open. |
| 17 | 2026-07-22 | **`/ws` requires an HMAC token** bound to persona, direction and a 5-min expiry, verified *before* `accept()`. | The signature gate on `/answer` proves nothing about who dials `/ws`, and persona was otherwise attacker-chosen. Minted into the `<Stream>` URL by `config.websocket_url`; secret defaults to `VOBIZ_AUTH_TOKEN`. |
| 18 | 2026-07-22 | **A failed turn or an unparseable frame degrades, never hangs up.** | A vendor hiccup should cost one turn, not the call. Previously any exception from `agent.start_turn`, or any parse error, terminated `run_conversation`. |
| 19 | 2026-07-22 | **`StreamStartEvent` emits `ResetAgentTurnAction` when leaving RESPONDING.** | It was the only exit from RESPONDING that did not, so a mid-response reconnect orphaned a live agent turn and desynced the state machine from the Agent for the rest of the call. |
| 20 | 2026-07-22 | **Console logging forced to UTF-8** ([log.py](shuo/log.py) `_utf8_stream`). | The Windows dev console is cp1252 and cannot encode `│`, the arrows, or the emoji — so *every* log line raised UnicodeEncodeError inside the handler and the operator saw a wall of tracebacks instead of the call trace. Same class as Bug C: fine on the Linux target, broken on the dev machine. Found while demonstrating the R0 diagnostic, which the live test depends on. |
| 21 | 2026-07-22 | **TTS stays on ElevenLabs. Sarvam and Cartesia are rejected.** Voice = `MmiGAbOYCaIFzgNItUWa` ("Krish - Modern Creator"). **Reverses** the Phase 4 Cartesia plan and [rules.md P1](rules.md). | User listened to candidate audio and judged ElevenLabs the most natural on Indian English — the criterion that decides this project. The open Sarvam codec question ([handoff-tts-swap.md](handoff-tts-swap.md) §4) is now moot; `scripts/probe_tts.py` was never run and does not need to be. ElevenLabs already emits `ulaw_8000`, so rules.md C1 holds with zero transcode, and P6's character-level alignment (`charStartTimesMs`) becomes available as the exact solution to Bug A rather than a fallback. Cost impact unmeasured — ElevenLabs was never in the ₹2.12/min model. |
| 25 | 2026-07-22 | **Lateness catches up within 100ms, then re-anchors.** Settled by measurement after a review pass argued for absorbing every stall. | The deploy target does not run this loop idle — [rules.md §5](rules.md) puts Silero VAD and Smart Turn v3.1 in-process on it (~12ms modern CPU, ~60ms small instance). Drift over 10s against the 20ms gate: idle 13.4 vs 8.8ms; **60ms stall every 2s: 126.7ms (absorb) vs 2.2ms (catch-up)** — absorbing fails by 6× because each stall is paid permanently and they accumulate. The burst that absorbing was meant to prevent did not appear: 8 back-to-back frames out of 500, identical worst gap (60.9ms). Past 100ms it *is* a stall and re-anchoring is right. **Lesson: the pacing gate must be measured under load; an idle-loop measurement tests a condition production never has.** `TestPacing.test_holds_the_gate_while_the_loop_is_stalled` now pins it. |
| 24 | 2026-07-22 | 🔴 **`_await_audio` must block until a whole frame is formable, not until the buffer is non-empty.** | Found by adversarial review, reproduced before fixing. The underrun branch of `_playback_loop` has exactly one suspension point. When the buffer held **1–159 bytes with TTS still open**, `_next_frame()` returned `None` while `_await_audio()` returned *without ever awaiting* — a spin that never yields. Measured: a 1000-byte ElevenLabs-shaped chunk gave **10.2M underruns and 7 event-loop ticks in 250ms**; only exact multiples of 160 survived, and 160 divides no vendor's framing. It wedges the whole worker — carrier reader, TTS reader, barge-in, hangup, and every concurrent call — permanently, because `stop_and_clear`'s `cancel()` is delivered at a suspension point that never comes. **Introduced by the Bug B rewrite** (the old code's starved branch was an unconditional `sleep(0.010)`), and the 196-test suite passed with it: every test that left `_tts_done` False used an exact multiple of 160. |
| 23 | 2026-07-22 | **The player paces on `time.perf_counter()`, not `loop.time()`.** Recorded as [rules.md C7](rules.md). | Found by measurement, not review. On Python 3.12/Windows `loop.time()` is `GetTickCount64` at **15.625ms resolution** — three quarters of a 20ms frame — so the scheduler could not tell "on time" from "a frame late" and settled at **32.5 fps**; with `perf_counter` the same 30s stream runs at **50.02 fps, +6.9ms drift**. Fixed in 3.13 upstream, so it would have vanished on the Linux target and been invisible until someone trusted a local test call. The test harness has the same requirement — a `loop.time()` stopwatch cannot see most of its own 20ms budget. |
| 24 | 2026-07-22 | **Lateness re-anchors; the player never catches up by bursting.** `_sleep_until` drops the old "recoverable if under 100ms late" branch — any missed deadline reschedules from *now*. | Found by measurement during a working-tree audit. The old branch yielded with `sleep(0)` and kept the missed deadline, so a loop running 20–100ms behind emitted frames **back-to-back — measured at 0.02ms apart, 25/25 runs under load**. That is Bug B by another name: it floods the handset de-jitter buffer (audio glitches) and leaves up to 100ms of unretractable audio committed at a barge-in, against the ~1 frame the module docstring promises. Cost: a stall now delays the stream permanently instead of being clawed back. User chose that trade explicitly — this is a personal-call/interview tool, so stream quality beats catching up. Matches the underrun path, which already re-anchored. Depends on [C7](rules.md): with `loop.time()`'s 15.625ms quantum, ticks read as late every third frame and re-anchoring turned each phantom stall into permanent drift (40.7ms over 150 frames). |
| 23b | 2026-07-22 | **Decision 23 / [rules.md C7](rules.md) had been silently reverted in the working tree and was re-applied.** | Discovered during the audit: `player.py` was back on `loop.time()` throughout despite C7 being a 🔴 rule and 23 a recorded decision. It was also re-reverted *mid-session*, immediately after being fixed — same "ghost edit" pattern flagged last session. Re-verify `grep -c 'loop.time()' shuo/services/player.py` returns only the prose reference before trusting any local pacing measurement. |
| 26 | 2026-07-22 | **The ElevenLabs fallback voice is `MmiGAbOYCaIFzgNItUWa` ("Krish - Modern Creator"), not Rachel.** [tts.py:39](shuo/services/tts.py#L39). | An unset `ELEVENLABS_VOICE_ID` used to default to `21m00Tcm4TlvDq8ikWAM` — Rachel, an American voice. On this project that is not a cosmetic default: the twin claims to be an Indian candidate, so the fallback is an instant character break (CLAUDE.md §4 / rules.md D3, D4) and it fails **silently, mid-call**, with no error anywhere. The fallback now matches decision 21's live config, so a missing env var degrades to the right voice instead of the wrong persona. |
| 27 | 2026-07-22 | **`playedStream` is the authoritative end of a turn** — the second half of Bug B. `_TurnCompletion` in [conversation.py](shuo/conversation.py) arms on the player's dispatch callback and emits `AgentTurnDoneEvent` when the carrier acks the checkpoint, or when a **250ms grace window** expires (`SHUO_CHECKPOINT_GRACE_MS`, 0 disables). Voided by barge-in, reconnect, `clearedAudio` and hangup. | The player's `on_done` only means the last frame was *dispatched*; the carrier still holds the pre-roll and the handset its de-jitter buffer, so the agent believed a turn was over before the caller had heard it. rules.md V18 makes the ack conditional and possibly absent, so it is a **gate, not a wait** — nothing blocks, and the worst case is 250ms. 250ms because the ack needs ~100–200ms in ap-south-1 and that sits at the median human response gap (rules.md H1), so the wait hides inside a gap we are going to sample anyway. **The state machine is unchanged and still pure** (CLAUDE.md rule 1): a grace window is a timer, so it lives at the dispatch boundary (rules.md A3) and `process_event` still sees exactly one `AgentTurnDoneEvent` per turn. Tuning input for the first live call: a `carrier_playback_timeout` marker now lands in the trace whenever the ack does not arrive — which also settles whether Vobiz sends `playedStream` at all. |
| 22 | 2026-07-22 | **Bug B's pacing half pulled forward from Phase 5**, on explicit user approval (CLAUDE.md rule 7). [player.py](shuo/services/player.py) now reframes to 160-byte frames and paces on a monotonic accumulating deadline. | Decision 21 makes it urgent rather than future work: ElevenLabs' ~125ms chunks drove the old `sleep(0.020)`-per-chunk loop at ~6.25× realtime. Over-buffering is unretractable on barge-in, and the candidate persona's 20–45s answers get interrupted constantly (rules.md D5). Reframing also decouples pacing from vendor chunk size, so this does not have to be revisited if TTS is ever swapped again. |
| 28 | 2026-07-22 | 🔴 **Deepgram socket messages are read shape-agnostically** (`_field` in [flux.py](shuo/services/flux.py)), and Flux's `error`/`close` events are now actually subscribed. Found on the **first live call**: audio flowed both ways, Deepgram answered 53 messages, and the agent never took a turn. | The SDK types its socket responses as `Union[ListenV2Connected, ListenV2TurnInfo, Any, …]`, and `construct_type` short-circuits **any union containing `Any`** by returning the decoded JSON untouched (`unchecked_base_model.py:222`). So messages arrive as plain **dicts**, `getattr(message, "type")` is always `None`, and **every `TurnInfo` was silently discarded** — no transcript, no LLM, no error. Three separate paths hid it: `on("Error", …)` used a capitalised name the emitter (`EventType.ERROR == "error"`) can never match; unknown Vobiz frames logged at DEBUG under an INFO default; media/FeedFlux logging was off. **Lesson: a vendor SDK's type hints are not a contract — assert the runtime shape.** Diagnostics kept: per-frame counters into Deepgram, a carrier frame count at hangup, once-per-call warnings for unknown carrier frames, and `SHUO_LOG_LEVEL`. Pinned by [tests/test_flux.py](tests/test_flux.py) (11 tests, both dict and model shapes). |
| 29 | 2026-07-23 | 🔴 **`MmiGAbOYCaIFzgNItUWa` ("Krish") cannot be synthesised on the current ElevenLabs plan.** Measured, not inferred: every model (`turbo_v2_5`, `flash_v2_5`, `multilingual_v2`), every codec (`ulaw_8000`, `pcm_16000`) and both auth styles return the *same* frame — `{"error": "payment_required", "message": "Free users cannot use library voices via the API", "code": 1008}` — then close 1008 having emitted **zero** audio. A sweep of all 23 voices on the account: **21/21 `premade` PASS** (~250ms TTFB, valid `ulaw_8000`), **0/2 `professional` PASS** — and the only two Indian-accented voices, Krish and Maya, are exactly the two that fail. | **`/v1/voices` listing a voice is not proof it can be synthesised.** The listing returns everything in the workspace; synthesising a `category: professional` (library) voice is a *separate, paid* entitlement, and the two are checked at different times by different services. `/v1/user/subscription` could not settle it either — this API key lacks the `user_read` scope and 401s — so the listing was the only signal available and it pointed the wrong way. The refusal also arrives **~500ms after the first generation is triggered**, on a socket that connected cleanly and logged `✓ TTS connected`, which is why it read as a streaming/deadlock bug rather than a config one. **Lesson: entitlement is proven by synthesising, not by listing.** Voice choice deferred to the user — see decision 30. |
| 30 | 2026-07-23 | 🔴 **A turn that produces no TTS audio now ends itself** ([agent.py](shuo/agent.py) `_on_tts_done` → `_end_turn`), and **`TTSPool` checks liveness, not just age**, before dispensing ([tts_pool.py](shuo/services/tts_pool.py)). | Decision 29's refusal was survivable; the pipeline's reaction to it was not. `AudioPlayer` is started *only* by `send_chunk`, so with zero chunks there was no `_playback_loop`, so `_on_playback_done` never fired, so no `AgentTurnDoneEvent` was ever emitted — `_active` stayed `True` and the machine sat in RESPONDING **until the caller gave up and barged in**. Any vendor fault therefore presented as a conversation fault. Second defect, latent and independent: `TTSPool.get` dispensed on `age < ttl` alone, so a socket that died while pooled was handed to a turn that fed an entire LLM response into it — `send()` and `flush()` both `return` silently when `_running` is False, producing no audio *and* no error. Both pinned by [tests/test_tts_failure.py](tests/test_tts_failure.py) (7 tests), verified to fail against pre-fix `agent.py` before being trusted. Live end-to-end after the fix: refused voice → turn ends at +2.5s with no checkpoint armed; working voice → 416 frames, all exactly 160B, 8320ms of audio, TTS first audio +500ms. |
| 31 | 2026-07-23 | **Voice is temporarily `JBFqnCBsd6RMkjVDRZzb` ("George" — premade, British), in `.env` and as the [tts.py](shuo/services/tts.py) fallback. Conditionally reverses decision 26.** User's call, taken with the accent cost in view. **Revert to Krish the moment the plan is upgraded.** | Decision 26 made Krish the fallback so a missing env var would degrade to the *right persona* rather than the wrong one. That reasoning held while Krish worked; decision 29 removed the premise. The trade is no longer right-accent vs. wrong-accent, it is **wrong-accent vs. no audio at all** — and an off-accent turn is a bad turn where silence is no conversation. British is the least-wrong stand-in for Indian English among the 21 premade voices (none is Indian), but it remains a live character break against CLAUDE.md §4.3 and rules.md D3/D4, so the candidate persona cannot be honestly evaluated on accent or Hinglish register until this is reverted. Turn-taking, barge-in, played-ms truncation and latency are all unaffected and *can* be tested now. |

### Verification posture

Phase 1 was checked three ways beyond the unit suite:

1. **A 6-lane adversarially-verified research pass** (80 agents) pinned the Vobiz wire format; 28 documented traps were checked against the implementation, which was already correct on 24. See [rules.md §3](rules.md).
2. **A 7-dimension adversarial code review** (164 agents, each finding refuted by 3 independent verifiers) produced 14 confirmed findings from 52 candidates — all fixed. 38 were refuted.
3. **Mutation testing** on the disconnect handler exposed a real coverage gap: deleting the `StreamStopEvent` on `WebSocketDisconnect` left all 156 tests green, even though it is the *only* thing that ends a Vobiz call. TestClient masks it by cancelling the task on context exit. `TestCallTermination` now drives `run_conversation` directly and fails with `TimeoutError` under that mutation.

**Lesson worth keeping:** a green suite is not evidence of correctness until a mutation proves the suite can fail. Apply this to Phase 5's player rewrite, where the pacing assertions will be easy to write tautologically.

**Applied to decision 27, and it paid immediately.** Seven mutations were run against the completion gate. The first pass caught 3 of 7 — the four survivors all traced to one flaw in the *test*, not the code: the stub agent reported playback complete inside `start_turn`, so turn N+1 always armed the gate the instant it began. That closed the exact window the gate exists to police (a turn running while the previous turn's checkpoint is still unacknowledged) and made every stale-ack bug untestable. With the stub corrected to make playback completion a separate step, 5 of 7 are caught. The two survivors are the paired `void` calls, which are mutually redundant — removing either alone still holds, removing both is caught. That is recorded in the code comments rather than papered over. **A stub that collapses a timing window is indistinguishable from a passing test.**

---

## 6. Open questions — need a vendor or the user

1. ~~**Vobiz account** — `auth_id`, `auth_token`, API base URL.~~ **Received 2026-07-22.** A provisioned **10-digit Indian DID** is still outstanding; a dummy number works for local testing.

8. 🔴 **Does a Vobiz recording capture audio injected over the WebSocket?** *This is the highest-risk open item in Phase 1.* Dual-channel is documented as separating "caller and callee" / "each leg" — but a `<Stream>` agent has only one PSTN leg, and `bidirectional="true"` forces `audioTrack="inbound"` so the socket can never carry our own audio. Undocumented by both Vobiz and Plivo. **Settle it with one live call:** place a call, let the agent speak, fetch the recording, and check whether the second channel contains agent audio or silence. If silent, the fallback is to write the agent channel from our own TTS buffer (the player already counts bytes) and mux offline.
9. **Which physical channel holds which speaker** in a stereo recording — undocumented. Keep the mapping a config constant.
10. **Does Vobiz's live V3 signature match its own docs** (`base_url + "." + nonce`) or actually clone Plivo V3 (full URL + sorted POST params)? No test vectors published. First live webhook settles it; a 403 with the logged `base_url` diagnoses it in seconds.
2. **Vobiz per-minute rate** — ₹0.45 stated on three of their own comparison pages, unconfirmed on the rate card (which renders US-only).
3. **Vobiz `<Stream>` forking fee** — audio streaming may bill per minute; rate not found. If material, Asterisk 23.4.1 + `chan_websocket` is the documented runner-up.
4. **Sarvam WebSocket billing basis** — audio-duration vs wall-clock. A ~2.9× swing on that line. Confirm contractually.
5. **Cartesia India/Asia endpoint** — their page claims "regional API endpoints" but enumerates no regions. Ask before committing.
6. **Founder voice sample** — ≤10s clean clip needed for the Cartesia clone (Phase 4).
7. **Candidate persona fact block** — the résumé/profile the twin is grounded on. Needed before persona testing can start.

---

## 7. Cost model (unchanged from plan.md Part 3)

20,000 conversation-min/month, 15 concurrent, 1 USD = ₹88:

Telephony 0.45 + channels 0.26 + STT 0.18 + turn-detect 0.00 + LLM 0.04 + speculative 0.03 + TTS 1.10 + fillers 0.00 + compute 0.06 = **₹2.12/min**.

Ceiling ₹5, stretch ₹3. Retell's verified floor: ₹6.42. Swapping Cartesia → Inworld TTS 1.5 Mini → **₹1.46/min** (largest remaining lever, Phase 4 A/B).

---

## 8. Session-restart checklist

If context was lost, do this before touching anything:

1. Read [CLAUDE.md](CLAUDE.md) — especially §2, the corrections that override `plan.md`.
2. Read this file §1 (current phase) and §5 (decision log).
3. Read [rules.md](rules.md) before writing any carrier, codec, or player code.
4. `git status` and `git log --oneline -5` — confirm what actually landed vs. what this file claims.
5. `python -m pytest tests/ -v` — the state machine must be green before and after every change.
