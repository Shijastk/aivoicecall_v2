# Current SHUO application

**VERIFIED IN CODE** at the revision in [README](README.md). This describes this
Python backend, not an independently inspected frontend or deployed system.

## Purpose and modes

SHUO connects caller speech to a conversational agent and generated speech.
`main.py::main` supports server-only inbound operation and optional outbound
number/persona arguments. `shuo/server.py::answer`, `place_outbound_call` and
`websocket_endpoint` implement carrier calling. `shuo/carrier/__init__.py::get_carrier`
selects Vobiz (default from `shuo/config.py::carrier_name`) or Twilio and caches
instances. Both implement `Carrier` and `CarrierSession` in
`shuo/carrier/base.py` for account call control and session media respectively.

`shuo/config.py::persona_for_did` resolves inbound DID routing;
`shuo/types.py::CallContext` carries persona/direction/attempt identifiers.
`shuo/agent.py::Agent` stores the persona ID, but this tree has no implemented
`shuo/persona/` catalog. Operator configuration supplies one resolved prompt and
voice through `shuo/runtime_config.py::load_call_settings`; passing a persona ID
is not proof of independent per-persona profiles.

Browser calling is registered on the call-server app through
`shuo/v2/api_v2.py::v2_browser_call` at `/v2/call/{persona}/{language}`.
`BrowserSession` accepts JSON start/media/stop and emits audio, transcript,
state, emotion and stop messages. These modules implement WebSockets, not
WebRTC. Frontend playback cannot be verified from this backend repository.

## Providers and conversation responsibilities

| Component | Implementation and responsibility |
|---|---|
| STT/turn detection | `shuo/services/flux.py::FluxService`: Deepgram EU endpoint, `flux-general-en`, µ-law/8000; EndOfTurn/StartOfTurn drive turns; Update feeds partial transcript |
| State/event loop | `shuo/state.py::process_event`, `shuo/types.py`; `shuo/conversation.py::run_conversation` performs dispatch and I/O |
| LLM | `shuo/services/llm.py::LLMService`: Groq via AsyncOpenAI, `LLM_MODEL` default `llama-3.3-70b-versatile`; streams tokens, keeps per-service history |
| Carrier TTS | `shuo/services/tts.py::TTSService`: ElevenLabs WebSocket text streaming, `ulaw_8000`; `tts_pool.py::TTSPool` warms voice-specific connections |
| Playback | `shuo/services/player.py::AudioPlayer`: reframe, pre-roll, deadline pacing, buffer clearing and checkpoints |
| Agent | `shuo/agent.py::Agent`: token → TTS → player callbacks, cancellation, tracing and monitor transcript |
| V2 agent | `shuo/v2/agent_v2.py::AgentV2`, `llm_v2.py::EvaluativeLLMService`: extracts intervention text/emotion from streamed model output; sends frontend updates |
| V2 TTS | `shuo/v2/services/tts_pool_v2.py::TTSPoolV2` returns `services/tts_router.py::V2TTSRouter`: English/default to Shunya, `ml` to Azure |

V2 Shunya/Azure `send` accumulates text until `flush`; their HTTP responses
stream chunks directly through `AgentV2._on_tts_audio`, bypassing carrier pacing.
The duplicate `shuo/v2/tts_router.py` is not the router imported by TTSPoolV2.
V2 uses the same English Flux configuration even for `ml`; a language route
parameter does not prove multilingual STT success.

## Configuration and operator services

`config_api.py::main` runs `shuo/config_api.py::app` separately on loopback 3041
by default; `main.py::start_server` runs calls on 3040 by default. `Procfile`
starts only `python main.py`. The config app exposes configuration, catalog,
test-call, monitoring, history and recording APIs. `shuo/call_client.py` proxies
operator requests over HTTP with an admin header, validates numbers/personas,
translates failures and applies a cooldown.

`shuo/config_store/models.py::ConfigDocument` models agent/persona/knowledge;
`store.py::ConfigStore` loads and atomically replaces the JSON document;
`voices.py::resolve_voice` resolves catalog IDs. `CallSettings` is frozen per
call; `assemble_system_prompt` joins base, constraints and facts. Missing or
malformed settings fall back rather than rejecting calls. Environment variable
names/defaults are defined in `shuo/config.py`, entrypoints and providers.

## Recording, monitoring and persistence

- `shuo/recording.py::CallTape` tees inbound µ-law and dispatched agent frames;
  `_write_wav` builds local 8 kHz, 16-bit stereo WAV (caller left, agent right).
  `SHUO_LOCAL_RECORDING` is independent of carrier `RECORD_CALLS`.
- `shuo/carrier/vobiz.py::start_recording` requests REST recording; `answer_xml`
  also implements XML recording mode. TwilioCarrier supplies recording flags.
  Actual carrier channel contents are not proven by request code.
- `shuo/call_monitor.py::CallMonitor`/`CallRecorder` maintain bounded in-memory
  calls/events, transcript, timing and archive submissions.
- `shuo/call_history.py::revision`, `append`, `merge`, `load` persist and fold
  append-only revisions with stable attempt IDs. `shuo/call_status.py` owns
  status classification and machine-readable ended codes.
- `shuo/spool.py::Spool` bounds queued writes, runs them through a worker with
  `asyncio.to_thread`, counts drops/failures and exposes drain/close.
- `shuo/notify.py::Notifier` watches history from the config API lifespan and
  sends selected metadata notifications when configured; no URL means disabled.
- `shuo/tracer.py::Tracer` records spans/markers and writes JSON traces.
  `shuo/log.py` supplies service/event logging.

## Dependencies, external boundaries and tests

`requirements.txt` declares FastAPI/Uvicorn/WebSockets/httpx, multipart,
Twilio, NumPy, dotenv, OpenAI, Deepgram, ElevenLabs, matplotlib, pytest and
pytest-asyncio with minimum versions. `audioop-lts` is commented, not active.
V2 imports `aiohttp` but it is not directly declared; installed availability
is unknown. Dependency changes require separate approval.

External boundaries are carrier HTTP/WebSockets, Deepgram, Groq, ElevenLabs,
V2 Shunya/Azure and optional notification HTTP; local boundaries include operator
HTTP and configuration/history/recording/trace files. Providers receive speech or
text (`FluxService.send`, `LLMService._generate`, TTS `send`/`flush`); notifications
are not the only outbound data flow. No live service success was tested here.

`tests/` covers state, carriers, fake transport integration, player/timing, Flux,
TTS failure/completion, configuration, lifecycle, history, monitoring, recording,
spool, notifications and logging. `tests/conftest.py` adds scripts to imports and
isolates history/recordings in temporary paths. `scripts/fake_vobiz.py::VobizProtocol`
is reused by integration tests. `scripts/test_v2_keys.py` contains provider probes,
not a safe offline regression command. See [TESTING](TESTING.md).
