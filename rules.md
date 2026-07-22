# rules.md — engineering constraints and vendor traps

Hard-won facts from the research in [plan.md](plan.md), extracted so they are checkable at code-review time. **Read the relevant section before writing code in that area.** Violating one of these produces silent corruption, not an exception.

---

## 1. Architecture invariants

| # | Rule | Why |
|---|---|---|
| A1 | `process_event` in [shuo/state.py](shuo/state.py) performs **no I/O** and is a total function. | It is the only fully-tested component. Purity is what makes 307 lines of tests meaningful. |
| A2 | New capability enters as **new Events and Actions**, never as a side effect in the state machine. | Keeps every transition testable without mocks. |
| A3 | Dispatch happens in [shuo/conversation.py](shuo/conversation.py), never inside the machine. | Single side-effect boundary. |
| A4 | Nothing buffers a complete LLM response or a complete TTS utterance. | Token-level streaming is the whole latency advantage. |
| A5 | No vendor SDK imported outside its provider module. | Config-only A/B swaps. |
| A6 | Session state keys on **`callId`**, never `streamId`. | Vobiz `maxRetries` reconnects replay a fresh `start` with a *new* streamId on the same call. |
| A7 | No POSIX-only paths. Use `pathlib.Path(tempfile.gettempdir())`. | Dev machine is Windows; deploy is Linux. Bug C. |

---

## 2. Audio and codec — the silent-corruption zone

| # | Rule |
|---|---|
| C1 | **µ-law 8kHz end to end.** `audio/x-mulaw;rate=8000` in, `pcm_mulaw`/8000 out of TTS, µ-law on the wire. |
| C2 | **No L16 code path may be reachable.** Vobiz inbound L16 is **big-endian**; outbound `playAudio` L16 must be **little-endian**. This is the single most likely silent-corruption bug in the port. µ-law is byte-oriented and endian-free — that is why we chose it. Add a test asserting no L16 path exists. |
| C3 | 1 byte of 8kHz µ-law = **125µs**. Therefore `played_ms = bytes_sent / 8`. One 20ms frame = **160 bytes**. |
| C4 | Player emits exactly **one 20ms frame per 20ms tick on a monotonic deadline** — never `await asyncio.sleep(0.020)` per chunk. TTS chunks are not 20ms; pacing by chunk count drifts. (Bug B.) |
| C5 | Pre-roll: **2–3 frames** (40–60ms). More adds unretractable barge-in latency; less risks underrun. |
| C6 | Upsample 8k→16k with `audioop.ratecv` **before** the turn detector. Measured 4.81µs per 20ms frame — free. See §5. |
| C7 | 🔴 **Pace against `time.perf_counter()`, never `loop.time()`.** On **Python 3.12 for Windows** `time.monotonic()` — which is what `loop.time()` returns — is `GetTickCount64()` with **15.625ms resolution**, measured on the dev machine (`time.get_clock_info('monotonic')` confirms it). Against a 20ms deadline that clock cannot distinguish "on time" from "a full frame late", so the catch-up branch never fires and playback degrades to **~32 frames/sec — audio plays back slow, on the only machine where it is heard before a live call.** Measured on one 30s stream: **32.5 fps with `loop.time()`, 50.02 fps with `perf_counter()`.** CPython moved `monotonic()` to `QueryPerformanceCounter` in **3.13**, so this is a 3.12-specific trap; `perf_counter()` is QPC (100ns) everywhere. Compare only *durations*, so it is safe that asyncio schedules its own wakeups on the coarser clock. Same shape as Bug C: correct on the Linux target, broken on the Windows dev box. **Test timing harnesses have the same requirement** — a `loop.time()` stopwatch cannot see 3/4 of a 20ms drift budget. |

---

## 3. Vobiz carrier traps

All verified from Vobiz's own docs, their `Vobiz-Python-Voice-API-Example`, `bolna-ai/bolna`, and production bug reports.

> **Verified 2026-07-22** by a 6-lane adversarially-verified research pass. Where this section differs from `plan.md`, **this section wins** — `plan.md` was written before the wire format was pinned down. Implemented in [shuo/carrier/vobiz.py](shuo/carrier/vobiz.py), pinned by [tests/test_vobiz.py](tests/test_vobiz.py).

### Protocol mapping (Twilio → Vobiz)

| Twilio | Vobiz | Note |
|---|---|---|
| `{"event":"media","media":{"payload":"<b64>"}}` | same nesting, **`streamId` top-level** | `start` nests its ids; `media` does not — asymmetric |
| `audio/x-mulaw` 8kHz base64 | `contentType="audio/x-mulaw;rate=8000"` (XML only) | **byte-identical payload** |
| `clear` | `clearAudio` → acked by `clearedAudio` | Twilio has no ack |
| `mark` / mark-ack | `checkpoint` → `playedStream` | **new capability**, wired in Phase 1 |
| `start.streamSid` | `start.streamId` **+** `start.callId` | key on `callId` (A6) |
| inbound `stop` event | **does not exist** | socket close is the only in-band signal |

### Exact wire format

Inbound (Vobiz → us):
```jsonc
{"sequenceNumber":0,"event":"start","start":{"callId":"…","streamId":"…","accountId":"500025",
  "tracks":["inbound"],"mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000}},"extra_headers":"{}"}
{"sequenceNumber":1,"streamId":"…","event":"media",
  "media":{"track":"inbound","timestamp":"1778597597091","chunk":1,"payload":"<b64>"}}
{"event":"playedStream","name":"turn-3"}          // name TOP-LEVEL, no streamId, no seq
{"sequenceNumber":7,"event":"clearedAudio","streamId":"…"}
```

Outbound (us → Vobiz):
```jsonc
{"event":"playAudio","streamId":"…",
  "media":{"contentType":"audio/x-mulaw","sampleRate":8000,"payload":"<b64>"}}
{"event":"clearAudio","streamId":"…"}
{"event":"checkpoint","streamId":"…","name":"turn-3"}
{"event":"stop","streamId":"…"}
```

### Answer-URL XML (exact working form)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response><Stream bidirectional="true" keepCallAlive="true"
  contentType="audio/x-mulaw;rate=8000"
  audioTrack="inbound"
  statusCallbackUrl="https://your-host/stream-status"
  statusCallbackMethod="POST">wss://your-host/ws</Stream></Response>
```

The WebSocket URL is the element's **text content**, not an attribute.

### Traps

| # | Trap |
|---|---|
| V1 | `keepCallAlive` **requires** `bidirectional="true"`, else the call hangs up when the XML completes. |
| V2 | `audio/x-l16;rate=24000` is **not** a valid `<Stream>` contentType. 24kHz is outbound-only via `playAudio.media.sampleRate`. |
| V3 | `audioTrack` defaults differ: XML `<Stream>` → `"inbound"`, REST `POST .../Stream/` → `"both"`. **Set it explicitly in both paths.** |
| V4 | `playedStream`'s `name` is **top-level**, not nested. |
| V5 | Outbound `playAudio` uses the **short** contentType (`"audio/x-mulaw"`) with `sampleRate` as a separate integer field — *not* the `;rate=8000` form used in the `<Stream>` attribute. |
| V6 | **Do not assume `start` arrives first.** Vobiz has been observed sending `media` before `start`. Buffer media in a deque until IDs populate, then drain. |
| V7 | Send `{"event":"stop"}` over the WebSocket **before** HTTP-DELETEing the call, or a phantom reconnect overwrites the transcript with empty data. |
| V8 | ⚠️ **`plan.md` gets this backwards.** **401 means credentials, full stop** — Vobiz's auth gate runs *before* routing, so a bad token returns an identical 401 on a correct path, a nonexistent path, and the bare root. It carries zero information about casing. **404 is the path-casing / trailing-slash symptom**, and only once credentials are valid. Chasing capitalisation on a 401 wastes hours. |
| V8b | 🔴 **The trailing slash is a resource discriminator, not cosmetic.** `POST /Call/` **requires** it. `DELETE /Call/{uuid}` **must not** have it — the slashed form is the *queued-call* resource and defines no DELETE. `POST`/`DELETE /Call/{uuid}/Record/` **require** it. Vobiz's own hangup doc page shows the wrong form. Getting it backwards gives a hangup error that, if swallowed, leaves a billed call running. |
| V8c | Auth is `X-Auth-ID` / `X-Auth-Token` headers. **Not HTTP Basic** — that is Plivo's scheme and produces a blanket 401 that reads like a bad token. |
| V9 | E.164: `%2B` in URL paths, literal `+` in JSON bodies. |
| V10 | Webhook HMAC is computed over the **callback URL**. Behind a TLS-terminating ALB, `request.url` is `http://internal…` and validation fails. Set `FORWARDED_ALLOW_IPS` and honour `X-Forwarded-Proto`. |
| V11 | **Turn AMD off.** `machine_detection_initial_silence` defaults to **4500ms** — that alone blows the first-turn budget. Use `machine_detection_url` for async AMD if ever needed. |
| V12 | The answer URL must respond **within 1–2s**, `Content-Type: application/xml`, HTTPS, <100KB — or the caller hears dead air. |
| V13 | Validate `X-Vobiz-Signature-V2`/`V3` HMAC-SHA256 **unconditionally**. Do not skip validation when the header is absent. |
| V14 | Log WebSocket close codes explicitly. **1006 = abnormal mid-call media drop** and is otherwise invisible. |
| V15 | 🔴 **Vobiz never sends an inbound `stop` event.** The WebSocket close is the only in-band end-of-stream signal. Code that waits for `stop` hangs forever. (`plan.md` implies otherwise; one Vobiz doc page contradicts four others — we accept it if it ever appears, but never depend on it.) |
| V16 | 🔴 **`<Stream contentType>` defaults to `audio/x-l16;rate=8000`.** Omitting it silently puts us on the L16 path and straight into the endianness trap (C2). Always set mu-law explicitly. |
| V17 | `bidirectional="true"` **requires** `audioTrack` to be `inbound` (or absent). `both`/`outbound` make Vobiz hang up with *"End Of XML Instructions"*. Consequence: **the WebSocket can never carry the agent's own audio** — the recording, or our own TTS buffer, is the only possible source for an agent channel. |
| V18 | `playedStream` is **conditional and may never arrive**. A `clearAudio`, a barge-in, or a disconnect voids every pending checkpoint permanently. Never block a turn on it. |
| V19 | Sending `playAudio` immediately after `clearAudio` **without waiting for `clearedAudio`** races the flush and partially drops the new audio. Relevant on every barge-in. |
| V20 | `start` nests its ids (`start.callId`, `start.streamId`) but `media` carries `streamId` at **top level**. Two of Vobiz's own sample repos read the start ids top-level; we read nested first and fall back. |
| V21 | Docs mark `playAudio.streamId` required; three official Vobiz samples omit it. We send it — docs-compliant is the safer side of an unverified divergence. |

### Signature validation (exact)

```
V2:  base64(HMAC-SHA256(auth_token, base_url + nonce))
V3:  base64(HMAC-SHA256(auth_token, base_url + "." + nonce))
base_url = URL with query, params and fragment stripped — nothing else normalised
```

| # | Rule |
|---|---|
| G1 | 🔴 **Vobiz V3 is NOT Plivo V3.** Plivo V3 signs the full URL *including* the query string plus alphabetically-sorted POST params. Vobiz V3 is "Plivo V2 with a period". Do not reuse `plivo-python`'s validator. |
| G2 | Headers: `X-Vobiz-Signature-V2/-V3`, `-V2-Nonce`/`-V3-Nonce`, and `-MA-V2`/`-MA-V3` (signed with the **parent** account token on sub-account callbacks). Vobiz spells it `MA`, Plivo `Ma` — **look up case-insensitively**. |
| G3 | **The POST body is not signed.** A valid signature authenticates the URL only. Cross-check `CallUUID` against known call state before acting on a payload. |
| G4 | Reconstruct the signed URL from the configured **`PUBLIC_URL` + request path**, never from `request.url` and never from `X-Forwarded-*`. Behind ngrok or an ALB `request.url` is the internal `http://localhost:…` form and validation fails on *every* call; forwarded headers are attacker-controllable unless the proxy overwrites them. |
| G5 | Compare with `hmac.compare_digest`, accumulate rather than short-circuit, and split the header on `,` (Plivo comma-joins when an account has several active tokens; undocumented on Vobiz but harmless). Return **403**, not 401. |
| G6 | Known-good V2 vector, independently reproduced: `url="https://answer.url"`, `nonce="12345"`, `token="my_auth_token"` → `ehV3IKhLysWBxC1sy8INm0qGoQYdYsHwuoKjsX7FsXc=`. Adding a query string yields the same signature, proving the strip. |

### Recording

| # | Rule |
|---|---|
| R1 | 🔴 **There is no recording parameter on the Call-create API.** Recording is a separate step: the XML `<Record>` element, or `POST /Account/{id}/Call/{uuid}/Record/`. |
| R2 | 🔴 **Dual-channel is REST-only.** Vobiz's XML `<Record>` documents 15 attributes and *none* is a channel selector. Stereo comes from `record_channel_type="stereo"` on the REST endpoint — the literal is **`"stereo"`**, not Twilio's `"dual"`. Default is `"mono"`, so omitting it silently breaks the requirement. |
| R3 | `<Record>` + `<Stream>` together ARE supported. `<Record/>` must be **self-closing** and placed **before** `<Stream>` as a sibling, with `redirect="false"` so the recording callback does not interrupt the stream. |
| R4 | REST `time_limit` defaults to **60 seconds**. Set it explicitly or every recording truncates at one minute. |
| R5 | Use `file_format="wav"`. MP3 stereo is joint-stereo and lossy across channels, which corrupts per-channel ASR. Costs ~10× the storage. |
| R6 | Completion callback fires `Event=RecordStop` with `RecordingID`, `RecordingDuration`, `RecordingEndReason`, and the file URL as **either** `RecordUrl` or `RecordFile` — accept both. Form-encoded, not JSON. |
| R7 | ⚠️ **UNVERIFIED, and load-bearing:** stereo is documented as separating *"caller and callee"* / *"each leg"*. A `<Stream>` agent has only ONE PSTN leg — the agent is a WebSocket, not a B-leg. Whether `playAudio` audio reaches the recording at all, lands on channel 2, or leaves it silent is undocumented by both Vobiz and Plivo. **Verify on the first live call.** Fallback if the right channel is silent: write the agent channel from our own TTS buffer and mux offline. |
| R8 | Which physical channel holds which speaker is **not documented**. Make the channel→speaker mapping a config constant; do not hard-code channel 0 = caller. |

### Security groups (AWS ap-south-1)

- **SIP signalling** UDP+TCP 5060/5061 from: `13.203.7.132`, `65.2.100.211`, `13.126.98.234`, `13.235.11.131`, `13.233.44.61`, `3.111.255.163`, `3.111.128.110`, `43.204.64.203`, `15.207.232.91`, `35.154.133.28`
- **RTP media** from 9 ranges (obtain current list from Vobiz)
- **Webhooks** HTTPS 443 from: `15.206.6.156`, `35.154.59.246`, `15.207.8.226`

---

## 4. Sarvam STT (`saaras:v3`)

`wss://api.sarvam.ai/speech-to-text/ws`

| # | Rule |
|---|---|
| S1 | **8kHz is a connection parameter only** — *"not in AudioData messages"*. The per-message `sample_rate` is legacy and accepts only 16000/22050/24000. Set once at connect. Default is 16kHz. |
| S2 | `language_code=en-IN`, `mode=codemix`. The locale enum's first value `unknown` is an auto-detect sentinel, not a language. |
| S3 | Billing is **per second of audio submitted**, not socket wall-clock. ₹30/hour = ₹0.50/audio-min, rounded up per request, 1s minimum. **VAD-gate the feed** — cuts the bill ~65%. |
| S4 | **Concurrency is the binding constraint:** 20 sockets (Starter) / 100 (Pro *and* Business). Business buys 4,000 req/min but still only 100 concurrent sockets. |
| S5 | The response `metrics` object carries `audio_duration` and `processing_latency`. **Measure our own TTFT from day one** — Sarvam's "sub-150ms" claim appears on neither Artificial Analysis nor Coval. |
| S6 | **Sarvam gives no semantic end-of-turn.** Deepgram Flux bundled STT + turn-taking; Sarvam does not. That capability moves in-process (§5). This is the biggest structural change in the migration. |

---

## 5. Turn detection (in-process, CPU)

| Layer | Component | Cost |
|---|---|---|
| Acoustic VAD | Silero VAD v5 ONNX | 189µs / 31.25ms chunk |
| Semantic EOT | Pipecat Smart Turn v3.1 (BSD-2, 8MB) | 12ms modern CPU, ~60ms small AWS instance |
| Backchannel rejection | custom classifier + token list | in-process |

| # | Rule |
|---|---|
| T1 | 🔴 **Pipecat issue #3844:** `WhisperFeatureExtractor` hardcodes 16kHz, so 8kHz telephony audio is read at **double speed with no error**. Measured damage: 6/20 utterances flipped Complete/Incomplete, mean turn duration fell 51%, phone numbers fragmented. **Mitigation: upsample 8k→16k before the detector, and assert the detector's expected input rate at startup.** |
| T2 | Zero network in the turn-taking path. Both models run locally in ap-south-1. Total EOT decision ~100–250ms including hangover. |
| T3 | **Minimum speech duration 500ms** before treating input as a barge-in (matches LiveKit and jambonz defaults). Detection threshold — not buffer flush — is the real barge-in latency. |
| T4 | Barge-in flush: drop our queue → send `clearAudio` → compute `played_ms` → truncate history. ~100–200ms of committed audio still plays; the handset de-jitter buffer (40–100ms) is unretractable. That is fine. |
| T5 | EOT thresholds are **per persona**: `eot_threshold` 0.5–0.9 (default 0.7), plus a lower `eager_eot_threshold` that must be ≤ it. Candidate persona → patient. Receptionist → eager. |

---

## 6. LLM

| # | Rule |
|---|---|
| L1 | Target **Gemini 2.5 Flash-Lite** on Vertex `asia-south1` — the only Gemini tier with thinking **OFF by default**. Gemini 3 Flash defaults to `high` (floor `minimal`); 3.1 Flash-Lite defaults to `minimal` with **no off switch** — the entire 3.x tier structurally cannot be non-reasoning (~15× latency gap on Artificial Analysis). |
| L2 | ⚠️ The `asia-south1` claim is **mis-sourced** in the research — Google's pricing page names no regions. **Settle it by re-running `/bench/ttft` from an ap-south-1 box** across Vertex asia-south1 / Vertex global / Groq / `gpt-4.1-nano`. Keep all four behind the provider interface. |
| L3 | **Cache the system prompt explicitly.** The persona fact block is large and constant per call. |
| L4 | **Do not use Claude Haiku 4.5 here.** Its 4,096-token minimum cacheable prefix means a 2k-token persona prompt **silently will not cache**, pushing ₹0.39 → ~₹1.18/min. |
| L5 | Speculative execution (fire on STT partial at the eager threshold, discard if the caller continues) buys 100–200ms for +50–70% LLM calls = **₹0.03/min**. Worth it. Ship behind a feature flag. |
| L6 | 2.5 Flash-Lite is exempt from the +10% non-global-endpoint surcharge, which Google scoped to *"the Generally available Gemini 3 and later families"* from 2026-07-01. |

---

## 7. TTS

> 🔴 **Superseded 2026-07-22 by [context.md](context.md) decision 21: TTS stays on ElevenLabs.** Sarvam and Cartesia were both evaluated and rejected on listening — Indian-English naturalness is the criterion that decides this project, and ElevenLabs won it. P1 and P7 below are therefore **research, not the plan**; P4/P5 (Sarvam) and P8 (rejections) still stand as vendor facts. The live config is `output_format=ulaw_8000` on `wss://api.elevenlabs.io/v1/text-to-speech/{voice}/stream-input`, voice `MmiGAbOYCaIFzgNItUWa` ("Krish - Modern Creator"), which satisfies C1 with zero transcode. **P6 is now load-bearing, not a fallback note:** ElevenLabs' character-level alignment is the exact solution to Bug A and should be used when Bug A is implemented.

| # | Rule |
|---|---|
| P1 | ~~**Cartesia Sonic 3.5**~~ **(superseded — see above)**, `container=raw, encoding=pcm_mulaw, sample_rate=8000` — the only shortlisted vendor emitting G.711µ directly. **Zero transcode in the hot path.** WebSocket *input* streaming with continuations matches the existing [agent.py](shuo/agent.py) design. |
| P2 | Concurrency ceilings: 8 Free / 12 Pro / 20 Startup ($49/mo) / 60 Scale ($299/mo). Startup covers a 5–20 concurrent pilot. |
| P3 | Voice cloning: ≤10s clip, self-serve from Pro. Cloned voices bill 1 credit/char; *Pro Voice Clone* voices bill 1.5×. |
| P4 | 🔴 **Never use Sarvam Bulbul v3 for streaming.** Its streaming WebSocket **emits MP3 only** — the enum lists `mulaw`/`alaw` but the field description says *"Currently supports MP3 only"*. 8kHz MP3 is MPEG-2.5 LSF with **576-sample (~72ms) granules** that do not align to 20ms frames. Disqualifying. |
| P5 | **Do** use Bulbul v3 **REST** for the offline filler cache — REST *does* support `mulaw` @ 8000Hz, is INR-billed, 37 speakers across 11 Indic locales. Latency is free when rendered ahead of time. |
| P6 | Keep ElevenLabs as fallback. Its **character-level alignment** (`charStartTimesMs`, `charDurationsMs`) is the cleanest reference solution to Bug A — note those are relative to *each returned chunk*, not the full response. |
| P7 | Phase 4 A/B: **Inworld Realtime TTS 1.5 Mini** — ₹0.44/conv-min vs Cartesia's ₹1.15, ~120ms median / <130ms P90 (published with percentiles). Gate the switch on verifying 8kHz µ-law output and Indian-English quality. |
| P8 | Rejected: Deepgram Aura-2 (no cloning product at all), Azure CNV (Limited-Access gated, ~₹2.6L/yr per voice), Google Chirp 3 HD (**not served from asia-south1** — Mumbai's cell in the endpoints matrix is empty). |

---

## 8. Human-likeness

| # | Rule |
|---|---|
| H1 | **Sample the response gap, never fix it.** Stivers et al. (PNAS 2009, 10 languages): modal human gap is **0ms**, median +100ms, mean +208ms. A 0ms reply does **not** feel robotic — what reads as machine is the *same* gap every turn, and replying before the caller finished. Sample median ~250ms, p90 ~700ms; longer for dispreferred/negative answers. |
| H2 | Hindi/Indian-English turn-taking timing data **does not exist** (documented negative — PubMed returns zero; Hindi was not in Stivers' sample). Cheapest defensible differentiator: **measure floor-transfer offsets on 50 recorded human calls from our own vertical.** |
| H3 | **Fillers must be gated on real latency.** Maintain rolling p50/p80 of LLM TTFT segmented by turn type. `predicted_ttfb = p80(turn_type) + tts_ttfb`. Arm only if `predicted_ttfb > 400ms` or a tool call is in flight. Ungated fillers are net-negative (Pfeifer & Bickmore, IVA '09). |
| H4 | Filler tiers: **0–350ms → say nothing** (a filler inside the median human gap *is* the tell); T+350ms → 250–400ms clip; T+1200ms → longer acknowledgement; T+3000ms → explicit hold + 4s keepalive. |
| H5 | Pre-render the cache: ~40 phrases × 3 prosodic variants = 120 clips, 8kHz µ-law, **keyed on a voice hash**, rebuilt only when voice/model/pace changes. ~₹7 one-off. **0ms TTFB** — the only zero-latency component in the pipeline, and it covers the greeting, where the caller forms their latency impression. |
| H6 | Indian filler inventory: `haan ji`, `ji`, `ek second`, `ek minute`, `theek hai`, `achha`, `sahi hai`, `bas`, `dekhiye`. Mid-utterance hedges (`matlab`, `toh`) belong in the **LLM's own text**, not the clip cache. Note Retell's Hindi defaults omit `ji` — the deference marker that matters most. |
| H7 | **Suppress false barge-in** in layers: min speech duration 500ms + min word count + backchannel token list + resume playback if the "interruption" resolves to a backchannel. Indian callers backchannel heavily and Indian mobile calls carry background speech. |
| H8 | **Do not add background ambience.** No evidence it helps; Retell's `ambient_sound` defaults to `null` and its "call-center" preset is **US room tone** — an American cue on a call claiming to be Indian is a net-negative tell. |

---

## 9. Persona layer (Digital Twin)

| # | Rule |
|---|---|
| D1 | A persona is **data**: system prompt + grounded fact block + voice ID + turn-taking profile (T5) + filler inventory + greeting clip. No persona logic in the loop. |
| D2 | **Grounded facts only.** The prompt forbids inventing anything outside the fact block and provides an explicit *"I don't have that detail"* escape hatch. A gap is always better than a fabrication. |
| D3 | **Never break character** — no meta-references to being an AI, a model, or a prompt. Identity probes get deflected in-persona. |
| D4 | Register: Indian English with natural Hinglish code-mixing. Lakh/crore, Indian phone-number grouping, Indian date forms. |
| D5 | The **candidate** persona must sustain 20–45s answers. This makes Bug A (played-ms truncation) and T3/T4 (barge-in) *critical path*, not polish — interviewers interrupt long answers constantly. |
| D6 | Every persona ships with an **adversarial test set**: identity probes, contradiction traps across turns, unanswerable specifics, mid-question pauses, rapid topic switches, and hostile/pressuring interviewers. |

---

## 10. Verification gates

| Layer | Gate |
|---|---|
| Unit | `python -m pytest tests/ -v` — 307 existing lines pass **unchanged** after Phase 1; extended for the 3-phase machine in Phase 3. |
| Carrier | Replay captured `start`/`media`/`stop` against the parser, **including media-before-start (V6)** and a `maxRetries` reconnect with a new `streamId` (A6). |
| Codec | Assert byte-exact µ-law round-trip and that **no L16 path is reachable** (C2). |
| Turn detector | Feed 8kHz fixtures, assert the detector **received 16kHz**, compare turn-duration distribution against a 16kHz control (guards T1/#3844). |
| Player | Assert exactly **50 frames/second over 60s with <1 frame of drift**, and `played_ms` matches wall-clock within 20ms. |
| Latency | `/bench/ttft` from ap-south-1 vs the existing baseline in [scripts/llm-bench.txt](scripts/llm-bench.txt) (`gpt-4.1-nano` 369ms, `gpt-4o-mini` 383ms). Add a `/bench/tts` twin measuring TTFB per provider from Mumbai. |
| End-to-end | Real calls to Jio / Airtel / Vi handsets: (1) mouth-to-ear by two-phone recording + cross-correlation, target p50 ≤ 600ms; (2) barge-in stops audio within ~200ms, `clearAudio` acked, history truncated to what was heard; (3) "haan… haan" during agent speech does **not** stop playback; (4) injected LLM delay → silence below 350ms, filler above; (5) blind A/B the cloned voice against the founder with 10 Indian listeners. |
| Persona | Run the D6 adversarial set. Score: character breaks, hallucinated facts, self-contradictions, and mean/variance of response gap. |
| Cost | Reconcile one week of Vobiz / Sarvam / Cartesia / Vertex invoices against the model. The two most likely wrong: Vobiz's ₹0.45/min, and whether Sarvam's *WebSocket* path truly bills audio-duration (a ~2.9× swing). |
