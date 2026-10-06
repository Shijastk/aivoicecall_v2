#!/usr/bin/env python3
"""Isolated Silero VAD + IndicConformer Malayalam worker.

stdin protocol:
    A<TAB><base64 mulaw/8k mono>
    Q

stdout protocol:
    READY
    START
    END<TAB><base64 utf8 transcript>

Raw caller audio is held in memory only and is never written to disk.
"""

from __future__ import annotations

import argparse
import audioop
import base64
from collections import deque
import sys

import numpy as np
import torch
import onnx_asr
from silero_vad import load_silero_vad, VADIterator


FRAME_SAMPLES = 512          # Silero 16 kHz frame = 32 ms
FRAME_BYTES = FRAME_SAMPLES * 2


def emit(line: str) -> None:
    print(line, flush=True)


def pcm16_to_tensor(frame: bytes) -> torch.Tensor:
    samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    samples /= 32768.0
    return torch.from_numpy(samples)


def transcribe(model, pcm: bytes) -> str:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    samples /= 32768.0

    result = model.recognize(
        samples,
        sample_rate=16000,
    )

    return str(result).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument(
        "--model-id",
        default="OpenVoiceOS/ai4bharat-indicconformer-ml-onnx",
    )
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--min-silence-ms", type=int, default=200)
    parser.add_argument("--speech-pad-ms", type=int, default=30)
    parser.add_argument("--preroll-frames", type=int, default=8)
    parser.add_argument("--max-speech-seconds", type=float, default=45.0)
    args = parser.parse_args()

    asr = onnx_asr.load_model(
        args.model_id,
        path=args.model_dir,
        providers=["CPUExecutionProvider"],
    )

    silero_model = load_silero_vad(
        onnx=True,
        sampling_rate=16000,
    )

    vad = VADIterator(
        silero_model,
        threshold=args.threshold,
        sampling_rate=16000,
        min_silence_duration_ms=args.min_silence_ms,
        speech_pad_ms=args.speech_pad_ms,
    )

    preroll = deque(
        maxlen=args.preroll_frames
    )

    rate_state = None
    frame_buffer = bytearray()
    speech = bytearray()
    in_speech = False

    max_speech_bytes = int(
        args.max_speech_seconds * 16000 * 2
    )

    emit("READY")

    for raw in sys.stdin:
        line = raw.rstrip("\n")

        if line == "Q":
            break

        if not line.startswith("A\t"):
            continue

        try:
            mulaw = base64.b64decode(
                line.split("\t", 1)[1],
                validate=True,
            )
        except Exception:
            continue

        pcm8 = audioop.ulaw2lin(
            mulaw,
            2,
        )

        pcm16, rate_state = audioop.ratecv(
            pcm8,
            2,
            1,
            8000,
            16000,
            rate_state,
        )

        frame_buffer.extend(pcm16)

        while len(frame_buffer) >= FRAME_BYTES:
            frame = bytes(
                frame_buffer[:FRAME_BYTES]
            )
            del frame_buffer[:FRAME_BYTES]

            tensor = pcm16_to_tensor(frame)
            event = vad(tensor)

            if not in_speech:
                preroll.append(frame)

                if (
                    isinstance(event, dict)
                    and "start" in event
                ):
                    in_speech = True
                    speech = bytearray(
                        b"".join(preroll)
                    )
                    preroll.clear()
                    emit("START")

                continue

            speech.extend(frame)

            ended = (
                isinstance(event, dict)
                and "end" in event
            )

            forced_end = (
                len(speech) >= max_speech_bytes
            )

            if not ended and not forced_end:
                continue

            transcript = transcribe(
                asr,
                bytes(speech),
            )

            encoded = base64.b64encode(
                transcript.encode("utf-8")
            ).decode("ascii")

            emit(f"END\t{encoded}")

            speech.clear()
            preroll.clear()
            in_speech = False
            vad.reset_states()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
