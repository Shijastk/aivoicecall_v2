# Known issues, evidence limits and documentation conflicts

No issue was fixed in this documentation task. All fixes below are **OUT OF SCOPE**
and **not authorized**. Source findings are VERIFIED IN CODE at the snapshot;
impact at runtime is unverified unless explicitly attributed to supplied history.
Every listed code issue predates any Bluetooth implementation in this task.

## Recorded historical failures

Historical 775 passed / 4 failed is task-owner evidence only, not a current result.
Full provenance and signatures are UNKNOWN; see [TESTING](TESTING.md).

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
its audio availability is validated but digital E2E, automated call control,
latency/echo and broader compatibility are not. These are roadmap gates, not
current defects with authorized fixes. Discovery property schema, codec library,
queue/latency budgets, manual-abort procedure and release support policy remain
TBD. Do not read live devices to fill gaps during this task.
