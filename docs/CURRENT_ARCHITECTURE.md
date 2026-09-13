# Current architecture

**VERIFIED IN CODE** at the revision in [README](README.md); runtime success is
not implied. See [KNOWN_ISSUES](KNOWN_ISSUES.md) for gaps and historical conflicts.

## Entrypoints, routes and trust boundaries

`main.py::main` validates selected carrier and pipeline environment requirements,
starts Uvicorn on `0.0.0.0`, default port 3040, and optionally originates through
`_place_and_flush` → `shuo/server.py::place_outbound_call`. Its SIGTERM handler
sets `_draining` and waits for `_active_calls` subject to `DRAIN_TIMEOUT`.
`config_api.py::main` starts a separate app on `127.0.0.1:3041` by default and
calls `shuo/config_api.py::enforce_exposure_policy` before binding.

| Call-server routes (`shuo/server.py`) | Responsibility |
|---|---|
| GET `/health` | Health, carrier/drain state and write statistics |
| GET/POST `/answer`, `/twiml` | Authenticate webhook, resolve context and return carrier stream XML |
| POST `/stream-status`, `/recording-status` | Carrier status callbacks |
| GET/POST `/ring`, `/hangup` | Attempt progression and terminal classification |
| GET/POST `/call/{phone_number:path}` | Admin-gated origination |
| GET `/calls/live`, `/calls/active`; POST `/calls/current/hangup` | Admin-gated monitor/control |
| GET `/trace/latest`, `/bench/ttft` | Admin-gated trace retrieval / external LLM benchmark |
| WS `/ws` | Carrier media, stream-token verification before accept |
| GET and WS `/v2/call/{persona}/{language}` | Imported V2 router; GET gives connection instructions |

`_authenticate` delegates webhook signatures to the selected carrier;
`_public_request_url` reconstructs the public callback URL. `_require_admin`
fails closed without a configured token. `shuo/config.py::mint_stream_token` /
`verify_stream_token` bind persona, direction, attempt and expiry for `/ws`.
These gates are not proof that V2 has the same protections: `v2_browser_call`
accepts directly and does not use the carrier token or active-call counter.

`shuo/config_api.py` exposes GET/PUT `/v1/agent/config`, `/v1/agent/persona`,
`/v1/agent/knowledge`; GET `/v1/config`, `/v1/voices`, `/health`,
`/v1/calls/history`, `/v1/calls/active`, `/v1/calls/live`,
`/v1/calls/{call_id}/recording`; POST `/v1/test-call`,
GET `/v1/test-call/status`, POST `/v1/test-call/hangup`.
`_guard_request` enforces the optional config token and body-size boundary;
error handlers produce a `message` envelope. `shuo/call_client.py::_request`
uses HTTP to the call server, not imports of conversation code. The current
server mounts V2, not the config API. Old API proposals are not this contract.

## Carrier call and media flow

```text
Inbound webhook / outbound originate → answer XML → signed /ws
  → CarrierSession.parse_message → domain events → event queue
  → process_event → FeedFlux / StartAgentTurn / ResetAgentTurn
  → Flux → turn events; Agent → Groq tokens → ElevenLabs → AudioPlayer
  → CarrierSession.play_audio / checkpoint / clear → carrier
```

`shuo/server.py::place_outbound_call` creates an attempt ID and persists call
revisions through SPOOL; `/answer` resolves inbound DID/persona or explicit
query context. `Carrier.originate` supplies answer/ring/hangup callback URLs.
`VobizCarrier.answer_xml` explicitly sets bidirectional, keepCallAlive, inbound
track and µ-law content type; `maxRetries="0"` avoids requesting carrier retries.
`TwilioCarrier.answer_xml` produces Connect/Stream XML with inbound track.

`shuo/carrier/base.py::CarrierSession.parse_message` tracks call/stream IDs and
bounds pre-start media with a deque. A start drains buffered events after the
start event. `VobizSession._parse_one` and `TwilioSession._parse_one` decode
base64 media and normalize start/stop, playback acknowledgment and DTMF where
available. Vobiz supports `playedStream` and `clearedAudio`; its outbound frames
are `playAudio`, `clearAudio`, `checkpoint`, `stop`. Twilio uses media/clear/mark
and has no outbound stream-stop frame (`_stop_frame` returns None).
`VobizSession._check_media_format` logs a reported incompatible format but does
not reject the stream; missing metadata returns without checking. The downstream
µ-law assumption is therefore not enforced by a fail-closed format gate.

`shuo/conversation.py::run_conversation` creates a per-WebSocket session, event
queue, monitor, local tape and tracer. Its reader turns socket close into
StreamStopEvent. A repeated start inside this conversation preserves the Agent
and its history while the state machine cancels an active turn. This is not
persistent history recovery across a new WebSocket/process. The event queue is
currently unbounded; Bluetooth bounded queues are a future requirement.

## State, STT and response generation

`shuo/state.py::process_event` is pure: inbound MediaEvent yields FeedFluxAction
in both LISTENING and RESPONDING; other tracks yield no feed action. Nonempty
FluxEndOfTurnEvent starts a response only while listening. FluxStartOfTurnEvent
interrupts responding. AgentTurnDoneEvent returns to listening. Stream start
updates identifiers; restart/stop while responding yields ResetAgentTurnAction.
Playback/clear/DTMF events are inert in the pure state function; the orchestration
layer observes them. This is a two-phase conversation state, separate from the
richer call-history lifecycle.

`shuo/services/flux.py::FluxService.start` connects Deepgram EU listen v2 with
`flux-general-en`, encoding mulaw, rate 8000. `_on_message` maps TurnInfo
EndOfTurn/StartOfTurn to callbacks; Update is a monitor partial, not an agent
trigger. No Sarvam/Silero/Smart Turn migration is wired here. Close/fatal/error
logging exists; automatic provider recovery must not be inferred.

`shuo/agent.py::Agent.start_turn` borrows a TTS connection, creates a per-turn
player and starts the persistent `LLMService`. `_generate` uses Groq's OpenAI-
compatible streaming API, default model `llama-3.3-70b-versatile`, max_tokens 500,
temperature 0.7. Tokens reach `_on_llm_token` → TTS.send as they arrive; LLM done
flushes TTS. `TTSService.start` requests ElevenLabs `ulaw_8000` WebSocket audio;
`_handle_message` dispatches audio/completion and provider failures. `TTSPool`
pre-connects, rebinds callbacks and replenishes/evicts connections for one voice.

## Playback, interruptions and completion

`shuo/services/player.py` fixes FRAME_BYTES=160, FRAME_SECONDS=0.020 and three
frames of pre-roll. `AudioPlayer._next_frame` pads a final partial frame with
µ-law silence; underruns wait for real audio, then re-anchor without inserting
silence. `_sleep_until` uses `time.perf_counter`, permits bounded catch-up when
lateness is at most 100 ms and re-anchors beyond that. Thus the old documentation
claim that every late frame permanently adds delay is not the current algorithm.
The byte buffer is not capacity-bounded today.

`Agent.cancel_turn` cancels LLM, TTS and active player; player stop clears local
and carrier audio. Generated transcript is marked interrupted. LLM history
retains generated text (partial text plus ellipsis on cancellation), not a
sample-aligned record of what was heard. No played-audio history truncation is
implemented in these functions. `AudioClearedEvent` voids completion but does not
implement a wait-before-next-playback barrier.

`AudioPlayer` sends a turn checkpoint after dispatch; `Agent._on_playback_done`
arms `shuo/conversation.py::_TurnCompletion`. Matching acknowledgment or grace
expiry queues AgentTurnDoneEvent; interruption/restart voids old checkpoints.
`Agent._on_tts_done` ends a no-audio turn immediately without a checkpoint.
Dispatch catches turn-start failures, cancels that turn and queues completion.
These containment paths do not prove universal recovery from all service errors.

## Cleanup and observation

`run_conversation` finally ends the monitor, cancels/awaits the reader, closes
completion, waits up to five seconds for background recording-start work, cleans
agent/pool/Flux, sends session stop, saves trace, queues final history and tape
conversion, then drains SPOOL. Background tasks still pending after that wait
are not explicitly cancelled there; teardown is not an absolute leak-free claim.
`shuo/server.py::hangup_current_call` owns operator hangup handling. Stream stop
must precede carrier REST hangup; `CarrierSession.send_stop` is idempotent.

`CallTape.caller` is fed only on FeedFluxAction; `AudioPlayer._send_frame` tees
outbound dispatched frames. These are separate from carrier recording requests.
`CallMonitor` bounds calls/events and publishes revisions through Spool.
`Spool.submit` never waits for disk on its normal running-loop path; overflow
can discard older work and counts losses. `call_history.load` folds revisions,
while `call_status` prevents status logic being tied to persistence.
`config_api.get_active_calls` merges live summaries and history;
`get_live_call` can fall back to history. Stable `endedCode` is for clients;
`endedReason` is human text.

`Notifier` runs from config API lifespan, reads history via `to_thread`, primes
without replaying old notifications and sends bounded/rate-limited metadata
notifications. `Tracer.save`, unlike spooled history/recording, currently writes
synchronously. Do not generalize the off-loop-write design to every I/O site.

## Configuration boundary

`ConfigStore.load` reads a JSON document and tolerates load errors; save uses
`_atomic_write`. `runtime_config.load_call_settings` is intentionally synchronous
once at call setup before pool creation. The resolved frozen CallSettings applies
to that call; saves affect later calls. Do not describe this existing read as
threaded or turn-by-turn hot reload. `AgentV2` replaces the inherited LLM with
its evaluative prompt, so carrier prompt/voice settings are not guaranteed to
control V2 speech in the same way.

## Browser/V2 differences

The active loop is `shuo/v2/api_v2.py::v2_browser_call`.
`shuo/v2/conversation_v2.py::run_web_conversation` is a separate similar function,
not called by that route. BrowserSession base64-decodes incoming media with no
codec negotiation/conversion, labels it inbound and feeds the shared µ-law Flux
service. AgentV2 sends outgoing provider chunks directly, bypassing the player
though the superclass still constructs one. It completes on TTS done, not actual
browser playback; BrowserSession sends a mark message but does not parse returned
marks. No browser clear-audio operation is supplied by this adapter.

V2 emits thinking/speaking/listening, partial/final transcripts and supported
emotions from EvaluativeLLMService. Its silence check runs after dequeued events,
not on an independent timer. Cleanup cancels the reader without awaiting it and
closes completion/agent/pool/Flux, emits stop and saves traces. It uses MONITOR
but does not create CallTape or initiate carrier recording. Equivalent recording,
authentication, drain, interruption or lifecycle guarantees are not established.

## Exact audio boundaries

| Boundary | Code-verified contract / limit |
|---|---|
| Carrier media → session → Flux | Base64 G.711 µ-law 8,000 Hz, single inbound track; raw decoded bytes; Vobiz constants and FluxService.start |
| ElevenLabs → player | Requested `ulaw_8000`, base64 audio; no PCM conversion in carrier path (`TTSService.start`) |
| Player → carrier | 160 µ-law bytes per full 20 ms frame, base64; Vobiz short `audio/x-mulaw` + integer 8000, Twilio media envelope |
| Local recording | µ-law tracks decoded offline to 8,000 Hz, 16-bit little-endian stereo WAV; `_write_wav`, caller left/agent right |
| Browser → Flux | Base64 bytes forwarded unchanged; Flux expects µ-law/8000, browser production format UNKNOWN |
| Shunya → browser | Request `response_format="pcm"`; rate/width/channels/endian not specified/validated here; first RIFF chunk strips fixed 44 bytes |
| Azure → browser | Request `raw-16khz-16bit-mono-pcm`; base64 HTTP chunks; code has no explicit byte-order validation |
| Bluetooth | No implemented application boundary; supplied reference S16LE/16,000/mono is in BLUETOOTH_ARCHITECTURE |
