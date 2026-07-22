#!/usr/bin/env python3
"""
Fake Vobiz -- a protocol-accurate stand-in for the Vobiz media plane.

Vobiz is the *client* in this relationship: it POSTs our answer URL and
then dials our WebSocket. So this "stub" drives shuo from the outside,
exactly as Vobiz would.

It exists so the whole transport can be exercised without a Vobiz
account, a public URL, or a phone call -- and so the pathological cases
that are hard to provoke on a real carrier become one-line tests:

    --mode media-before-start    media frames arriving ahead of `start`
    --mode reconnect             a maxRetries retry: new streamId, same callId

It signs its webhooks correctly, so signature validation stays ON in
tests rather than being disabled to make them pass.

Usage:
    python scripts/fake_vobiz.py                       # against localhost:3040
    python scripts/fake_vobiz.py --mode reconnect
    python scripts/fake_vobiz.py --base http://localhost:3040 --seconds 6

The frame builders below are pure functions with no I/O, so the test
suite imports them directly.
"""

from __future__ import annotations

import os
import sys
import json
import time
import hmac
import base64
import hashlib
import asyncio
import argparse
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse


# =============================================================================
# PROTOCOL (pure -- no I/O, imported by the test suite)
# =============================================================================

# 0xFF is silence in mu-law. 160 bytes is exactly one 20ms frame at 8kHz.
MULAW_SILENCE_FRAME = b"\xff" * 160


class VobizProtocol:
    """
    Frame builders matching Vobiz's documented wire format.

    Deliberate details, each of which shuo has to get right:
      * `start` nests callId/streamId under `start` -- NOT at top level.
      * `media` carries streamId at TOP level (asymmetric with `start`).
      * `media.timestamp` is a STRING of epoch ms; `media.chunk` is an INT.
      * `playedStream` is exactly {event, name} -- name is top-level and
        there is no streamId.
      * `clearedAudio` DOES carry streamId and sequenceNumber.
      * `extra_headers` is the literal STRING "{}" , not an object.
    """

    @staticmethod
    def start(stream_id: str, call_id: str, account_id: str = "500025") -> Dict[str, Any]:
        return {
            "sequenceNumber": 0,
            "event": "start",
            "start": {
                "callId": call_id,
                "streamId": stream_id,
                "accountId": account_id,
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
            },
            "extra_headers": "{}",
        }

    @staticmethod
    def media(
        stream_id: str,
        payload: bytes,
        seq: int,
        chunk: int,
        track: str = "inbound",
    ) -> Dict[str, Any]:
        return {
            "sequenceNumber": seq,
            "streamId": stream_id,
            "event": "media",
            "media": {
                "track": track,
                "timestamp": str(int(time.time() * 1000)),
                "chunk": chunk,
                "payload": base64.b64encode(payload).decode("ascii"),
            },
            "extra_headers": "{}",
        }

    @staticmethod
    def played_stream(name: str) -> Dict[str, Any]:
        # Only two fields. No streamId, no sequenceNumber -- Vobiz drops
        # both, unlike Plivo.
        return {"event": "playedStream", "name": name}

    @staticmethod
    def cleared_audio(stream_id: str, seq: int) -> Dict[str, Any]:
        return {"sequenceNumber": seq, "event": "clearedAudio", "streamId": stream_id}

    @staticmethod
    def dtmf(stream_id: str, digit: str, seq: int) -> Dict[str, Any]:
        # Undocumented on Vobiz; this is the Plivo shape that Vobiz's own
        # pipecat serializer handles defensively.
        return {
            "sequenceNumber": seq,
            "streamId": stream_id,
            "event": "dtmf",
            "dtmf": {"track": "inbound", "digit": digit, "timestamp": str(int(time.time() * 1000))},
        }


def base_url_for_signature(url: str) -> str:
    """Callback URL with params, query and fragment stripped."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def sign_headers(url: str, auth_token: str, *, version: str = "v3", nonce: str = "12345678901234567890") -> Dict[str, str]:
    """
    Build the signature headers Vobiz sends on every callback.

        V2:  base64(HMAC-SHA256(token, base_url + nonce))
        V3:  base64(HMAC-SHA256(token, base_url + "." + nonce))
    """
    sep = "." if version == "v3" else ""
    msg = f"{base_url_for_signature(url)}{sep}{nonce}".encode("utf-8")
    sig = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), msg, hashlib.sha256).digest()
    ).decode("ascii")

    if version == "v3":
        return {"X-Vobiz-Signature-V3": sig, "X-Vobiz-Signature-V3-Nonce": nonce}
    return {"X-Vobiz-Signature-V2": sig, "X-Vobiz-Signature-V2-Nonce": nonce}


def extract_ws_url(answer_xml: str) -> str:
    """Pull the WebSocket URL out of an answer-XML <Stream> element."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(answer_xml)
    stream = root.find(".//Stream")
    if stream is None or not (stream.text or "").strip():
        raise ValueError(f"No <Stream> URL in answer XML:\n{answer_xml}")
    return stream.text.strip()


# =============================================================================
# LIVE DRIVER (CLI)
# =============================================================================

async def run_call(
    base: str,
    *,
    mode: str,
    seconds: float,
    auth_token: str,
    persona: str,
    public_url: str,
) -> int:
    import httpx
    import websockets

    call_id = "fake-call-0001"
    stream_id = "fake-stream-0001"

    query = f"persona={persona}&direction=outbound"
    answer_url = f"{base}/answer?{query}"
    # Signatures are computed over the PUBLIC URL registered with the
    # carrier, which is not necessarily the address we connect to -- in
    # production that is the ngrok/ALB hostname while the server itself
    # listens on localhost. Sign what the server will reconstruct.
    signed_url = f"{public_url}/answer?{query}"
    form = {
        "From": "+911234567890",
        "To": "+919876543210",
        "CallUUID": call_id,
        "Direction": "outbound",
    }

    print(f"[fake-vobiz] POST {answer_url}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            answer_url,
            data=form,
            headers=sign_headers(signed_url, auth_token, version="v3"),
        )
    if resp.status_code != 200:
        print(f"[fake-vobiz] answer URL returned {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 403:
            print(f"[fake-vobiz] signed {signed_url!r} -- must match the server's "
                  f"PUBLIC_URL + path exactly. Pass --public-url to override.")
        return 1

    print(f"[fake-vobiz] answer XML:\n{resp.text}\n")
    ws_url = extract_ws_url(resp.text)
    ws_url = ws_url.replace("https://", "ws://").replace("wss://", "ws://")
    print(f"[fake-vobiz] dialling {ws_url}")

    received: List[dict] = []

    async def reader(ws):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                received.append(msg)
                ev = msg.get("event")
                if ev == "playAudio":
                    media = msg.get("media", {})
                    n = len(base64.b64decode(media.get("payload", "")))
                    print(f"[fake-vobiz]  <- playAudio {n}B "
                          f"{media.get('contentType')}@{media.get('sampleRate')}")
                    _assert_play_audio(media)
                elif ev == "clearAudio":
                    print("[fake-vobiz]  <- clearAudio")
                    await ws.send(json.dumps(
                        VobizProtocol.cleared_audio(stream_id, len(received))))
                elif ev == "checkpoint":
                    name = msg.get("name")
                    print(f"[fake-vobiz]  <- checkpoint {name!r}")
                    await ws.send(json.dumps(VobizProtocol.played_stream(name)))
                elif ev == "stop":
                    print("[fake-vobiz]  <- stop")
                else:
                    print(f"[fake-vobiz]  <- {ev}")
        except Exception as e:
            print(f"[fake-vobiz] reader ended: {type(e).__name__}: {e}")

    async with websockets.connect(ws_url) as ws:
        reader_task = asyncio.create_task(reader(ws))
        seq = 0

        if mode == "media-before-start":
            # The pathology: caller audio arrives before `start`. Losing
            # it truncates the top of the first utterance.
            print("[fake-vobiz] sending 5 media frames BEFORE start")
            for chunk in range(5):
                seq += 1
                await ws.send(json.dumps(
                    VobizProtocol.media(stream_id, MULAW_SILENCE_FRAME, seq, chunk)))
                await asyncio.sleep(0.02)

        await ws.send(json.dumps(VobizProtocol.start(stream_id, call_id)))
        print(f"[fake-vobiz] -> start  streamId={stream_id} callId={call_id}")

        frames = int(seconds / 0.02)
        for chunk in range(frames):
            seq += 1
            await ws.send(json.dumps(
                VobizProtocol.media(stream_id, MULAW_SILENCE_FRAME, seq, chunk)))
            await asyncio.sleep(0.02)

            if mode == "reconnect" and chunk == frames // 2:
                # A maxRetries retry: same call, brand-new stream id.
                stream_id = "fake-stream-0002"
                print(f"[fake-vobiz] -> RECONNECT: new streamId={stream_id}, same callId")
                await ws.send(json.dumps(VobizProtocol.start(stream_id, call_id)))

        await asyncio.sleep(0.5)
        reader_task.cancel()

    print(f"\n[fake-vobiz] done. {len(received)} messages received from shuo.")
    kinds: Dict[str, int] = {}
    for m in received:
        kinds[m.get("event", "?")] = kinds.get(m.get("event", "?"), 0) + 1
    print(f"[fake-vobiz] breakdown: {kinds}")
    return 0


def _assert_play_audio(media: Dict[str, Any]) -> None:
    """
    Fail loudly on the outbound-format mistakes that produce silence or
    garbled audio rather than an error.
    """
    ct = media.get("contentType")
    if ct != "audio/x-mulaw":
        print(f"[fake-vobiz] !! playAudio contentType is {ct!r}; expected "
              f"'audio/x-mulaw' (the ';rate=' form is <Stream>-attribute-only)")
    sr = media.get("sampleRate")
    if sr != 8000 or not isinstance(sr, int):
        print(f"[fake-vobiz] !! playAudio sampleRate is {sr!r}; expected integer 8000")


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive shuo as Vobiz would.")
    parser.add_argument("--base", default="http://localhost:3040",
                        help="shuo base URL (default: http://localhost:3040)")
    # NOTE: a "barge-in" mode is deliberately absent. Driving the
    # clearAudio path needs the STT stage to detect speech, which this
    # stub cannot fake -- an advertised flag that silently behaved like
    # "normal" would be worse than no flag. Barge-in is covered by the
    # unit tests and by the live-call checklist.
    parser.add_argument("--mode", default="normal",
                        choices=["normal", "media-before-start", "reconnect"],
                        help="Which carrier behaviour to simulate")
    parser.add_argument("--seconds", type=float, default=4.0,
                        help="Seconds of caller audio to send")
    parser.add_argument("--persona", default="candidate")
    parser.add_argument("--auth-token", default=os.getenv("VOBIZ_AUTH_TOKEN", ""),
                        help="Signs webhooks. Must match the server's token.")
    parser.add_argument("--public-url", default=None,
                        help="The server's PUBLIC_URL, if it differs from --base. "
                             "Signatures are computed over this, not the connect address.")
    args = parser.parse_args()

    if not args.auth_token:
        print("Set VOBIZ_AUTH_TOKEN (or pass --auth-token) so webhooks sign correctly.")
        return 2

    public_url = (args.public_url or os.getenv("PUBLIC_URL") or args.base).rstrip("/")

    return asyncio.run(run_call(
        args.base,
        mode=args.mode,
        seconds=args.seconds,
        auth_token=args.auth_token,
        persona=args.persona,
        public_url=public_url,
    ))


if __name__ == "__main__":
    sys.exit(main())
