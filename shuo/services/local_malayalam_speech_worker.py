#!/usr/bin/env python3
"""Isolated Silero VAD + IndicConformer Malayalam worker.

stdin protocol:
    A<TAB><base64 mulaw/8k mono>
    Q

stdout protocol:
    READY
    START
    END<TAB><base64 utf8 transcript>[<TAB>key=value ...]

Raw caller audio is held in memory only and is never written to disk.
"""

from __future__ import annotations

import argparse
import audioop
import base64
from collections import deque
import math
import struct
import sys
import time


FRAME_SAMPLES = 512          # Silero 16 kHz frame = 32 ms
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_MS = FRAME_SAMPLES / 16000 * 1000.0
DEFAULT_COMMIT_SILENCE_MS = 320
DEFAULT_START_QUALIFY_MS = 128
DEFAULT_START_MIN_RMS = 500


def emit(line: str) -> None:
    print(line, flush=True)


def _has_event(event, name: str) -> bool:
    return isinstance(event, dict) and name in event


class SpeechTurnBuffer:
    """Merge nearby Silero acoustic segments before conversational EOT.

    Silero may emit an acoustic end after the configured minimum silence. SHUO
    must not immediately promote that boundary to a conversational EndOfTurn,
    because a normal within-sentence pause can otherwise split one caller turn
    into several Agent turns. After an acoustic end, keep buffering for a small
    bounded commit window. A new Silero start inside that window resumes the
    same caller turn and does not emit another START.
    """

    def __init__(
        self,
        *,
        preroll_frames: int,
        commit_silence_ms: int,
        start_qualify_ms: int = DEFAULT_START_QUALIFY_MS,
        start_min_rms: int = DEFAULT_START_MIN_RMS,
    ) -> None:
        if preroll_frames <= 0:
            raise ValueError("preroll_frames must be positive")
        if commit_silence_ms <= 0:
            raise ValueError("commit_silence_ms must be positive")
        if start_qualify_ms <= 0:
            raise ValueError("start_qualify_ms must be positive")
        if start_min_rms < 0:
            raise ValueError("start_min_rms must be non-negative")

        self.preroll = deque(maxlen=preroll_frames)
        self.commit_frames = max(
            1,
            math.ceil(commit_silence_ms / FRAME_MS),
        )
        self.start_qualify_frames = max(
            1,
            math.ceil(start_qualify_ms / FRAME_MS),
        )
        self.start_min_rms = start_min_rms
        self.speech = bytearray()
        self.in_speech = False
        self.pending_end_frames = None
        self.start_emitted = False
        self.start_window = deque(maxlen=self.start_qualify_frames)
        self.best_start_rms = 0

    def feed(
        self,
        frame: bytes,
        event,
        *,
        max_speech_bytes: int,
    ):
        if not self.in_speech:
            self.preroll.append(frame)
            if _has_event(event, "start"):
                self.in_speech = True
                self.speech = bytearray(
                    b"".join(self.preroll)
                )
                self.preroll.clear()
                self.pending_end_frames = None
                self.start_emitted = False
                self.start_window.clear()
                self.start_window.append(frame)
                self.best_start_rms = 0
                return None, None
            return None, None

        self.speech.extend(frame)

        if not self.start_emitted:
            self.start_window.append(frame)
            if len(self.start_window) >= self.start_qualify_frames:
                start_rms = int(
                    audioop.rms(
                        b"".join(self.start_window),
                        2,
                    )
                )
                self.best_start_rms = max(
                    self.best_start_rms,
                    start_rms,
                )
                if start_rms >= self.start_min_rms:
                    self.start_emitted = True
                    return "start", None

        if len(self.speech) >= max_speech_bytes:
            return "commit", (
                "max_speech",
                bytes(self.speech),
            )

        if self.pending_end_frames is not None:
            if _has_event(event, "start"):
                # Speech resumed before the conversational commit window
                # expired. Keep one continuous buffered caller turn.
                self.pending_end_frames = None
                return None, None

            self.pending_end_frames += 1
            if self.pending_end_frames >= self.commit_frames:
                return "commit", (
                    "vad_silence",
                    bytes(self.speech),
                )
            return None, None

        if _has_event(event, "end"):
            self.pending_end_frames = 0

        return None, None

    def reset(self) -> None:
        self.preroll.clear()
        self.speech.clear()
        self.in_speech = False
        self.pending_end_frames = None
        self.start_emitted = False
        self.start_window.clear()
        self.best_start_rms = 0


def pcm16_to_tensor(frame: bytes):
    import numpy as np
    import torch

    samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    samples /= 32768.0
    return torch.from_numpy(samples)


def transcribe(model, pcm: bytes) -> str:
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    samples /= 32768.0

    result = model.recognize(
        samples,
        sample_rate=16000,
    )

    return str(result).strip()


def _segment_metrics(pcm: bytes):
    if not pcm:
        return 0, 0, 0
    samples = len(pcm) // 2
    buffered_audio_ms = round(samples / 16000 * 1000)
    peak = int(audioop.max(pcm, 2))
    rms = int(audioop.rms(pcm, 2))
    return buffered_audio_ms, peak, rms


def transcribe_mulaw8(model, mulaw: bytes) -> str:
    pcm8 = audioop.ulaw2lin(mulaw, 2)
    pcm16, _ = audioop.ratecv(
        pcm8,
        2,
        1,
        8000,
        16000,
        None,
    )
    return transcribe(model, pcm16)


def _verify_pcm_metrics(
    pcm: bytes,
    *,
    sample_rate: int,
):
    usable = pcm[: len(pcm) - (len(pcm) % 2)]
    samples = len(usable) // 2
    if not samples:
        return 0, 0, 0.0, 0.0
    peak = int(audioop.max(usable, 2))
    rms = float(audioop.rms(usable, 2))
    near_full = sum(
        1
        for (sample,) in struct.iter_unpack("<h", usable)
        if abs(sample) >= 32700
    )
    buffered_audio_ms = round(
        samples / sample_rate * 1000
    )
    return (
        buffered_audio_ms,
        peak,
        rms,
        near_full / samples,
    )


def _format_verify_frame(
    segment_id: str,
    transcript: str,
    pcm: bytes,
    *,
    sample_rate: int,
) -> str:
    encoded = base64.b64encode(
        transcript.encode("utf-8")
    ).decode("ascii")
    (
        buffered_audio_ms,
        peak,
        rms,
        near_full_scale_ratio,
    ) = _verify_pcm_metrics(
        pcm,
        sample_rate=sample_rate,
    )
    return (
        f"VERIFY\t{segment_id}\t{encoded}"
        f"\tbuffered_audio_ms={buffered_audio_ms}"
        f"\tpeak={peak}"
        f"\trms={rms:.1f}"
        f"\tnear_full_scale_ratio={near_full_scale_ratio:.8f}"
    )


def format_verify_mulaw_frame(
    segment_id: str,
    transcript: str,
    mulaw: bytes,
) -> str:
    pcm8 = audioop.ulaw2lin(mulaw, 2)
    return _format_verify_frame(
        segment_id,
        transcript,
        pcm8,
        sample_rate=8000,
    )


def format_verify_pcm_frame(
    segment_id: str,
    transcript: str,
    pcm16: bytes,
) -> str:
    return _format_verify_frame(
        segment_id,
        transcript,
        pcm16,
        sample_rate=16000,
    )


def format_end_frame(
    transcript: str,
    speech: bytes,
    *,
    asr_ms: float,
    reason: str,
    start_qualified: bool = True,
    start_rms: int = 0,
) -> str:
    """Build the worker END protocol frame without exposing raw audio."""
    buffered_audio_ms, peak, rms = _segment_metrics(speech)
    encoded = base64.b64encode(
        transcript.encode("utf-8")
    ).decode("ascii")
    return (
        f"END\t{encoded}"
        f"\tbuffered_audio_ms={buffered_audio_ms}"
        f"\tasr_ms={asr_ms:.1f}"
        f"\tpeak={peak}"
        f"\trms={rms}"
        f"\treason={reason}"
        f"\tstart_qualified={int(start_qualified)}"
        f"\tstart_rms={int(start_rms)}"
    )


def main() -> int:
    import onnx_asr
    from silero_vad import load_silero_vad, VADIterator

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
    parser.add_argument(
        "--commit-silence-ms",
        type=int,
        default=DEFAULT_COMMIT_SILENCE_MS,
    )
    parser.add_argument(
        "--start-qualify-ms",
        type=int,
        default=DEFAULT_START_QUALIFY_MS,
    )
    parser.add_argument(
        "--start-min-rms",
        type=int,
        default=DEFAULT_START_MIN_RMS,
    )
    parser.add_argument("--max-speech-seconds", type=float, default=45.0)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    asr = onnx_asr.load_model(
        args.model_id,
        path=args.model_dir,
        providers=["CPUExecutionProvider"],
    )

    if args.verify_only:
        emit("READY")
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if line == "Q":
                break
            if not (
                line.startswith("V\t")
                or line.startswith("P\t")
            ):
                continue
            try:
                kind, segment_id, encoded_audio = line.split("\t", 2)
                audio = base64.b64decode(
                    encoded_audio,
                    validate=True,
                )
            except Exception:
                continue

            if kind == "V":
                transcript = transcribe_mulaw8(
                    asr,
                    audio,
                )
                frame = format_verify_mulaw_frame(
                    segment_id,
                    transcript,
                    audio,
                )
            else:
                transcript = transcribe(
                    asr,
                    audio,
                )
                frame = format_verify_pcm_frame(
                    segment_id,
                    transcript,
                    audio,
                )
            emit(frame)
        return 0

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

    turn = SpeechTurnBuffer(
        preroll_frames=args.preroll_frames,
        commit_silence_ms=args.commit_silence_ms,
        start_qualify_ms=args.start_qualify_ms,
        start_min_rms=args.start_min_rms,
    )

    rate_state = None
    frame_buffer = bytearray()

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

            event = vad(
                pcm16_to_tensor(frame)
            )

            action, payload = turn.feed(
                frame,
                event,
                max_speech_bytes=max_speech_bytes,
            )

            if action == "start":
                emit("START")
                continue

            if action != "commit":
                continue

            reason, speech = payload

            asr_started = time.perf_counter()
            transcript = transcribe(
                asr,
                speech,
            )
            asr_ms = (
                time.perf_counter() - asr_started
            ) * 1000.0

            emit(
                format_end_frame(
                    transcript,
                    speech,
                    asr_ms=asr_ms,
                    reason=reason,
                    start_qualified=turn.start_emitted,
                    start_rms=turn.best_start_rms,
                )
            )

            turn.reset()
            vad.reset_states()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
