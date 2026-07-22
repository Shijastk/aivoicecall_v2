"""
FastAPI server for shuo.

Endpoints:
- GET  /health          - Health check
- *    /answer          - Call-control XML (inbound AND outbound)
- *    /twiml           - Deprecated alias for /answer (Twilio wording)
- POST /stream-status   - Carrier stream status callbacks
- WS   /ws              - Media stream endpoint
- GET  /call/{number}   - Initiate an outbound call
- GET  /trace/latest    - Most recent call trace as JSON
- GET  /bench/ttft      - Benchmark TTFT across LLM providers
"""

import json
import os
import hmac
import time
import asyncio
import random
from collections import defaultdict
from typing import List, Optional

from fastapi import FastAPI, WebSocket, Request, Response, Query
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from . import config
from .carrier import get_carrier
from .conversation import run_conversation
from .types import CallContext, CallDirection
from .tracer import TRACE_DIR
from .log import get_logger

logger = get_logger("shuo.server")

app = FastAPI(title="shuo", docs_url=None, redoc_url=None)

# ── Graceful shutdown / connection draining ───────────────────────────
_draining = False          # Set True on SIGTERM — reject new calls
_active_calls = 0          # Count of live WebSocket conversations
_drain_event = asyncio.Event()  # Signalled when _active_calls hits 0


# =============================================================================
# WEBHOOK AUTHENTICATION
# =============================================================================

def _validate_signatures() -> bool:
    """
    Whether to enforce carrier webhook signatures.

    Defaults on. The only supported reason to turn it off is pointing the
    server at a carrier whose signing secret you do not have; the fake
    Vobiz stub signs correctly, so tests do not need this.
    """
    return os.getenv("VALIDATE_WEBHOOK_SIGNATURES", "true").strip().lower() not in {
        "0", "false", "no", "off"
    }


def _public_request_url(request: Request) -> str:
    """
    Reconstruct the URL the carrier actually signed.

    Signatures are computed over the PUBLIC callback URL. Behind a
    TLS-terminating proxy -- ngrok in dev, an ALB in prod -- `request.url`
    is the internal `http://localhost:3040/...` form, so validating
    against it fails on every single call.

    We rebuild from the configured PUBLIC_URL plus the request path rather
    than from X-Forwarded-* headers, for two reasons: those headers are
    attacker-controllable unless the proxy is known to overwrite them, and
    PUBLIC_URL is by definition the value registered with the carrier, so
    it is the one that has to match.
    """
    base = config.public_url()
    if not base:
        # No PUBLIC_URL configured -- best effort, and the mismatch
        # warning in the carrier will say so.
        return str(request.url)

    url = f"{base}{request.url.path}"
    # Vobiz strips the query before signing, but Twilio includes it --
    # keep it and let each carrier decide.
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _require_admin(request: Request) -> Optional[Response]:
    """
    Gate operator-only endpoints behind a shared secret.

    PUBLIC_URL is by definition internet-reachable, and the server binds
    0.0.0.0, so an ungated endpoint here is an ungated endpoint on the
    internet. Placing calls costs money, traces contain what the caller
    said, and the benchmark burns paid LLM credits.

    Fails CLOSED: with no SHUO_ADMIN_TOKEN set these endpoints refuse to
    serve rather than defaulting to open.
    """
    expected = config.admin_token()
    if not expected:
        logger.warning(
            f"Refused {request.url.path}: SHUO_ADMIN_TOKEN is not set, so "
            f"operator endpoints are disabled. Set it in .env to enable."
        )
        return JSONResponse(
            {"error": "operator endpoints disabled (SHUO_ADMIN_TOKEN unset)"},
            status_code=403,
        )

    supplied = request.headers.get("x-shuo-admin-token") or ""
    if not hmac.compare_digest(supplied, expected):
        logger.warning(
            f"Rejected {request.url.path} from "
            f"{request.client.host if request.client else 'unknown'}: bad admin token"
        )
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


async def _authenticate(request: Request) -> Optional[Response]:
    """
    Validate a carrier webhook. Returns a 403 response on failure, else None.

    Called unconditionally: a missing signature header is a validation
    failure, not a reason to skip the check.
    """
    if not _validate_signatures():
        logger.warning(
            "Webhook signature validation is DISABLED "
            "(VALIDATE_WEBHOOK_SIGNATURES) -- never run this in production"
        )
        return None

    body = await request.body()
    try:
        form = dict(await request.form()) if body else {}
    except Exception:
        form = {}

    carrier = get_carrier()
    ok = carrier.validate_signature(
        url=_public_request_url(request),
        headers=dict(request.headers),
        body=body,
        form=form,
    )
    if not ok:
        logger.warning(
            f"Rejected unsigned/invalid webhook on {request.url.path} "
            f"from {request.client.host if request.client else 'unknown'}"
        )
        return JSONResponse({"error": "invalid signature"}, status_code=403)
    return None


# =============================================================================
# CALL CONTROL
# =============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "carrier": config.carrier_name(), "draining": _draining}


@app.api_route("/answer", methods=["GET", "POST"])
@app.api_route("/twiml", methods=["GET", "POST"])
async def answer(request: Request):
    """
    Return call-control XML instructing the carrier to fork media to us.

    Serves BOTH directions. Outbound calls carry ?persona= from the
    originate call; inbound calls resolve the persona from the dialled
    DID, so one number per Digital Twin role needs no code change.

    Must respond well under a second -- a slow answer URL is dead air on
    the caller's handset.
    """
    denied = await _authenticate(request)
    if denied is not None:
        return denied

    carrier = get_carrier()

    if _draining:
        logger.info("Draining -- rejecting new call")
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response><Speak>Sorry, we are updating. "
                "Please call back in a moment.</Speak><Hangup/></Response>"
            ),
            media_type="application/xml",
        )

    params = dict(request.query_params)
    try:
        form = dict(await request.form())
    except Exception:
        form = {}

    direction_raw = (params.get("direction") or "").lower()
    direction = "inbound" if direction_raw == "inbound" else (
        "outbound" if direction_raw == "outbound" else _infer_direction(form)
    )

    # Outbound: persona was chosen when the call was placed.
    # Inbound: route on the number that was dialled.
    persona_id = params.get("persona")
    if not persona_id:
        dialled = form.get("To") or form.get("to")
        persona_id = config.persona_for_did(dialled)

    ws_url = config.websocket_url(persona_id=persona_id, direction=direction)
    xml = carrier.answer_xml(
        ws_url,
        status_callback_url=config.status_callback_url(),
        record=config.record_calls(),
        recording_callback_url=config.recording_callback_url(),
    )

    logger.info(f"Answer  direction={direction}  persona={persona_id}  ws={ws_url}")
    return Response(content=xml, media_type="application/xml")


def _infer_direction(form: dict) -> str:
    """Best-effort direction from the carrier's callback params."""
    raw = str(form.get("Direction") or form.get("direction") or "").lower()
    if "inbound" in raw:
        return "inbound"
    if "outbound" in raw:
        return "outbound"
    return "outbound"


@app.post("/stream-status")
async def stream_status(request: Request):
    """Carrier stream lifecycle callbacks. Logged for debugging."""
    denied = await _authenticate(request)
    if denied is not None:
        return denied
    try:
        payload = dict(await request.form())
    except Exception:
        payload = {}
    logger.info(f"Stream status: {payload}")
    return {"status": "ok"}


@app.get("/call/{phone_number:path}")
async def trigger_call(
    request: Request,
    phone_number: str,
    persona: Optional[str] = Query(None, description="Persona the twin should adopt"),
):
    """
    Initiate an outbound call. Operator-only -- this spends money.

        curl -H "X-Shuo-Admin-Token: $SHUO_ADMIN_TOKEN" \\
             https://your-server/call/+919876543210
    """
    denied = _require_admin(request)
    if denied is not None:
        return denied

    if _draining:
        return JSONResponse({"error": "server is draining"}, status_code=503)

    if not phone_number.startswith("+"):
        phone_number = f"+{phone_number}"

    persona_id = persona or config.default_persona()
    carrier = get_carrier()

    try:
        result = await carrier.originate(
            phone_number,
            answer_url=config.answer_url(persona_id=persona_id, direction="outbound"),
            persona_id=persona_id,
            record=config.record_calls(),
        )
        return {
            "status": "calling",
            "to": phone_number,
            "carrier": carrier.name,
            "persona": persona_id,
            "call_id": result.call_id,
        }
    except Exception as e:
        # Log the detail; do not echo the carrier's raw response body back
        # to the client.
        logger.error(f"Originate failed: {e}")
        return JSONResponse({"error": "origination failed"}, status_code=502)


@app.api_route("/hangup", methods=["GET", "POST"])
async def hangup(request: Request):
    """
    Carrier callback fired when the call ends.

    This is the authoritative source of `CallUUID` -- the `request_uuid`
    returned when a call is created is NOT reliably the same value.
    It also fires on every termination path, including caller hangup,
    which the stream status callback does not.
    """
    denied = await _authenticate(request)
    if denied is not None:
        return denied
    try:
        payload = dict(await request.form())
    except Exception:
        payload = {}

    logger.info(
        f"Call ended  uuid={payload.get('CallUUID')}  "
        f"from={payload.get('From')} to={payload.get('To')}  "
        f"duration={payload.get('Duration')}s  "
        f"cause={payload.get('HangupCause') or payload.get('HangupCauseName')}  "
        f"source={payload.get('HangupSource')}"
    )
    return {"status": "ok"}


@app.api_route("/ring", methods=["GET", "POST"])
async def ring(request: Request):
    """Carrier callback fired when the far end starts ringing."""
    denied = await _authenticate(request)
    if denied is not None:
        return denied
    return {"status": "ok"}


@app.post("/recording-status")
async def recording_status(request: Request):
    """
    Carrier callback fired when a recording is ready (Event=RecordStop).

    Form-encoded, not JSON. The file URL arrives as RecordUrl on some
    events and RecordFile on others, so accept either.
    """
    denied = await _authenticate(request)
    if denied is not None:
        return denied
    try:
        payload = dict(await request.form())
    except Exception:
        payload = {}

    url = payload.get("RecordUrl") or payload.get("RecordFile")
    logger.info(
        f"Recording ready  id={payload.get('RecordingID')}  "
        f"duration={payload.get('RecordingDuration')}s  "
        f"reason={payload.get('RecordingEndReason')}  url={url}"
    )
    return {"status": "ok"}


@app.get("/trace/latest")
async def latest_trace(request: Request):
    """
    Return the most recent call trace as JSON. Operator-only -- traces
    contain the per-turn transcript of what the caller said.
    """
    denied = _require_admin(request)
    if denied is not None:
        return denied

    if not TRACE_DIR.exists():
        return JSONResponse({"error": "No traces found"}, status_code=404)

    traces = sorted(TRACE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not traces:
        return JSONResponse({"error": "No traces found"}, status_code=404)

    return JSONResponse(json.loads(traces[0].read_text()))


# =============================================================================
# MEDIA STREAM
# =============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for carrier media streams.

    Handles the bidirectional audio stream for a single call.
    Tracks active connections for graceful shutdown draining.
    """
    global _active_calls

    params = websocket.query_params
    persona_id = params.get("persona") or config.default_persona()
    direction_raw = (params.get("direction") or "outbound").lower()

    # Verify BEFORE accepting. The signature gate on /answer says nothing
    # about who dials this socket, and persona/direction are otherwise
    # attacker-chosen. The token is minted into the <Stream> URL by
    # config.websocket_url and is bound to persona, direction and expiry.
    if not config.verify_stream_token(
        params.get("token") or "",
        persona_id=persona_id,
        direction=direction_raw,
        expires_at=params.get("exp") or "",
    ):
        logger.warning(
            f"Rejected /ws connection from "
            f"{websocket.client.host if websocket.client else 'unknown'}: "
            f"missing/invalid stream token"
        )
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()
    _active_calls += 1

    direction = (
        CallDirection.INBOUND if direction_raw == "inbound" else CallDirection.OUTBOUND
    )
    carrier = get_carrier()

    # call_id is unknown until the carrier's `start` frame arrives; the
    # session fills it in and the loop keys everything on it from there.
    context = CallContext(
        call_id="",
        direction=direction,
        persona_id=persona_id,
        carrier=carrier.name,
    )

    logger.info(f"Call connected  (active: {_active_calls})")

    try:
        await run_conversation(websocket, context, carrier)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        _active_calls -= 1
        logger.info(f"Call ended  (active: {_active_calls})")
        if _draining and _active_calls <= 0:
            _drain_event.set()


# =============================================================================
# TTFT BENCHMARK
# =============================================================================

BENCH_PROMPT = "Explain how a combustion engine works."

# Each entry: (display_name, provider_key, model_id)
DEFAULT_MODELS = [
    # OpenAI 4-series
    ("gpt-4o-mini",   "openai", "gpt-4o-mini"),
    ("gpt-4o",        "openai", "gpt-4o"),
    ("gpt-4.1-nano",  "openai", "gpt-4.1-nano"),
    ("gpt-4.1-mini",  "openai", "gpt-4.1-mini"),
    ("gpt-4.1",       "openai", "gpt-4.1"),
    # OpenAI 5-series
    ("gpt-5-nano",    "openai", "gpt-5-nano"),
    ("gpt-5-mini",    "openai", "gpt-5-mini"),
    ("gpt-5",         "openai", "gpt-5"),
    ("gpt-5.1",       "openai", "gpt-5.1"),
    ("gpt-5.2",       "openai", "gpt-5.2"),
    # Groq
    ("groq/llama-3.3-70b",  "groq", "llama-3.3-70b-versatile"),
    ("groq/llama-3.1-8b",   "groq", "llama-3.1-8b-instant"),
]

BENCH_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": BENCH_PROMPT},
]


def _make_clients() -> dict:
    """Build provider → AsyncOpenAI client map."""
    clients = {}
    oai_key = os.getenv("OPENAI_API_KEY", "")
    if oai_key:
        clients["openai"] = AsyncOpenAI(api_key=oai_key)
    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        clients["groq"] = AsyncOpenAI(
            api_key=groq_key,
            base_url="https://api.groq.com/openai/v1",
        )
    return clients


async def _measure_ttft(client: AsyncOpenAI, model: str) -> float:
    """
    Single TTFT measurement in milliseconds.

    Opens a streaming completion, records time-to-first-content-token,
    then closes the stream immediately.
    """
    # GPT-5+ uses max_completion_tokens; older models use max_tokens
    is_new = model.startswith(("gpt-5", "o1", "o3", "o4"))
    token_param = "max_completion_tokens" if is_new else "max_tokens"

    params: dict = {
        "model": model,
        "messages": BENCH_MESSAGES,
        "stream": True,
        token_param: 20,
    }
    if is_new:
        params["extra_body"] = {"reasoning_effort": "none"}
    else:
        params["temperature"] = 0

    t0 = time.perf_counter()
    try:
        stream = await client.chat.completions.create(**params)
    except Exception as e:
        if is_new and "none" in str(e).lower():
            params["extra_body"] = {"reasoning_effort": "minimal"}
            t0 = time.perf_counter()
            stream = await client.chat.completions.create(**params)
        else:
            raise
    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            ttft_ms = (time.perf_counter() - t0) * 1000
            await stream.close()
            return ttft_ms
    return (time.perf_counter() - t0) * 1000


# One request fans out to len(models) * runs paid upstream calls, so both
# sides of that product need a ceiling.
MAX_BENCH_MODELS = 20


@app.get("/bench/ttft")
async def bench_ttft(
    request: Request,
    models: Optional[str] = Query(
        None,
        description="Comma-separated model names. Defaults to a built-in list.",
    ),
    runs: int = Query(30, ge=1, le=100, description="Runs per model"),
):
    """
    Benchmark TTFT across OpenAI-compatible models. Operator-only -- this
    spends paid LLM credits, amplified by `runs`.

        curl -H "X-Shuo-Admin-Token: $SHUO_ADMIN_TOKEN" \\
             "https://your-server/bench/ttft?models=gpt-4o-mini&runs=5"
    """
    denied = _require_admin(request)
    if denied is not None:
        return denied

    clients = _make_clients()

    if models:
        entries = []
        for m in models.split(",")[:MAX_BENCH_MODELS]:
            m = m.strip()
            if not m:
                continue
            if m.startswith("groq/"):
                entries.append((m, "groq", m.removeprefix("groq/")))
            else:
                entries.append((m, "openai", m))
        model_entries = entries
    else:
        model_entries = DEFAULT_MODELS

    model_entries = [(name, prov, mid) for name, prov, mid in model_entries if prov in clients]

    schedule = [(name, prov, mid, i) for name, prov, mid in model_entries for i in range(runs)]
    random.shuffle(schedule)

    total = len(schedule)
    names = [name for name, _, _ in model_entries]
    logger.info(f"TTFT benchmark: {len(model_entries)} models × {runs} runs = {total} calls (randomised)")

    times_by_model: dict[str, list[float]] = defaultdict(list)
    errors_by_model: dict[str, list[str]] = defaultdict(list)

    for idx, (name, prov, mid, run_i) in enumerate(schedule, 1):
        try:
            ms = await _measure_ttft(clients[prov], mid)
            times_by_model[name].append(round(ms, 1))
            logger.info(f"  [{idx}/{total}] {name} #{run_i+1} → {ms:.0f} ms")
        except Exception as e:
            errors_by_model[name].append(f"run {run_i+1}: {e}")
            logger.info(f"  [{idx}/{total}] {name} #{run_i+1} → ERROR")

    results = []
    for name in names:
        t = times_by_model.get(name, [])
        errs = errors_by_model.get(name, [])
        if not t:
            results.append({"model": name, "error": errs[0] if errs else "no data"})
            logger.info(f"  {name} → ERROR: {errs[0] if errs else 'no data'}")
            continue
        avg = round(sum(t) / len(t), 1)
        entry: dict = {
            "model": name,
            "runs": len(t),
            "avg_ms": avg,
            "min_ms": min(t),
            "max_ms": max(t),
            "all_ms": t,
        }
        if errs:
            entry["errors"] = errs
        results.append(entry)
        logger.info(f"  {name} → avg {avg} ms  (min {min(t)}, max {max(t)})")

    return JSONResponse({
        "prompt": BENCH_PROMPT,
        "runs_per_model": runs,
        "results": results,
    })
