"""
Carrier-neutral configuration.

Everything that used to be spelled TWILIO_* and is not actually about
Twilio lives here under a neutral name, with the old name still accepted
so existing .env files keep working.
"""

from __future__ import annotations

import os
import hmac
import time
import base64
import hashlib
from urllib.parse import urlencode, urlsplit, urlunsplit
from typing import Optional


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among `names`."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def carrier_name() -> str:
    """Which carrier implementation to use: 'vobiz' or 'twilio'."""
    return _env("CARRIER", default="vobiz").strip().lower()


def public_url() -> str:
    """
    Our externally reachable base URL (ngrok in dev, the ALB in prod).

    TWILIO_PUBLIC_URL is still honoured so existing .env files work.
    """
    return _env("PUBLIC_URL", "TWILIO_PUBLIC_URL").rstrip("/")


def default_persona() -> str:
    """Persona used when nothing more specific is selected."""
    return _env("PERSONA", default="candidate")


def record_calls() -> bool:
    """
    Whether to record calls.

    Recording is a *test instrument* for this project, not a feature:
    dual-channel audio is how we score character breaks, review
    interruptions, and measure real mouth-to-ear latency.
    """
    return _env("RECORD_CALLS", default="true").strip().lower() in {"1", "true", "yes", "on"}


def checkpoint_grace_seconds() -> float:
    """
    How long a finished turn waits for the carrier's playback ack.

    The player's own completion only means the last frame was *dispatched*
    -- the carrier still holds the pre-roll and the handset its de-jitter
    buffer, so the caller has not heard it yet. `playedStream` is the
    signal that they have. rules.md V18 makes that ack conditional, so
    this is the bound on how long we sit in RESPONDING waiting for one
    that may never come.

    250ms: above the ~100-200ms the ack needs in ap-south-1 (60ms
    pre-roll + 40-100ms de-jitter + a same-region round trip), and at the
    median human response gap (rules.md H1), so the wait hides inside the
    gap we are going to sample anyway rather than adding to it. Tune it
    against `carrier_playback_done` in the trace after the first live
    call. **0 disables the gate**, restoring the guessed completion.
    """
    raw = _env("SHUO_CHECKPOINT_GRACE_MS", default="250")
    try:
        return max(0.0, float(raw) / 1000.0)
    except ValueError:
        return 0.25


def persona_for_did(did: Optional[str]) -> str:
    """
    Resolve which persona answers a given inbound number.

    Configured as `PERSONA_ROUTES=+911234567890:receptionist,+919999:recruiter`.
    Falls back to the default persona. This is the seam the Digital Twin
    role-switch runs through -- one DID per role, no code change.
    """
    routes = _env("PERSONA_ROUTES")
    if did and routes:
        for pair in routes.split(","):
            if ":" not in pair:
                continue
            number, persona = pair.split(":", 1)
            if number.strip() == did.strip():
                return persona.strip()
    return default_persona()


# ── Admin + media-stream authentication ──────────────────────────────

def admin_token() -> str:
    """
    Shared secret for operator-only endpoints (/call, /trace/latest,
    /bench/ttft). Unset means those endpoints refuse to serve.
    """
    return _env("SHUO_ADMIN_TOKEN")


def _stream_secret() -> str:
    """
    Key for media-stream tokens.

    Falls back to the carrier auth token so this works without a new
    variable -- the carrier token is already required, already secret,
    and already scoped to this deployment.
    """
    return _env("SHUO_STREAM_SECRET", "VOBIZ_AUTH_TOKEN", "TWILIO_AUTH_TOKEN")


# How long a minted /ws token stays valid. The carrier dials the socket
# within seconds of fetching the answer XML, so this is generous.
STREAM_TOKEN_TTL_SECONDS = 300


def mint_stream_token(*, persona_id: str, direction: str, expires_at: int) -> str:
    """
    Bind a /ws URL to one persona, direction and expiry.

    Without this the media WebSocket accepts any connection: the
    signature gate on /answer proves nothing about who dials /ws, and the
    persona is otherwise attacker-chosen.
    """
    secret = _stream_secret()
    if not secret:
        return ""
    msg = f"{persona_id}|{direction}|{expires_at}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_stream_token(
    token: str, *, persona_id: str, direction: str, expires_at: str
) -> bool:
    """Validate a /ws token. Any malformed or expired input fails closed."""
    secret = _stream_secret()
    if not secret or not token or not expires_at:
        return False
    try:
        exp = int(expires_at)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    expected = mint_stream_token(
        persona_id=persona_id, direction=direction, expires_at=exp
    )
    return bool(expected) and hmac.compare_digest(token, expected)


def websocket_url(*, persona_id: str, direction: str) -> str:
    """
    The wss:// URL the carrier should fork media to.

    Persona and direction travel in the query string rather than in a
    carrier-specific custom-parameter mechanism, because every carrier
    forks to exactly the URL it is given. Twilio has <Parameter>, Vobiz's
    equivalent is unconfirmed -- the query string needs neither.

    The URL is signed: /ws is otherwise an open door onto the agent.
    """
    base = public_url()
    if not base:
        raise ValueError("PUBLIC_URL (or TWILIO_PUBLIC_URL) is not set")

    parts = urlsplit(base)
    scheme = "wss" if parts.scheme in ("https", "wss") else "ws"

    expires_at = int(time.time()) + STREAM_TOKEN_TTL_SECONDS
    params = {"persona": persona_id, "direction": direction, "exp": str(expires_at)}
    token = mint_stream_token(
        persona_id=persona_id, direction=direction, expires_at=expires_at
    )
    if token:
        params["token"] = token

    return urlunsplit((scheme, parts.netloc, "/ws", urlencode(params), ""))


def answer_url(*, persona_id: str, direction: str) -> str:
    """The https:// URL the carrier fetches call-control XML from."""
    base = public_url()
    if not base:
        raise ValueError("PUBLIC_URL (or TWILIO_PUBLIC_URL) is not set")
    query = urlencode({"persona": persona_id, "direction": direction})
    return f"{base}/answer?{query}"


def status_callback_url() -> str:
    base = public_url()
    return f"{base}/stream-status" if base else ""


def recording_callback_url() -> str:
    """Where the carrier posts RecordStop when a recording is ready."""
    base = public_url()
    return f"{base}/recording-status" if base else ""
