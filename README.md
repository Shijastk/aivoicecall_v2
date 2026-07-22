# shuo 说

A voice agent framework in ~2,500 lines of Python, re-targeted at Indian telephony.

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

## How it works

Two abstractions, one pure function:

- **Carrier** — telephony behind one interface. Vobiz (production, AWS ap-south-1) and Twilio (regression path) both drop in behind it.
- **Agent** — self-contained LLM → TTS → Player pipeline, owns conversation history
- **`process_event(state, event) → (state, actions)`** — the entire state machine, no I/O, fully tested

Everything streams. LLM tokens feed TTS immediately, TTS audio feeds the carrier immediately. On barge-in the agent cancels everything and flushes the carrier's audio buffer.

```
LISTENING ──EndOfTurn──→ RESPONDING ──Done──→ LISTENING
    ↑                        │
    └────StartOfTurn─────────┘  (barge-in)
```

The agent is a **multi-persona Digital Twin**: the persona (system prompt, grounded facts, voice, turn-taking profile) is runtime configuration, selected per call. Outbound picks it at originate time; inbound resolves it from the dialled number.

## Project structure

```
shuo/
  types.py              # Immutable state, events, actions
  state.py              # Pure state machine
  conversation.py       # Main event loop
  agent.py              # LLM → TTS → Player pipeline
  config.py             # Carrier-neutral configuration
  log.py                # Colored logging
  server.py             # FastAPI endpoints
  tracer.py             # Per-turn latency spans
  carrier/
    base.py             # Carrier + CarrierSession interfaces
    vobiz.py            # Vobiz <Stream> (production)
    twilio.py           # Twilio Media Streams (regression path)
  services/
    flux.py             # Deepgram Flux (STT + turns)
    llm.py              # Streaming LLM
    tts.py              # ElevenLabs WebSocket streaming
    tts_pool.py         # TTS connection pool (warm spares)
    player.py           # Paces audio out through the carrier
scripts/
  fake_vobiz.py         # Protocol-accurate Vobiz stand-in (no account needed)
  bench_sarvam.py       # Full-pipeline latency benchmark
```

Project context lives in [CLAUDE.md](CLAUDE.md) (scope and rules), [context.md](context.md) (current state), [rules.md](rules.md) (engineering constraints and carrier traps), and [plan.md](plan.md) (the research this is built on).

## Setup

Requires Python 3.9+, [ngrok](https://ngrok.com/), a Vobiz account, and API keys for Deepgram, OpenAI/Groq, and ElevenLabs.

Configure your [ngrok authentication token](https://dashboard.ngrok.com/get-started/your-authtoken):
```bash
ngrok config add-authtoken <YOUR_NGROK_AUTH_TOKEN>
```

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your keys
ngrok http 3040             # in another terminal — put the URL in PUBLIC_URL
python main.py +919876543210
```

Switch carriers with `CARRIER=vobiz|twilio` in `.env`. Nothing else changes.

## Testing without a phone call

`scripts/fake_vobiz.py` speaks the Vobiz protocol — signed webhooks and all — so the whole transport can be exercised locally:

```bash
python main.py                                        # terminal 1
python scripts/fake_vobiz.py                          # terminal 2
python scripts/fake_vobiz.py --mode media-before-start
python scripts/fake_vobiz.py --mode reconnect
```

## Tests

```bash
python -m pytest tests/ -v
```

The state machine is pure, so every transition is tested without I/O or mocks.

## License

MIT
