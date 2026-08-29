# shuo 说

A **multi-persona, emotionally intelligent voice Digital Twin** for Indian telephony — a real-time AI voice agent that answers and places phone calls over a standard 10-digit Indian DID, in ~2,500 lines of Python behind a Next.js control panel.

```bash
python main.py +919876543210
```

```
🚀 Server starting on port 3040
✓  Ready https://mature-spaniel-physically.ngrok-free.app
   carrier=vobiz  persona=candidate  recording=dual-channel
📞 Calling +919876543210...
✓  Call initiated SID: fake-cal...
🔌 WebSocket connected
▶  Stream started SID: 4a1f9c2e...
← Flux EndOfTurn "So, tell me about your experience with Python."
◆ LISTENING → RESPONDING
→ Start Agent "So, tell me about your experience with Python."
← Carrier played "turn-1"
← Agent turn done
◆ RESPONDING → LISTENING
```

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Tech stack](#2-tech-stack)
3. [Architecture & the process split](#3-architecture--the-process-split)
4. [Cloning & installation](#4-cloning--installation)
5. [Environment variables](#5-environment-variables)
6. [Running it](#6-running-it)
7. [Ngrok & Vobiz telephony setup](#7-ngrok--vobiz-telephony-setup)
8. [Testing & verification](#8-testing--verification)
9. [Project structure](#9-project-structure)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What it does

`shuo` is a personal AI voice assistant that holds a **real, interruptible phone conversation**. A caller speaks; speech-to-text detects the end of their turn; a streaming LLM starts answering token-by-token; those tokens feed text-to-speech immediately; the resulting µ-law audio is paced back out to the carrier one 20 ms frame at a time. Nothing is buffered end-to-end — that streaming chain is the whole latency advantage.

The **persona is runtime configuration, not code**. One pipeline serves many roles, selected at call setup: outbound calls pick a persona when they are placed, inbound calls resolve one from the number that was dialled.

| Phase | Persona | Direction | Status |
|---|---|---|---|
| **Test Phase 1** | **Job candidate being interviewed** | outbound | **current focus** |
| Future | Technical interviewer | outbound | planned |
| Future | HR recruiter | outbound | planned |
| Future | Inbound receptionist | inbound | planned |

The job-candidate persona is deliberately first because it is the *hardest*: the candidate does not control the conversation, so it maximally stresses the four failure modes this project hunts — character break, hallucination, context loss, and robotic delivery.

### The latency contract

Real-time conversation is the product, so the numbers are specified rather than hoped for:

| Metric | Target |
|---|---|
| **Server-side turn latency** (end-of-turn → first TTS byte queued) | **300–450 ms** ← the number we own and optimise |
| Mouth-to-ear (includes PSTN + carrier legs) | 500–750 ms (p50 ≈ 600 ms) |
| Perceived, with gated fillers | ~250 ms |

Everything in this README that looks like over-engineering — the two-process split, the µ-law-only audio path, the monotonic frame pacing — exists to protect that first row.

### The state machine

```
LISTENING ──EndOfTurn──→ RESPONDING ──Done──→ LISTENING
    ↑                        │
    └────StartOfTurn─────────┘  (barge-in)
```

`process_event(state, event) → (state, actions)` is a **pure function with zero I/O** ([shuo/state.py](shuo/state.py)). Every capability enters as new events and actions, never as a side effect inside the machine — which is why every transition is testable without mocks or a network.

---

## 2. Tech stack

### Frontend — the operator control panel

| | |
|---|---|
| Framework | **Next.js 16** (App Router, Server Actions, Route Handlers) |
| UI | **React 19**, **Tailwind CSS v4**, **shadcn/ui** on Radix primitives |
| Icons | lucide-react |
| Language | TypeScript 5 |

### Backend — the call server & config API

| | |
|---|---|
| Language | **Python 3.12** (3.9+ supported) |
| Web / transport | **FastAPI**, **Uvicorn**, **WebSockets** (`websockets`, native FastAPI WS) |
| Speech-to-text + turn detection | **Deepgram Flux** |
| LLM | **Groq** (`llama-3.3-70b-versatile`), OpenAI-compatible client |
| Text-to-speech | **ElevenLabs** WebSocket streaming, `output_format=ulaw_8000` |
| Telephony carrier | **Vobiz** (production, Indian DID) · **Twilio** (regression path) |
| Validation / config | Pydantic, python-dotenv |
| Tests | pytest, pytest-asyncio |

**Audio is µ-law 8 kHz end to end.** No L16 path is reachable anywhere in the pipeline — TTS is asked for µ-law directly, so audio arrives in the only codec that reaches the caller and is never transcoded.

---

## 3. Architecture & the process split

```
                 ┌─────────────────────────────┐
   Browser ──────│  Next.js control panel      │  :3000
                 │  (Server Actions only —     │
                 │   never talks to :3040)     │
                 └──────────────┬──────────────┘
                                │ HTTP (server-side, loopback)
                                ▼
                 ┌─────────────────────────────┐
                 │  Config & Test-Call API     │  :3041   python config_api.py
                 │  FastAPI · disk I/O · auth  │  127.0.0.1 only
                 └──────────────┬──────────────┘
                                │ HTTP over loopback (shuo/call_client.py)
                                │ X-Shuo-Admin-Token
                                ▼
   PSTN ◄──── Vobiz ◄────┐  ┌─────────────────────────────┐
                         └──│  Call Server                │  :3040   python main.py
                            │  FastAPI + WebSocket media  │  0.0.0.0 (ngrok/ALB)
                            │  20 ms player deadline      │
                            └─────────────────────────────┘
                                        │
              Deepgram Flux ──► Groq ──► ElevenLabs ──► Player (20 ms frames)
```

### Why two Python processes, on two ports

**This is the single most important architectural fact in the repo, and it is not optional.**

The call server paces audio out at **one 160-byte frame every 20 ms** against a monotonic deadline. When a deadline is missed, the player **re-anchors rather than catching up** — so a single blocking operation on that event loop does not cost one frame, it costs *permanent added stream delay for the remainder of the call*. A ~50 ms blocking disk write while an operator presses **Save** would be audible to the person on the phone, forever, until they hang up.

So the two concerns live in two processes that share nothing but a JSON file:

| | Call server — `:3040` | Config API — `:3041` |
|---|---|---|
| Entrypoint | `python main.py` | `python config_api.py` |
| Binds | `0.0.0.0` (must be internet-reachable) | `127.0.0.1` (loopback is the security boundary) |
| Workload | Soft-real-time audio, 20 ms deadline | HTTP forms, blocking disk writes |
| Blocking I/O | **Forbidden** | Deliberate and safe |
| Auth | Carrier webhook signatures + `SHUO_ADMIN_TOKEN` on operator routes | Loopback, plus optional `SHUO_CONFIG_API_TOKEN` |

Consequences worth internalising before you touch the code:

- **Never mount config routes on `shuo/server.py`.** The split is only real if nothing crosses it.
- **The two processes talk over HTTP on loopback, never by importing across the seam.** [shuo/call_client.py](shuo/call_client.py) is the entire conversation between them, and a test pins that no `conversation`/`agent`/`server`/`state` import reaches it.
- **The browser never holds `SHUO_ADMIN_TOKEN`.** The browser talks to Next, Next talks to `:3041` server-side, and only `:3041` holds the credential `:3040` demands. Reaching the control panel is therefore *not* the same as being able to spend money on PSTN calls.

### Vendor isolation

No vendor SDK is imported outside its provider module. Swapping ElevenLabs ↔ Cartesia ↔ Inworld, or Vobiz ↔ Twilio, is a config change (`CARRIER=vobiz|twilio`) and nothing else.

---

## 4. Cloning & installation

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12 recommended (3.9+ works) | On 3.13+, uncomment `audioop-lts` in `requirements.txt` |
| Node.js | 20+ (24 tested) | For the Next.js panel |
| ngrok | any | Needed only for real telephony |
| Accounts | Vobiz, Deepgram, Groq, ElevenLabs | Vobiz optional for local testing — see the fake carrier |

### Layout

The backend and the control panel are **two repositories, checked out side by side**. The panel defaults to `http://127.0.0.1:3041`, so this layout works with zero configuration:

```
01_Projects/
├── shuo/            ← this repo (Python backend)
└── shuo-frontend/   ← Next.js control panel
```

### Backend

```bash
git clone <backend-repo-url> shuo
cd shuo

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env              # then fill it in — see §5
```

### Frontend

```bash
cd ..
git clone <frontend-repo-url> shuo-frontend
cd shuo-frontend

npm install
```

The panel needs no `.env.local` for local development — `SHUO_API_URL` defaults to the config API's loopback address. Create one only to override that or to force the mock backend (§5).

> **Note:** `.gitignore` covers `.env*`, so a fresh clone has no secrets and no config. `var/` (the operator's stored prompt and knowledge base) is also ignored — it can contain somebody's résumé.

---

## 5. Environment variables

### Backend — `shuo/.env`

Start from [.env.example](.env.example), which documents every variable and the trap behind it. The essentials:

#### Required to place or receive a call

| Variable | Purpose |
|---|---|
| `PUBLIC_URL` | Externally reachable base URL, **no trailing slash**. Your ngrok domain in dev, the ALB hostname in prod. The carrier fetches `/answer` and forks media to `/ws` here. |
| `CARRIER` | `vobiz` (production) or `twilio` (regression path only). |
| `VOBIZ_BASE_URL` | `https://api.vobiz.ai/api/v1` — **without** the trailing `/Account/` segment. Path casing is significant; Vobiz returns 401 (not 404) on wrong casing. |
| `VOBIZ_AUTH_ID` / `VOBIZ_AUTH_TOKEN` | Trunk credentials. |
| `VOBIZ_PHONE_NUMBER` | The 10-digit Indian DID in E.164 (`+91…`). A dummy value is fine locally. |
| `DEEPGRAM_API_KEY` | Speech-to-text + turn detection (Flux). |
| `GROQ_API_KEY` | LLM. |
| `ELEVENLABS_API_KEY` | Text-to-speech. |

#### Servers & ports

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `3040` | Call server port. |
| `CONFIG_API_PORT` | `3041` | Config API port. |
| `CONFIG_API_HOST` | `127.0.0.1` | Binding off-loopback **without** `SHUO_CONFIG_API_TOKEN` is refused at startup, not warned about. |
| `DRAIN_TIMEOUT` | `300` | Seconds to let live calls finish on SIGTERM. |
| `SHUO_CONFIG_PATH` | `<repo>/var/agent_config.json` | Where operator configuration is persisted. Must survive restarts — not a temp dir. |
| `SHUO_TRACE_DIR` | `<system temp>/shuo` | Per-call latency traces. |

#### Security — read this part

| Variable | Purpose |
|---|---|
| **`SHUO_ADMIN_TOKEN`** | Gates the money-spending and privacy-sensitive routes on `:3040` (`/call`, `/calls/live`, `/calls/current/hangup`, `/trace/latest`, `/bench/ttft`), sent as `X-Shuo-Admin-Token`. **Set it in the environment of *both* processes** — `:3041` presents it to `:3040` on the panel's behalf. Leave it unset and those routes **fail closed**, refusing to serve at all. Inbound calls do not need it. |
| `SHUO_CALL_SERVER_URL` | Default `http://127.0.0.1:3040`. Where `:3041` finds the call server for the test-call button. |
| `SHUO_CONFIG_API_TOKEN` | Optional shared secret for `:3041`, sent as `X-Shuo-Config-Token`. Deliberately *not* `SHUO_ADMIN_TOKEN` — the panel sends no auth header today, so reusing that one would silently break every save. |
| `SHUO_STREAM_SECRET` | Signs the `wss://` URL in the answer XML, binding the media socket to one persona, direction and expiry. Defaults to `VOBIZ_AUTH_TOKEN`. |
| `SHUO_NOTIFY_URL` | Optional. Push notifications for inbound / missed / failed calls, from `:3041` only. **Unset means off — no task, no request.** 🔴 On ntfy the topic in the URL *is* the credential: anyone holding it reads every notification, so generate it long and random and keep it out of the panel and out of logs (nothing prints it; `/health` shows `https://ntfy.sh/(redacted)`). It is also **the only thing in the system that sends call data off the machine** — metadata only, never a transcript. |
| `SHUO_NOTIFY_TOKEN` | Optional bearer credential for the notification target. A public ntfy.sh topic does not need one. |
| `VALIDATE_WEBHOOK_SIGNATURES` | Default `true`. A *missing* signature header is a failure, not a reason to skip the check. Only set false against a carrier whose signing secret you do not hold — the bundled fake Vobiz signs correctly, so tests do not need it. |

Generate the tokens with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

#### Persona, voice & recording

| Variable | Default | Purpose |
|---|---|---|
| `PERSONA` | `candidate` | Persona used when nothing more specific applies. |
| `PERSONA_ROUTES` | — | Inbound routing: `+911234567890:receptionist,+911234567891:recruiter`. |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | Groq model. |
| `OPENAI_API_KEY` | — | Used by `/bench/ttft` and the OpenAI-compatible client. |
| `ELEVENLABS_VOICE_ID` | `JBFqnCBsd6RMkjVDRZzb` (George) | See the warning below. |
| `RECORD_CALLS` | `true` | Dual-channel recording — caller on one channel, agent on the other. A test instrument, not a feature: channel separation is how character breaks and real mouth-to-ear latency get scored. |

> ⚠️ **A voice appearing in `/v1/voices` does not mean you can synthesise it.** Voices with `category: professional` are *library* voices requiring a paid ElevenLabs entitlement. On a free plan they return `payment_required` and emit **zero audio** — the call is silent, not merely off-accent, and the refusal arrives ~500 ms after a socket that connected cleanly. `category: premade` voices work on every plan. **Prove a voice by synthesising with it, never by finding it in a listing.**

### Frontend — `shuo-frontend/.env.local`

All of these are **server-side only** (deliberately not `NEXT_PUBLIC_`), so none reach the browser bundle.

| Variable | Default | Purpose |
|---|---|---|
| `SHUO_API_URL` | `http://127.0.0.1:3041` | The config API. The default is the working value, so a fresh clone talks to the real service rather than silently to a stub. Override for deployment. |
| `SHUO_USE_MOCK` | unset | `=1` routes everything to the in-process mock — the only way to work on the forms with the backend stopped. |
| `SHUO_MOCK_FAIL` | unset | `=1` forces the mock's failure path, for exercising error rendering. |
| `SHUO_MOCK_LATENCY_MS` | — | Artificial mock latency. |

---

## 6. Running it

The full system is **three processes in three terminals**. Start them in this order.

### Terminal 1 — Call server (`:3040`)

```bash
cd shuo
source .venv/bin/activate           # Windows: .venv\Scripts\activate

python main.py                      # server-only — waits for inbound calls
python main.py +919876543210        # outbound call, default persona
python main.py +919876543210 candidate   # outbound with an explicit persona
```

It refuses to start with a clear list if any required variable is missing. Verify:

```bash
curl localhost:3040/health          # {"status":"ok","carrier":"vobiz","draining":false}
```

### Terminal 2 — Config & Test-Call API (`:3041`)

```bash
cd shuo
source .venv/bin/activate
python config_api.py
```

```bash
curl localhost:3041/health          # check "test_call_ready": true
curl localhost:3041/v1/config       # stored + resolved view — what the agent would run with
curl localhost:3041/v1/voices       # voices that can actually be synthesised
```

`test_call_ready: false` means `SHUO_ADMIN_TOKEN` is unset **in this process** — the single most likely reason a test call fails on a fresh machine, and the one thing that cannot be diagnosed from the panel.

<details>
<summary><strong>Config API surface</strong></summary>

```
PUT  /v1/agent/config          { "voiceModel": "...", "systemPrompt": "..." }
PUT  /v1/agent/persona         { "rules": "..." }
PUT  /v1/agent/knowledge       { "context": "..." }
GET  /v1/voices                synthesisable voice catalogue
GET  /v1/config                stored + resolved view
POST /v1/test-call             { "phoneNumber": "+91…", "persona": "…" }  ← rings a real phone
GET  /v1/test-call/status      ?since=<seq> for incremental updates
POST /v1/test-call/hangup
GET  /health
```

Every failure returns `{"message": "<a sentence>"}` — never `detail`, never a stack trace — because the panel renders that string verbatim next to the button the operator pressed.
</details>

### Terminal 3 — Next.js control panel (`:3000`)

```bash
cd shuo-frontend
npm run dev
```

Open **http://localhost:3000**. Pages: `/` (dashboard), `/agent` (system prompt + voice), `/persona` (rules), `/knowledge` (fact block), `/calls` (test call + live transcript).

### Terminal 4 (optional) — Test call from the CLI

Needs **both** Python processes running and `SHUO_ADMIN_TOKEN` set in both. This rings a real phone and spends real money:

```bash
curl -X POST localhost:3041/v1/test-call \
     -H 'content-type: application/json' \
     -d '{"phoneNumber":"+919876543210"}'

curl localhost:3041/v1/test-call/status              # live state, transcript, latency
curl "localhost:3041/v1/test-call/status?since=42"   # only what is new
curl -X POST localhost:3041/v1/test-call/hangup
```

A 10-second cooldown guards the endpoint that spends money against a double-click.

---

## 7. Ngrok & Vobiz telephony setup

### Expose port 3040 — and only 3040

The carrier needs to reach the **call server** to fetch call-control XML and to fork media over a WebSocket. It never needs the config API.

> 🔒 **Never tunnel `:3041`.** It binds loopback because loopback *is* its security boundary — that API decides what the twin says on a live phone call, and exposing it without `SHUO_CONFIG_API_TOKEN` is refused at startup anyway. The panel on `:3000` also stays local; it reaches `:3041` server-side.

```bash
ngrok config add-authtoken <YOUR_NGROK_AUTH_TOKEN>
ngrok http 3040
```

Copy the `https://` forwarding URL into `.env` and **restart the call server** — the URL is read at process start:

```bash
PUBLIC_URL=https://your-subdomain.ngrok-free.app     # no trailing slash
```

A reserved ngrok domain is strongly recommended. Free ephemeral URLs change on every restart, and each change means re-editing `.env` *and* re-configuring the Vobiz number.

### Configure the Vobiz number

In the Vobiz console, point your DID at the tunnel:

| Vobiz setting | Value |
|---|---|
| **Answer URL** | `https://<your-ngrok-domain>/answer` (method `POST`) |
| **Hangup URL** | `https://<your-ngrok-domain>/hangup` |
| **Ring URL** *(optional)* | `https://<your-ngrok-domain>/ring` |

The call server adds the rest itself when it answers: `/stream-status` for stream lifecycle callbacks, `/recording-status` for completed recordings, and the signed `wss://…/ws` media socket carrying `persona` and `direction` in its query string.

`/answer` serves **both directions**. Outbound calls carry `?persona=` from origination; inbound calls resolve the persona from the dialled number via `PERSONA_ROUTES`, so one number per Digital Twin role needs no code change.

### Two things that will bite you

1. **Webhook signatures are validated against `PUBLIC_URL` + the request path**, never against `request.url`. Behind ngrok or an ALB, `request.url` is the internal `http://localhost:…` form, so validating against it fails on *every* live call. If signature validation rejects everything, `PUBLIC_URL` does not match what the carrier actually called — check the scheme, the trailing slash, and that you restarted after editing it.
2. **The answer URL must respond well under a second.** A slow `/answer` is dead air on the caller's handset.

### Testing the transport with no Vobiz account

[scripts/fake_vobiz.py](scripts/fake_vobiz.py) speaks the Vobiz protocol — signed webhooks and all — so the entire transport can be exercised locally, including its nastier edge cases:

```bash
python main.py                                        # terminal 1
python scripts/fake_vobiz.py                          # terminal 2
python scripts/fake_vobiz.py --mode media-before-start
python scripts/fake_vobiz.py --mode reconnect
```

---

## 8. Testing & verification

### Backend

```bash
cd shuo
python -m pytest tests/ -v          # full suite
python -m pytest tests/ -q          # quiet
python -m pytest tests/test_update.py -v   # the pure state machine
```

**Expected: `511 passed`.** The state machine is pure, so every transition is tested without I/O, network, or mocks. `tests/test_update.py` must stay green — it is the contract on `process_event`.

### Frontend

```bash
cd shuo-frontend
npm run build       # type-check + production build
npm run lint        # eslint
```

**Expected: `✓ Compiled successfully`** with 11 routes (7 static, 4 dynamic API handlers).

### Latency benchmarks

No telephony needed for either:

```bash
python scripts/bench_sarvam.py              # full-pipeline latency
curl localhost:3040/bench/ttft \
     -H "X-Shuo-Admin-Token: $SHUO_ADMIN_TOKEN"    # LLM time-to-first-token comparison
python scripts/getfreevocies.py             # re-measure which voices synthesise
python scripts/probe_tts.py                 # TTS socket probe
```

Vendor latency claims do not earn a place in the pipeline until they are benchmarked from `ap-south-1`. Measure, don't trust.

### End-to-end smoke test

```bash
curl localhost:3040/health                  # call server up
curl localhost:3041/health                  # config API up, test_call_ready true
curl localhost:3041/v1/config               # what the agent would actually run with
# then: open :3000, edit the prompt, Save, and place a test call
```

---

## 9. Project structure

```
shuo/
  types.py              # Immutable state, events, actions
  state.py              # Pure state machine — zero I/O
  conversation.py       # Main event loop
  agent.py              # LLM → TTS → Player pipeline
  config.py             # Carrier-neutral configuration + URL builders
  runtime_config.py     # Resolves the stored config for a call
  log.py                # Colored logging
  server.py             # Call server FastAPI app          :3040
  tracer.py             # Per-turn latency spans
  carrier/
    base.py             # Carrier + CarrierSession interfaces
    vobiz.py            # Vobiz <Stream> (production)
    twilio.py           # Twilio Media Streams (regression path)
  services/
    flux.py             # Deepgram Flux (STT + turn detection)
    llm.py              # Streaming LLM
    tts.py              # ElevenLabs WebSocket streaming
    tts_pool.py         # TTS connection pool (warm spares)
    player.py           # Paces audio out at 20ms monotonic frames

  # ── Configuration plane. No import crosses into the code above. ──
  config_api.py         # Config + test-call REST API        :3041
  call_client.py        # The ONLY link to :3040 — HTTP over loopback
  call_monitor.py       # Bounded in-memory live-call buffer
  config_store/
    models.py           # The three DTOs + the anti-robotic ruleset
    store.py            # One JSON document, replaced atomically
    voices.py           # Measured, synthesisable voice catalogue

main.py                 # Call server entrypoint             :3040
config_api.py           # Config API entrypoint              :3041
scripts/
  fake_vobiz.py         # Protocol-accurate Vobiz stand-in (no account needed)
  bench_sarvam.py       # Full-pipeline latency benchmark
  probe_tts.py          # TTS socket probe
  getfreevocies.py      # Re-measure which voices synthesise
tests/                  # 511 tests
```

### Further reading

| Document | What it holds |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Scope, non-negotiable rules, persona rules |
| [context.md](context.md) | Living project state, phase tracker, **decision log** |
| [rules.md](rules.md) | Engineering constraints and carrier traps |
| [plan.md](plan.md) | The original research archive (read CLAUDE.md §2 corrections first) |

---

## 10. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `Missing environment variables: …` on startup | Copy `.env.example` → `.env` and fill it in. The message lists exactly what is missing. |
| Config API exits immediately at startup | You set `CONFIG_API_HOST` off-loopback without `SHUO_CONFIG_API_TOKEN`. That is refused, not warned about. |
| Test call fails; `test_call_ready: false` | `SHUO_ADMIN_TOKEN` is unset **in the config API's environment**. It must be set for *both* processes. |
| Operator routes on `:3040` return 403 | `SHUO_ADMIN_TOKEN` unset (they fail closed) or the `X-Shuo-Admin-Token` header is missing/wrong. |
| Every webhook rejected as an invalid signature | `PUBLIC_URL` does not match the URL the carrier actually called. Check scheme, trailing slash, and restart after editing. |
| Call connects but is **completely silent** | The configured ElevenLabs voice is a `professional` library voice your plan cannot synthesise — it emits zero audio. Switch to a `premade` voice (e.g. `JBFqnCBsd6RMkjVDRZzb`). |
| Vobiz returns 401 rather than 404 | Wrong path casing in `VOBIZ_BASE_URL`. Casing is significant; do not lowercase it. |
| Panel saves succeed but change nothing | `SHUO_USE_MOCK=1` is set — everything is going to the in-process mock. |
| Panel cannot reach the service | The config API is not running, or `SHUO_API_URL` points somewhere else. Check `curl localhost:3041/health`. |
| `audioop` import error | Python 3.13+ removed it. Uncomment `audioop-lts` in `requirements.txt`. |

---

## Contributing rules

Four rules override convenience, every time:

1. **The state machine stays pure.** New capabilities enter as events and actions — never as a side effect inside `process_event`.
2. **Never break the streaming chain.** Token-level LLM → TTS → Player streaming is the entire latency advantage. Nothing buffers a full response.
3. **Vendor code stays behind a provider interface.** No vendor SDK imported outside its provider module.
4. **µ-law 8 kHz end to end.** No L16 path may be reachable.

The dev machine is Windows; the deploy target is Linux `ap-south-1`. Do not introduce POSIX-only paths.

---

## License

MIT
