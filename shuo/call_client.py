"""
The config API's client onto the call server -- the other side of W3.

:3041 and :3040 are separate processes on purpose (decision 32), and the
test-call feature is the first thing that needs them to talk. This module is
the entire conversation between them, and it is **HTTP over loopback and
nothing else**: no import of `conversation`, `agent`, `server`, `state` or
any service reaches this file or the module that uses it. That is what keeps
the split a split. `TestIsolation` in tests/test_config_api.py pins it.

Three responsibilities:

1. **Hold the admin credential on the server side of the seam.** The panel
   never sees `SHUO_ADMIN_TOKEN`: the browser talks to Next, Next talks to
   :3041 (loopback, decision 37), and only :3041 holds the token that :3040
   demands (decision 16). Placing a call therefore requires being on the
   host, not merely reaching the panel.
2. **Bound every wait.** A carrier that hangs must surface as a sentence in
   the panel, not as a spinner that never resolves.
3. **Translate :3040's `{"error": ...}` into :3041's `{"message": ...}`.**
   The panel renders `message` verbatim (decision 35), so every string that
   leaves here is one an operator can act on, and every one ends by saying
   what happened to the call -- the exact counterpart of "Nothing was saved."

The cooldown lives here rather than in the route handler for one reason:
this is the function that spends money, so the guard against spending it
twice belongs where it cannot be routed around.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from .log import get_logger

logger = get_logger("shuo.call_client")


# =============================================================================
# POLICY
# =============================================================================

DEFAULT_CALL_SERVER_URL = "http://127.0.0.1:3040"

# Connect fast (it is loopback -- a slow connect means nothing is listening),
# then allow the origination request the time a carrier REST call can take.
PLACE_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=5.0)

# The status poll runs about once a second while the panel is open. It reads
# an in-memory buffer on the other side, so anything slower than this is a
# symptom rather than a slow response worth waiting for.
POLL_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)

# A test call rings a real phone and spends real money, and the button that
# places it is one double-click away from doing it twice.
COOLDOWN_SECONDS = 10.0

# E.164: a leading +, a non-zero country digit, then 7-14 more. Validated
# here rather than at the carrier because `/call/{number}` prepends a `+` to
# whatever it is given, so a typo currently becomes an opaque 502 instead of
# a sentence the operator can act on.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# Persona ids name a Digital Twin role, and travel in a query string and
# then into a signed WebSocket URL. Keep them boring.
_PERSONA = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_TOKEN_HEADER = "X-Shuo-Admin-Token"


def call_server_url() -> str:
    """Where `python main.py` is listening. Loopback unless told otherwise."""
    raw = os.getenv("SHUO_CALL_SERVER_URL", "").strip() or DEFAULT_CALL_SERVER_URL
    return raw.rstrip("/")


def admin_token() -> str:
    """
    The call server's operator secret (decision 16).

    Read from the environment on every use rather than captured at import, so
    a `.env` fix does not need a restart of *this* process to be picked up.
    """
    return os.getenv("SHUO_ADMIN_TOKEN", "")


# =============================================================================
# ERRORS
# =============================================================================

class CallServerError(Exception):
    """
    A failure with a sentence already written for the operator.

    `message` goes to the panel verbatim, so it is prose, never a code and
    never a vendor body. `status_code` is what :3041 should answer with.
    """

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# What :3040 says, and what the operator should read instead. The left-hand
# side is matched as a substring of the JSON `error` field, because the call
# server's wording is terse by design -- it was written for a curl session,
# not for a control panel.
_TRANSLATIONS = (
    (
        "operator endpoints disabled",
        "Outbound calling is switched off: SHUO_ADMIN_TOKEN is not set on the "
        "call server. No call was placed.",
    ),
    (
        "forbidden",
        "The configuration API's admin token does not match the call "
        "server's. Set the same SHUO_ADMIN_TOKEN for both processes. No call "
        "was placed.",
    ),
    (
        "draining",
        "The call server is shutting down and is not accepting new calls. No "
        "call was placed — try again once it has restarted.",
    ),
    (
        "origination failed",
        "The carrier refused the call. No call was placed — check the number "
        "and the call server's log for the carrier's reason.",
    ),
    (
        "no call in progress",
        "There is no call in progress to hang up.",
    ),
    (
        "call mismatch",
        "The call shown in this panel is no longer the one in progress. "
        "Nothing was hung up — reopen the panel to see the current call.",
    ),
    (
        "call not yet identified",
        "The carrier has not confirmed the call id yet. Nothing was hung up — "
        "try again in a second.",
    ),
    (
        "hangup failed",
        "The carrier refused to end the call. It may still be connected — "
        "check the call server's log.",
    ),
)


def _translate(body: Any, status_code: int, *, fallback: str) -> str:
    raw = ""
    if isinstance(body, dict):
        raw = str(body.get("error") or body.get("message") or "")

    lowered = raw.lower()
    for needle, sentence in _TRANSLATIONS:
        if needle in lowered:
            return sentence

    logger.warning(f"Call server answered {status_code}: {raw or body!r}")
    return fallback


# =============================================================================
# CLIENT
# =============================================================================

_client: Optional[httpx.AsyncClient] = None
_last_call_at: float = 0.0


def _http() -> httpx.AsyncClient:
    """
    One client for the process, created on first use.

    Reused so a 1Hz status poll does not open a fresh TCP connection every
    second for the life of the panel.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient()
    return _client


async def close() -> None:
    """Release the pooled connections. Called from the app's lifespan."""
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def _request(
    method: str,
    path: str,
    *,
    timeout: httpx.Timeout,
    params: Optional[Dict[str, Any]] = None,
    fallback: str,
) -> Dict[str, Any]:
    """One round trip to the call server, with the credential and the excuses."""
    token = admin_token()
    if not token:
        raise CallServerError(
            "SHUO_ADMIN_TOKEN is not set for the configuration API, so it "
            "cannot authenticate to the call server. No call was placed.",
            status_code=503,
        )

    url = f"{call_server_url()}{path}"

    try:
        response = await _http().request(
            method,
            url,
            params=params,
            headers={_TOKEN_HEADER: token},
            timeout=timeout,
        )
    except httpx.TimeoutException:
        raise CallServerError(
            "The call server did not respond in time. It may still be "
            "starting the call — check the phone before trying again.",
            status_code=504,
        )
    except httpx.HTTPError:
        raise CallServerError(
            f"Could not reach the call server at {call_server_url()}. Is "
            f"`python main.py` running? No call was placed.",
            status_code=502,
        )

    if response.status_code >= 400:
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        raise CallServerError(
            _translate(body, response.status_code, fallback=fallback),
            # 403/409 are the call server's verdict on the request and are
            # worth passing through; anything else becomes a plain 502,
            # because from the panel's side this *is* an upstream failure.
            status_code=response.status_code
            if response.status_code in (403, 409)
            else 502,
        )

    try:
        payload = response.json()
    except ValueError:
        raise CallServerError(
            "The call server returned something that was not JSON. No call "
            "was placed — check that :3040 is the shuo call server.",
            status_code=502,
        )

    return payload if isinstance(payload, dict) else {"result": payload}


# ── Operations ────────────────────────────────────────────────────────

def validate_phone_number(raw: str) -> str:
    """E.164 or a sentence explaining why not. Raises `ValueError`."""
    number = (raw or "").strip().replace(" ", "").replace("-", "")
    if not number:
        raise ValueError("Enter a phone number to call. No call was placed.")
    if not number.startswith("+"):
        raise ValueError(
            "The phone number must be in international format, starting with "
            "+ and the country code — for example +919876543210. No call was "
            "placed."
        )
    if not _E164.match(number):
        raise ValueError(
            f'"{number}" is not a valid phone number. Use international '
            f"format: + followed by 8 to 15 digits. No call was placed."
        )
    return number


def validate_persona(raw: Optional[str]) -> Optional[str]:
    """A persona id, or None for the call server's default."""
    persona = (raw or "").strip().lower()
    if not persona:
        return None
    if not _PERSONA.match(persona):
        raise ValueError(
            f'"{raw}" is not a valid persona name. Use lowercase letters, '
            f"digits, hyphens and underscores. No call was placed."
        )
    return persona


async def place_call(number: str, persona: Optional[str] = None) -> Dict[str, Any]:
    """
    Ring the operator's phone. **This spends money.**

    The number is already validated by the time it gets here; it is still
    percent-encoded on the way into the path, because `/call/{n:path}` will
    otherwise happily accept extra segments.
    """
    global _last_call_at

    elapsed = time.monotonic() - _last_call_at
    if _last_call_at and elapsed < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - elapsed) + 1
        raise CallServerError(
            f"A test call was placed a moment ago. Wait {wait} seconds before "
            f"placing another — no second call was made.",
            status_code=429,
        )

    params = {"persona": persona} if persona else None
    # Claim the cooldown *before* the request, not after: two clicks a
    # hundred milliseconds apart would otherwise both find it unclaimed and
    # both place a call, which is the exact thing being prevented.
    _last_call_at = time.monotonic()

    try:
        return await _request(
            "POST",
            f"/call/{quote(number, safe='+')}",
            timeout=PLACE_TIMEOUT,
            params=params,
            fallback=(
                "The call server could not place the call. No call was placed "
                "— check its log."
            ),
        )
    except CallServerError:
        # A refusal costs nothing, so it must not lock the operator out for
        # ten seconds while they fix a typo.
        _last_call_at = 0.0
        raise


async def live_call(since: int = 0, call: Optional[str] = None) -> Dict[str, Any]:
    """One poll of the call server's live view."""
    params: Dict[str, Any] = {"since": max(0, since)}
    if call:
        params["call"] = call

    return await _request(
        "GET",
        "/calls/live",
        timeout=POLL_TIMEOUT,
        params=params,
        fallback=(
            "The call server could not report on the call. The call itself "
            "is unaffected."
        ),
    )


async def hangup(expect: Optional[str] = None) -> Dict[str, Any]:
    """End the call in progress, optionally only if it is the one shown."""
    params = {"expect": expect} if expect else None

    return await _request(
        "POST",
        "/calls/current/hangup",
        timeout=POLL_TIMEOUT,
        params=params,
        fallback=(
            "The call server could not end the call. It may still be "
            "connected — check its log."
        ),
    )


def reset_cooldown() -> None:
    """Forget the last call time. For tests, and for nothing else."""
    global _last_call_at
    _last_call_at = 0.0
