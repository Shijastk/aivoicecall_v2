#!/usr/bin/env python3
"""
Sarvam latency benchmark -- measures every stage of the voice pipeline
without a phone call, a public URL, or a telephony account.

Only SARVAM_API_KEY is required.

    python scripts/bench_sarvam.py                 # all stages, 5 runs each
    python scripts/bench_sarvam.py --runs 10
    python scripts/bench_sarvam.py --stage llm

Stages measured:

    TTS connect   websocket handshake + config ack     (why tts_pool.py exists)
    LLM  ttft     request sent -> first content token
    TTS  ttfb     text sent -> first audio chunk back
    STT  eot      last audio frame -> END_SPEECH signal
    STT  final    last audio frame -> final transcript

    turn latency  STT final + LLM ttft + TTS ttfb
                  = silence the caller hears after they stop talking,
                    before the first byte of reply audio leaves this process.
                    Add PSTN round-trip (~80-150ms in India) for what the
                    caller actually experiences.
"""

import os
import sys
import json
import time
import asyncio
import base64
import argparse
import statistics
from typing import List, Optional, Tuple

import websockets
from openai import AsyncOpenAI

SARVAM_BASE = "https://api.sarvam.ai/v1"
STT_WS = "wss://api.sarvam.ai/speech-to-text/ws"
TTS_WS = "wss://api.sarvam.ai/text-to-speech/ws"

SAMPLE_RATE = 8000          # telephony rate -- matches what Vobiz/Twilio deliver
FRAME_MS = 20               # media frame size on the wire

LLM_MODEL = "sarvam-m"
TTS_MODEL = "bulbul:v2"
TTS_SPEAKER = "anushka"

SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Keep your responses concise and "
    "conversational, as they will be spoken aloud."
)
LLM_PROMPT = "What are your opening hours on weekends?"
TTS_TEXT = "Sure, I can help you with that. Let me pull up your account details."
STT_TEXT = "Hi, I wanted to check the status of my order from last week."


def pct(values: List[float], p: float) -> float:
    """p-th percentile of a small sample, nearest-rank."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * len(s) + 0.5) - 1))
    return s[k]


async def ws_connect(url: str, key: str):
    """Open a websocket with the Sarvam auth header (websockets 12 vs 14+ API)."""
    headers = {"api-subscription-key": key}
    try:
        return await websockets.connect(url, additional_headers=headers)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers)


def strip_wav_header(audio: bytes) -> bytes:
    """Sarvam prefixes a RIFF header on some codecs -- drop it, keep raw PCM."""
    if audio[:4] == b"RIFF" and b"data" in audio[:64]:
        return audio[audio.index(b"data") + 8:]
    return audio


# -- TTS ----------------------------------------------------------------------


async def tts_once(key: str, text: str, collect_all: bool) -> Tuple[float, float, bytes]:
    """
    One TTS turn. Returns (connect_ms, first_audio_ms, audio_bytes).

    connect_ms     handshake + config, paid once per connection
    first_audio_ms text sent -> first audio chunk, paid once per turn
    """
    t0 = time.perf_counter()
    ws = await ws_connect(TTS_WS, key)
    await ws.send(json.dumps({
        "type": "config",
        "data": {
            "model": TTS_MODEL,
            "target_language_code": "en-IN",
            "speaker": TTS_SPEAKER,
            "output_audio_codec": "linear16",
            "speech_sample_rate": str(SAMPLE_RATE),
        },
    }))
    connect_ms = (time.perf_counter() - t0) * 1000

    try:
        t1 = time.perf_counter()
        await ws.send(json.dumps({"type": "text", "data": {"text": text}}))
        await ws.send(json.dumps({"type": "flush"}))

        first_ms = 0.0
        chunks: List[bytes] = []
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                break

            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "audio":
                if not first_ms:
                    first_ms = (time.perf_counter() - t1) * 1000
                if not collect_all:
                    break
                chunks.append(base64.b64decode(msg["data"]["audio"]))
            elif kind == "error":
                raise RuntimeError(f"TTS error: {msg.get('data')}")
            elif kind in ("flush", "done", "end"):
                break

        return connect_ms, first_ms, strip_wav_header(b"".join(chunks))
    finally:
        await ws.close()


async def bench_tts(key: str, runs: int) -> dict:
    connect, first = [], []
    for i in range(runs):
        c, f, _ = await tts_once(key, TTS_TEXT, collect_all=False)
        connect.append(c)
        first.append(f)
        print(f"  tts  #{i + 1}  connect {c:6.0f}ms   first audio {f:6.0f}ms")
    return {"TTS connect": connect, "TTS ttfb": first}


# -- LLM ----------------------------------------------------------------------


async def bench_llm(key: str, runs: int) -> dict:
    client = AsyncOpenAI(api_key=key, base_url=SARVAM_BASE)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": LLM_PROMPT},
    ]

    ttft = []
    for i in range(runs):
        t0 = time.perf_counter()
        stream = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            stream=True,
            max_tokens=100,
            temperature=0.2,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                ms = (time.perf_counter() - t0) * 1000
                await stream.close()
                ttft.append(ms)
                print(f"  llm  #{i + 1}  first token {ms:6.0f}ms")
                break
    return {"LLM ttft": ttft}


# -- STT ----------------------------------------------------------------------


async def stt_once(key: str, pcm: bytes, lang: str, codec: str, encoding: str) -> Tuple[float, float, str]:
    """
    Stream pre-recorded speech at real-time rate, then measure the silence.

    Returns (end_of_speech_ms, final_transcript_ms, transcript), both measured
    from the moment the last audio frame left this process.
    """
    url = (
        f"{STT_WS}?language-code={lang}&model=saaras:v3"
        f"&sample_rate={SAMPLE_RATE}&vad_signals=true&input_audio_codec={codec}"
    )
    ws = await ws_connect(url, key)

    frame_bytes = int(SAMPLE_RATE * 2 * FRAME_MS / 1000)  # 16-bit mono
    eot_ms = final_ms = 0.0
    transcript = ""
    t_last = 0.0

    async def sender() -> None:
        nonlocal t_last
        for off in range(0, len(pcm), frame_bytes):
            frame = pcm[off:off + frame_bytes]
            await ws.send(json.dumps({
                "audio": {
                    "data": base64.b64encode(frame).decode(),
                    "sample_rate": str(SAMPLE_RATE),
                    "encoding": encoding,
                },
            }))
            await asyncio.sleep(FRAME_MS / 1000)
        t_last = time.perf_counter()
        await ws.send(json.dumps({"type": "flush"}))

    try:
        send_task = asyncio.create_task(sender())
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
            except asyncio.TimeoutError:
                break

            msg = json.loads(raw)
            kind = msg.get("type")
            data = msg.get("data") or {}

            if kind == "error":
                raise RuntimeError(
                    f"STT error: {data}\n"
                    f"  (tried input_audio_codec={codec}, encoding={encoding} "
                    f"-- see --stt-codec / --stt-encoding)"
                )
            if kind == "events" and data.get("signal_type") == "END_SPEECH" and t_last:
                eot_ms = (time.perf_counter() - t_last) * 1000
            elif kind == "data" and data.get("transcript"):
                transcript = data["transcript"]
                if t_last:
                    final_ms = (time.perf_counter() - t_last) * 1000
                break

        await send_task
        return eot_ms, final_ms, transcript
    finally:
        await ws.close()


async def bench_stt(key: str, runs: int, lang: str, codec: str, encoding: str) -> dict:
    print("  preparing a speech sample via TTS...")
    _, _, pcm = await tts_once(key, STT_TEXT, collect_all=True)
    if not pcm:
        raise RuntimeError("TTS returned no audio -- cannot build an STT sample")
    print(f"  sample: {len(pcm) / 2 / SAMPLE_RATE:.1f}s of {SAMPLE_RATE}Hz PCM\n")

    eot, final = [], []
    for i in range(runs):
        e, f, text = await stt_once(key, pcm, lang, codec, encoding)
        if e:
            eot.append(e)
        final.append(f)
        print(f"  stt  #{i + 1}  end-of-speech {e:6.0f}ms   transcript {f:6.0f}ms   \"{text[:48]}\"")

    out = {"STT final": final}
    if eot:
        out["STT eot"] = eot
    return out


# -- report -------------------------------------------------------------------


def report(results: dict, runs: int) -> None:
    print(f"\n{'stage':<16}{'p50':>9}{'p95':>9}{'min':>9}{'max':>9}")
    print("-" * 52)
    for name in ("TTS connect", "STT eot", "STT final", "LLM ttft", "TTS ttfb"):
        v = results.get(name)
        if not v:
            continue
        print(f"{name:<16}{pct(v, 50):>8.0f}ms{pct(v, 95):>8.0f}ms"
              f"{min(v):>8.0f}ms{max(v):>8.0f}ms")

    critical = [results.get(k) for k in ("STT final", "LLM ttft", "TTS ttfb")]
    if all(critical):
        p50 = sum(pct(v, 50) for v in critical)
        p95 = sum(pct(v, 95) for v in critical)
        print("-" * 52)
        print(f"{'turn latency':<16}{p50:>8.0f}ms{p95:>8.0f}ms")
        print("\n  turn latency = STT final + LLM ttft + TTS ttfb")
        print("  add PSTN round-trip (~80-150ms in India) for what the caller hears.")
        if results.get("TTS connect"):
            print(f"  TTS connect ({pct(results['TTS connect'], 50):.0f}ms) is excluded -- "
                  "tts_pool.py pre-warms it.")
    print(f"\n  {runs} runs per stage, sequential, from this machine.")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Measure Sarvam voice-pipeline latency.")
    ap.add_argument("--runs", type=int, default=5, help="runs per stage (default 5)")
    ap.add_argument("--stage", choices=["all", "llm", "tts", "stt"], default="all")
    ap.add_argument("--lang", default="en-IN", help="STT language code (default en-IN)")
    ap.add_argument("--stt-codec", default="pcm_s16le",
                    help="input_audio_codec: pcm_s16le, pcm_l16, pcm_raw, wav")
    ap.add_argument("--stt-encoding", default="audio/wav",
                    help="encoding field on the audio message")
    args = ap.parse_args()

    key = os.getenv("SARVAM_API_KEY", "")
    if not key:
        print("Set SARVAM_API_KEY first:\n  $env:SARVAM_API_KEY=\"sk_...\"   (PowerShell)")
        return 1

    results: dict = {}
    try:
        if args.stage in ("all", "llm"):
            print("LLM  (sarvam-m, streaming)")
            results |= await bench_llm(key, args.runs)
            print()
        if args.stage in ("all", "tts"):
            print(f"TTS  ({TTS_MODEL}, {TTS_SPEAKER}, linear16 @ {SAMPLE_RATE}Hz)")
            results |= await bench_tts(key, args.runs)
            print()
        if args.stage in ("all", "stt"):
            print(f"STT  (saaras:v3, {SAMPLE_RATE}Hz, VAD signals on)")
            results |= await bench_stt(key, args.runs, args.lang,
                                       args.stt_codec, args.stt_encoding)
    except Exception as e:
        print(f"\nFAILED: {e}")
        return 1

    report(results, args.runs)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
