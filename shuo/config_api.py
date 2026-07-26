"""
Configuration REST API -- the seam between the Next.js panel and the agent.

    PUT  /v1/agent/config      { voiceModel, systemPrompt }
    PUT  /v1/agent/persona     { rules }
    PUT  /v1/agent/knowledge   { context }
    GET  /v1/voices            the voices that can actually be synthesised
    GET  /v1/config            the whole document (operator verification)
    POST /v1/test-call         ring the operator's phone (W3)
    GET  /v1/test-call/status  what the twin is doing right now (W3)
    POST /v1/test-call/hangup  end the call in progress (W3)
    GET  /health

**This is a separate FastAPI app on a separate port in a separate process
from `shuo/server.py`, and that is the single most important thing about
it.**

Not a design preference -- a latency one. `server.py`'s app shares its event
loop with the media WebSocket, and the player emits a 160-byte frame every
20ms against an accumulating monotonic deadline (decision 22/23). A handler
that writes a 50KB knowledge block and fsyncs it blocks that loop for as long
as the disk takes, and decision 24 removed the player's ability to claw
lateness back: a missed deadline now re-anchors, so a blocking write does not
cost a frame, it costs permanent stream delay for the rest of the call.
Mounting these routes on the call-serving app would mean an operator pressing
Save could audibly degrade a call in progress.

Two processes make that impossible rather than unlikely. They share nothing
but the config file on disk, and the file is swapped with `os.replace`, so
the reader cannot observe a partial write either.

Consequences worth stating:

- **No CORS configuration, and none is needed.** The caller is the Next.js
  *server* (`transport.ts` is server-side only -- `SHUO_API_URL` is
  deliberately not `NEXT_PUBLIC_`), so requests do not originate in a
  browser. Adding CORS here would only widen the surface.
- **The error envelope is `{"message": "..."}`,** not FastAPI's default
  `{"detail": ...}`. The panel reads `body.message` and renders it verbatim
  in the save row; anything else and every rejection collapses to "The agent
  service returned 422", losing the reason.
- **Loopback by default.** See `enforce_exposure_policy`.

W3 added three routes that reach the *call* server, and they do not weaken
any of the above: they speak HTTP to :3040 over loopback through
`call_client.py` and import nothing from the audio pipeline. The credential
that :3040 demands (`SHUO_ADMIN_TOKEN`, decision 16) is held here, on the
server side of the seam, so the panel can ask for a call without ever
holding the ability to place one.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import call_client
from .config_store import (
    AgentConfig,
    ConfigStore,
    KnowledgeConfig,
    PersonaConfig,
    resolve_voice,
    selectable_voices,
)
from .log import get_logger

logger = get_logger("shuo.config_api")


# =============================================================================
# LIMITS AND POLICY
# =============================================================================

# Ceiling on the raw request body. The largest legitimate payload is a 50,000
# character knowledge block, which is at most ~200KB once it is UTF-8 encoded
# and JSON-escaped. 512KB leaves generous headroom and still refuses a body
# large enough to matter, which uvicorn will otherwise buffer in full before
# a single validator runs.
MAX_BODY_BYTES = 512 * 1024

# Addresses that are not reachable from off the machine.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_TOKEN_HEADER = "x-shuo-config-token"


def config_api_token() -> str:
    """
    Optional shared secret for this API.

    Separate from `SHUO_ADMIN_TOKEN` on purpose. Reusing that one would be
    tidier but is a trap: it is very likely already set (decision 16 requires
    it for `/call`), and the panel sends no auth header today, so reuse would
    mean this API rejects every save the moment someone enables outbound
    calling. A distinct variable makes enabling auth here a deliberate act
    with a matching one-line change in the panel's `transport.ts` headers.
    """
    return os.getenv("SHUO_CONFIG_API_TOKEN", "")


def enforce_exposure_policy(host: str) -> None:
    """
    Refuse to start in a configuration that would be open on the internet.

    Decision 16's rule is that operator endpoints fail closed rather than
    default to open, and this API is squarely an operator endpoint -- it
    decides what the twin says on a real phone call, which is a more
    interesting thing to hijack than a trace.

    The rule is applied through the bind address rather than by demanding a
    token unconditionally. Bound to loopback there is no remote attacker to
    authenticate, and requiring a token there would break the panel for no
    security gain. Bound anywhere else there is, so a token becomes
    mandatory.

    Raises `RuntimeError` rather than warning, because a warning on a control
    panel that is quietly world-writable is not a control.
    """
    if host in _LOOPBACK_HOSTS:
        return
    if config_api_token():
        return
    raise RuntimeError(
        f"Refusing to bind the config API to {host!r} without "
        f"SHUO_CONFIG_API_TOKEN set. This API decides what the agent says on "
        f"a live call, so it must not be reachable off-host unauthenticated. "
        f"Either bind to 127.0.0.1 (the default) or set the token and send "
        f"it as the {_TOKEN_HEADER} header."
    )


# =============================================================================
# APP
# =============================================================================

@asynccontextmanager
async def _lifespan(_: FastAPI):
    """Nothing to warm; one pooled HTTP client to release."""
    yield
    await call_client.close()


app = FastAPI(
    title="shuo config API",
    summary="Operator configuration for the voice agent. Loopback-only.",
    version="1",
    lifespan=_lifespan,
)

# One store for the process. Cheap, stateless, holds no handles -- it re-reads
# from disk on every load so nothing here goes stale.
store = ConfigStore()


def _error(message: str, status_code: int) -> JSONResponse:
    """
    The only error shape this API emits.

    `message` is rendered verbatim to the operator, so it must be a sentence
    they can act on -- never a code, never a stack trace, never a raw vendor
    body.
    """
    return JSONResponse({"message": message}, status_code=status_code)


# ── Guards ────────────────────────────────────────────────────────────

@app.middleware("http")
async def _guard_request(request: Request, call_next):
    """
    Body-size ceiling and optional token check, before any handler runs.

    Both live in middleware rather than a dependency so they also cover
    routes added later -- a guard that has to be remembered per endpoint is a
    guard that will be forgotten on one.
    """
    expected = config_api_token()
    if expected:
        supplied = request.headers.get(_TOKEN_HEADER) or ""
        # compare_digest, not `==`: the comparison is against a secret.
        if not hmac.compare_digest(supplied, expected):
            logger.warning(
                f"Rejected {request.method} {request.url.path} from "
                f"{request.client.host if request.client else 'unknown'}: "
                f"bad or missing {_TOKEN_HEADER}"
            )
            return _error("Not authorised. Nothing was saved.", 401)

    # Content-Length is advisory -- a chunked request has none -- so this
    # catches the ordinary oversized POST cheaply and the field validators
    # catch everything else. It is a guard against buffering an absurd body,
    # not a security boundary.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _error(
            f"The request body is too large "
            f"({int(declared):,} bytes; the limit is {MAX_BODY_BYTES:,}). "
            f"Nothing was saved.",
            413,
        )

    return await call_next(request)


# ── Error handling ────────────────────────────────────────────────────

def _field_name(loc: tuple[Any, ...]) -> Optional[str]:
    """The field a validation error points at, ignoring the 'body' prefix."""
    for part in reversed(loc):
        if isinstance(part, str) and part != "body":
            return part
    return None


# Every message this API emits ends by saying what happened to the
# operator's data -- it is the only way they learn whether to retype what
# they had. On a route that places a call rather than saving a field, the
# same sentence has to say what happened to the *call*, or the panel reports
# "Nothing was saved" about a phone that did not ring.
_NOTHING_SAVED = "Nothing was saved."
_NO_CALL = "No call was placed."


def _outcome_for(path: str) -> str:
    return _NO_CALL if path.startswith("/v1/test-call") else _NOTHING_SAVED


def _sentence_for(error: dict, outcome: str = _NOTHING_SAVED) -> str:
    """
    Turn one pydantic error into something an operator can act on.

    The field validators in `config_store.models` already raise finished
    sentences; pydantic wraps those as `"Value error, <sentence>"`, so the
    work here is mostly unwrapping. The other branches exist because a
    contract mismatch has to be *diagnosable from the save row* -- "the field
    voiceModel is missing" tells the person wiring the panel exactly what to
    fix, where "422" tells them to go read logs on a machine they may not
    have.
    """
    kind = error.get("type", "")
    raw = str(error.get("msg", "")).strip()
    field = _field_name(tuple(error.get("loc", ())))

    if kind == "value_error":
        return raw.removeprefix("Value error, ")

    if kind == "json_invalid":
        return f"The request body was not valid JSON. {outcome}"

    if kind == "missing":
        return (
            f'The field "{field}" is missing from the request. {outcome}'
            if field
            else f"The request body was missing. {outcome}"
        )

    if kind == "extra_forbidden":
        # The outcome goes last, like every other message here. It is the
        # part the operator needs -- whether to retype what they lost -- and
        # burying it mid-sentence is how it gets skimmed past.
        return (
            f'"{field}" is not a field this endpoint accepts — check the '
            f"payload against the API contract. {outcome}"
        )

    if field:
        return f'The field "{field}" is not valid: {raw}. {outcome}'
    return f"The request was not valid: {raw}. {outcome}"


@app.exception_handler(RequestValidationError)
async def _on_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Rejected input, reported in this API's envelope.

    400 rather than FastAPI's default 422: the panel shows the message either
    way, but 422 in a log reads like a framework artefact where 400 reads
    like a decision. Only the first error is reported -- the save row is one
    line, and a list of five is a list nobody reads.
    """
    outcome = _outcome_for(request.url.path)
    errors = exc.errors()
    message = (
        _sentence_for(errors[0], outcome)
        if errors
        else f"The request was not valid. {outcome}"
    )
    logger.warning(f"Rejected {request.method} {request.url.path}: {message}")
    return _error(message, 400)


# FastAPI raises a bare 400 with this detail when the body cannot be decoded
# at all -- most plausibly a client that sent something other than UTF-8. It
# arrives as an HTTPException rather than a validation error, so it misses the
# handler above, and its wording is the framework's rather than this API's.
# Found by sending a body with invalid UTF-8 to the running service.
_FASTAPI_BODY_PARSE_DETAIL = "There was an error parsing the body"


@app.exception_handler(StarletteHTTPException)
async def _on_http_error(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """
    Re-envelope framework errors (404, 405, unreadable body) so nothing emits
    `detail` and every message ends by saying what happened to the data.

    That last part is not cosmetic: "Nothing was saved" is the only way the
    operator learns whether to retype what they had. A message in the
    framework's voice omits it.
    """
    detail = str(exc.detail)
    if detail == _FASTAPI_BODY_PARSE_DETAIL:
        detail = (
            f"The request body could not be read — it must be UTF-8 encoded "
            f"JSON. {_outcome_for(request.url.path)}"
        )
    return _error(detail, exc.status_code)


@app.exception_handler(Exception)
async def _on_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """
    Anything unexpected -- a disk that is full, a permissions error on the
    config path.

    The detail is logged, never returned: the panel renders the message
    verbatim to an operator, and a stack trace or a filesystem path in that
    row is both useless to them and more than they should be shown.
    """
    logger.error(f"{request.method} {request.url.path} failed: {exc!r}")

    if request.url.path.startswith("/v1/test-call"):
        return _error(
            "The agent service could not reach the call server. No call was "
            "placed — check the service logs.",
            500,
        )

    return _error(
        "The agent service could not store the configuration. Nothing was "
        "saved — check the service logs.",
        500,
    )


# =============================================================================
# ROUTES
# =============================================================================

@app.get("/health")
async def health() -> dict:
    """
    Liveness, plus where this process is reading and writing config.

    `test_call_ready` is false when `SHUO_ADMIN_TOKEN` is unset here, which
    is the single most likely reason a test call fails on a fresh machine --
    and the one thing that cannot be diagnosed from the panel, since the
    failure happens a process away.
    """
    return {
        "status": "ok",
        "service": "shuo-config-api",
        "config_path": str(store.path),
        "configured": store.path.exists(),
        "call_server": call_client.call_server_url(),
        "test_call_ready": bool(call_client.admin_token()),
    }


@app.put("/v1/agent/config")
async def put_agent_config(payload: AgentConfig) -> dict:
    """
    Store the system prompt and the voice model.

    Returns the stored section, which is what the panel types the response
    as. `updatedAt` rides along as an extra field; the panel's `AgentConfig`
    type simply ignores it.

    Blocking disk I/O in an async handler is deliberate and safe *here* --
    this process carries no audio. See the module docstring.
    """
    section = store.save_agent(payload)
    logger.info(
        f"Agent config saved  voice={section.voice_model}  "
        f"prompt={len(section.system_prompt)} chars"
    )
    return section.model_dump(by_alias=True)


@app.put("/v1/agent/persona")
async def put_persona_config(payload: PersonaConfig) -> dict:
    """
    Store the persona rules -- the anti-robotic constraints.

    Two things this endpoint guarantees, both of which are easy to break by
    accident:

    1. **The text is stored as written.** Ends trimmed, interior whitespace
       untouched. The ruleset is paragraph-separated prose and a normalising
       pass that collapsed the blank lines would silently rewrite an
       operator's constraints into one wall of text.
    2. **Cleared stays cleared.** An empty body is accepted and persisted as
       empty; it is not quietly replaced with the default. The default only
       applies to an install where this endpoint was never called at all
       (`ConfigDocument.resolved_persona_rules`).
    """
    section = store.save_persona(payload)
    logger.info(
        f"Persona rules saved  {len(section.rules)} chars"
        + ("" if section.rules else "  (cleared — no constraints)")
    )
    return section.model_dump(by_alias=True)


@app.put("/v1/agent/knowledge")
async def put_knowledge_config(payload: KnowledgeConfig) -> dict:
    """
    Store the unstructured knowledge base text.

    Stored as one opaque block. Nothing is parsed into fields, because
    inventing a schema for this text is how a fact block acquires facts
    nobody wrote -- the exact hallucination failure the persona rules forbid
    (CLAUDE.md §4.1).
    """
    section = store.save_knowledge(payload)
    logger.info(f"Knowledge saved  {len(section.context)} chars")
    return section.model_dump(by_alias=True)


@app.get("/v1/voices")
async def get_voices() -> dict:
    """
    The voices an operator may pick, in the panel's own `VoiceModel` shape.

    Serving this from the backend is the answer to open question 11. The
    picker's four fixture IDs were not voices -- none could be synthesised,
    and one (`el-rachel-v2`, its *default*) names a voice that is not in the
    workspace at all -- so the panel offered a menu on which every item was
    broken and the config API stored whatever was chosen. The catalogue is
    backend-owned because only the backend can know the answer: entitlement
    is a property of the API key, proven by synthesising (decision 29).

    Only selectable voices are listed. An operator cannot be offered
    something `PUT /v1/agent/config` would refuse -- the two read from one
    table, so the picker and the validator cannot disagree.

    Each entry carries `previewUrl`: an ElevenLabs-hosted MP3 for the
    picker's play button, handed over as a string for the panel to put in an
    `<audio>`. Nothing here fetches or proxies it, so a preview costs this
    process nothing and cannot delay the response. It may be `null`, and the
    panel must render that row without a play button rather than an empty
    player.

    Six of those URLs are minted with an embedded timestamp rather than being
    stable object URLs (`config_store.voices._PREVIEWS` names them, George
    among them). If they turn out to expire, the fix is a
    `GET /v1/voices/{id}/preview` here that resolves the voice and 302s to a
    freshly listed URL -- deliberately not built yet, because it trades a
    constant for a per-press upstream call and we have no evidence of expiry.

    Unauthenticated in the same sense the rest of this API is: loopback, or
    the shared token. There is nothing secret in the list.
    """
    voices = selectable_voices()
    return {
        "voices": [voice.model_dump(by_alias=True) for voice in voices],
        "count": len(voices),
    }


@app.get("/v1/config")
async def get_config() -> dict:
    """
    The whole document, for verification.

    Deliberately *not* the per-screen read path the panel has deferred -- it
    returns one document rather than three endpoints, so it does not
    prejudge the shape `getAgentConfig`/`getPersonaConfig` will want. It
    exists so a save can be confirmed with `curl` and so Phase 2 can check
    what the agent is about to read without starting a call.

    `resolved` is the reader's view: what the agent would actually run with,
    including the persona ruleset standing in for a section that was never
    written.

    `resolved.voice` is the one entry that is *looked up* rather than read:
    the stored `voiceModel` is a catalogue ID, and the question an operator
    is asking with this curl is whether the next call will produce audio.
    `null` there means the saved value no longer resolves -- which happens
    for real when a plan change alters entitlements, so the stored string
    stays valid-looking while the voice behind it stops working.
    """
    document = store.load()
    voice = resolve_voice(document.resolved_voice_model or "")
    return {
        "stored": document.model_dump(by_alias=True),
        "resolved": {
            "systemPrompt": document.resolved_system_prompt,
            "voiceModel": document.resolved_voice_model,
            "voice": voice.model_dump(by_alias=True) if voice else None,
            "personaRules": document.resolved_persona_rules,
            "knowledge": document.resolved_knowledge,
        },
    }


# =============================================================================
# TEST CALL (W3)
# =============================================================================
#
# These three are the only routes here that talk to another process. They do
# it over HTTP to :3040 through `call_client.py`, never by importing it --
# the two servers stay two servers, and the split that decision 32 exists to
# protect is unchanged.
#
# The call is a *real* one over the carrier to a real handset. There is no
# browser-microphone path and no simulated audio, deliberately: the thing
# being measured is real-world latency and audio quality over Indian
# telephony, and a WebRTC loopback in the operator's browser measures
# neither.

class TestCallRequest(BaseModel):
    """
    `POST /v1/test-call` -- ring this number and let the twin answer it.

    Not part of the stored configuration and deliberately not modelled next
    to it in `config_store/models.py`: the number an operator happens to be
    testing from is harness state, not something the twin is. The panel keeps
    it in `localStorage`; nothing here persists it.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    phone_number: str = Field(alias="phoneNumber")
    persona: Optional[str] = None


@app.post("/v1/test-call")
async def post_test_call(payload: TestCallRequest) -> dict:
    """
    Place an outbound test call. **This rings a phone and spends money.**

    The whole point of the process split shows up in this handler: it holds
    `SHUO_ADMIN_TOKEN` and the panel does not. The browser asks Next, Next
    asks this loopback service, and only this service can satisfy :3040's
    operator gate (decision 16). Reaching the panel is therefore not the same
    as being able to place calls -- you have to be on the host.

    Validation happens here rather than at the carrier because
    `/call/{number}` prepends a `+` to whatever it is handed, so a typo would
    otherwise come back as an opaque 502 with the carrier's reason
    deliberately swallowed.
    """
    try:
        number = call_client.validate_phone_number(payload.phone_number)
        persona = call_client.validate_persona(payload.persona)
    except ValueError as exc:
        return _error(str(exc), 400)

    try:
        result = await call_client.place_call(number, persona)
    except call_client.CallServerError as exc:
        logger.warning(f"Test call to {number} refused: {exc.message}")
        return _error(exc.message, exc.status_code)

    logger.info(f"Test call placed  to={number}  persona={persona or 'default'}")
    return {
        "status": result.get("status", "calling"),
        "to": result.get("to", number),
        "persona": result.get("persona"),
        "carrier": result.get("carrier"),
        "callId": result.get("call_id") or "",
    }


@app.get("/v1/test-call/status")
async def get_test_call_status(
    since: int = Query(0, ge=0, description="`nextSeq` from the previous poll"),
    call: Optional[str] = Query(None, description="Which call to report on"),
) -> Any:
    """
    What the twin is doing right now, for the split-pane panel.

    A read-only view of the call server's in-memory ring buffer: state,
    transcript, the config provenance line and per-turn latency milestones.
    No audio and no audio-derived data crosses this boundary -- not a sample,
    not an amplitude envelope -- so the µ-law-end-to-end rule has nothing to
    say about it and the panel's visualiser stays driven by state rather than
    by a signal we would have to copy out of the player.

    Polled, not streamed. The reasoning is on `GET /calls/live` in
    `server.py`: an SSE response would be a resident task on the event loop
    that paces 20ms audio frames. This process carries no audio, so if the
    panel later wants a push channel, it can be added *here* without going
    anywhere near that loop.

    Unreachability is reported as a field rather than an error: the panel
    polls this on a timer, and a red banner on every tick because the call
    server restarted is noise. `callServer` says which it is.
    """
    try:
        snapshot = await call_client.live_call(since=since, call=call)
    except call_client.CallServerError as exc:
        return {
            "callServer": "unreachable",
            "message": exc.message,
            "live": False,
            "state": None,
            "events": [],
            "nextSeq": since,
        }

    snapshot["callServer"] = "ok"
    return snapshot


@app.post("/v1/test-call/hangup")
async def post_test_call_hangup(
    expect: Optional[str] = Query(
        None, description="Only hang up if the live call has this id"
    ),
) -> dict:
    """
    End the call in progress.

    `expect` is the `id` the panel is displaying. Passing it turns "the panel
    was left open on a finished call and a different one has since started"
    into a refusal rather than into hanging up someone else's conversation.
    """
    try:
        result = await call_client.hangup(expect=expect)
    except call_client.CallServerError as exc:
        return _error(exc.message, exc.status_code)

    logger.info(f"Operator ended call {result.get('call_id') or '(unknown)'}")
    return {"status": "ended", "callId": result.get("call_id") or ""}
