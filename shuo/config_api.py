"""
Configuration REST API -- the seam between the Next.js panel and the agent.

    GET  /v1/agent/config      { voiceModel, systemPrompt, updatedAt }
    PUT  /v1/agent/config      { voiceModel, systemPrompt }
    GET  /v1/agent/persona     { rules, updatedAt }
    PUT  /v1/agent/persona     { rules }
    GET  /v1/agent/knowledge   { context, updatedAt }
    PUT  /v1/agent/knowledge   { context }
    GET  /v1/voices            the voices that can actually be synthesised
    GET  /v1/config            the whole document (operator verification)
    GET  /v1/calls/history     completed calls, from disk
    GET  /v1/calls/{id}/recording   the call's stereo WAV (Range-capable)
    GET  /v1/calls/active      every call in flight -- live + ringing (W5c)
    GET  /v1/calls/live        one call's transcript as it happens (W5c)
    POST /v1/test-call         ring the operator's phone (W3)
    GET  /v1/test-call/status  alias of /v1/calls/live (W3)
    POST /v1/test-call/hangup  end the call in progress (W3)
    GET  /health               plus the notifier's counters (W5d)

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

W5d adds the one thing in this process that is not a request handler: a ~1Hz
background poller (`notify.py`) that pushes a notification when an inbound call
starts, is missed, or fails. It runs **only** when `SHUO_NOTIFY_URL` is set, and
it runs here for the same reason the rest of this file does -- it makes an
outbound HTTPS request, and this is the process that is allowed to wait on a
network.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import call_client
from . import call_history
from . import call_status
# W5d's notifier. It lives in *this* process for the same reason everything
# else here does: it makes an outbound HTTPS request, and this is the process
# that is allowed to wait on the network. It imports `call_history` and httpx
# and nothing of the pipeline, which `TestIsolation` pins.
from . import notify
# The recordings are written by the *call* process; this one only reads them
# off the same disk, the same way it reads the call log (decision 44). The
# import is safe in that direction: `recording` depends on the standard
# library, the logger, `call_history` and the spool -- nothing of the audio
# pipeline follows it, which `TestIsolation` pins.
from . import recording
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

# How recent a non-terminal row on disk has to be to count as an active call.
#
# A `pending` or `ringing` row is *evidence* of a call in flight, not proof of
# one: it stays non-terminal forever if the process that wrote it was killed
# between origination and teardown, because the revision that would have closed
# it was never written. Without a bound, one `kill -9` leaves a phantom ringing
# call in the panel for the life of the log file.
#
# Ten minutes: far longer than the gap between placing a call and the carrier
# reporting *something* about it, and short enough that a phantom clears while
# the operator is still in the same sitting. Rows that :3040 confirms are live
# are exempt -- the call process saying "this is up" outranks a clock.
ACTIVE_WINDOW_SECONDS = 600

# How long a call may sit `pending`/`ringing` with nothing further heard about
# it before this API reports it as over.
#
# 🔴 **The carrier's hangup webhook is not guaranteed to fire.** `originate`
# only began sending a `hangup_url` in Phase 8 and whether Vobiz honours it is
# unverified (see the note in `carrier/vobiz.py`), so "the far end declined and
# nothing told us" is a live possibility rather than a theoretical one. Without
# a bound, that call stays `ringing` in the panel until `ACTIVE_WINDOW_SECONDS`
# hides it — ten minutes of a ringing screen for a call that ended in three
# seconds, which is the symptom this constant exists to stop.
#
# Three minutes, and the margin is the point. A handset rings for 30–60s
# before the network gives up, and Plivo — whose REST API Vobiz clones —
# defaults `ring_timeout` to 120s, so anything at or under two minutes could
# cut off a call that is genuinely still ringing and report it as failed while
# the phone is still in the operator's hand. Reporting a live call as dead is a
# worse failure than the ten-minute phantom this replaces, so the backstop sits
# clear of the longest plausible ring.
#
# It is a **derived** status, not a written one — see `_timed_out`. Nothing on
# disk is rewritten on a guess, so a hangup webhook that arrives late still
# folds normally and the next poll reports the carrier's real outcome.
RING_TIMEOUT_SECONDS = 180

# How long a call stays in `/v1/calls/active` after it has ended.
#
# It has to be non-zero, and that is the whole point of it. This route lists
# calls *in flight*, so before W5e a call reaching a terminal status simply
# vanished from the next poll — and a panel polling at 1Hz cannot tell "it
# ended" from "the request failed" or "I looked at the wrong moment". The
# frontend was left holding a ringing screen with no event to close it on.
#
# Twenty seconds is many poll intervals at 1Hz, so the terminal status is
# observed rather than raced past, and short enough that the list still means
# "now". The entries are unmistakable while they linger: `live` is false and
# `status` is terminal.
TERMINAL_GRACE_SECONDS = 20

# How many of the newest calls `/v1/calls/active` folds out of the log.
#
# A call in flight is by definition among the newest, so folding the whole
# 200-row log to find at most a handful of them is work with no possible
# result. Forty leaves an order of magnitude of headroom over `MAX_CALLS` on
# :3040 and keeps the fold small on a route the panel polls every second.
#
# It does not reduce the *read* -- `call_history.load` reads the file whole
# either way -- and that is accepted rather than overlooked: it is the same
# read `/v1/calls/history` already does, on the process that exists to do
# disk-bound work (decision 32), and the file is bounded by the log's own trim.
ACTIVE_SCAN_CALLS = 40

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
    """
    Nothing to warm; one pooled HTTP client to release, and the notifier.

    `NOTIFIER.start()` is a no-op unless `SHUO_NOTIFY_URL` is set -- the check
    lives in `notify.py` so that "off by default" is a property of that module
    rather than of one line here that could be got wrong. On a fresh install no
    task is created at all.

    Started **here and not on :3040** for the reason decision 32 exists: an
    outbound HTTPS request, with a DNS lookup and a TLS handshake in front of
    it, has no business on the loop that paces a 160-byte frame every 20ms --
    and a notifier whose vendor is down must never be able to affect a call.
    """
    notify.NOTIFIER.start()
    yield
    await notify.NOTIFIER.stop()
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
#
# A read has no third sentence: nothing happened to anything, and appending
# "Nothing was saved." to a failed page load describes a save the operator
# never asked for. The panel draws the same three-way distinction on the same
# boundary (`transport.ts`'s `outcomeFor`), so a failure reads the same
# whichever side wrote the sentence.
_NOTHING_SAVED = "Nothing was saved."
_NO_CALL = "No call was placed."
_NOTHING = ""


def _outcome_for(request: Request) -> str:
    if request.url.path.startswith("/v1/test-call"):
        return _NO_CALL
    if request.method in ("GET", "HEAD"):
        return _NOTHING
    return _NOTHING_SAVED


def _with_outcome(sentence: str, outcome: str) -> str:
    """Join a sentence to its outcome, tolerating an empty one."""
    return f"{sentence} {outcome}".strip() if outcome else sentence


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
        return _with_outcome("The request body was not valid JSON.", outcome)

    if kind == "missing":
        return _with_outcome(
            f'The field "{field}" is missing from the request.'
            if field
            else "The request body was missing.",
            outcome,
        )

    if kind == "extra_forbidden":
        # The outcome goes last, like every other message here. It is the
        # part the operator needs -- whether to retype what they lost -- and
        # burying it mid-sentence is how it gets skimmed past.
        return _with_outcome(
            f'"{field}" is not a field this endpoint accepts — check the '
            f"payload against the API contract.",
            outcome,
        )

    if field:
        return _with_outcome(f'The field "{field}" is not valid: {raw}.', outcome)
    return _with_outcome(f"The request was not valid: {raw}.", outcome)


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
    outcome = _outcome_for(request)
    errors = exc.errors()
    message = (
        _sentence_for(errors[0], outcome)
        if errors
        else _with_outcome("The request was not valid.", outcome)
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
        detail = _with_outcome(
            "The request body could not be read — it must be UTF-8 encoded "
            "JSON.",
            _outcome_for(request),
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

    if request.method in ("GET", "HEAD"):
        # A read that failed lost nothing, and saying "Nothing was saved"
        # here would send an operator hunting for a save they never made.
        return _error(
            "The agent service could not read the stored configuration — "
            "check the service logs.",
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
        # Where the call log is, and whether the call server has ever written
        # to it. An empty `/call-logs` screen has two very different causes --
        # no calls yet, or the two processes disagreeing about the path -- and
        # this is the only place that tells them apart.
        "call_history_path": str(call_history.default_history_path()),
        "calls_recorded": call_history.count(),
        # Same diagnosis as the line above, for audio. A `/call-logs` screen
        # where every play button 404s has two very different causes -- nothing
        # recorded yet, or the two processes disagreeing about the directory --
        # and this is the only place that tells them apart.
        "recordings_path": str(recording.default_recordings_dir()),
        "recordings_stored": _recordings_stored(),
        # W5d. `enabled` false is the ordinary state; `dropped`/`failed`
        # non-zero is the only signal that the operator is not being told
        # something, because the notifier swallows its own failures on purpose.
        # `target` is redacted here and everywhere -- on ntfy the topic in the
        # URL *is* the credential.
        "notifications": notify.NOTIFIER.stats(),
    }


def _recordings_stored() -> int:
    """How many WAVs are on disk. A directory listing; never raises."""
    try:
        return sum(1 for _ in recording.default_recordings_dir().glob("*.wav"))
    except OSError:
        return 0


# ── The three reads ───────────────────────────────────────────────────
#
# The read half of the three form screens, and the answer to the closing note
# in `shuo-frontend/lib/api/agent-settings.ts` -- a panel that showed empty
# fields after a reload was not showing "nothing configured", it was showing
# nothing *known*, and an operator who then pressed Save wrote that emptiness
# over a working prompt.
#
# Two properties, and both are load-bearing:
#
# 1. **Each GET answers with the same shape its PUT returns.** `voiceModel`,
#    `systemPrompt`, `rules`, `context`, plus `updatedAt`. The panel already
#    types the PUT response as `AgentConfig`/`PersonaConfig`/`KnowledgeConfig`
#    and ignores the extra stamp, so the read path needed no new type on that
#    side and there is no second shape to drift.
# 2. **They serve the *resolved* value, not the raw section.** That is the
#    whole reason these are not just `GET /v1/config` sliced three ways: what
#    the operator has to see is what the twin would run with on the next call,
#    which for persona rules is `DEFAULT_PERSONA_RULES` on an install nobody
#    has configured and `""` on one where somebody deliberately cleared the
#    field. `ConfigDocument.resolved_*` is the single place that distinction
#    is made (models.py), and it is made once rather than once here and once
#    in `runtime_config.py`.
#
# Unauthenticated in the same sense the rest of this API is -- loopback, or
# the shared token. Worth stating plainly because these *do* return the
# operator's own text, where `/v1/voices` returns a public catalogue: with the
# API bound off-loopback, `enforce_exposure_policy` is what stands between a
# stranger and the résumé in the knowledge block.

def _stamp(section: Optional[Any]) -> Optional[str]:
    """The section's `updatedAt`, or None when it was never written."""
    return getattr(section, "updated_at", None) if section is not None else None


@app.get("/v1/agent/config")
async def get_agent_config() -> dict:
    """
    The stored system prompt and voice model, for the `/agent` screen.

    Empty strings rather than `null` for a section that was never written.
    The panel puts these straight into a `defaultValue`, and React renders
    `null` there as an uncontrolled field with no value -- which is the same
    blank box this endpoint exists to stop, arrived at by a different route.
    An empty string is a textarea showing its placeholder, which is exactly
    what "nothing configured" should look like.

    `voiceModel` may name a voice that no longer resolves; it is returned
    unchecked on purpose. The picker is populated from `/v1/voices` and will
    simply not find it, falling back to its own default -- and that is a
    better failure than this endpoint silently substituting a voice the
    operator did not choose, which would make a lapsed entitlement look like
    a save that never happened.
    """
    document = store.load()
    return {
        "voiceModel": document.resolved_voice_model or "",
        "systemPrompt": document.resolved_system_prompt or "",
        "updatedAt": _stamp(document.agent),
    }


@app.get("/v1/agent/persona")
async def get_persona_config() -> dict:
    """
    The persona rules the next call would run under.

    The asymmetry documented on `ConfigDocument.resolved_persona_rules` is
    visible here and is the point of the endpoint:

        never saved   -> DEFAULT_PERSONA_RULES, so the screen shows the
                         constraints that are actually in force
        saved empty   -> "", because an operator who cleared the field made a
                         decision and reloading the page must not undo it

    That second line is why the panel's own `DEFAULT_PERSONA_RULES` copy has
    to stop being a `defaultValue` now this exists. A client that seeds the
    textarea with the ruleset whenever the response is empty would silently
    resurrect the rules on every reload, and the next Save would write them
    back -- an operator's deliberate clear, undone by a page load.
    """
    document = store.load()
    return {
        "rules": document.resolved_persona_rules,
        "updatedAt": _stamp(document.persona),
    }


@app.get("/v1/agent/knowledge")
async def get_knowledge_config() -> dict:
    """
    The stored knowledge block, verbatim.

    No default and no fallback -- `resolved_knowledge` is `""` until somebody
    writes one. A default here would be invented biography, which is the
    hallucination failure the persona rules exist to forbid (CLAUDE.md §4.1).
    """
    document = store.load()
    return {
        "context": document.resolved_knowledge,
        "updatedAt": _stamp(document.knowledge),
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
# CALL HISTORY
# =============================================================================

@app.get("/v1/calls/history")
async def get_call_history(
    limit: int = Query(
        call_history.MAX_LIMIT,
        ge=1,
        le=call_history.MAX_LIMIT,
        description="How many of the most recent calls to return",
    ),
) -> dict:
    """
    Completed calls, newest first, with their transcripts and timings.

    **Read from disk here, not proxied from the call server**, and that is a
    deliberate departure from the three `/v1/test-call` routes above.

    Those go over HTTP to :3040 because live state exists nowhere else -- it
    is in the call process's memory and vanishes with it. History is the
    opposite: it is written to a file once per call in `conversation.py`'s
    teardown, and this is the process that does disk-bound work (decision 32).
    Reading it directly buys two things. The call log loads with `main.py`
    stopped, which is the ordinary state of the machine between test calls and
    exactly when somebody sits down to review one. And it adds no route to the
    app that shares an event loop with the media socket -- serving a hundred
    archived transcripts from :3040 would mean a JSON encode of megabytes on
    the loop that paces 20ms frames.

    No import crosses the seam to make this work: `call_history` depends on
    nothing but `json`, `pathlib` and the logger, and the call process and
    this one meet at a file, the same way they do for configuration.

    `missed` and `failed` rows carry an empty transcript. That is a record of
    what happened, not a gap in the data, and the panel renders it as such.

    Each row's `recording` is answered from the *filesystem*, not from what
    the row remembers. A recording deleted by the retention budget leaves the
    row untouched, and offering a play button for it would give the operator a
    control that 404s -- which reads as a broken panel rather than as an old
    call.
    """
    calls = call_history.load(limit=limit)
    for call in calls:
        call["recording"] = recording.describe(str(call.get("id") or ""))
    return {"calls": calls, "count": len(calls)}


# =============================================================================
# CALLS IN FLIGHT (W5c)
# =============================================================================
#
# `/v1/calls/active` is the only route here that reads *both* sides of the
# split, and it has to, because neither side knows about all the calls:
#
#     :3040's memory   answered calls -- it has a media socket for each
#     the log on disk  every attempt, including the ones that are still
#                      ringing and have no socket to be in memory *of*
#
# A live-only view shows an empty panel for the whole time the phone is
# ringing, which is exactly the window an operator sits watching. A disk-only
# view cannot say what the twin is doing this second. So: union, keyed on the
# attempt id.

def _iso_age_seconds(stamp: str) -> Optional[float]:
    """
    How long ago an ISO stamp was, or `None` if it is not one.

    Never raises on a malformed value: these come off a file that a previous
    build wrote, and a row with a stamp this build cannot parse must be
    *reported* rather than crash a 1Hz poll.
    """
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _row_stamp(row: Dict[str, Any]) -> str:
    """The newest thing a log row says about when it last changed."""
    return str(row.get("revisedAt") or row.get("startedAt") or "")


def _iso_plus(stamp: str, seconds: float) -> str:
    """
    An ISO stamp shifted forward, or `""` if it could not be parsed.

    Used for exactly one thing: dating the *conclusion* that a silent call is
    over, which is `RING_TIMEOUT_SECONDS` after the last thing anybody said
    about it -- not the moment of that last revision, and not now.

    The distinction is what makes the timeout observable. Dated to the last
    revision, a call is already three minutes stale by the time it turns
    terminal, so it falls outside `TERMINAL_GRACE_SECONDS` immediately and
    disappears without ever having been reported as failed -- which is the same
    silent vanishing this whole change exists to stop, arrived at by a
    different route. Dated to `now`, it would linger forever, because every
    poll would re-date it.
    """
    if not stamp:
        return ""
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _is_recent(row: Dict[str, Any]) -> bool:
    """
    Whether a non-terminal log row is recent enough to still mean anything.

    An unparseable or missing stamp counts as recent: it is the newest
    revision of a row nothing has closed, and hiding a call because its
    timestamp was odd is the wrong failure. The window is what stops a
    `kill -9` between origination and teardown leaving a phantom ringing call
    in the panel forever.
    """
    age = _iso_age_seconds(_row_stamp(row))
    return age is None or age <= ACTIVE_WINDOW_SECONDS


def _timed_out(row: Dict[str, Any]) -> bool:
    """
    Whether nothing has been heard about a still-ringing call for too long.

    Deliberately measured from `revisedAt` -- the last thing anybody wrote
    about this call -- rather than from `startedAt`. The question is not "has
    this call been going a while", it is "has the carrier gone quiet", and a
    call that reported `ringing` forty seconds in is a call the carrier is
    still talking to us about.

    An unparseable stamp is not a timeout, for the same reason it counts as
    recent above: reporting a call as failed on the strength of a timestamp we
    could not read would invent an outcome.
    """
    age = _iso_age_seconds(_row_stamp(row))
    return age is not None and age > RING_TIMEOUT_SECONDS


def _within_grace(entry: Dict[str, Any]) -> bool:
    """Whether a finished call ended recently enough to still be worth listing."""
    age = _iso_age_seconds(entry.get("endedAt") or entry.get("revisedAt") or "")
    return age is not None and age <= TERMINAL_GRACE_SECONDS


def _entry(
    call_ref: str,
    live: Optional[Dict[str, Any]],
    row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    One active call, from whichever of the two sources knows each field.

    **Live wins where it has a value; the log fills the rest.** The two
    disagree in one direction each, and both directions are known:

    - `to`/`from` are known at origination and the monitor may never have been
      told them, so the log is usually the only place they exist.
    - `state`, `turns` and `live` exist only while a socket is open.
    - `callId` is the carrier's, and arrives on the media `start` frame -- so
      live has it first, but the log keeps it after the socket is gone.

    `status` is resolved by `call_status.best_status` rather than by preferring
    a side, which is the same rank the log already folds revisions with. It is
    what stops a live summary still reporting `in_progress` from demoting a row
    that teardown has already written as `completed` -- the two race, and the
    rank exists precisely because either can land first.

    `startedAt` prefers the log. It is `_FIRST_WINS` there for a reason: an
    outbound attempt starts when the operator places it, not when the media
    socket happens to open seconds later, and the same call showing two
    different start times in this list and in the call log reads as a bug.

    `durationMs` is therefore derived from `startedAt` here rather than taken
    from the monitor's socket clock -- **one clock, one meaning**: how long
    this attempt has been going. The monitor's precise socket duration is still
    on `/v1/calls/live`, which is where per-call precision is what the operator
    is looking at.
    """
    live = live or {}
    row = row or {}

    started_at = str(row.get("startedAt") or live.get("startedAt") or "")
    age = _iso_age_seconds(started_at)

    is_live = bool(live.get("live"))
    status = call_status.best_status(
        str(live.get("status") or ""), str(row.get("status") or "")
    )
    ended_code = str(live.get("endedCode") or row.get("endedCode") or "")
    ended_reason = str(live.get("endedReason") or row.get("endedReason") or "")
    ended_at = str(live.get("endedAt") or row.get("endedAt") or "")

    # The carrier went quiet on a call nobody has closed. Reported as failed
    # rather than left ringing -- see `RING_TIMEOUT_SECONDS`. Only ever a
    # *reading*: the row on disk still says what was actually observed, so a
    # hangup webhook arriving late still lands on a `ringing` row and folds
    # normally, and the next poll reports the carrier's real outcome instead of
    # this one.
    if not is_live and status and not call_status.is_terminal(status) and _timed_out(row):
        status = call_status.FAILED
        ended_code = call_status.NO_CARRIER_RESPONSE
        ended_reason = call_status.sentence_for(call_status.NO_CARRIER_RESPONSE)
        # Dated to when the timeout was *crossed*, so the grace window below
        # gets its seconds to be observed in -- see `_iso_plus`.
        ended_at = ended_at or _iso_plus(_row_stamp(row), RING_TIMEOUT_SECONDS)

    # Never `""` on a finished call. A panel switching on the code should not
    # have to carry a branch for "terminal, but we did not say why" -- rows
    # written before `endedCode` existed are exactly that, and so is any row a
    # future writer forgets it on.
    if not ended_code and call_status.is_terminal(status):
        ended_code = call_status.code_for_status(status)
        ended_reason = ended_reason or call_status.sentence_for(ended_code)

    return {
        "id": call_ref,
        "callId": str(live.get("callId") or row.get("callId") or ""),
        "direction": str(live.get("direction") or row.get("direction") or ""),
        "persona": str(live.get("persona") or row.get("persona") or ""),
        "to": str(live.get("to") or row.get("to") or ""),
        "from": str(live.get("from") or row.get("from") or ""),
        # `None` rather than a guess when there is no socket: a ringing call is
        # not "connecting" in the panel's four-state sense, it has not got as
        # far as having a state.
        "state": live.get("state"),
        "live": is_live,
        "status": status,
        "startedAt": started_at,
        "durationMs": max(0, int(age * 1000)) if age is not None else 0,
        "turns": int(live.get("turns") or 0),
        "endedReason": ended_reason,
        # The machine-readable half, and the field a frontend should branch on
        # to leave a ringing screen. `endedReason` beside it is prose for a
        # human and is not a stable string.
        "endedCode": ended_code,
        # When it ended, so a panel can tell a call that finished a moment ago
        # from one it simply has not polled since -- and so the grace window
        # below has something to measure.
        "endedAt": ended_at,
        "revisedAt": _row_stamp(row),
        # Which sources vouch for this call. Worth returning rather than
        # inferring from `live`: "on disk but not in the call process" is the
        # signature of a ringing phone *and* of a process that died mid-call,
        # and an operator debugging the second one should not have to guess.
        "source": "live" if live else "log",
    }


def _merge_active(
    live_calls: List[Dict[str, Any]], rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Live summaries ∪ non-terminal log rows, newest attempt first.

    A call earns a place by being **live, not yet finished, or finished within
    the last few seconds**, and the three are deliberately not the same test:

    - A call the call server says is **live** is kept even when the log already
      calls it terminal. That happens for real: the carrier's hangup webhook
      can land before the loop's teardown, so for a moment the log says
      `cancelled` while the socket is still open. Keeping it -- and showing the
      terminal status, via `best_status` -- is the honest reading, and it clears
      itself on the next poll.
    - 🔴 **A call that ended in the last `TERMINAL_GRACE_SECONDS` is kept, with
      `live` false and a terminal `status`.** Before W5e it was dropped the
      instant it finished, and that was the bug behind a panel stuck on a
      ringing screen: a poll-driven client learns a call ended by *seeing it
      end*, and a row that silently disappears between two polls is
      indistinguishable from a request that failed. The grace window is what
      turns "it is gone" into "it was declined", which is a thing a frontend can
      act on. Older finished calls still belong to the history table, which is a
      different screen reading a different route.

    A live id also exempts its row from the staleness window: :3040 asserting
    the socket is open outranks a clock.
    """
    live_by_id = {
        str(call.get("id") or ""): call
        for call in live_calls
        if isinstance(call, dict) and call.get("id")
    }

    rows_by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        call_ref = str(row.get("id") or "")
        if not call_ref:
            continue
        if call_ref in live_by_id:
            # Merged for its fields -- `to`/`from` live only here -- whatever
            # its status says.
            rows_by_id[call_ref] = row
        elif not call_status.is_terminal(str(row.get("status") or "")):
            if _is_recent(row):
                rows_by_id[call_ref] = row
        elif _within_grace(row):
            # Finished, but only just. Carried so the panel gets one poll -- in
            # practice many -- in which to observe the terminal status.
            rows_by_id[call_ref] = row

    entries = [
        _entry(call_ref, live, rows_by_id.get(call_ref))
        for call_ref, live in live_by_id.items()
    ]
    entries.extend(
        _entry(call_ref, None, row)
        for call_ref, row in rows_by_id.items()
        if call_ref not in live_by_id
    )

    entries = [
        entry
        for entry in entries
        if entry["live"]
        or not call_status.is_terminal(entry["status"])
        or _within_grace(entry)
    ]

    # Newest attempt first. The stamps are the same UTC ISO format from the
    # same helper on both sides, so a string sort is a chronological one, and
    # a row with no stamp sorts last rather than crashing the poll.
    entries.sort(key=lambda entry: entry["startedAt"], reverse=True)
    return entries


@app.get("/v1/calls/active")
async def get_active_calls() -> dict:
    """
    Every call in flight: answered ones from :3040, ringing ones from disk.

    The route the panel polls at 1Hz to know what exists. It lists calls; it
    does not carry their transcripts -- for the one call an operator has open,
    that is `/v1/calls/live?call=`, which takes a cursor so a steady poll
    carries nothing.

    **Unreachability is a field, not a status code**, the same as
    `/v1/test-call/status`. Two reasons, and the second is the real one: at 1Hz
    a red banner because `main.py` is stopped is noise; and the disk half of
    this answer is still *correct* without :3040 -- a call that is ringing right
    now is on disk, and refusing to serve it because the call server is down
    would hide the calls most worth seeing. The no-`SHUO_ADMIN_TOKEN` case
    lands here too: this process cannot authenticate to :3040, which from the
    panel's side is the same situation as :3040 not being there.
    """
    rows = call_history.load(limit=ACTIVE_SCAN_CALLS)

    try:
        payload = await call_client.active_calls()
    except call_client.CallServerError as exc:
        entries = _merge_active([], rows)
        return {
            "calls": entries,
            "count": len(entries),
            "callServer": "unreachable",
            "message": exc.message,
        }

    raw = payload.get("calls")
    live_calls = raw if isinstance(raw, list) else []

    entries = _merge_active(live_calls, rows)
    return {
        "calls": entries,
        "count": len(entries),
        "callServer": "ok",
    }


@app.get("/v1/calls/live")
async def get_live_call(
    since: int = Query(0, ge=0, description="`nextSeq` from the previous poll"),
    call: Optional[str] = Query(None, description="Which call to report on"),
) -> Any:
    """
    One call's transcript, state and latency milestones as they happen.

    A read-only view of the call server's in-memory ring buffer. No audio and
    no audio-derived data crosses this boundary -- not a sample, not an
    amplitude envelope -- so the µ-law-end-to-end rule has nothing to say about
    it and the panel's visualiser stays driven by state rather than by a signal
    we would have to copy out of the player.

    Polled, not streamed. The reasoning is on `GET /calls/live` in `server.py`:
    an SSE response would be a resident task on the event loop that paces 20ms
    audio frames. This process carries no audio, so if the panel later wants a
    push channel, it can be added *here* without going anywhere near that loop.

    `call` is the id from `/v1/calls/active` -- our attempt id, or the
    carrier's. Omitting it means "the most recent call", which is the right
    default for one operator and one test call and the wrong one for a box
    running eight: the panel should name the call it is showing.

    Unreachability is reported as a field rather than an error: the panel polls
    this on a timer, and a red banner on every tick because the call server
    restarted is noise. `callServer` says which it is.

    🔴 **It answers from the call log when :3040 has never heard of the call**,
    and that is not an edge case -- it is the entire life of a call that is
    ringing, and the whole life of one that is declined. Neither ever opens a
    media socket, so `call_monitor` has nothing to say about either, and this
    route used to return `{live: false, state: null, events: []}` forever: the
    same payload while the phone rang and after the far end hung up on it. A
    panel polling that has no transition to see and no reason to stop ringing.

    The fallback is a read of the same file `/v1/calls/history` already reads,
    on the process that exists to do disk-bound work (decision 32), and it
    reaches nothing across the seam that was not already reached.
    """
    try:
        snapshot = await call_client.live_call(since=since, call=call)
    except call_client.CallServerError as exc:
        return _from_the_log(
            call,
            since,
            call_server="unreachable",
            message=exc.message,
        )

    # `id` is None exactly when :3040's monitor could not find the call. The
    # log is then the only source there is, and on a ringing or declined call it
    # is an authoritative one rather than a consolation.
    if not snapshot.get("id"):
        return _from_the_log(call, since, call_server="ok")

    snapshot["callServer"] = "ok"
    snapshot["source"] = "live"
    return snapshot


def _from_the_log(
    call: Optional[str],
    since: int,
    *,
    call_server: str,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    """
    One call's lifecycle read off disk, in `/v1/calls/live`'s shape.

    Carries no `events` and never will: the transcript lives in :3040's memory
    while the call is running and in the *history* row once it is over, and a
    call this function is answering for has neither -- it never got as far as
    having anything said on it. `nextSeq` is echoed back unchanged so a panel
    that later reaches the live view keeps the cursor it had; returning a
    smaller number here would replay the buffer.

    With no `call` named, the newest attempt is used, which is the same "most
    recent call" default `MONITOR.latest()` applies on the other side.
    """
    row = (
        call_history.find(call)
        if call
        else next(iter(call_history.load(limit=1)), None)
    )

    if not row:
        # Nothing anywhere knows this call. Not an error: an id from a stale
        # browser tab, or a log that has been trimmed past it.
        return {
            "callServer": call_server,
            **({"message": message} if message else {}),
            "id": call,
            "callId": "",
            "live": False,
            "state": None,
            "status": "",
            "endedReason": "",
            "endedCode": "",
            "endedAt": "",
            "events": [],
            "nextSeq": since,
            "missed": 0,
            "source": "none",
        }

    entry = _entry(str(row.get("id") or call or ""), None, row)

    return {
        "callServer": call_server,
        **({"message": message} if message else {}),
        "id": entry["id"],
        "callId": entry["callId"],
        # A call with no media socket is not live, whatever else is true of it.
        # The panel's four-state `state` stays null for the same reason it does
        # in `_entry`: `connecting` would be a guess, and a ringing phone has
        # not got as far as having a state.
        "live": False,
        "state": None,
        "status": entry["status"],
        "endedReason": entry["endedReason"],
        "endedCode": entry["endedCode"],
        "endedAt": entry["endedAt"],
        "direction": entry["direction"],
        "persona": entry["persona"],
        "to": entry["to"],
        "from": entry["from"],
        "startedAt": entry["startedAt"],
        "durationMs": entry["durationMs"],
        "turns": entry["turns"],
        "partial": "",
        "config": str(row.get("config") or ""),
        "events": [],
        "nextSeq": since,
        "missed": 0,
        "source": "log",
    }


@app.get("/v1/calls/{call_id}/recording")
async def get_call_recording(call_id: str) -> Any:
    """
    The stereo WAV of one call -- caller left, agent right.

    **Served from :3041, and it could not be served from :3040.** A ten-megabyte
    file read off a disk and streamed out is precisely the work that must not
    happen on the loop pacing 20ms frames (decision 32); this process carries
    no audio, so the same read costs nobody anything.

    Range requests are what make it useful rather than merely present: a
    browser seeking inside an `<audio>` element asks for byte ranges, and
    `FileResponse` answers them. Without that the panel can play a recording
    but cannot skip to the interesting part of it.

    The id is never trusted as a path. `recording.recording_path` checks its
    shape *and* re-checks that the resolved file is inside the recordings
    directory, and answers `None` rather than a file if either fails -- these
    ids arrive from a carrier's callback URL and from a browser's address bar.

    Operator-only in the same sense as everything else here: loopback, or the
    shared token. Worth saying plainly, because this is the one endpoint that
    returns *the audio of a real conversation* rather than text about it.
    """
    path = recording.recording_path(call_id)

    if path is None:
        return _error("That is not a call id this service recognises.", 400)

    if not path.exists():
        # 404 rather than 500: no recording is an ordinary state -- recording
        # can be switched off, a call can end before any audio flows, and the
        # retention budget deletes the oldest.
        return _error(
            "There is no recording for that call. It may have been switched "
            "off, the call may have ended before any audio was exchanged, or "
            "the recording may have aged out.",
            404,
        )

    return FileResponse(
        path,
        media_type="audio/wav",
        filename=f"{call_id}.wav",
        # `inline` so the panel's <audio> element plays it where it is,
        # instead of the browser offering to download a file nobody asked for.
        content_disposition_type="inline",
    )


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
        # The attempt id (Phase 8). **This is the field the panel should
        # keep**, not `callId`.
        #
        # `callId` here is the carrier's `request_uuid`, which is not reliably
        # the `CallUUID` everything downstream quotes -- and on a call nobody
        # answers there is never a `CallUUID` at all. The attempt id is minted
        # before the carrier is called and never changes, so it is the only
        # handle that can follow a call from `pending` through `ringing` to a
        # row in the log, including for the calls that never connect.
        "attempt": result.get("attempt") or "",
    }


@app.get("/v1/test-call/status")
async def get_test_call_status(
    since: int = Query(0, ge=0, description="`nextSeq` from the previous poll"),
    call: Optional[str] = Query(None, description="Which call to report on"),
) -> Any:
    """
    Alias of `GET /v1/calls/live`. Identical response, kept so nothing breaks.

    The name was wrong from the start, and W5c is where that became visible:
    this was never about the test call, it is *the live view*, and a route that
    says `test-call` reads as though watching a real inbound call needed a
    different endpoint. `/v1/calls/live` is the name; this is the one the
    panel already ships against, so it stays until the panel moves.

    Deliberately a delegation and not a copy -- two handlers returning "the
    same" shape is how a field gets added to one of them.
    """
    return await get_live_call(since=since, call=call)


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
