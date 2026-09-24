# context.md — living project state

> Knowledge setup milestone (2026-09-13): [docs/README.md](docs/README.md) indexes the inspected source snapshot and the separate Bluetooth roadmap. Bluetooth Phase 1 is complete only on supplied runtime evidence; Phases 2–8 remain planned. No application code or tests changed. This historical milestone/decision log and its existing instructions remain intact; see [known documentation differences](docs/KNOWN_ISSUES.md).

**Last updated:** 2026-07-27
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
| **Last completed** | **Phase 8 W5e — terminal call states actually reach the panel** (decision 51). A declined call left the UI frozen on its ringing screen, because `/v1/calls/live` never read the call log and `/v1/calls/active` dropped a call the instant it ended. `declined` is now a status of its own, `endedCode` is the machine-readable half of `endedReason`, a finished call lingers 20s so the transition can be seen, and a carrier that goes silent on a ringing call is reported as failed after 180s. **777 tests green** (+14); wire-tested across two processes at **15ms** from webhook to visible terminal state. Before that, **Phase 8 W5d — push notifications, and with it Phase 8's backend is complete** (decision 50). A ~1Hz task in :3041 watches the call log and pushes to ntfy.sh when an inbound call starts, is missed, or fails — **off entirely unless `SHUO_NOTIFY_URL` is set**, because it is the only thing in the system that sends call data (metadata, never a transcript) off the machine. The topic in the URL *is* the credential, so it is redacted everywhere including `/health`. **757 tests green** (+52), 14/14 mutations caught, wire-tested against a loopback stand-in for ntfy. 🔴 It also surfaced a **pre-existing durability bug in the call log**: a torn final line has no newline, so the next append fused to it and *both* records were lost — healed in `call_history.append`. Before that, **Phase 8 W5c — the panel can watch every call at once** (decision 49). The monitor's one shared 200-event ring became **one ring per call** (up to 8), so two concurrent calls no longer evict each other's transcripts; `GET /calls/active` on :3040 lists them, and `GET /v1/calls/active` on :3041 **unions that with the non-terminal rows on disk** — which is the only way a phone that is still *ringing* can appear at all, since it has no media socket to be in memory of. `/v1/calls/live` is the honest name for the live view; `/v1/test-call/status` stays as an alias. Still **polling only, no SSE** on either hop. **Measured: 1.43µs per publish worst case (ring full, evicting on every append) — 0.007% of the player's 20ms budget; `summaries()` 8µs.** **705 tests green** (+49), 6/6 mutations caught. The state machine was not touched, and neither was the streaming chain. Before that, **Phase 8 W5b — local stereo call recording** (decision 48). The µ-law the pipeline already holds is teed to disk through the spool and converted to a stereo WAV (caller left, agent right); `GET /v1/calls/{id}/recording` on :3041 serves it with Range support so the panel can seek. **Measured at 4.5µs per 20ms tick — 0.02% of the player's frame budget** — and the real pacing gate passes with the tape attached. **656 tests green** (+50), 4/4 mutations caught. Before that, **Phase 8 W5a — every call attempt is in the log** (decisions 46–47). A call that rings out, is declined or is cancelled now leaves a row, because origination and the carrier's webhooks write one — through [shuo/spool.py](shuo/spool.py), so the process that paces 20ms frames never waits on a disk. An **attempt id** minted before the carrier is called ties the stub, the live call and the archived row together; revisions fold into one row on read. **606 tests green** (+58), 4/4 mutations caught. The state machine was not touched, and neither was the streaming chain. 🔴 `ring_url`/`hangup_url` are sent to Vobiz for the first time and are **unverified** — one live call settles it. Before that, **W4 — the panel reads what is stored** (decisions 43–45). Three `GET` endpoints behind the three form screens, so a reload shows the saved configuration instead of three empty boxes; the `/call-logs` fixture replaced with real records, written once per call in the call loop's teardown to `var/call_history.jsonl` and read directly off disk by :3041; and the delivery rules (adaptive length, fillers, no spoken structure, no backchannel-as-a-turn) added to the persona ruleset, which is data. **547 tests green** (+36). The state machine was not touched, and neither was the streaming chain. Before that, **W3 — the test call** (decisions 40–42). `POST /v1/test-call` on :3041 places a **real** call through :3040's existing telephony path, and a bounded in-memory monitor in the call process feeds the panel's split pane a live state, both sides of the transcript and per-turn latency. **511 tests green** (+95). **18/18 mutations caught**, and the chain was exercised over the wire between two running processes, not only through `TestClient`. The state machine was not touched. Before that, **W2 — the agent reads the config store** (decision 39). A Save in the panel now changes what the *next call* runs with: system prompt, persona rules and knowledge block assembled into one system message, voice resolved from catalogue ID to provider ID before the TTS pool warms. **416 tests green** (+34). The state machine was not touched. Before that, **W1.5 — the voice catalogue** (decision 38). `GET /v1/voices` + save-time validation against a measured list, closing open question 11. **378 tests green.** Before that, **W1 — the config REST API** (decisions 32–37): three `PUT` endpoints reconciled against the panel's declared contract, an atomically-replaced JSON store, and **a separate process from the call server so a Save can never stall a live call.** 16/16 mutations caught. |

### Frontend integration state

The Next.js control panel (`../shuo-frontend`) finished its Phase 5 mock layer; W1 is the backend half of that seam.

| | |
|---|---|
| **Contract source** | `shuo-frontend/lib/api/transport.ts` (paths) + `lib/api/types.ts` (DTOs) — both carried `TODO(api)` markers saying every path was a guess. **W1 adopted their guesses verbatim**, so switching over is one env var, not an edit. |
| **To connect** | `SHUO_API_URL=http://127.0.0.1:3041` in `shuo-frontend/.env.local`. Unset, `transport.ts` keeps using its in-process mock. |
| **Wired** | `PUT` **and now `GET`** on `/v1/agent/config`, `/v1/agent/persona`, `/v1/agent/knowledge`, plus `GET /v1/calls/history`. Field limits (16k/16k/50k) confirmed and adopted, closing that `TODO(api)`. |
| **The read half** | **Landed (decision 43).** All four screens are `force-dynamic` and render from the service; verified over the wire with both processes running, and with the service stopped so the failure branch could be seen. `GET /v1/config` still exists for `curl` verification and is unchanged. |
| **Voice catalogue** | **Backend-owned and enforced (decision 38).** `GET /v1/voices` serves the 21 measured-synthesisable voices in the panel's `VoiceModel` shape; `PUT /v1/agent/config` refuses anything else. |
| **Voice picker** | Live against `GET /v1/voices`, and now preselects the **saved** voice — checked against the catalogue first, so a lapsed entitlement falls back to `el-george` rather than showing a picker that reads correct while the call speaks in something else. |
| **Fixtures remaining in the product path** | **None.** `/calls`' `PLACEHOLDER_CALLS` was the last one and is deleted. The only fixtures left are behind `SHUO_USE_MOCK=1`, and every id they mint starts `mock-`. |
| **Test call (W3)** | Backend complete and wire-tested. `POST /v1/test-call {phoneNumber, persona?}`, `GET /v1/test-call/status?since=`, `POST /v1/test-call/hangup?expect=`. `test-call-panel.tsx` is still the static preview — three `ENDPOINTS` entries in `transport.ts`, a ~1s poll, and its `TestCallState` union already matches the backend's four states exactly (no type change needed). The phone number belongs in `localStorage`, not in the config document — it is harness state, not something the twin is. |
| **Live monitor (W5c)** | Backend complete. `GET /v1/calls/active` lists every call in flight — live ones from :3040 unioned with `pending`/`ringing` rows off disk — and `GET /v1/calls/live?call=&since=` reads the one the operator has open. **Poll both at ~1Hz**; `since=` makes a steady-state poll carry nothing, and `callServer: "unreachable"` is a field, not an error, so a stopped call server must not paint a banner on every tick. `/v1/test-call/status` still answers identically, so nothing breaks before the panel moves. **Two contract notes:** rows carry `status` in the **eight**-state vocabulary (same badge fallback decision 47 flagged), and the panel should send `expect=<id>` on hangup — with eight concurrent calls, "the current call" is not a thing it can safely mean. |
| **Call termination (W5e)** | **Backend complete and wire-tested** (decision 51). The panel's ringing screen should close on **`live === false`**, and branch on **`endedCode`** — a closed set (`remote_declined`, `remote_busy`, `no_answer`, `cancelled_by_us`, `origination_refused`, `no_carrier_response`, `carrier_error`, `completed`, `call_failed`) — never on `endedReason`, which is prose for a human and is not a stable string. `status` gains an **eighth** value, `declined`, so any badge map needs it (it renders as a decline, not a miss). `GET /v1/calls/live?call=` now answers for a call that is only *ringing*, so the panel can poll one route from the moment it has an `attempt` id; `source` says whether the answer came from `live`, the `log`, or `none`. A call that has ended stays in `/v1/calls/active` for 20s with `live: false` — poll at ~1Hz and it cannot be missed. **Keep the `attempt` id from `POST /v1/test-call`, not `callId`**: a call nobody answers never has a `CallUUID`. |

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
| **W1** | **Backend wiring 1 — config REST API** (NEW, not in plan.md) | **code complete, 16/16 mutations caught** |
| **W1.5** | **Voice catalogue — `GET /v1/voices` + save-time validation** | **code complete, 378 tests green, verified on the wire** |
| **W2** | **Backend wiring 2 — the audio process reads the config store** | **code complete, 416 tests green, resolution verified against the real `var/agent_config.json`** |
| **W3** | **Backend wiring 3 — Test Call endpoint (ring the operator's phone)** | **code complete, 511 tests green, 18/18 mutations caught, wire-tested** |
| **W5a** | **Phase 8 — every call *attempt* in the log, written through a spool** | **code complete, 606 tests green, 4/4 mutations caught**; 🔴 `ring_url`/`hangup_url` unverified — needs one live call |
| **W5b** | **Phase 8 — local stereo recording (µ-law tee) + retrieval on :3041** | **code complete, 656 tests green, 4/4 mutations caught** |
| **W5c** | **Phase 8 — multi-call live monitor + `/calls/active` (polling)** | **code complete, 705 tests green, 6/6 mutations caught**; frontend not yet wired |
| **W5d** | **Phase 8 — opt-in ntfy notifier and its poller** | **code complete, 757 tests green, 14/14 mutations caught, wire-tested against a loopback ntfy**; off unless `SHUO_NOTIFY_URL` is set |
| **W4** | **Backend wiring 4 — the read half + the durable call log** | **code complete, 547 tests green, wire-tested with the service both up and stopped** |

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
| [shuo/config_api.py](shuo/config_api.py) | 320 | Config REST API — **separate app, separate process** (decision 32) | Keep; no route may move onto `server.py` |
| [shuo/config_store/](shuo/config_store/) | 480 | Operator config: models + atomic JSON store | Read by `runtime_config.py` (W2) |
| [shuo/runtime_config.py](shuo/runtime_config.py) | 210 | **The reader half of the config seam** (W2) — assembles the system prompt, resolves the voice | Phase 6.5 merges persona defaults here |
| [config_api.py](config_api.py) | 80 | Config API entrypoint, `:3041`, loopback | Keep |

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
| 32 | 2026-07-26 | 🔴 **The config API is a separate process on a separate port (`python config_api.py`, :3041), not routes on `shuo/server.py`.** [config_api.py](shuo/config_api.py), [config_store/](shuo/config_store/). Pinned by `TestIsolation` in [tests/test_config_api.py](tests/test_config_api.py), which fails if either app imports the other's concerns. | A latency decision, not a tidiness one. `server.py`'s app shares its event loop with the media WebSocket, and the player emits a 160-byte frame every 20ms against an accumulating monotonic deadline. **Decision 24 removed the player's ability to claw lateness back** — a missed deadline now re-anchors — so a handler that fsyncs a 50KB knowledge block does not cost one frame, it costs *permanent stream delay for the rest of the call*. Mounted on the call server, an operator pressing Save could audibly degrade a call in progress. Two processes make that impossible rather than merely unlikely; they share only the config file, swapped with `os.replace`, so a reader cannot observe a partial write either. |
| 33 | 2026-07-26 | **Config is one JSON document replaced atomically, not SQLite.** [config_store/store.py](shuo/config_store/store.py). `load()` never raises — absent, empty, truncated, invalid, or future-versioned all degrade to "nothing configured". | The reader lands in the *audio* process in W2, so the read path has to be a file open and a `json.loads` — no driver, no connection, no lock anywhere near the 20ms deadline. `os.replace` is atomic on Windows too (`MoveFileExW`), so the reader sees the whole old document or the whole new one. Two Windows-only traps handled up front rather than discovered, the same class as Bug C and decision 20: the replace is **retried** because `os.replace` raises `PermissionError` when the destination is open by another process (i.e. exactly what the agent reading config looks like), and the file is written `newline="\n"` so a save on the dev machine is not a whole-file diff against the Linux target. |
| 34 | 2026-07-26 | **"Never written" and "written empty" are different states, and only the first resolves to the anti-robotic ruleset.** `DEFAULT_PERSONA_RULES` lives in [config_store/models.py](shuo/config_store/models.py); `ConfigDocument.resolved_persona_rules` is the only place the distinction becomes a value. | Both halves matter and they pull opposite ways. An install nobody configured must not run with *zero* persona constraints — robotic delivery is one of the four failure modes in CLAUDE.md §1 — so an absent section resolves to the ruleset. But an operator who selected all and deleted has made a decision, and writing the default back over them would overrule it while reporting success. The frontend's `defaults.ts` already anticipated the backend owning this text: its TODO says its copy becomes the fallback once a read path exists. **Keep the two in step until that lands.** |
| 35 | 2026-07-26 | **The API's error envelope is `{"message": ...}` on every path, and every message ends by saying what happened to the data.** Overrides FastAPI's `detail` for validation errors, `HTTPException`, and unhandled exceptions alike. | The panel reads `body.message` and renders it verbatim in the save row ([transport.ts](../shuo-frontend/lib/api/transport.ts) `readErrorMessage`); anything else collapses to "The agent service returned 422" and the reason never reaches the person who can fix it. Length-limit wording and `,` digit grouping deliberately match the frontend's `checkLength`, so a value the panel would have rejected locally produces the same sentence when the backend rejects it instead. Two cases were found only by exercising the running service, not by the suite: an **invalid-UTF-8 body** arrives as a bare `HTTPException` in FastAPI's voice, bypassing the validation handler; and the extra-field message had "Nothing was saved" buried mid-sentence. Both now normalised, both pinned. |
| 36 | 2026-07-26 | **Request bodies use `extra="forbid"`; the stored document uses `extra="ignore"`.** | Asymmetric on purpose. Silently dropping an unrecognised *request* field is the worst failure available here: the panel says "Saved", the operator believes it applied, and the agent runs the old value on a live call. A 400 naming the field is a contract mismatch caught in seconds. The *document* is the opposite case — a file written by a newer build with a section this one does not know about should still yield the sections it does. |
| 37 | 2026-07-26 | **Config API auth is enforced through the bind address, with an optional `SHUO_CONFIG_API_TOKEN` — deliberately *not* `SHUO_ADMIN_TOKEN`.** Non-loopback host without a token is refused **at startup**. | Decision 16's rule is that operator endpoints fail closed, and this is squarely one — it decides what the twin says on a real call, a more interesting thing to hijack than a trace. But the panel sends no auth header today, and `SHUO_ADMIN_TOKEN` is very likely already set (decision 16 requires it for `/call`), so reusing it would mean enabling outbound calling silently breaks every save. Loopback is the boundary instead: bound to 127.0.0.1 there is no remote caller to authenticate and a token would be friction for no gain; bound anywhere else there is, so it becomes mandatory. Refused rather than warned, because a warning on a world-writable control panel is not a control. |
| 38 | 2026-07-26 | **The voice catalogue is backend-owned, measured, and enforced.** [config_store/voices.py](shuo/config_store/voices.py) maps public IDs (`el-george`) to provider IDs (`JBFqnCBsd6RMkjVDRZzb`); `GET /v1/voices` serves it in the panel's own `VoiceModel` shape; `PUT /v1/agent/config` refuses anything not in it. **Closes open question 11.** | The panel picked from one table and the API validated against nothing, so an unsynthesisable voice saved cleanly and failed on a live call — and per decision 29 that failure is not an error but *silence*, until the caller gives up. One table now feeds both halves, pinned by `TestThePickerAndTheValidatorAgree`. Three things fell out of building it. (a) **The list is measured, not remembered:** `scripts/getfreevocies.py` re-run 2026-07-26 synthesises one character through each voice and keeps what returns audio — 21 pass, 2 return 402, unchanged from 2026-07-23. Listing is not permission (decision 29). (b) **`el-rachel-v2` — the picker's own `defaultValue` — names a voice that is not in this workspace at all**, so the single most likely save in the system stored a voice that could never have been spoken. (c) **Strict on write, permissive on read.** `AgentSection` overrides the catalogue check because `ConfigStore.load` validates the file back through it and turns *any* failure into an empty document — so one stale voice ID would have silently discarded the system prompt, persona rules and knowledge block too. Entitlements change under us; the voice must fail loudly at call setup without taking the rest of the config with it. A raw provider ID is accepted as input and canonicalised to its catalogue ID on the way to disk. |
| 31 | 2026-07-23 | **Voice is temporarily `JBFqnCBsd6RMkjVDRZzb` ("George" — premade, British), in `.env` and as the [tts.py](shuo/services/tts.py) fallback. Conditionally reverses decision 26.** User's call, taken with the accent cost in view. **Revert to Krish the moment the plan is upgraded.** | Decision 26 made Krish the fallback so a missing env var would degrade to the *right persona* rather than the wrong one. That reasoning held while Krish worked; decision 29 removed the premise. The trade is no longer right-accent vs. wrong-accent, it is **wrong-accent vs. no audio at all** — and an off-accent turn is a bad turn where silence is no conversation. British is the least-wrong stand-in for Indian English among the 21 premade voices (none is Indian), but it remains a live character break against CLAUDE.md §4.3 and rules.md D3/D4, so the candidate persona cannot be honestly evaluated on accent or Hinglish register until this is reverted. Turn-taking, barge-in, played-ms truncation and latency are all unaffected and *can* be tested now. |
| 39 | 2026-07-26 | 🔴 **W2 — the audio process reads the config store, through one new module: [shuo/runtime_config.py](shuo/runtime_config.py).** `load_call_settings()` → `CallSettings(system_prompt, voice_id, …)`. Called **once per call, synchronously, after the carrier reader task is created and before the event loop starts** ([conversation.py](shuo/conversation.py)). `TTSPool`, `TTSService` and `LLMService` gained a `voice_id` / `system_prompt` parameter; `Agent` takes a `CallSettings`. **[shuo/state.py](shuo/state.py) untouched — no new events, no new actions.** | The gap this closes: W1/W1.5 shipped a config API that saved correctly to a file **nothing read**, so every Save was a no-op on a live call while the panel reported success. Five things settled while building it. (a) **Prompt assembly is prompt → `## Constraints` → `## What you know about yourself`, one system message.** Facts go last because knowledge can run to 50,000 chars and burying the behavioural rules underneath it is how the rules stop being followed. An empty section contributes *no heading* — a bare "## What you know about yourself" with nothing under it reads to the model as "you know nothing about yourself", which is a different instruction. (b) **Snapshot per call, never per turn.** A prompt that can change mid-conversation is failure mode 3 (context loss) introduced by our own plumbing; it also means an operator saving mid-call cannot alter the call in progress. (c) **The read must beat the TTS pool.** ElevenLabs binds the voice into the `stream-input` URL at connect time and `TTSPool` pre-connects, so a pool warmed before the read serves turn 1 in the env voice while the panel shows the operator's pick. The pool is therefore built at `StreamStart` rather than at loop setup, and is voice-scoped — **both** its construction sites take `voice_id`, because the cold path missing it presents as ElevenLabs being inconsistent rather than as a wiring bug. (d) 🔴 **`asyncio.to_thread` was tried here and is wrong.** The await it introduces is a *cancellation point* between accepting the media socket and building the `Agent`: a call that ends in that window tore down with no settings resolved, and it also delayed the frames the carrier replays for the top of the caller's first utterance. Four transport tests caught it. Decision 33 already made the read cheap enough to do inline (one file open, one `json.loads`, tens of µs, before any audio flows — the 20ms deadline only binds once playback has started), so it is synchronous and the docstring says why. (e) **An unresolvable voice falls back to the env voice and logs loudly, keeping the rest of the config.** Continues decisions 29/31 — off-accent audio beats silence — and completes decision 38(c): entitlements change under a file nobody edited, and the voice must fail without taking the system prompt with it. |
| 40 | 2026-07-26 | 🔴 **The test call goes through the real telephony path, and the two processes talk over HTTP on loopback — never by importing across the seam.** `POST /v1/test-call` on :3041 → `POST /call/{number}` on :3040, through [call_client.py](shuo/call_client.py). `SHUO_ADMIN_TOKEN` is read by **both** processes and never reaches the browser. | No browser-microphone path and no simulated audio, deliberately: what is being measured is real-world latency and audio quality over Indian telephony, and a WebRTC loopback in the operator's browser measures neither. The import ban is what keeps decision 32 true — a shared import would make the split a merge with extra steps and put a Save back on the loop that paces 20ms frames. Three things fell out of building it. (a) **The credential boundary is the feature's security argument**: the browser asks Next, Next asks loopback :3041, and only :3041 can satisfy :3040's operator gate (decision 16), so *reaching the panel is not the same as being able to place calls*. Pinned by `TestTheTokenBoundary`, and the mutation that deletes the header leaves every other test in the file green. (b) **The number is validated at :3041, not at the carrier** — `/call/{n:path}` prepends `+` to whatever it is handed, so a typo used to become an opaque 502 with the carrier's reason deliberately swallowed. (c) **A 10s cooldown, claimed *before* the request and released on refusal**: the button is one double-click from two calls, but a typo must not lock the operator out while they fix it. `POST /call/{number}` was added as the honest verb; the GET survives for the curl runbook. |
| 41 | 2026-07-26 | 🔴 **The live view is a bounded in-memory ring buffer in the call process ([call_monitor.py](shuo/call_monitor.py)), polled — not SSE, not a file, not the state machine.** `CallRecorder` publishes from the dispatch boundary; `GET /calls/live?since=` serves it; :3041 proxies. **[shuo/state.py](shuo/state.py) untouched — no new events, no new actions.** | A latency decision at every level. **Polling over SSE**: an SSE response is a resident task with keepalives on the event loop that paces a 160-byte frame every 20ms, and decision 24 removed the player's ability to claw lateness back, so a client that never disconnects leaves work on that loop forever. A poll costs tens of microseconds and then nothing, and costs *zero* when the panel is closed — and the :3041→browser hop can be SSE later without going near that loop, which is the point of the split. **Publishing is a `deque.append` and nothing else** — no disk, no lock, no `await`, no JSON; serialization happens in the read handler where a slow poll costs only the poller. Asserted, not assumed: 10,000 publishes under a measured ceiling. **It never raises** — a monitor that can end a call is worse than no monitor. Four design points worth keeping. (a) 🔴 **Interim caller text is a field, not an event.** Deepgram Flux emits `Update` many times a second; appending each would flush a real transcript out of a 200-entry buffer within seconds — live typing and no history, the opposite of what the panel is for. It is also never an `Event`: acting on interim text is how you answer half a question. The hook already existed on `FluxService` and was simply unwired. (b) **The agent's own text is joined once per turn, not per token** — the token loop is the streaming chain the whole latency argument rests on (CLAUDE.md rule 2), so it gets one `list.append` and nothing more; `+=` on a growing string would be O(n²) over a 20-45s answer. (c) **The sequence is global across calls**, so a panel's cursor stays valid when a second call starts; a per-call counter would replay or skip the whole buffer. `missed` is reported rather than hidden — judging whether the twin contradicted itself three turns ago is the reason the panel exists, and a transcript with a silent hole cannot answer that. (d) **No audio crosses the boundary** — not a sample, not an amplitude envelope. The visualiser is driven by state, so µ-law-end-to-end has nothing to say about W3 and the player is never asked for a copy of what it is pacing. |
| 42 | 2026-07-26 | **Hangup keys on the call id the monitor learned from the carrier's `start` frame, never on what `originate` returned.** `POST /calls/current/hangup?expect=<panel's id>`. | The `request_uuid` from `originate` is **not reliably** the `CallUUID` — [server.py](shuo/server.py)'s `/hangup` webhook handler already recorded that, and hanging up is the one operation where being wrong about which call this is ends someone else's conversation. So the panel hangs up "the live call", and `expect` guards the case ordering cannot: a panel left open on a finished call, pressed after a different one started, is refused rather than obeyed. A call whose `start` frame has not landed yet has no id to hang up by and says so (409) rather than guessing. The panel's close button already said "Hide", not "Close", so the two actions were never conflated in the UI either. |

| 43 | 2026-07-27 | 🔴 **The read half lands as one step across all three form screens: `GET /v1/agent/config`, `GET /v1/agent/persona`, `GET /v1/agent/knowledge`, each answering in the same shape its `PUT` returns plus `updatedAt`.** The panel's `DEFAULT_PERSONA_RULES` is demoted from `defaultValue` to the **unreachable-service** fallback — *not* the empty-response one. All four screens become `force-dynamic`. | The gap this closes is the mirror of W2's: the panel could write but never read, so a reload showed three empty textareas — which does not mean "nothing configured", it means "this screen does not know" — and an operator who then pressed Save wrote that emptiness over a working prompt. Four things settled while building it. (a) 🔴 **The reads serve `resolved_*`, not the raw section.** That is why they are not `GET /v1/config` sliced three ways: what the operator must see is what the *next call* would run with, which for persona rules is the ruleset on an install nobody configured. One property answers it (`ConfigDocument.resolved_persona_rules`), and it is the same one `runtime_config` reads, so the panel and the call cannot disagree. (b) 🔴 **An empty `rules` response now means "cleared on purpose", so the client must not re-seed its own copy.** The service already applies the default to a section that was never written; a frontend fallback on empty would resurrect the rules on every reload and write them back on the next save — undoing an operator's deliberate decision with a page load. The local copy survives for exactly one case: the service not answering at all, where an empty box would look like a configuration. (c) **Empty strings, never `null`.** React renders `null` in a `defaultValue` as a field with no value — the same blank box, reached by a different route. (d) **The outcome sentence became three-way on both sides.** A failed *read* must not end "Nothing was saved.", which describes a save the operator never asked for; `_outcome_for` now switches on method as well as path, matching `transport.ts`'s `outcomeFor` on the same boundary. A saved voice that no longer resolves is returned **unchanged** and the picker substitutes silently — the service is the only party that knows *why* an ID lapsed, and it says so on the next save. |
| 44 | 2026-07-27 | 🔴 **Completed calls are archived to an append-only JSONL file ([shuo/call_history.py](shuo/call_history.py), `var/call_history.jsonl`), written once per call in `conversation.py`'s teardown, and read *directly off disk* by :3041 — not proxied from :3040.** Per-call transcript and milestone lists live on `_Call` in [call_monitor.py](shuo/call_monitor.py), separate from the ring buffer. `GET /v1/calls/history?limit=` serves them; `/calls` renders them and `PLACEHOLDER_CALLS` is deleted. | **The direction of the seam is the decision.** W3's live view goes over HTTP because live state exists only in the call process's memory; history is on disk, and :3041 is the process that does disk-bound work (decision 32). Reading it directly means the call log loads with `main.py` **stopped**, which is the ordinary state of the machine when somebody sits down to review a call, and it adds no route to the app that paces 20ms frames — serving a hundred archived transcripts from :3040 would be a multi-megabyte JSON encode on that loop. No import crosses: `call_history` depends on `json`, `pathlib` and the logger, so the two processes meet at a file exactly as they do for configuration. Four points worth keeping. (a) 🔴 **The archive is not the ring buffer.** The ring holds 200 events across 3 calls, so a 40-turn call has evicted its own first question before it ends; an archive derived from the buffer at teardown would save the *tail* of a conversation and call it the conversation. Both are `list.append` on the same guarded write site, so the hot path is unchanged and nothing here touches disk during a call. (b) **The duration clock freezes at `ended()`, not at write time** — teardown closes the TTS pool, Deepgram and the trace in between, and a call log that reports a two-minute call as however long ago it was written is wrong in a way nobody would think to check. (c) **JSONL and skip-on-parse-failure**: a writer killed mid-append leaves half a line, and refusing to serve the other 199 records over it turns a lost row into a broken screen. Trim is triggered by `stat().st_size`, so the expensive rewrite lands on one teardown in a thousand. (d) **`startedAtLabel` is formatted server-side with locale *and* timezone pinned (`en-IN` / `Asia/Kolkata`)** — the decision the old `TODO` demanded. Pinning both is what makes server formatting safe: bare `toLocaleString()` would render the same call differently in ap-south-1, on a European dev box, and again on hydration. **Found while building it:** the suite was writing call records into the developer's real `var/` — 42 of them — because the write happens in teardown, which no test names. `tests/conftest.py` now isolates it autouse. `var/` is gitignored so nothing was going to be committed, but a suite that quietly accumulates transcripts in a working directory will one day do it on a machine holding real ones. |
| 45 | 2026-07-27 | **Conversational delivery — adaptive answer length, opening fillers, self-correction, no spoken structure, no backchannel-as-a-turn — is added to `DEFAULT_PERSONA_RULES` (data), not to the agent response loop (code).** Phase 6 remains not started. | Real test calls sounded rigid: uniform-length single blocks, no hesitation, "firstly/secondly" structure — failure mode 4 in CLAUDE.md §1, and the one the candidate persona exists to expose. The ruleset is the layer that can fix the *text* without touching the streaming chain, and it is the layer an operator can edit, so that is where it went. What was **not** done, deliberately: response-gap sampling and true backchannel suppression are *timing*, not text — they need a scheduled delay before playback and a way to swallow a caller's "mm-hm" without ending the twin's turn, which means new events, a timer in the loop, and interaction with `_TurnCompletion` and Bug A. That is Phase 6, and CLAUDE.md rule 7 says it needs approval before code. The prompt-level change is measurable today from the archived transcripts (decision 44), which is the right order: change the text, listen to a call, then decide whether timing work is still needed. |

| 46 | 2026-07-27 | 🔴 **Phase 8 W5a — every call *attempt* is recorded, not only the answered ones, and the call process may now write to disk while a call is running because it does so through a spool.** New: [shuo/spool.py](shuo/spool.py) (`submit` = `put_nowait`, one worker, `asyncio.to_thread`), [shuo/call_status.py](shuo/call_status.py) (the seven-status vocabulary), an **attempt id** minted before the carrier is called and signed into every callback URL, and revision-based rows folded by `call_history.load`. **606 tests green** (+58). The state machine was not touched; the streaming chain was not touched. | **The ask was "write a stub at `trigger_call`", and `trigger_call` is on the media socket's event loop** — so the literal instruction was the exact hazard decision 32 exists to prevent. :3041 could not do it either: it never sees an inbound call, and it never sees a call placed by `curl` from the runbook. Hence the spool: the producer does a bounded `put_nowait` and a counter (the cost class `call_monitor` is already held to) and all I/O happens on a worker thread. Six things settled while building it. (a) 🔴 **Nothing that existed could correlate a ringing call with the answered one.** `request_uuid` is documented as *not* reliably the `CallUUID`, `CallUUID` only exists once the media `start` frame lands — which on an unanswered call never happens — and `_Call.id` is minted when the socket opens. The attempt id is minted before the carrier is told anything and rides on the answer/ws/ring/hangup URLs; it is folded into the `/ws` HMAC because an unsigned one would let anyone who can reach the socket write a transcript into somebody else's row. (b) **A row is written many times and the file is still append-only.** An update is a new line with the same `id`; `load` folds field-level, not last-line-wins, because the hangup webhook and the call loop's teardown genuinely race and only the origination stub knows the number dialled. Status is resolved by **rank**, so a late webhook cannot rewrite a real conversation as `missed`. (c) 🔴 **A pre-existing bug fixed in passing:** teardown's `call_history.append` was blocking, justified by "no player is pacing frames" — true of *that* call and false of the process, so with a second call live it stalled the loop pacing its frames. It is spooled now, then `await`ed, which keeps the durability the blocking write had by accident. (d) **`call_status.py` exists because `call_monitor` is pinned to import nothing that can reach a disk** — importing the log writer for a string constant would have handed the hot-path observer the ability to fsync, and the existing test caught exactly that. (e) **`main.py`'s CLI was a second, untracked origination site**; `place_outbound_call` is now the only one, so a call from the runbook appears in the log like any other. (f) 🔴 **`originate` never sent `ring_url`/`hangup_url`**, so `/ring` and `/hangup` may never have fired at all. They are sent now; whether Vobiz honours them, and with which parameter names, is **unverified and needs one live call** (docs/phase8-plan.md §1.5). Degrades to the row staying `pending`. |
| 47 | 2026-07-27 | **The call log's vocabulary is seven statuses, and `cancelled` is deliberately distinct from `missed`.** `pending`, `ringing`, `in_progress`, `completed`, `missed`, `cancelled`, `failed` — [shuo/call_status.py](shuo/call_status.py). **This is a frontend contract change:** `call-log.ts` renders three and will meet four it has never seen. | The old three were *readings of how an answered call finished*, derived at teardown. Nothing described a call that never got that far, which is why an unanswered call produced no row at all — the log recorded conversations, not attempts, and an operator could not tell "I never placed that call" from "I placed it and nobody picked up". The two that had to be separate are `missed` and `cancelled`: missed is the callee not picking up, cancelled is the operator or the carrier stopping a call that was still ringing, and collapsing them makes "did I hang up on a ringing phone" unanswerable — which on a test-call log is one of the two questions anybody asks. The rank ordering is part of the decision, not an implementation detail: terminal beats transient, and `completed` beats every other terminal because it is the only one requiring positive evidence that somebody spoke. |

| 48 | 2026-07-27 | 🔴 **Phase 8 W5b — calls are recorded locally by teeing the µ-law the pipeline already holds, and served as a stereo WAV from :3041.** [shuo/recording.py](shuo/recording.py): caller teed at `conversation.py`'s `FeedFluxAction` dispatch, agent teed in `AudioPlayer._send_frame` before the base64; chunks flushed through the spool every 5s; converted to 8kHz 16-bit stereo (caller left, agent right) on the worker thread. `GET /v1/calls/{id}/recording` serves it with Range support. **Measured: 4.5µs per 20ms tick, 0.02% of the player's frame budget**, and the real pacing gate (50 fps, <1 frame drift) passes with the tape attached. **656 tests green** (+50), 4/4 mutations caught. | **The audio was already in memory in exactly the form we wanted**, so recording is a bookkeeping problem, not a capture one — which is why the local tee beat downloading the carrier's recording: no carrier credentials in a new process, no billable storage, works on any carrier, and it captures what *this pipeline* sent rather than what the carrier's bridge heard. The carrier's REST recording is untouched and still governed by `RECORD_CALLS`; the local one has its own switch, because turning off the billable copy must not take the free one with it. Six things settled. (a) 🔴 **The caller's byte count is the clock.** `process_event` emits a `FeedFluxAction` for every inbound frame in every phase — it must, because barge-in requires listening while the twin speaks — so the caller track's length *is* the call's duration, and the agent's frames are written at the caller offset current when each run began. (b) **Nothing is padded on the hot path.** A turn starting after ten seconds of listening needs ten seconds of silence in the agent track; each flushed chunk carries the offset it belongs at and the *worker thread* pads, so `_send_frame` never allocates beyond the frame it was handed. (c) **Memory is bounded by flushing, not by a cap on the call** — ~80KB resident whether the call is 20 seconds or 45 minutes. (d) 🔴 **`audioop` was removed in Python 3.13**, so the µ-law decode is a 256-entry table built at import; the test checks every entry against `audioop` where the interpreter still has it. The interleave is an extended-slice assignment on an `array`, not a Python loop: 21 million samples is the difference between a conversion that finishes during teardown and one that does not (measured: a 10-minute call converts in 0.54s). (e) **An id becomes a path in exactly one function**, guarded twice — by shape and by re-resolving inside the root — because these arrive from a carrier's callback URL and a browser's address bar. (f) **`/v1/calls/history` answers `recording` from the filesystem, not from the row**, so a recording deleted by the retention budget stops being offered rather than leaving a play button that 404s. **Found while building it:** the first full suite run left a directory of WAVs in the developer's `var/` — the identical trap decision 44 found with call rows, one turn worse, because these are *the audio of conversations*. `tests/conftest.py` now isolates the recordings directory autouse, next to the call log. |

| 49 | 2026-07-27 | 🔴 **Phase 8 W5c — the live view became plural: one event ring *per call* (8 concurrent), `GET /calls/active` on :3040, and `GET /v1/calls/active` on :3041 that unions it with the non-terminal rows on disk.** [call_monitor.py](shuo/call_monitor.py) moves `events` onto `_Call` and keeps `_seq` global; [config_api.py](shuo/config_api.py) gains the merge, `/v1/calls/live` as the general form of the live view, and `/v1/test-call/status` as a delegating alias. **Polling only on both hops — no SSE, no WebSocket, no resident task in either process.** **Measured: 1.43µs per publish in the worst case (ring full, evicting on every append) — 0.007% of the 20ms frame budget; `summaries()` 8µs over 8 calls.** **705 tests green** (+49), 6/6 mutations caught. | **One shared 200-event ring was correct for exactly as long as there was one call.** With two, a busy line flushed a quiet one's opening turns out of the buffer from the other side of the event loop, so the transcript an operator was reading lost its beginning for reasons that had nothing to do with that call — and the whole point of the panel is judging whether the twin contradicted itself three turns ago. Five things settled while building it. (a) 🔴 **`missed` had to become per call, and it is the subtle half of the change.** It was `oldest_seq_in_the_buffer - since - 1`, which counts lost events only while the sequence numbers in the buffer are contiguous; with one ring per call they are not, so that arithmetic would have reported the whole of call B's traffic as holes in call A's transcript and every concurrent call would have accused the others of losing its events. It is now two ints maintained at the write site. It can over-report for a cursor behind the eviction point, which is the same safe direction the old form erred in. (b) **`_seq` stays global while the rings went per call**, and the two are not in tension: the deque decides what a call keeps, the sequence decides what a panel has already seen. A per-call sequence would reset to zero on the next call and a panel holding cursor 40 would either replay everything or skip it all. (c) 🔴 **A ringing call exists only on disk**, because it has no media socket — so a live-only view is blank for the entire window an operator sits watching, and `/v1/calls/active` is the one route in the repo that reads both sides of the split. Nothing new crosses the seam: :3040 over loopback HTTP as W3 already did, and the log off the same disk as W4 already did. (d) **A non-terminal row is evidence of a call in flight, not proof of one** — it stays non-terminal forever if the writing process was killed between origination and teardown, so one `kill -9` would otherwise leave a phantom ringing call in the panel for the life of the file. Hence a **600s staleness window**, from which :3040 saying "this socket is open" exempts a row: a long call is not a phantom. (e) **`expect` on `/calls/current/hangup` now *selects* the call instead of merely asserting about it.** `MONITOR.current()` means "the most recently opened live call", which was unambiguous with one call and is not with eight: an operator ending the interview they were watching, on a box that had since answered the receptionist line, would have hung up the receptionist. **Frontend not yet wired:** `/v1/calls/active` returns the seven-status vocabulary and needs the same badge fallback decision 47 already flagged. |

| 50 | 2026-07-27 | 🔴 **Phase 8 W5d — an opt-in push notification on the calls nobody was watching: [shuo/notify.py](shuo/notify.py), in :3041, off unless `SHUO_NOTIFY_URL` is set.** ntfy.sh by default (`POST https://ntfy.sh/<topic>`, no account, no card, free forever, real push via their app). Fires on three transitions only — **inbound started, missed, failed** — from a ~1Hz background task started by :3041's lifespan. `/health` gains a `notifications` block. **757 tests green** (+52), 14/14 mutations caught, and wire-tested against a loopback stand-in for ntfy (16/16 checks) rather than only through `TestClient`. | **No free permanent SMS to an Indian handset exists** — A2P needs DLT principal-entity registration, which CLAUDE.md §2.1 puts out of scope, and every provider trial is credit-limited, expiring and card-gated, so anything built on one stops working without warning or starts billing. ntfy needs none of it. Five things settled. (a) 🔴 **The plan said poll `/calls/active`; it polls the *call log* instead.** That route answers "what is in flight" and therefore *excludes* terminal rows by design (decision 49), so `missed` and `failed` — two of the three triggers — would only ever have appeared there as a call quietly vanishing. The log is where a terminal status is actually written, and reading it means the notifier needs nothing from :3040 at all: it keeps working while the call server restarts, which is precisely a window in which calls go missed. (b) 🔴 **The topic name is the entire credential**, so it is never logged and never returned: every mention goes through `redacted_target()`, `/health` shows `https://ntfy.sh/(redacted)`, and a test asserts that not even a *prefix* of it survives. (c) **It primes silently on the first tick.** Without that, a restart pages the operator about every call already in the log — the notification equivalent of replaying the transcript, which is the same bug the panel's cursor exists to avoid. (d) **Two rate limits, because they stop different things**: one notification per call per transition (a flapping carrier describes one ringing phone, not four — and the log's own status *rank* absorbs the backwards steps before this module sees them), plus a global 10-per-minute ceiling, dropped and counted rather than queued. A transition is marked attempted **before** the network, so a vendor outage costs one request rather than one per second. (e) 🔴 **It is the only thing in the system that sends call data off the machine.** It carries metadata — direction, number, persona, duration — and **no transcript**, which is the line between a notification and a data export; a test pins it. Off by default for the same reason. **Found while building it:** a **pre-existing durability bug in the call log** — a torn final line has no terminating newline, so the *next* `append` landed on the same line and fused the two into one unparseable record, losing the live row as well as the historical one, against the guarantee the JSONL format exists to provide. Healed in `append` with one seek and one byte. It had no symptom before W5d; here it was a push that never arrived. |

| 51 | 2026-07-28 | 🔴 **Phase 8 W5e — a call that ends now *reports* that it ended.** A declined outbound/test call left the panel frozen on its ringing screen: `/v1/calls/live` answered `{status: "", state: null, endedReason: "", live: false}` forever, and `/v1/calls/active` kept saying `ringing`. Four changes, in [call_status.py](shuo/call_status.py), [server.py](shuo/server.py), [call_history.py](shuo/call_history.py) and [config_api.py](shuo/config_api.py). **777 tests green** (+14), and wire-tested across two real uvicorn processes: the terminal state is visible **15ms** after the carrier's webhook. | Four independent reasons the UI could not see the end, and every one of them had to go. (a) 🔴 **`/v1/calls/live` never read the call log.** It proxies :3040's in-memory monitor, and a call that is *ringing* or *declined* never opens a media socket — so the monitor has never heard of it, and the route returned the same empty payload while the phone rang and after the far end hung up on it. Nothing in the response ever changed, so a poll-driven client had no transition to see. It now falls back to the log, which is the same file `/v1/calls/history` already reads on the process that exists to do disk-bound work (decision 32) — no new import crosses the seam. (b) 🔴 **A terminal call vanished from `/v1/calls/active` instead of appearing as terminal.** That route lists calls in flight, so the row disappeared on the poll after it ended — indistinguishable, to a 1Hz client, from a failed request. A **20s grace window** (`TERMINAL_GRACE_SECONDS`) keeps it listed with `live: false` and a terminal `status`, so the transition is *observed* rather than raced past. (c) **`declined` is now a status of its own**, split out of `missed`, with `endedCode` (`remote_declined`/`remote_busy`/`no_answer`/…) as the machine-readable half beside `endedReason`'s prose — a panel cannot branch on a sentence, and `notify.py` renders that sentence to a human. `declined` outranks `missed` in `STATUS_RANK` because it rests on positive evidence where missed is inferred from an absence. (d) 🔴 **The carrier may never send the hangup webhook at all** — `hangup_url` is still unverified (decision 46) — so a row could sit `ringing` for the full 600s `ACTIVE_WINDOW_SECONDS`. `RING_TIMEOUT_SECONDS` (180s) now *derives* `failed`/`no_carrier_response` from silence. **Derived, never written**: nothing on disk is rewritten on a guess, so a late webhook still folds normally and the next poll reports the carrier's real outcome. 180s, not 120s, because Plivo — whose REST API Vobiz clones — defaults `ring_timeout` to 120s, and reporting a live call as dead while the phone is still in someone's hand is a worse failure than the phantom it replaces. **Found while building it:** writing `endedReason` from the webhook exposed that the fold protected the *status* by rank and left the sentence beside it last-write-wins — so a decline landing after teardown would have left a row reading `completed` with "the far end declined the call" next to it. `call_history.merge` now makes the ended pair follow the status that won, in either arrival order, and **drops** the loser's rather than keeping it as a fallback: a reason that contradicts the status is the one shape an operator cannot reason about. **Still polling, still no SSE** — decision 24's arithmetic on :3040 is unchanged, and at 15ms end-to-end there is nothing a push channel would buy. |

### Verification posture

Phase 1 was checked three ways beyond the unit suite:

1. **A 6-lane adversarially-verified research pass** (80 agents) pinned the Vobiz wire format; 28 documented traps were checked against the implementation, which was already correct on 24. See [rules.md §3](rules.md).
2. **A 7-dimension adversarial code review** (164 agents, each finding refuted by 3 independent verifiers) produced 14 confirmed findings from 52 candidates — all fixed. 38 were refuted.
3. **Mutation testing** on the disconnect handler exposed a real coverage gap: deleting the `StreamStopEvent` on `WebSocketDisconnect` left all 156 tests green, even though it is the *only* thing that ends a Vobiz call. TestClient masks it by cancelling the task on context exit. `TestCallTermination` now drives `run_conversation` directly and fails with `TimeoutError` under that mutation.

**Applied again to the config layer (decisions 32–37), and it paid twice.** 16 mutations were run against the 93 new tests. The first pass caught 14: one **survived** — dropping the timestamp from a save — because the only stamp assertion covered `knowledge` and the stamps are three separate call sites; and one **skipped** on a bad anchor, which is its own lesson, since a mutation that fails to apply reports as a pass if you are not counting. With a stamp assertion covering all three sections, **16/16 are caught**. Separately, two defects were found only by curling the *running* service and never by the suite: an invalid-UTF-8 body arriving in FastAPI's voice, and "Nothing was saved" buried mid-sentence in the extra-field message. **A suite that only ever calls `TestClient` does not test the wire.**

**Lesson worth keeping:** a green suite is not evidence of correctness until a mutation proves the suite can fail. Apply this to Phase 5's player rewrite, where the pacing assertions will be easy to write tautologically.

**Applied again to W3 (decisions 40–42): 18/18 caught on the first pass** — but only after the mutation list itself found a hole. Two of the three tests covering the agent's transcript set `_response` by hand, so *deleting the token accumulation entirely* would have left them both green while the panel showed a caller line and no reply on every single turn. The mutation "agent.py: the token loop stops accumulating" was written before the test that catches it, and writing it is what exposed the gap; `test_the_token_loop_actually_accumulates` now drives the real `_on_llm_token`. **The lesson generalises: a test that constructs the state under test bypasses the code that produces it.** The other 17 covered the credential header, the cooldown claim/release pair, the cursor, interim-text flooding, "never raises", the hangup guards, the operator gate on `/calls/live`, and each of the five publish sites in the call loop individually — a publish site is exactly the kind of one-line wiring that fails silently and shows up as a blank panel during a live call.

**And the wire found what `TestClient` could not, again.** Both services were started and curled: the invalid-UTF-8 body arrives on `/v1/test-call` as a bare `HTTPException` in FastAPI's voice (the same trap as decision 35) and must be re-enveloped with "No call was placed" rather than "Nothing was saved" — a route that saves nothing reporting on saving is reporting on something the operator never asked for. Also confirmed live: the `:3041 → :3040` proxy, the operator gate answering `forbidden` vs `operator endpoints disabled`, and `test_call_ready` on `/health` as the one-line diagnosis for "the button does nothing on a fresh machine".

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
7. **Candidate persona fact block** — the résumé/profile the twin is grounded on. Needed before persona testing can start. **Partly unblocked:** the control panel's `/knowledge` screen now has somewhere to put it (`PUT /v1/agent/knowledge`), so the content is the only thing still missing.

11. ~~🔴 **`voiceModel` from the panel is not an ElevenLabs voice ID.**~~ **Closed 2026-07-26 by decision 38** — backend-owned catalogue in [config_store/voices.py](shuo/config_store/voices.py), served by `GET /v1/voices`, enforced by `PUT /v1/agent/config`. One thing still needs the *frontend*: the picker must fetch that endpoint instead of rendering `PLACEHOLDER_VOICE_MODELS`, because until it does, its default (`el-rachel-v2`) is refused on every save.

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

## 2026-09-14 — Phase 4B shadow speculation live milestone

Phase 4B shadow speculation was exercised on the validated Bluetooth reference
path after offline gates passed (32 focused tests and 86 Bluetooth tests).

Live result: 10 final turns produced 0 ready-before-final shadow results and
10 not-ready-by-final results; 4 eager candidates were retracted by
`TurnResumed`. One retracted candidate reached first token at 500.8ms and was
correctly discarded.

Decision: retain Phase 4B as shadow-only and do not advance to Phase 4C
prepared-response reuse from this evidence. Next latency work should address
the measured TTS 8-second warm-pool churn and investigate an earlier safe
speculative trigger.

## 2026-09-14 — TTS warm idle boundary correction

Owner-supplied controlled live evidence: warm reuse succeeded at 10,544ms
(255ms TTS), 11,078ms (260ms) and 17,451ms (258ms). Checkout at 19,819ms caused
a caller-visible silent turn with ElevenLabs `input_timeout_exceeded` (20-second
input timeout). The old eight-second eviction was too aggressive; unlimited-age
reuse was disproven. Revised decision: a separate 15-second maximum safe idle
age, liveness checks, proactive expiry/refill, no synthetic keepalive input.
The margin and latency improvement await another controlled live validation.
Phase 4C remains deferred. See docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md.

## 2026-09-14 — Initial TTS readiness correction

Owner-reported first-turn `TTS 4132ms setup` occurred while background warmup
completed almost simultaneously. Source confirmed `TTSPool.start()` returned
after task creation, letting early checkout open a duplicate cold connection.
Decision: keep start nonblocking, await explicit bounded readiness in Bluetooth
Agent creation, and have initial checkout join warmup for shared callers.
Startup cancellation/failure remains pool-owned and cleaned up; 15-second idle
behavior is retained. Offline validation is not proof of live latency improvement.

## 2026-09-14 — Final controlled TTS warm-pool validation

Task-owner supplied live results: initial warmth preceded caller audio
forwarding; first turn used warm TTS with 0ms setup instead of the prior 4132ms
cold setup. Reuse succeeded at 10.164s, 12.995s and 14.629s idle. Over-age sockets
were evicted/replaced at 15.000–15.001s, with no over-limit checkout and no
`input_timeout_exceeded`. Later `quota_exceeded` was provider-account exhaustion,
unrelated to pool design. Retain the 15-second policy and readiness barrier.
Only connection/setup behavior is proven, not lower ElevenLabs synthesis latency.
See docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md for scope/limits.
Phase 4/4D acceptance and Phase 4C/later-phase authorization remain unchanged.

## 2026-09-15 — Earlier shadow transcript experiment

Implemented the task-owner-authorized Phase 4B follow-up: opt-in repeated Flux
Updates can trigger the isolated first-token probe before eager/final EOT.
Decision: require identical full text across 200ms, 3+ words and <=2000 chars;
limit early mode to two attempts per turn and 1s between attempts, including eager
fallback. Mutation/resume cancels; no replacement overlaps unfinished closure.
Final EOT always runs normal Agent generation. No speculative TTS/output reuse,
private-content telemetry, dependencies, real calls, commit or push.
Offline evidence and limits are in docs/TESTING.md and the Phase 4 realtime record.
Live usefulness remains unknown; Phase 4C remains deferred. Source audit also
found production admission is per call, despite historical global-gate plans.

### 2026-09-15 — Phase 4B.1 admission review

Reviewed content-free metadata from the owner's `/tmp/bt-phase4b1-shadow.log`:
9 eager generations, zero interim triggers. Existing telemetry cannot identify
repeat/word/order rejection or establish that early mode was enabled. The helper
omits the early flag, a conditional explanation pending launch provenance.
Added diagnostic-only receipt/delivery/admission records; thresholds and shadow
semantics remain unchanged. No live calls or Phase 4C. Detailed evidence and next
controlled test: `docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md`.

Decision log addendum: measure the actual Update admission failure before tuning
thresholds or changing speculative behavior; do not infer it from Groq HTTP 200
or eager-only generation records.

### 2026-09-15 — Phase 4B.1 measured repeat revision

Decision log addendum: after reviewing allowlisted timing/admission metadata in
`/tmp/bt-phase4b1-diagnostics.log`, lower only the identical confirming-Update
minimum from 200ms to 100ms. A 111.629ms post-resume repeat preceded the next eager
by ~156ms but changed ~118ms later; the useful-lead/waste tradeoff needs a new
controlled shadow comparison. Preserve shared 1s cooldown, two attempts/turn,
matching-eager deduplication, invalidation and normal final Agent behavior.
No speculative TTS, Phase 4C, dependencies, live calls/devices/providers or
commit/push. Source/tests plus the Phase 4 realtime record own current evidence;
docs/TESTING.md records regression results. Prior worktree changes preserved.

### Bluetooth barge-in investigation — 2026-09-15

Reviewed the newest matching live diagnostic log: 9 finals → 9 normal starts →
9 TTS/audio dispatch completions, zero normal Agent cancellations; all four
resumes precede final EOT. Audible cut/missing-answer root cause remains unknown.
Added content-free lifecycle diagnostics and real Agent/player synthetic restart
coverage with stale shadow work. Decision: preserve thresholds and cancellation
semantics pending a captured failure; Phase 4C/6 do not advance. See
[full evidence](docs/BLUETOOTH_BARGE_IN_INVESTIGATION.md) and
[test results](docs/TESTING.md#bluetooth-barge-in-lifecycle-investigation--2026-09-15).

## 2026-09-15 — Phase 4C repository-verified implementation

Task-owner authorization advanced Phase 4C implementation while explicitly
deferring real-call/provider/device testing until repository automation passed.
A default-off prepared-response seam now retains at most a first-token-ready LLM
stream, promotes it once only after exact final transcript/history/prompt match,
and otherwise falls back to ordinary final-EOT generation. TTS remains
non-speculative and carrier/browser/default startup is unchanged.

GitHub Actions run `34966008520` passed compile, focused Phase 4, complete Bluetooth,
full baseline-aware regression and diff validation; verified source commit:
`bc9996f727bf4ff7870d6f112e73200d82a49b87`. This supersedes the earlier *implementation authorization* stop but
not the historical BT-D23 evidence or Phase 4 live-acceptance gate. No latency or
caller-heard improvement is claimed until final real-call validation is possible.

## 2026-09-15 — Phase 4D repository hardening milestone

Task-owner authorization to continue Phase 4 through 4D was applied without
advancing later phases. Revision `b56a38b132951b322ed5d63059a42f0a7600f829` adds default-off bounded TTS
phrase grouping, provider-visible history budgeting with full system/digital-twin
prompt and canonical history preservation, optional content-free Groq usage timing,
fail-clean parallel Bluetooth startup, and a rules-C5 two/three-frame player
pre-roll A/B control. The earlier validated 15-second TTS warm-pool policy was not
changed.

Automated evidence: GitHub Actions `34968119711` passed 195 focused tests and
149 complete Bluetooth tests; the full root result was 976 passed / 4 failed / 4
warnings, with only the exact documented baseline failure identities/signatures.
An initial full run exposed a new `Agent.__new__()` compatibility regression; it
was fixed and the entire relevant gate was rerun rather than bypassed. No live
provider/device/cellular call was performed. Phase 4 remains open only for
controlled real-path acceptance evidence; no new default or latency claim and no
Phase 5 advancement follows from this repository milestone.
## 2026-09-23 — rootless Android ADB cellular synthetic-caller TX milestone

Task-owner supplied experiments on itel P683L/Android 13 proved a unique
`TYPE_TELEPHONY` output is visible to shell UID 2000 and that an AudioTrack
request actually routes to telephony type 18 during `MODE_IN_CALL`. Tone,
Pocket-generated speech and live binary PCM sent over USB ADB stdin were heard
clearly on the remote Galaxy A10 over the real cellular network. A final stream
ended with `STREAM_DONE` / `ADB_EXIT=0`.

Decision: codify only the proven transmit half as an isolated dev/benchmark
harness using the existing Pocket provider seam and `BluetoothOutboundCodec`.
Keep carrier/core mu-law unchanged, preserve manual call control and no raw-audio
persistence, do not depend on Vobiz for this path, and label caller-heard latency
unmeasured. Local timing observed Pocket-ready 531.8ms and first PCM 630.2ms from
probe-process start; these are not remote latency evidence. Reverse cellular
downlink capture is the next evidence gate and is not claimed implemented.
Repository verification for the Android ADB TX milestone: GitHub Actions run
`35847203988` passed Python 3.12/3.14 compile, Java-to-DEX build, 17 focused
tests, 162 Bluetooth tests, full-suite `1016 passed / 4 expected failed` exact
baseline verification, and full branch diff validation. The verified source tree
still makes no reverse/downlink or caller-heard latency claim.
## 2026-09-23 — reference cellular downlink capability proven

Upstream scrcpy 4.1 was run against the itel P683L during a manually answered
real cellular `MODE_IN_CALL` session with
`voice-call-downlink --require-audio`. Galaxy A10 speech reached Ubuntu
clearly; after Ubuntu playback was moved to headphones, the owner reported
**clear, no echo**.

This proves the reference Android runtime can expose real cellular downlink
through shell-UID `VOICE_DOWNLINK` capture. A SHUO-owned no-file receive bridge
candidate is now implemented on a feature branch using the same proven
PCM16/48k/stereo capture shape and an in-memory conversion to the unchanged SHUO
mu-law/8k boundary. Its own reference probe remains the explicit promotion gate;
no caller-heard latency or automatic call-control claim follows.
RX candidate correction: the first SHUO `doctor` stopped on a false-negative
permission classification because it expected dangerous/runtime
`RECORD_AUDIO` in the privapp allowlist. AOSP source confirms
`RECORD_AUDIO` and privileged `CAPTURE_AUDIO_OUTPUT` have different grant
classes. The branch now checks the privileged allowlist and the explicit runtime
package grant separately, still fail-closed. The SHUO helper itself has not yet
run on the reference phone, so its live gate remains pending.
## 2026-09-23 — SHUO-owned cellular RX reference PASS

The repository-owned `TelephonyRxBridge` has passed its independent itel
P683L live gate during a manually controlled real cellular call:
1,519,616 PCM bytes / 371 chunks / 7.915 s, peak RMS 4300, average RMS 1541.0,
and 63,318 bytes converted in memory to the existing SHUO mu-law/8k boundary.
The probe explicitly persisted no raw audio and logged no caller speech content.

Caller-side Android cellular TX and RX transports are now both independently
reference-validated. The next missing product/test layer is a closed-loop
synthetic-human controller that listens via RX and replies via TX; automatic
dial/answer/hangup is still out of scope and Phase 5 remains not accepted.
## 2026-09-23 — closed-loop real-cellular caller candidate prepared

After independent itel ADB TX and RX PASS evidence, an isolated Phase-5
controller was implemented to listen to real SHUO cellular downlink through RX,
advance a deterministic script with a private Deepgram Flux observer, and reply
through the existing TX bridge using pre-synthesized Pocket audio.

The scenario includes normal turns, a 650 ms prepared thinking pause, two
barge-in attempts and continuity facts, with a 300-second cap. Response text is
never serialized and raw audio is never persisted. GitHub Actions run
`35853641593` passed 36 focused tests, 162 Bluetooth tests and full-root
`1035 passed / exact 4 historical failures` on Python 3.12/3.14.

This is not yet merged or reference-qualified. One combined itel/Galaxy/SHUO
live run is the explicit next gate. Call answer/hangup remain manual.
## 2026-09-23 — first combined closed-loop live attempt stopped at Bluetooth preflight

The itel ADB caller side was connected and `MODE_IN_CALL`; Galaxy A10 was
paired/trusted/connected over Bluetooth. The Galaxy-side
`scripts/run_bluetooth_ai.py` failed before SHUO session startup with
`PipeWireSelectionError: no compatible Bluetooth downlink target for
04:BA:8D:42:97:B1`.

This is currently a PipeWire/HFP readiness or capability-discovery question, not
evidence against the Android RX/TX transports or the new closed-loop controller.
Do not relax capability matching. Capture the content-free live PipeWire graph
while the call is active and identify whether the SCO node is absent or which
required property differs.
### 2026-09-24 — two-device closed loop and latency seam

- Owner-supplied real itel P683L USB/ADB <-> Galaxy A10 Bluetooth/SHUO cellular
  run completed the deterministic scenario with 10 observed remote response EOTs.
- Closed-loop completion, seed response, 650 ms thinking-pause behavior, both
  interruption-send-while-remote-speaking checks, and first barge-in fruit
  continuity passed.
- Second barge-in codeword continuity and final fruit continuity failed; overall
  supplemental result remains FAIL. Phase 5 is not accepted and Phase 6 remains
  unauthorized.
- Owner authorized per-response latency instrumentation.
- Feature-branch controller now records each response from host paced caller-TX
  completion to itel VOICE_DOWNLINK Flux-observer StartOfTurn, plus min/average/max.
- Measurement is explicitly `MEASURED_HOST_CORRELATED`; observer delay is included,
  no acceptance threshold was invented, and
  `CALLER_HEARD_LATENCY=NOT_MEASURED` remains unchanged.
- Barge-in replacement baselines are captured before interrupt TX so a fast
  replacement StartOfTurn cannot be skipped merely because it arrives while the
  prepared interrupt audio is still being streamed.
