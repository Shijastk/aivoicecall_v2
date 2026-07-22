# Indian Telecalling Voice Agent — Technical PRD & Migration Plan

*Research basis: two adversarially-verified research workflows (18 lanes, ~120 agents, 102 primary-source claims, 46 refutations) run 2026-07-21. FX assumption throughout: 1 USD = ₹88. Duty-cycle assumption: a connected conversation-minute is ~45% agent speech, ~35% caller speech, ~20% silence.*

---

## Context

`shuo` is a ~1,400-line Python voice-agent framework achieving sub-second turn-taking over Twilio Media Streams via Deepgram Flux → Groq → ElevenLabs. The architecture is genuinely good — a pure, unit-tested state machine ([state.py](shuo/state.py)) driving an event/action loop ([conversation.py](shuo/conversation.py)), with every stage streaming into the next.

It is built for a US/EU deployment calling US numbers. Every vendor endpoint is in Virginia or Frankfurt, the STT model is Western English, and the telephony layer is Twilio — which **cannot legally place outbound calls to India from an Indian number at all** (Twilio's own India guidelines: *"Outbound calls to India can only be made from international (non-Indian) numbers"*), and costs ₹4.36/min to Indian mobiles, consuming 87% of the budget on its own.

**Goal:** re-target this codebase at Indian outbound telecalling — Indian-accented English, a cloned founder voice, the lowest achievable latency, and all-in cost under ₹5/min (target ₹3/min) versus Retell AI's verified floor of ₹6.42/min.

### Confirmed constraints

| Decision | Answer | Consequence |
|---|---|---|
| Volume | Pilot, <50k min/month (~5–25 concurrent) | Managed APIs, not self-hosted GPUs. Self-hosting only wins above ~3–8 lakh min/month. |
| Languages | Indian English only at launch | Widest vendor choice. Must still degrade gracefully on the Hindi words in any real Indian call. |
| Telephony | Own **Vobiz** SIP trunk + Indian DID | Biggest architectural change — and the source of the best news in this document (§2.1). |
| Refactor | Keep the loop, swap providers | `process_event` and the 308 lines of tests survive. Work goes into a provider layer plus state-machine extensions. |

---

## Part 0 — Read this first: two findings that change the brief

### 0.1 🔴 The plan is legally blocked before it is technically blocked

Cold outbound promotional calling from an ordinary 10-digit DID is **not legal in India in 2026**, regardless of how good the AI is. Verified against the consolidated TCCCPR 2018 (as amended to 21 May 2026) and TRAI Press Release 91/2026 (10 July 2026):

- **Schedule-I item 2** requires the Calling Line Identity for Promotional/Transactional/Service voice calls to come from the **140xx series**. TRAI restated on 10 Jul 2026: *"TRAI has mandated use of 140xx series numbers for making promotional calls by entities of any sector."* (1600xx is BFSI/Government-only, to *existing* customers.)
- TRAI has explicitly named this exact pattern as the abuse it is targeting: *"many entities have started making promotional calls using 10-digit mobile/landline numbers… also resorting to the use of Auto Dialer / Robo calls… bypassing regulatory provisions."*
- Your Python agent is, by definition, an **"Auto Dialer"** and a **"Robo call"**. **Reg 4** requires advance *written* notification to the Originating Access Provider that you use them, and of their objective.
- **Reg 25 sanction ladder:** first violation = outgoing services barred on **all** your telecom resources *"including PRI/SIP trunks, SIMs etc."* by **all** access providers for 15 days — whether or not those resources were used. Second violation = 1-year disconnection, blacklisting, and device blocking.

**Two hard engineering requirements fall directly out of this** and belong in the code, not a compliance doc:
- Abandoned calls ≤ **3%** of attempts over any rolling 24h; silent calls ≤ **1%** (Schedule-IV item 3). A slow TTS cold-start *is* a silent call. Instrument and alarm on both.
- Default permitted calling window ≈ **10:00–21:00 IST** (Schedule-II bands 00–06, 06–08, 08–10, 21–24 are default-OFF for every subscriber). Hard gate in the dialer scheduler.

Also: DND scrubbing **cannot** be done in-process — it runs as a pre-dial batch against a registered Scrubbing Function which returns tokenised virtual identities. Consent must be captured through the operator's CCAF with OTP against a registered Consent Template.

Registration is **not an API signup**: Schedule-I item 1(4) mandates physical verification of the entity plus biometric authentication of the authorised person. Published fees (Vi VILPOWER, the only operator publishing them): ₹5,000 non-refundable per function, plus ₹50,000 refundable deposit for a Telemarketer Delivery Function.

**Good news, verified:** there is **no Indian rule requiring disclosure that the caller is an AI** as of 21 Jul 2026 — keyword scans of the consolidated TCCCPR, the 2024 review consultation and the March 2026 Draft Third Amendment return zero hits for "synthetic", "deepfake" or any caller-identity disclosure duty. India's synthetic-media rule (IT Amendment Rules, G.S.R. 120(E), 10 Feb 2026) binds *intermediaries* for published content — it catches your demo reels and published call recordings, not the calls themselves. And DPDP 2023 uses a **negative list** for cross-border transfer: US-hosted STT is lawful (no restricting notification for the US), voice is *ordinary* personal data with no special localisation tier. **So ap-south-1 is a latency choice, not a legal one** — unless you serve RBI-regulated clients.

> **🔴 BLOCKING DUE DILIGENCE:** research could not confirm whether Vobiz holds a DoT Unified Licence or UL-VNO authorisation, or whether it can allot **140xx** resources. An unlicensed or grey-route trunk exposes you to disconnection independently of TCCCPR. Ask Vobiz for their licence number and 140xx allotment capability **before writing code against them.**

### 0.2 🟡 "<400ms end-to-end" is not achievable in India and should be re-specified

No one achieves it. The single most credible published India number is LiveKit's own, from a vendor motivated to publish a good one: **~1.67s end-to-end** with GPT-4o + Cartesia + co-located Deepgram, in their Mumbai region (blog 14 Feb 2026). Retell markets ~600–800ms for *US* traffic. Artificial Analysis measures no hosted speech-to-speech model under 500ms time-to-first-audio even before network.

The physics don't move: fibre propagates at ~204,000 km/s, and a voice turn makes three sequential vendor round trips. Azure's July 2026 P50 matrix gives Central India → East US at **202ms**, → West Europe **138ms**, → Southeast Asia **59ms**, → South India **23ms**. Those US figures are already at the physics limit — there is no engineering fix, only relocation.

**Re-specify the target as three separate numbers:**

| Metric | Target | What it is |
|---|---|---|
| **Server-side turn latency** | **300–450ms** | EOT detected → first TTS byte queued. This is what we control and can hit. |
| **Mouth-to-ear** | **500–750ms** | Caller stops speaking → caller hears audio. Includes PSTN legs and the unretractable handset de-jitter buffer. |
| **Perceived latency** | **~250ms** | What the caller experiences once gated fillers mask the gap (§5). |

This is the honest framing, and it is still substantially better than every managed competitor operating in India.

---

## Part 1 — Current-state analysis

### 1.1 Module map

| File | Role | Fate |
|---|---|---|
| [shuo/state.py](shuo/state.py) | Pure `process_event` state machine, 2 phases | **Keep, extend to 3 phases** |
| [shuo/types.py](shuo/types.py) | Immutable State/Event/Action | Extend |
| [shuo/conversation.py](shuo/conversation.py) | The event loop | Keep structure, add event sources |
| [shuo/agent.py](shuo/agent.py) | Per-turn LLM→TTS→Player pipeline | Heavy modification |
| [shuo/services/flux.py](shuo/services/flux.py) | Deepgram Flux (STT + turn detection) | **Replace** → `stt/` + `turn/` |
| [shuo/services/tts.py](shuo/services/tts.py) | ElevenLabs WS | **Replace** → provider interface |
| [shuo/services/tts_pool.py](shuo/services/tts_pool.py) | Warm connection pool, TTL eviction | **Keep pattern**, generalise |
| [shuo/services/player.py](shuo/services/player.py) | Paces audio to Twilio | **Rewrite** (pacing + played-ms) |
| [shuo/services/twilio_client.py](shuo/services/twilio_client.py) | Outbound call + WS parsing | **Replace** → `carrier/vobiz.py` |
| [shuo/services/llm.py](shuo/services/llm.py) | Groq streaming | Modify (provider, prompt, caching) |
| [shuo/tracer.py](shuo/tracer.py) | Per-turn span tracing | Keep; fix path, export metrics |
| [shuo/server.py](shuo/server.py) | FastAPI + TTFT bench harness | Modify endpoints; **reuse bench** |

### 1.2 Geography problems

1. **Deepgram Flux pinned to the EU endpoint** — [flux.py:56-70](shuo/services/flux.py#L56-L70) hardcodes `wss://api.eu.deepgram.com`. Paid on every audio frame. Research also confirms Flux has **a single global endpoint** (`wss://api.deepgram.com/v2/listen`) with no India region for direct customers.
2. **Groq in the US** — [llm.py:34-36](shuo/services/llm.py#L34-L36). Fast inference, slow network.
3. **ElevenLabs US/EU** — [tts.py:59-63](shuo/services/tts.py#L59-L63); the code even expects "Netherlands" in the region header at [tts.py:70-74](shuo/services/tts.py#L70-L74). ElevenLabs routes India traffic to its SE-Asia cluster; its India residency offering is **storage-only** ("processing may nevertheless occur outside the selected location") and Enterprise-gated.
4. **Twilio pinned to Frankfurt** — [twilio_client.py:36](shuo/services/twilio_client.py#L36): `edge="frankfurt", region="us1"`. Twilio has **no India edge** (Sydney, São Paulo, Dublin, Frankfurt, Tokyo, Singapore, Ashburn, Umatilla only) and Media Streams Regions exist only for US1/IE1/AU1.

### 1.3 Three latent bugs to fix during migration

**Bug A — the agent believes it said things the caller never heard.** On barge-in, [llm.py:109-112](shuo/services/llm.py#L109-L112) appends the *entire generated* assistant text (plus `"..."`) to history. TTS lags the LLM and playback lags TTS, so the caller may have heard only the first few words. The agent then reasons as though it delivered the whole pitch. Fix requires **played-millisecond accounting** mapped back to characters (§4.4).

**Bug B — playback pacing drifts; turn-completion is guessed.** [player.py:111-124](shuo/services/player.py#L111-L124) does `await asyncio.sleep(0.020)` once per *chunk*, regardless of how much audio a chunk contains. TTS chunks are not 20ms, so the loop paces by chunk count rather than audio duration. Worse, `_on_playback_done` fires when the *local list* empties, not when audio was heard — so `AgentTurnDoneEvent` fires early.

**Bug C — `/tmp/shuo` is POSIX-only.** [tracer.py:26](shuo/tracer.py#L26), [server.py:80](shuo/server.py#L80). Breaks on the Windows dev machine.

### 1.4 What must survive

- **The pure state machine** — [state.py:22-60](shuo/state.py#L22-L60) is a total function with no I/O and 308 lines of tests. Every extension below is new events/actions through that same function.
- **The warm TTS pool** — [tts_pool.py](shuo/services/tts_pool.py) pre-opens WebSockets with TTL eviction so a turn never pays connection setup. Generalise it; apply to STT too.
- **Token-level streaming chain** — [agent.py:158-193](shuo/agent.py#L158-L193).
- **The span tracer** — already records `llm_first_token` and `tts_first_audio` per turn: exactly the instrumentation needed to defend a latency budget.
- **The TTFT benchmark harness** — [server.py:200-283](shuo/server.py#L200-L283) does randomised, interleaved, N-run measurement. [scripts/llm-bench.txt](scripts/llm-bench.txt) shows `gpt-4.1-nano` at 369ms and `gpt-4o-mini` at 383ms avg **from the current host**. Re-run it from ap-south-1 — that is how we settle the LLM decision empirically (§2.4).

---

## Part 2 — Target architecture

```
   Indian mobile (PSTN)
        │  G.711 A-law, 20ms — Vobiz SBC transcodes
        ▼
   Vobiz SBC + media plane   ── entirely AWS ap-south-1 (Mumbai)
        │  wss:// JSON frames, base64 audio/x-mulaw;rate=8000
        │  ~1-5ms intra-region
        ▼
┌─────────────────────────────────────────────────────────┐
│  shuo agent — EC2 c7i.xlarge, AWS ap-south-1            │
│                                                          │
│  carrier/vobiz.py ──▶ event queue ──▶ process_event()   │
│                            │              (pure)         │
│    ┌───────────────────────┼──────────────────────┐     │
│    ▼                       ▼                      ▼     │
│  Silero VAD v5      Sarvam saaras:v3        Filler      │
│  + SmartTurn v3      (India-hosted)         engine      │
│  (in-process CPU)    8kHz native, en-IN     (pre-       │
│         │            codemix mode           rendered)   │
│         └──── EOT ────────┬──────────────────┘          │
│                           ▼                              │
│              Gemini 2.5 Flash-Lite (Vertex asia-south1)  │
│                           │ tokens                       │
│                           ▼                              │
│              Cartesia Sonic 3.5 — cloned founder voice   │
│                  raw / pcm_mulaw / 8000  (zero transcode)│
│                           ▼                              │
│              Player: 20ms monotonic-deadline pacing      │
└─────────────────────────────────────────────────────────┘
```

### 2.1 Transport — Vobiz `<Stream>` (the best news in this document)

**Vobiz is `vobiz.ai` — Ilaimitado Private Limited, Bengaluru, CIN U62090KA2025PTC201765, incorporated 23 Apr 2025.**

Vobiz ships a **native Twilio-Media-Streams-equivalent WebSocket media API**, and it is a deliberate Plivo API clone. This means we do **not** need FreeSWITCH, Asterisk, jambonz, rtpengine, or any self-hosted media infrastructure. Confirmed three ways: Vobiz's official docs, their own `Vobiz-Python-Voice-API-Example/agent.py`, and an independent implementation in `bolna-ai/bolna`.

| Twilio (current) | Vobiz (target) | Delta |
|---|---|---|
| `{"event":"media","media":{"payload":"<b64>"}}` | **identical nesting** | event-name mapping only |
| `audio/x-mulaw` 8kHz base64 | `contentType="audio/x-mulaw;rate=8000"` | **byte-identical** |
| `clear` | `clearAudio` | rename |
| `mark` / mark-ack | `checkpoint` → `playedStream` | **new capability we don't use today** |
| `start.streamSid` | `start.streamId` + `start.callId` | rename |
| Frankfurt/US1 edge | **AWS ap-south-1 (Mumbai)** | ~1-5ms vs ~200ms |

Answer-URL XML (exact working form from Vobiz's reference repo):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response><Stream bidirectional="true" keepCallAlive="true"
  contentType="audio/x-mulaw;rate=8000"
  statusCallbackUrl="https://your-host/stream-status"
  statusCallbackMethod="POST">wss://your-host/ws</Stream></Response>
```

**Staying on μ-law is a deliberate decision, not inertia.** Indian PSTN is A-law (E1/ITU) — but Vobiz's SBC transcodes, so the application layer never sees it. Critically, Vobiz's inbound L16 payload is **big-endian** while outbound `playAudio` L16 must be **little-endian** — the single most likely silent-corruption bug in the port. μ-law is byte-oriented and endian-free, sidestepping it entirely. Their own `Vobiz-24KHz` README concedes the PSTN leg may still be 8kHz, so 24kHz buys little.

**Traps to code around** (all verified from Vobiz's own docs and production bug reports):
- `keepCallAlive` **requires** `bidirectional="true"`, else the call hangs up when the XML completes.
- `audio/x-l16;rate=24000` is **not** a valid `<Stream>` contentType; 24kHz is outbound-only via `playAudio.media.sampleRate`.
- `audioTrack` defaults differ: XML `<Stream>` → `"inbound"`, REST `POST .../Stream/` → `"both"`. **Set it explicitly in both paths.**
- `playedStream`'s `name` is **top-level**, not nested.
- Outbound `playAudio` uses the *short* contentType (`"audio/x-mulaw"`) with `sampleRate` as a separate integer field — **not** the `;rate=8000` form used in the `<Stream>` attribute.
- **Do not assume `start` arrives first** — Vobiz has been observed sending `media` before `start`. Buffer media in a deque until IDs populate, then drain.
- Key session state on **`callId`, not `streamId`** — `maxRetries` reconnects replay a fresh `start` with a *new* streamId on the same call.
- Send `{"event":"stop"}` over the WebSocket *before* HTTP-DELETEing the call, or a phantom reconnect overwrites your transcript with empty data.
- Path casing is not uniform and wrong casing returns **401, not 404**. Trailing slashes are semantic (`GET /Call?status=live` vs `GET /Call/?status=queued`). E.164: `%2B` in paths, literal `+` in JSON bodies.
- Webhook HMAC is computed over the callback URL — behind a TLS-terminating ALB, `request.url` is `http://internal…` and validation fails. Set `FORWARDED_ALLOW_IPS` and honour `X-Forwarded-Proto`.
- **Turn AMD off.** `machine_detection_initial_silence` defaults to **4500ms** — that alone blows the first-turn budget. Use `machine_detection_url` for async AMD if needed at all.

**Security groups (all AWS ap-south-1):** SIP signalling UDP+TCP 5060/5061 to 10 IPs (13.203.7.132, 65.2.100.211, 13.126.98.234, 13.235.11.131, 13.233.44.61, 3.111.255.163, 3.111.128.110, 43.204.64.203, 15.207.232.91, 35.154.133.28); RTP media from 9 ranges; webhooks HTTPS 443 from 15.206.6.156, 35.154.59.246, 15.207.8.226.

> **Runner-up, documented for the record:** Asterisk 23.4.1 + `chan_websocket` (GPLv2, released 25 Jun 2026). It is a near-exact Twilio clone — `FLUSH_MEDIA` = `clear`, `MARK_MEDIA`/`MEDIA_MARK_PROCESSED` = `mark`, plus `MEDIA_XON`/`XOFF` flow control Twilio never had, and Asterisk paces for you via `ast_timer_open()` at 50Hz. **Switch to it only if** you need carrier independence or Vobiz's per-minute stream-forking fee proves material. Caveats: pin ≥23.4.1/22.10.1 (15 chan_websocket defects closed Dec-2025→Jun-2026 including segfaults and garbled audio), and lower `QUEUE_LENGTH_MAX` from its default 1000 frames — that is **20 seconds** of unretractable audio.
>
> **Rejected: terminating SIP inside Python.** Measured localhost TCP round-trip for a 20ms frame is p50 **34µs** — 0.01% of the budget and 400× smaller than the G.711 packetisation interval you cannot avoid. You would trade 4–8 engineer-weeks plus permanent carrier-interop risk for 0.05ms. PJSIP was still fixing dialog-teardown races on 2026-07-21.
>
> **Rejected: jambonz.** Introduced a commercial licence 20 Jan 2026 — MIT stops at 0.9.x; v10+ requires a key at $7/concurrent-session/mo or $2,000/mo unlimited (₹176,000/mo).

### 2.2 STT — Sarvam AI `saaras:v3` streaming

`wss://api.sarvam.ai/speech-to-text/ws`

**Why:** it is the **only** vendor in the market with a published Indian benchmark — 19.31% WER across the 10 most-spoken IndicVoices languages, claiming to beat Deepgram Nova-3, ElevenLabs Scribe v2, Gemini 3 Pro and GPT-4o Transcribe on Indian speech, trained on ~1M hours. It is India-hosted (no transoceanic RTT), has native **8kHz** streaming, a dedicated **`codemix`** mode, `en-IN` as a first-class locale, and bills **per second of audio submitted** — not wall-clock socket time.

That billing model is decisive. AssemblyAI bills *"the time the WebSocket connection is open, not the duration of audio sent. Idle connection time counts"*; Deepgram Flux bills 100% of the call's wall-clock. Sarvam's audio-duration billing lets client-side VAD gating cut the bill ~65%.

**Exact facts to build against:**
- **23 selectable locales** (the 24-value enum's first entry is `unknown`, an auto-detect sentinel). `en-IN` is Indian English.
- **8kHz is only available as a connection parameter** — *"not in AudioData messages"*. The per-message `sample_rate` is legacy and accepts only 16000/22050/24000. Set it once at connect. Default is 16kHz.
- **₹30/hour** = ₹0.50/audio-minute, rounded up per request, 1-second minimum charge. (₹45/hr with diarization — we don't need it.)
- **WebSocket concurrency is the binding constraint, not rate limits:** STT streaming **20 concurrent (Starter) / 100 (Pro *and* Business)**. Business buys 4,000 req/min but still only 100 concurrent sockets — a hard scaling wall well beyond pilot.
- Response includes a `metrics` object with `audio_duration` and `processing_latency` — **measure your own TTFT from day one** rather than trusting the vendor's unbenchmarked "sub-150ms" claim (Sarvam appears on neither Artificial Analysis nor Coval).

**The one thing Sarvam does not give us: semantic end-of-turn detection.** Deepgram Flux bundled STT + turn-taking; Sarvam does not. That capability moves in-process (§2.3) — this is the single biggest structural change in the migration.

### 2.3 Turn detection — in-process, on CPU

| Layer | Component | Measured cost |
|---|---|---|
| Acoustic VAD | **Silero VAD v5 ONNX** | **189µs** per 31.25ms chunk |
| Semantic EOT | **Pipecat Smart Turn v3.1** (BSD-2, 8MB) | 12ms on modern CPU, ~60ms on a small AWS instance |
| Backchannel rejection | Custom classifier + token list (§5.3) | in-process |

Both run locally in ap-south-1 — **zero network in the turn-taking path**, which is the whole point. Total EOT decision cost ~100–250ms including endpoint hangover, versus Flux's ~260ms *plus* a transoceanic round trip.

> ⚠️ **Known trap:** Pipecat issue **#3844** — `WhisperFeatureExtractor` hardcodes 16kHz, so 8kHz telephony audio is read at double speed **with no error**. Measured damage: 6 of 20 utterances flipped Complete/Incomplete and mean turn duration fell 51%, fragmenting phone numbers. Closed via PR #3857, but published workarounds are stale. **Mitigation: upsample 8k→16k before the detector** — `audioop.ratecv` measures **4.81µs per 20ms frame**, i.e. free. Assert the detector's input rate at startup.

*Rejected:* Deepgram Flux (best-in-class EOT, Hindi confirmed in `flux-general-multi`, but single global endpoint — the RTT negates the advantage; self-host needs an Enterprise contract plus an Ampere GPU). LiveKit's Adaptive Interruption Handling (30ms, 86% precision / 100% recall, rejects 51% of VAD false positives) is excellent but **cannot be self-hosted** and requires LiveKit Cloud.

### 2.4 LLM — Gemini 2.5 Flash-Lite on Vertex AI `asia-south1`

**Why this specific model:** it is the **only** Gemini tier with thinking **OFF by default**. Gemini 3 Flash defaults to `high` with a floor of `minimal`; Gemini 3.1 Flash-Lite defaults to `minimal` with **no off switch at all** — the entire 3.x tier structurally cannot be made non-reasoning, which is why it shows a ~15× latency gap on Artificial Analysis. For voice, 2.5 Flash-Lite is the pick: 0.37s AA latency as a non-reasoning model, and **~₹0.04 per conversation-minute** with a cached system prompt.

It is also exempt from the +10% non-global-endpoint surcharge, which Google scoped to *"the Generally available Gemini 3 and later families"* effective 2026-07-01.

> ⚠️ **Verify empirically, do not trust the docs.** Adversarial verification found the asia-south1 claim *"partially true but mis-sourced"* — Google's pricing page contains no region names at all, and the status-page evidence is an incident-feed mirror, not a synthetic probe. **Resolution: re-run the existing `/bench/ttft` harness ([server.py:200-283](shuo/server.py#L200-L283)) from an ap-south-1 box** against Vertex asia-south1, Vertex global, Groq, and `gpt-4.1-nano`. That is a half-day of work and it settles the question with your own numbers. Keep all four behind the provider interface.

**The LLM is not the cost problem — it is the latency problem.** Every viable 2026 small model lands between ₹0.02 and ₹0.40/conversation-minute (2–8% of budget). Claude Haiku 4.5 is the outlier: its 4,096-token minimum cacheable prefix means a 2k-token sales script **silently will not cache at all**, pushing it from ₹0.39 to ~₹1.18/min.

### 2.5 TTS — Cartesia Sonic 3.5, with the founder's cloned voice

**Why:** it is the only shortlisted vendor that emits **`container=raw, encoding=pcm_mulaw, sample_rate=8000`** — documented explicitly as the G.711µ/telephony format. **Zero transcode in the hot path.** It supports WebSocket *input* streaming with continuations (token-by-token from the LLM, matching the existing [agent.py](shuo/agent.py) design), covers 42 languages including Hindi, and its 3.5 release notes specifically call out *"dramatically better alphanumeric read-out"* for codes, numbers and IDs — which is most of what a telecaller says.

**Voice cloning:** Instant Voice Cloning from a clip *"up to 10 seconds"*, *"fast and free"*, self-serve from the Pro tier ($5/mo). Cloned-voice synthesis bills at the normal 1 credit/char (Pro Voice Clone voices bill 1.5×).

**Concurrency is the constraint to plan around:** 8 (Free) / 12 (Pro) / 20 (Startup $49/mo) / 60 (Scale $299/mo). Startup covers a 5–20 concurrent pilot; you will need Scale or Enterprise beyond that.

> 🔴 **Why not Sarvam Bulbul v3, despite being the obvious India-native choice:** its **streaming WebSocket emits MP3 only**. The schema enum lists `mulaw`/`alaw`, but the field's own description reads *"Currently supports MP3 only, optimized for real-time playback"*. That forces MP3 frame-decode → PCM → resample → µ-law → 20ms reframing in the hot path, and 8kHz MP3 is MPEG-2.5 LSF with **576-sample granules (~72ms)** that do not align to 20ms frames. Disqualifying for streaming.
>
> **But use Bulbul v3 REST for the offline filler cache** (§5.2) — the REST endpoint *does* support `mulaw` at 8000Hz, it is INR-billed with no FX exposure, and offers 37 speakers across 11 Indic locales. Latency is free when you render ahead of time.

**Cost-reduction candidate to A/B in Phase 4:** **Inworld Realtime TTS 1.5 Mini** — $15/1M chars on-demand falling to $7/1M on Growth, ~120ms median / <130ms P90 (published *with percentiles*, better disclosure than most), Hindi GA, instant cloning from 3–15s on **all** plans, concurrency 5 → 500+. That is **₹0.44/conv-min vs Cartesia's ₹1.15** — the single largest remaining cost lever. Gate the switch on verifying 8kHz µ-law output and Indian-English quality.

*Rejected:* ElevenLabs Flash v2.5 (₹1.78/conv-min, no India processing, SE-Asia cluster; independent measurement puts real TTFB at ~255ms vs the "~75ms" claim, which its own docs footnote as *"excluding application & network latency"*) — **but keep it as the fallback provider**, since the code already implements it and its **character-level alignment** (`charStartTimesMs`, `charDurationsMs`) is the cleanest solution to Bug A. Azure Custom Neural Voice (Limited-Access gated, ~₹2.6 lakh/year per voice in endpoint hosting). Deepgram Aura-2 (**no cloning product at all** — disqualifying). Google Chirp 3 HD (**not served from asia-south1**; verified by raw-HTML inspection of the endpoints matrix — Mumbai's cell is empty).

---

## Part 3 — Cost model

At **20,000 conversation-minutes/month, 15 concurrent**, all-in:

| Component | Vendor | Unit price | Assumption | ₹/conv-min |
|---|---|---|---|---|
| Telephony | Vobiz outbound | ₹0.45/min | 100% of connected minute | **0.45** |
| Channels | Vobiz | ₹349/channel/mo | 15 ch ÷ 20k min | **0.26** |
| STT | Sarvam `saaras:v3` | ₹0.50/audio-min | VAD-gated, 35% caller speech | **0.18** |
| Turn detection | Silero + Smart Turn | self-hosted CPU | in-process | **0.00** |
| LLM | Gemini 2.5 Flash-Lite | ~$0.0005/min | 2k cached prefix, ~40 out-tok/turn | **0.04** |
| + speculative execution | " | +70% LLM calls | §4.3 | **0.03** |
| TTS | Cartesia Sonic 3.5 | $0.0000374/char | 740 ch/min × 45% = 333 ch | **1.10** |
| Filler cache | Sarvam Bulbul v3 REST | one-off ~₹7 | pre-rendered | **0.00** |
| Compute | EC2 c7i.xlarge ap-south-1 | $0.214/hr | ÷ 15 concurrent | **0.06** |
| | | | **TOTAL** | **₹2.12** |

**Comfortably under the ₹3 stretch target, and 3× under the ₹5 ceiling.**

Swapping Cartesia → Inworld TTS 1.5 Mini takes this to **₹1.46/min**. Even if Vobiz's ₹0.45 turns out to be ₹0.90 (their public rate card renders US-only; ₹0.45 is stated on three of their own comparison pages but unconfirmed on the rate card) the total is ₹2.57.

**Competitor comparison — every figure verified against live 2026 pricing pages:**

| Platform | Floor ₹/min | Note |
|---|---|---|
| **This plan** | **₹2.12** | |
| Bolna.ai (India-native) | ₹5.52 | list |
| Retell AI | **₹6.42** | $0.055 infra + $0.015 TTS + $0.003 GPT-5-nano, **on BYO SIP** — 28% over your ceiling before a single Indian telephony rupee |
| ElevenLabs Agents | ₹7.04 | flat on every tier; ₹14.08 burst |
| Bland AI | ₹9.68 | cheapest tier |
| Twilio (telephony alone) | ₹4.36 | to Indian mobile, before any AI |

The structural insight: **every managed platform's orchestration fee alone consumes the budget** — Vapi ₹4.40, Retell ₹4.84, Twilio ConversationRelay ₹6.16, Deepgram Voice Agent ₹6.60, Agora ₹8.80. Retell *cannot* reach ₹5/min even at its cheapest configuration. Keeping the custom loop is not merely a preference; it is the only way the unit economics work.

---

## Part 4 — Latency budget and the turn-taking loop

### 4.1 Mouth-to-ear budget

| Hop | ms | Basis |
|---|---|---|
| Handset → carrier → Vobiz SBC | 30–60 | Indian mobile-terminated, one-way |
| Vobiz → our EC2 | 1–5 | intra-ap-south-1 |
| G.711 packetisation | 20 | 20ms ptime, unavoidable |
| VAD + Smart Turn EOT decision | 100–250 | Silero 189µs + SmartTurn 12–60ms + hangover |
| Sarvam final transcript | 60–120 | *overlaps* EOT — streams continuously |
| LLM TTFT | 120–200 | Flash-Lite non-reasoning, cached prefix, Mumbai |
| TTS TTFB | 90–250 | Cartesia claim vs measured range |
| Player pre-roll | 40–60 | design parameter: 2–3 frames |
| Vobiz → carrier → handset | 30–60 | reverse |
| Handset de-jitter buffer | 40–100 | **unretractable** |
| | **~470–1,025** | **p50 target ≈ 600ms** |

Server-side (EOT → first byte queued) is **300–450ms**, which is the number to instrument and optimise. Everything else is physics or the handset.

### 4.2 Barge-in — where the audio actually is

With our own paced queue, the flushable buffer is **in our process**. Quantified from Asterisk source and rtpengine defaults: rtpengine adds ~0 (jitter buffer disabled by default), Asterisk ~0–20ms, our socket holds only the pre-roll we allow, network inside ap-south-1 is ~20–40ms one-way, and the **handset/carrier de-jitter buffer is the one unretractable 40–100ms**.

Net: **~100–200ms of already-committed audio plays after we decide to stop** — and that is fine, because it is dwarfed by the *detection* gate. LiveKit defaults `min_interruption_duration` to 0.5s; jambonz's example is 0.5s; Pipecat's VAD `start_secs` is 0.2s. The detection threshold, not the flush, is the barge-in latency.

**Design rule:** keep the outbound queue in our process, emit exactly one 20ms frame per 20ms tick on a **monotonic deadline** (not `sleep(0.02)` — that drifts), allow 2–3 frames of pre-roll, and on barge-in drop the queue and send `clearAudio`.

### 4.3 Speculative execution — take it

Fire the LLM on the STT partial when Smart Turn confidence crosses an *eager* threshold; discard if the caller continues. Deepgram's own docs quantify the equivalent trade: **100–200ms saved for 50–70% more LLM calls**.

At Gemini Flash-Lite's ₹0.04/min, +70% costs **₹0.03/min** — about 1.4% of the budget for 100–200ms on every turn. Unambiguously worth it. Config the thresholds like Flux does: an `eot_threshold` (0.5–0.9, default 0.7) and a lower `eager_eot_threshold` that must be ≤ it.

### 4.4 Transcript truncation (Bug A)

The Player counts bytes actually emitted. At 8kHz µ-law, **1 byte = 125µs**, so `played_ms = bytes_sent / 8`. Map that to characters via the TTS vendor's alignment (Cartesia timestamps; ElevenLabs `charStartTimesMs`/`charDurationsMs` — noting those are *relative to each returned chunk*, not the full response) and truncate the assistant message in history to what was actually heard. Use Vobiz's `checkpoint` → `playedStream` acknowledgement as the authoritative cross-check.

---

## Part 5 — Human-likeness

### 5.1 Response gap: sample it, never fix it

The common intuition is wrong. Stivers et al. (PNAS 2009, 10 languages) measured the **modal human response gap at 0ms**, cross-linguistic median +100ms, mean +208ms, with only ~250ms of spread between the fastest and slowest language. **A 0ms reply does not feel robotic.** What reads as machine is (a) the *same* gap every turn and (b) responding before the caller finished.

So: sample the gap from a distribution (median ~250ms, p90 ~700ms, longer for dispreferred/negative answers) rather than adding a fixed delay.

> Hindi/Indian-English turn-taking timing data **does not exist** — a PubMed query returned zero results, and Hindi was not in Stivers' sample. This is a documented negative, not an unsearched gap. **Cheapest defensible differentiator available: measure floor-transfer offsets on 50 recorded human agent calls from your own vertical.** Nobody, vendor or academic, has this number.

### 5.2 Gated two-tier fillers

Evidence base: CUI '25 (arXiv 2507.22352) — latency above 4s degrades quality of experience, and natural fillers measurably improve *perceived* response time in high-delay conditions. Earlier work (Pfeifer & Bickmore, IVA '09) found mixed results, so fillers are net-positive **only when gated on real latency**.

**Gate:** maintain rolling p50/p80 of LLM TTFT segmented by turn type (chitchat / objection / tool-call). `predicted_ttfb = p80(turn_type) + tts_ttfb`. Arm only if `predicted_ttfb > 400ms` **or** a tool call is in flight.

| Tier | Fires at | Content |
|---|---|---|
| 0 | 0–350ms | **Say nothing.** The median human gap is 100ms; a filler inside that window *is* the tell. |
| 1 | T+350ms | 250–400ms pre-rendered clip |
| 2 | T+1200ms | longer acknowledgement |
| 3 | T+3000ms | explicit hold + 4s keepalive |

**Pre-render the cache.** ~40 phrases × 3 prosodic variants = 120 clips at 8kHz µ-law, keyed on a voice hash, rebuilt only when the voice/model/pace changes. Total cost at Sarvam Bulbul v3 REST: **~₹7**. Cached clips have **0ms TTFB** — the only component in the pipeline with a guaranteed zero India-region cost, and it covers the greeting, which is where the caller forms their latency impression.

**Indian inventory:** `haan ji`, `ji`, `ek second`, `ek minute`, `theek hai`, `achha`, `sahi hai`, `bas`, `dekhiye`. Mid-utterance hedges (`matlab`, `toh`) belong in the LLM's own text, not the clip cache. For reference, Retell ships Hindi backchannel defaults for 11labs voices as अरे, हाँ, उम्म, अच्छा, सही, ओह — notably **omitting `ji`**, the deference marker that matters most for cold outbound to strangers.

**Reference implementation to tune against** (we build, not buy — ElevenLabs Agents costs ₹7.04/min): `soft_timeout_config.timeout_seconds` range 0.5–8.0, default **-1 (disabled)**, recommended **3.0**; default message literally `"Hhmmmm...yeah."`; `turn_eagerness` ∈ {eager, normal, patient}.

### 5.3 False barge-in suppression

Indian callers backchannel heavily, and Indian mobile calls carry a lot of background speech. Suppress with layered gating: minimum speech duration (**500ms**, matching LiveKit/jambonz defaults), a minimum word count, a backchannel token list, and resumption of playback if the "interruption" resolves to a backchannel. Retell's knobs for calibration: `enable_backchannel` defaults **false**; `backchannel_frequency` defaults **0.8** on [0,1]; `interruption_sensitivity` and `responsiveness` both default **1**.

### 5.4 Do not add background ambience

Research found **no evidence it helps**. Retell's `ambient_sound` defaults to `null`, and its "call-center" preset is US room tone — adding a specifically *American* cue to a call claiming to be from an Indian agent is a net-negative tell. Skip it.

---

## Part 6 — Migration plan

### Phase 0 — Compliance & procurement *(parallel, blocks production not development)*
1. Ask Vobiz for their DoT licence number and **140xx allotment capability**. If they cannot allot 140xx, this stack cannot legally do promotional outbound and the trunk choice must be revisited.
2. Begin DLT Principal Entity registration (physical verification + biometric auth — budget weeks, not hours). Register content and consent templates; voice templates *are* in scope.
3. File the **Reg 4** written auto-dialer/robo-call declaration with the Originating Access Provider **before the first pilot call**.
4. Arrange scrubbing access via a registered Scrubbing Function; design the CRM to store scrub reference + validity window per number.
5. Diarise annual self-certification — failure causes **automatic suspension**.

### Phase 1 — Transport swap: Twilio → Vobiz
- New `shuo/carrier/` package with a `Carrier` protocol (`originate`, `parse_message`, `play_audio`, `clear_audio`, `checkpoint`); implement `vobiz.py`. Build the abstraction against the **Plivo shape** — Vobiz is a deliberate Plivo clone, so both drop in behind one interface.
- Rewrite [twilio_client.py](shuo/services/twilio_client.py) → `carrier/vobiz.py`: `POST https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/`, headers `X-Auth-ID`/`X-Auth-Token`.
- `/twiml` → `/answer` returning the `<Stream>` XML above (`Content-Type: application/xml`, HTTPS, <100KB, **within 1–2s** or the caller hears dead air).
- Add `X-Vobiz-Signature-V2`/`V3` HMAC-SHA256 validation, verified **unconditionally** — do not skip when the header is absent.
- Extend [types.py](shuo/types.py): `StreamStartEvent` gains `call_id`; add `PlaybackMarkEvent`, `AudioClearedEvent`.
- Implement the defensive parser rules from §2.1 (media-before-start buffering, `callId` keying, `stop`-before-DELETE).
- **Deploy target: EC2 `c7i.xlarge` in ap-south-1** with an Elastic IP and the Vobiz security groups. Replace the [Procfile](Procfile)/ngrok dev flow.

### Phase 2 — Provider abstraction
- `shuo/providers/` with `STTProvider`, `TTSProvider`, `LLMProvider`, `TurnDetector` protocols.
- Generalise [tts_pool.py](shuo/services/tts_pool.py) into `providers/pool.py` — the warm-connection + TTL-eviction pattern applies to STT sockets too.
- Move all vendor selection into config so A/B swaps need no code change.

### Phase 3 — STT + turn detection *(the structural change)*
- Delete [flux.py](shuo/services/flux.py). Add `providers/stt/sarvam.py` — connect with `sample_rate=8000` **as a connection parameter**, `language_code=en-IN`, `mode=codemix`.
- Add `providers/turn/` — Silero VAD v5 ONNX + Smart Turn v3.1, **upsampling 8k→16k via `audioop.ratecv`** with a startup assertion on the detector's expected rate (guards issue #3844).
- Extend the state machine to **three phases**: `LISTENING → THINKING → RESPONDING → LISTENING`. Fillers fire only in `THINKING`. New events: `VadSpeechStart/End`, `SttPartial/Final`, `TurnEndDetected`, `FillerDue`. New actions: `FeedSttAction`, `ArmFillerTimerAction`, `PlayFillerAction`, `ClearAudioAction`, `TruncateHistoryAction`.
- Extend [tests/test_update.py](tests/test_update.py) for every new transition — the state machine stays pure and fully covered.
- Add VAD gating on the STT feed (cuts the Sarvam bill ~65%).

### Phase 4 — TTS + voice cloning
- Add `providers/tts/cartesia.py` — WebSocket input streaming with continuations, `container=raw, encoding=pcm_mulaw, sample_rate=8000`.
- Clone the founder's voice (≤10s clip, self-serve, free at Pro tier). Store the voice ID in config; key the filler cache on a hash of it.
- Keep [tts.py](shuo/services/tts.py) as `providers/tts/elevenlabs.py` behind the same interface (fallback + character-alignment reference).
- Add `providers/tts/inworld.py` and A/B it on 8kHz µ-law output and Indian-English quality — the ₹0.66/min saving is the largest remaining lever.

### Phase 5 — Player rewrite *(fixes Bugs A & B)*
- Rewrite [player.py](shuo/services/player.py): **monotonic-deadline scheduler** emitting exactly one 20ms frame (160 bytes µ-law) per tick, with 2–3 frames of pre-roll — not `sleep(0.02)` per chunk.
- Byte counter → `played_ms = bytes_sent / 8`.
- Wire Vobiz `checkpoint`/`playedStream` for authoritative playback completion, replacing the guessed `on_done`.
- On barge-in: drop queue → send `clearAudio` → compute `played_ms` → truncate the assistant message in history to the characters actually heard.

### Phase 6 — Filler engine & conversational polish
- `shuo/fillers/` — cache builder (offline, Sarvam Bulbul v3 REST → 8kHz µ-law) plus the tiered runtime gate from §5.2.
- Rolling TTFT percentile tracker segmented by turn type.
- Response-gap sampler (§5.1) and backchannel suppression (§5.3).

### Phase 7 — LLM
- `providers/llm/vertex.py` for Gemini 2.5 Flash-Lite on asia-south1, with **explicit context caching** for the system prompt.
- **Re-run [server.py](shuo/server.py) `/bench/ttft` from ap-south-1** across Vertex asia-south1 / Vertex global / Groq / `gpt-4.1-nano` and pick on measured data.
- Replace the placeholder `SYSTEM_PROMPT` at [llm.py:15](shuo/services/llm.py#L15) with a telecalling script: Indian English register, number normalisation (lakh/crore, phone-number grouping), objection handling, hard "not interested" exit.
- Add speculative execution (§4.3) behind a feature flag.

### Phase 8 — Dialer & compliance gates
- `shuo/dialer/` — calling-window gate (10:00–21:00 IST), pacing, retry policy, per-number scrub-validity check.
- **Abandoned-call and silent-call ratio counters over rolling 24h windows, with alarms at 3% and 1%.** These are regulatory thresholds, not SLOs.
- Call-outcome classification from SIP response codes; AMD **off** by default.

### Phase 9 — Observability
- Fix [tracer.py:26](shuo/tracer.py#L26) → `pathlib.Path(tempfile.gettempdir())`.
- Export per-turn spans as metrics: EOT-decision, STT-final, LLM-TTFT, TTS-TTFB, first-byte-queued, played-ms-at-barge-in.
- Log WebSocket close codes explicitly — **1006 = abnormal mid-call media drop** and is otherwise invisible.
- Track the p50/p95 of server-side turn latency as the primary SLO.

---

## Part 7 — Verification

**Unit** — `python -m pytest tests/ -v`. The existing 308 lines must pass unchanged after Phase 1; extend for the three-phase machine in Phase 3. State machine stays pure, so every transition is testable without I/O.

**Component**
- Vobiz WebSocket: replay a captured `start`/`media`/`stop` sequence against the parser, including the media-before-start case and a `maxRetries` reconnect with a new `streamId`.
- Codec: assert byte-exact µ-law round-trip and that no L16 path is reachable (endianness trap).
- Turn detector: feed 8kHz fixtures, assert the detector received 16kHz, and check turn-duration distribution against a 16kHz control (guards #3844).
- Player: assert exactly 50 frames/second over 60s with <1 frame of drift, and that `played_ms` matches wall-clock within 20ms.

**Latency** — re-run `/bench/ttft` from ap-south-1 and compare against the existing [scripts/llm-bench.txt](scripts/llm-bench.txt) baseline (`gpt-4.1-nano` 369ms, `gpt-4o-mini` 383ms). Add a `/bench/tts` twin measuring TTFB per provider from Mumbai.

**End-to-end** — place real calls to a handset on each of Jio / Airtel / Vi:
1. Measure mouth-to-ear with a two-phone recording and cross-correlation. Target p50 ≤ 600ms.
2. Barge-in: interrupt mid-sentence; verify audio stops within ~200ms, `clearAudio` is acked, and history is truncated to what was actually heard.
3. Backchannel: say "haan… haan" during agent speech; verify playback does **not** stop.
4. Filler gate: inject artificial LLM delay; verify silence below 350ms and a filler above it.
5. Voice: blind A/B the cloned voice against a recording of the founder with 10 Indian listeners.

**Cost** — reconcile one week of Vobiz, Sarvam, Cartesia and Vertex invoices against the §3 model. The two numbers most likely to be wrong are Vobiz's ₹0.45/min (unconfirmed on their rate card) and whether Sarvam's *WebSocket* path truly bills audio-duration rather than wall-clock — a ~2.9× swing on that line.

**Compliance** — verify 140xx CLI presentation on a live handset; verify the calling-window gate rejects a 21:30 IST attempt; verify abandoned/silent counters increment correctly.

---

## Open items requiring your input or a vendor call

1. **Vobiz DoT licence + 140xx capability** — blocking (§0.1).
2. **Vobiz India DID rental, setup fee, and confirmed per-minute rate** — not published; their rate card renders US-only.
3. **Vobiz `<Stream>` forking fee** — audio streaming may be billed per minute; rate not found. If material, the Asterisk runner-up becomes attractive.
4. **Sarvam WebSocket billing basis** — audio-duration vs wall-clock. Confirm contractually.
5. **Cartesia India/Asia endpoint** — their deployments page claims "regional API endpoints ensuring in-region processing" but enumerates no regions. Ask before committing.
