#!/usr/bin/env python3
"""
TTS vendor probe -- settles the audio-contract questions empirically.

Rule 4 says measure, don't trust. Vendor docs disagree with vendor docs on
exactly the details that decide this integration, so this script asks the
live API instead of reading about it.

What it answers, per (model x codec x sample-rate) combination:

    accepted?      does the server take the config at all, or error
    wire format    magic-byte sniff: RIFF / MP3 sync / Ogg / raw
    header         is a RIFF header prepended to the first chunk only
    chunk sizes    what the server actually emits, in bytes and in ms
    20ms aligned?  does chunk_bytes % 160 == 0 for mu-law (or % 320 for PCM16)
    ttfb           text sent -> first audio byte back

And separately:

    concatenation  do two `text` messages join into ONE prosodic utterance,
                   or two disjoint ones? This decides whether we can feed
                   LLM tokens straight through (agent.py:172) or must
                   buffer to clause boundaries first.

    python scripts/probe_tts.py                    # Sarvam, full matrix
    python scripts/probe_tts.py --quick            # the 4 combos that matter
    python scripts/probe_tts.py --concat-only      # just the concat test
    python scripts/probe_tts.py --save-wav out/    # dump audio to listen to

Nothing here writes to the repo or touches the running pipeline. It only
needs SARVAM_API_KEY.
"""

import os
import sys
import json
import time
import wave
import base64
import asyncio
import argparse
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

import websockets

TTS_WS = "wss://api.sarvam.ai/text-to-speech/ws"

# Sarvam's own OpenAPI description for `output_audio_codec` reads
# "currently supports MP3 only" while the enum in the same document lists
# mulaw/alaw/linear16, and their IVR guide prescribes mulaw@8k over this
# very socket. rules.md P4 bans the streaming path on the strength of that
# sentence. Nobody has put a wire capture against it -- so the decisive
# test here is not what the server *says*, it is the measured byte rate:
#
#   ~8000 B/s   -> mu-law 8kHz          (ideal: zero transcode)
#   ~16000 B/s  -> linear16 8kHz        (fine: one lin2ulaw call)
#   ~1000-2000  -> MP3                  (P4 confirmed, disqualifying)
#
# Bit rate does not lie about the codec, and no amount of documentation
# outranks it.
BYTES_PER_SEC = {"mulaw": 8000, "alaw": 8000, "linear16": 16000, "wav": 16000}

# One sentence with the things Indian English actually has to survive:
# a Hinglish particle, a name, and an Indian number form.
# പഴയ വരി മാറ്റി ഇത് നൽകുക
PROBE_TEXT = "Hi, thanks for joining the call today. I have your profile in front of me, and your experience looks quite interesting. Before we dive into the technical questions, I would like to give you a brief overview of the role. We are currently expanding our core team to build a new application, and this role requires strong problem-solving skills and the ability to handle personal and professional calls seamlessly. Could you start by walking me through your most recent project? I am specifically interested in the architecture you chose, the challenges you faced during development, and how you managed the deployment process. Take your time, aur detail mein bataiye. Whenever you are ready, we can begin."

# The concatenation probe. Split mid-clause on purpose -- if the engine
# treats each message as its own utterance we will hear a hard prosodic
# break and a burst of silence exactly at the seam.
CONCAT_PARTS = ["Haan ji, Priya here. Your expected CTC is ", "twelve lakh per annum, theek hai?"]

# 8kHz mu-law: 1 byte = 125us, so a 20ms carrier frame is 160 bytes.
# 8kHz PCM16: 2 bytes per sample, so the same frame is 320 bytes.
MULAW_FRAME_BYTES = 160
PCM16_FRAME_BYTES = 320


# ── wire-format sniffing ─────────────────────────────────────────────

def sniff(audio: bytes) -> str:
    """Identify a container/codec from its first bytes."""
    if len(audio) < 4:
        return "empty"
    if audio[:4] == b"RIFF":
        return "wav/riff"
    if audio[:4] == b"OggS":
        return "ogg"
    if audio[:3] == b"ID3":
        return "mp3/id3"
    # MP3 frame sync is 11 set bits: 0xFF followed by 0xE0-0xFF.
    if audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0:
        return "mp3/sync"
    if audio[:4] == b"fLaC":
        return "flac"
    return "raw"


def riff_data_offset(audio: bytes) -> Optional[int]:
    """
    Byte offset of RIFF payload, by walking the chunk list.

    Never assume 44 -- a WAV with a LIST/fact chunk pushes `data` further
    out, and slicing a fixed 44 leaves header bytes in the audio, which is
    audible as a click on every single chunk.
    """
    if audio[:4] != b"RIFF" or len(audio) < 12:
        return None
    pos = 12
    while pos + 8 <= len(audio):
        cid = audio[pos:pos + 4]
        size = int.from_bytes(audio[pos + 4:pos + 8], "little")
        if cid == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)
    return None


def classify_codec(total_bytes: int, codec: str, rate: int, text: str,
                   sniffed: str) -> Tuple[Optional[float], str]:
    """
    Decide what the server ACTUALLY sent, from the byte rate.

    A container sniff catches MP3/RIFF when a header is present, but a
    bare MPEG stream that happens not to start on a sync word, or raw
    audio with no magic at all, sniffs as "raw" and tells us nothing. The
    byte rate is the honest signal: an uncompressed 8kHz stream is either
    8000 B/s (mu-law) or 16000 B/s (PCM16), and MP3 is an order of
    magnitude below both.

    Speech rate varies, so this brackets the duration rather than
    pretending to know it: ~9-20 characters per second covers slow
    careful delivery through to fast conversational Indian English.
    """
    if total_bytes <= 0:
        return None, "no audio"

    chars = max(1, len(text))
    # 10-18 chars/sec spans slow-careful to fast-conversational TTS. An
    # earlier, wider band (9-20) was mush: combined with a +-35% tolerance
    # it let a 4000 B/s MP3 stream pass as valid mu-law. Compare against
    # the midpoint estimate, not mere band overlap.
    est_seconds = chars / 14.0
    mid = total_bytes / est_seconds

    if sniffed.startswith("mp3"):
        return mid, "MP3 -- disqualifying (P4 confirmed)"
    if sniffed == "ogg":
        return mid, "Ogg/Opus -- disqualifying (needs a decoder in the hot path)"

    def near(expected: int) -> bool:
        # +-60% absorbs the speech-rate guess; codec rates are 2x apart or
        # more, so the classes stay separable.
        return 0.6 <= mid / expected <= 1.6

    expected = BYTES_PER_SEC.get(codec)
    if expected and near(expected):
        return mid, f"matches {codec} @ {rate}Hz -- uncompressed, usable"

    # Report the CLOSEST class, not the first one that happens to overlap
    # -- the higher PCM rates sit only ~1.4x apart and would otherwise be
    # mislabelled.
    known = (("mu-law 8k", 8000), ("PCM16 8k / mu-law 16k", 16000),
             ("PCM16 16k", 32000), ("PCM16 22.05k", 44100),
             ("PCM16 24k", 48000))
    matches = [(abs(mid / exp - 1.0), name) for name, exp in known if near(exp)]
    if matches:
        _, name = min(matches)
        return mid, f"looks like {name}, NOT the requested {codec}@{rate}"

    if mid < 5000:
        return mid, f"compressed ~{mid:.0f} B/s (MP3/Opus-like) -- disqualifying"
    return mid, f"unrecognised rate ~{mid:.0f} B/s -- inspect manually"


def describe_alignment(sizes: List[int], frame_bytes: int) -> str:
    """How well the server's chunking lines up with 20ms carrier frames."""
    if not sizes:
        return "no audio"
    body = sizes[:-1] if len(sizes) > 1 else sizes  # last chunk is short by nature
    aligned = sum(1 for s in body if s % frame_bytes == 0)
    return f"{aligned}/{len(body)} chunks are exact multiples of {frame_bytes}B"


# ── connection ───────────────────────────────────────────────────────

async def ws_connect(url: str, key: str, header: str):
    """
    Open the socket. websockets renamed the kwarg in v14, and Sarvam has
    used two different auth header names across doc revisions, so both are
    tried rather than guessed.
    """
    headers = {header: key} if header != "Authorization" else {header: f"Bearer {key}"}
    try:
        return await websockets.connect(url, additional_headers=headers)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers)


async def detect_auth_header(key: str) -> Optional[str]:
    """Find which auth header this account/endpoint actually accepts."""
    for header in ("api-subscription-key", "Authorization", "x-api-key"):
        try:
            ws = await asyncio.wait_for(ws_connect(TTS_WS, key, header), timeout=15)
            await ws.close()
            return header
        except Exception:
            continue
    return None


# ── the format matrix ────────────────────────────────────────────────

async def probe_format(
    key: str,
    header: str,
    model: str,
    codec: str,
    rate: int,
    speaker: str,
    language: str,
    timeout: float = 25.0,
    rate_as_int: bool = False,
) -> Dict[str, Any]:
    """Run one (model, codec, rate) combination end to end."""
    result: Dict[str, Any] = {
        "model": model, "codec": codec, "rate": rate,
        "accepted": False, "error": None, "ttfb_ms": None,
        "chunks": 0, "total_bytes": 0, "sizes": [],
        "format": None, "riff_offset": None, "audio": b"",
        "content_type": None, "saw_final": False, "measured_bps": None,
        "codec_verdict": None,
    }

    # The model belongs in the query string; `config.data.model` is
    # redundant but every shipping integration sends both, so do the same.
    url = f"{TTS_WS}?model={model}&send_completion_event=true"

    try:
        ws = await asyncio.wait_for(ws_connect(url, key, header), timeout=15)
    except Exception as e:
        result["error"] = f"connect failed: {type(e).__name__}: {e}"
        return result

    try:
        await ws.send(json.dumps({
            "type": "config",
            "data": {
                "model": model,
                "target_language_code": language,
                "speaker": speaker,
                "output_audio_codec": codec,
                # AsyncAPI types this as a string, the official SDK sends an
                # int. --rate-int flips it so the probe can tell us which the
                # server actually honours rather than which doc is newer.
                "speech_sample_rate": rate if rate_as_int else str(rate),
            },
        }))

        t0 = time.perf_counter()
        await ws.send(json.dumps({"type": "text", "data": {"text": PROBE_TEXT}}))
        await ws.send(json.dumps({"type": "flush"}))

        chunks: List[bytes] = []
        deadline = time.perf_counter() + timeout

        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.perf_counter())
            except asyncio.TimeoutError:
                break
            except websockets.exceptions.ConnectionClosed as e:
                if not chunks:
                    result["error"] = f"closed before audio: {e}"
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                # Some vendors send binary audio frames directly.
                if isinstance(raw, (bytes, bytearray)):
                    if not chunks:
                        result["ttfb_ms"] = (time.perf_counter() - t0) * 1000
                    chunks.append(bytes(raw))
                continue

            kind = msg.get("type")
            data = msg.get("data") or {}

            if kind == "audio" and data.get("audio"):
                if not chunks:
                    result["ttfb_ms"] = (time.perf_counter() - t0) * 1000
                    result["accepted"] = True
                    # The server names its own format here -- worth having
                    # even though the byte rate below is what we trust.
                    result["content_type"] = data.get("content_type")
                chunks.append(base64.b64decode(data["audio"]))
            elif kind == "error":
                # `code` is an integer on the WS path (the REST envelope
                # uses a string enum -- different shape, same vendor).
                result["error"] = json.dumps(data)[:300]
                break
            elif kind == "event":
                # The documented terminator. Older revisions of the docs
                # showed bare `flush`/`done`, so accept those too rather
                # than sit here until the timeout.
                if data.get("event_type") == "final":
                    result["saw_final"] = True
                    break
            elif kind in ("flush", "done", "end", "complete"):
                break

        if chunks:
            result["accepted"] = True
            joined = b"".join(chunks)
            result["chunks"] = len(chunks)
            result["sizes"] = [len(c) for c in chunks]
            result["total_bytes"] = len(joined)
            result["format"] = sniff(chunks[0])
            result["riff_offset"] = riff_data_offset(chunks[0])
            result["audio"] = joined
            result["measured_bps"], result["codec_verdict"] = classify_codec(
                len(joined), codec, rate, PROBE_TEXT, result["format"]
            )

    except Exception as e:
        result["error"] = result["error"] or f"{type(e).__name__}: {e}"
    finally:
        try:
            await ws.close()
        except Exception:
            pass

    return result


# ── the concatenation question ───────────────────────────────────────

async def probe_concat(
    key: str, header: str, model: str, codec: str, rate: int,
    speaker: str, language: str,
) -> Dict[str, Any]:
    """
    Send the same sentence as one message and as two, and compare.

    If the engine concatenates, both renders are near-identical in length
    and the two-part version has no interior silence at the seam. If it
    treats each message as its own utterance, the split version comes back
    noticeably longer -- it pays utterance-final lengthening and a pause in
    the middle of a clause.
    """
    async def render(parts: List[str]) -> Tuple[int, float]:
        url = f"{TTS_WS}?model={model}&send_completion_event=true"
        ws = await ws_connect(url, key, header)
        try:
            await ws.send(json.dumps({
                "type": "config",
                "data": {
                    "model": model, "target_language_code": language,
                    "speaker": speaker, "output_audio_codec": codec,
                    "speech_sample_rate": str(rate),
                },
            }))
            t0 = time.perf_counter()
            for p in parts:
                await ws.send(json.dumps({"type": "text", "data": {"text": p}}))
            await ws.send(json.dumps({"type": "flush"}))

            total = 0
            ttfb = 0.0
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=25)
                except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind, data = msg.get("type"), (msg.get("data") or {})
                if kind == "audio" and data.get("audio"):
                    if not ttfb:
                        ttfb = (time.perf_counter() - t0) * 1000
                    total += len(base64.b64decode(data["audio"]))
                elif kind == "event" and data.get("event_type") == "final":
                    break
                elif kind in ("flush", "done", "end", "complete", "error"):
                    break
            return total, ttfb
        finally:
            await ws.close()

    whole_bytes, whole_ttfb = await render(["".join(CONCAT_PARTS)])
    split_bytes, split_ttfb = await render(CONCAT_PARTS)

    drift = abs(split_bytes - whole_bytes) / whole_bytes if whole_bytes else 0.0
    return {
        "whole_bytes": whole_bytes, "split_bytes": split_bytes,
        "whole_ttfb_ms": whole_ttfb, "split_ttfb_ms": split_ttfb,
        "drift_pct": drift * 100,
        # >8% extra audio from the same words means an inserted pause and
        # utterance-final lengthening -- i.e. two utterances, not one.
        "verdict": "SEPARATE utterances (must buffer to clause boundaries)"
                   if drift > 0.08 else
                   "CONCATENATES (token-level feed is safe)",
    }


# ── reporting ────────────────────────────────────────────────────────

def bytes_to_ms(n: int, codec: str, rate: int) -> float:
    per_sample = 2 if "16" in codec or codec in ("wav", "linear16") else 1
    return n / (rate * per_sample) * 1000


def print_matrix(results: List[Dict[str, Any]]) -> None:
    print(f"\n{'model':<11}{'codec':<10}{'rate':>6}  {'ok':<3}{'sniff':<10}"
          f"{'content_type':<20}{'ttfb':>7}{'chunks':>7}{'B/s':>8}  verdict")
    print("-" * 118)
    for r in results:
        if not r["accepted"]:
            err = (r["error"] or "rejected")[:44].replace("\n", " ")
            print(f"{r['model']:<11}{r['codec']:<10}{r['rate']:>6}  "
                  f"{'--':<3}{'':<10}{'':<20}{'':>7}{'':>7}{'':>8}  {err}")
            continue

        hdr = f"+RIFF@{r['riff_offset']}" if r["riff_offset"] else (r["format"] or "")
        bps = f"{r['measured_bps']:.0f}" if r["measured_bps"] else "?"
        print(f"{r['model']:<11}{r['codec']:<10}{r['rate']:>6}  "
              f"{'OK':<3}{hdr:<10}{(r['content_type'] or '-')[:19]:<20}"
              f"{r['ttfb_ms']:>6.0f}m{r['chunks']:>7}{bps:>8}  {r['codec_verdict']}")

    print("\nframing (only meaningful for uncompressed results):")
    for r in results:
        if not r["accepted"] or not r["codec_verdict"]:
            continue
        if "usable" not in r["codec_verdict"]:
            continue
        frame = PCM16_FRAME_BYTES if r["codec"] in ("linear16", "wav") else MULAW_FRAME_BYTES
        sizes = sorted(r["sizes"])
        median = sizes[len(sizes) // 2] if sizes else 0
        ms = bytes_to_ms(median, r["codec"], r["rate"])
        final = "final ✓" if r["saw_final"] else "no final event"
        print(f"  {r['model']:<11}{r['codec']:<10}{r['rate']:>6}  "
              f"median chunk {median}B ({ms:.0f}ms)  "
              f"{describe_alignment(r['sizes'], frame)}  {final}")


def save_wavs(results: List[Dict[str, Any]], outdir: Path) -> None:
    """Dump accepted renders so the accent can actually be listened to."""
    outdir.mkdir(parents=True, exist_ok=True)
    for r in results:
        if not r["accepted"] or not r["audio"]:
            continue
        audio = r["audio"]
        off = riff_data_offset(audio)
        name = f"{r['model'].replace(':', '-')}_{r['codec']}_{r['rate']}"

        if r["format"] == "wav/riff":
            (outdir / f"{name}.wav").write_bytes(audio)
        elif r["format"].startswith("mp3"):
            (outdir / f"{name}.mp3").write_bytes(audio)
        elif r["format"] == "raw" and ("16" in r["codec"] or r["codec"] == "linear16"):
            with wave.open(str(outdir / f"{name}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(r["rate"])
                w.writeframes(audio[off:] if off else audio)
        else:
            (outdir / f"{name}.{r['codec']}.raw").write_bytes(audio)
    print(f"\n  audio written to {outdir}/ -- listen before deciding on accent.")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Probe TTS vendors for telephony fitness.")
    ap.add_argument("--quick", action="store_true", help="only the combos that decide the design")
    ap.add_argument("--concat-only", action="store_true", help="only the concatenation test")
    ap.add_argument("--rate-int", action="store_true",
                    help="send speech_sample_rate as an int (SDK style) not a string")
    ap.add_argument("--speaker", default="anushka")
    ap.add_argument("--language", default="en-IN")
    ap.add_argument("--save-wav", metavar="DIR", help="write renders here to listen to")
    args = ap.parse_args()

    key = os.getenv("SARVAM_API_KEY", "")
    if not key:
        print('Set SARVAM_API_KEY first:\n  $env:SARVAM_API_KEY="sk_..."   (PowerShell)')
        return 1

    print("detecting auth header...")
    header = await detect_auth_header(key)
    if not header:
        print("  could not open the socket with any known auth header.")
        print("  checked: api-subscription-key, Authorization: Bearer, x-api-key")
        return 1
    print(f"  auth header: {header}\n")

    if args.quick:
        # The four that decide the design, on both models.
        combos = [
            ("bulbul:v3", "mulaw", 8000),
            ("bulbul:v3", "linear16", 8000),
            ("bulbul:v2", "mulaw", 8000),
            ("bulbul:v2", "linear16", 8000),
        ]
    else:
        combos = [
            (m, c, r)
            for m in ("bulbul:v3", "bulbul:v2")
            for c in ("mulaw", "linear16", "alaw", "wav", "mp3")
            for r in (8000, 16000, 22050)
        ]

    results: List[Dict[str, Any]] = []
    if not args.concat_only:
        print(f"probing {len(combos)} combination(s) -- text: {PROBE_TEXT!r}\n")
        for model, codec, rate in combos:
            r = await probe_format(key, header, model, codec, rate,
                                   args.speaker, args.language,
                                   rate_as_int=args.rate_int)
            results.append(r)
            flag = "ok " if r["accepted"] else "ERR"
            print(f"  {flag} {model:<11} {codec:<10} {rate}")
        print_matrix(results)

        # The assumption-free test. Byte-rate classification leans on a
        # guess about speech tempo; this does not. For identical text at
        # identical sample rate, PCM16 carries exactly 2 bytes per sample
        # where mu-law carries 1, so linear16/mulaw must be 2.00. Any
        # other ratio means at least one of them is not what it claims --
        # and if both are compressed, the ratio collapses toward 1.0.
        for model in ("bulbul:v3", "bulbul:v2"):
            mu = next((r for r in results if r["accepted"] and r["model"] == model
                       and r["codec"] == "mulaw" and r["rate"] == 8000), None)
            pcm = next((r for r in results if r["accepted"] and r["model"] == model
                        and r["codec"] == "linear16" and r["rate"] == 8000), None)
            if not (mu and pcm and mu["total_bytes"]):
                continue
            ratio = pcm["total_bytes"] / mu["total_bytes"]
            if 1.85 <= ratio <= 2.15:
                verdict = "CONFIRMED both uncompressed and honoured"
            elif ratio < 1.3:
                verdict = "SUSPECT -- ratio near 1.0 means both are likely compressed"
            else:
                verdict = "SUSPECT -- one codec is not what it claims"
            print(f"\ncodec cross-check on {model} @ 8000Hz "
                  f"(expect linear16 = 2.00 x mulaw):")
            print(f"  mulaw    {mu['total_bytes']:>8}B")
            print(f"  linear16 {pcm['total_bytes']:>8}B   ratio {ratio:.2f}  -> {verdict}")

        # AsyncAPI types speech_sample_rate as a string, the official SDK
        # sends an int. If the server silently ignores the one we send, we
        # get 22050Hz audio labelled 8000 and every call sounds like a
        # chipmunk. Send it the other way and compare byte counts.
        probe = next((r for r in results if r["accepted"] and r["rate"] == 8000), None)
        if probe and not args.rate_int:
            alt = await probe_format(key, header, probe["model"], probe["codec"],
                                     8000, args.speaker, args.language,
                                     rate_as_int=True)
            print("\nspeech_sample_rate type check (string vs int):")
            print(f"  as string '8000': {probe['total_bytes']:>8}B  "
                  f"{probe['codec_verdict']}")
            if alt["accepted"]:
                same = abs(alt["total_bytes"] - probe["total_bytes"]) / max(1, probe["total_bytes"])
                print(f"  as int    8000 : {alt['total_bytes']:>8}B  {alt['codec_verdict']}")
                print(f"  -> {'both honoured (sizes agree)' if same < 0.15 else 'DIFFERENT -- one form is being ignored'}")
            else:
                print(f"  as int    8000 : rejected -- {alt['error']}")
                print("  -> send speech_sample_rate as a STRING")

    # The concat test runs on whatever 8kHz combination was accepted.
    winner = next((r for r in results if r["accepted"] and r["rate"] == 8000), None)
    if args.concat_only:
        winner = {"model": "bulbul:v2", "codec": "linear16", "rate": 8000}
    if winner:
        print(f"\nconcatenation test on {winner['model']} / {winner['codec']} @ {winner['rate']}")
        c = await probe_concat(key, header, winner["model"], winner["codec"],
                               winner["rate"], args.speaker, args.language)
        print(f"  one message : {c['whole_bytes']:>7}B  ttfb {c['whole_ttfb_ms']:.0f}ms")
        print(f"  two messages: {c['split_bytes']:>7}B  ttfb {c['split_ttfb_ms']:.0f}ms")
        print(f"  drift       : {c['drift_pct']:.1f}%")
        print(f"  -> {c['verdict']}")

    if args.save_wav and results:
        save_wavs(results, Path(args.save_wav))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
