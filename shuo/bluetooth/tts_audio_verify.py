from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..log import ServiceLogger


log = ServiceLogger("TTSOutboundTranscript")

_MAX_SEGMENT_BYTES = 8_000 * 60
_MAX_SEGMENTS = 64
_STARTUP_TIMEOUT_SECONDS = 20.0
_VERIFY_TIMEOUT_SECONDS = 30.0


@dataclass
class _CapturedSegment:
    index: int
    audio: bytearray
    checkpoint: str = ""
    cancelled: bool = False
    truncated: bool = False


class OutboundAudioTranscriptCapture:
    """Capture dispatched SHUO mu-law frames in memory for post-call ASR.

    This diagnostic sits after AudioPlayer framing and before Bluetooth codec
    conversion. It never writes raw audio to disk. A bounded in-memory copy is
    retained only until post-call verification finishes; the persisted artifact
    contains transcript/metadata only.
    """

    def __init__(
        self,
        *,
        max_segment_bytes: int = _MAX_SEGMENT_BYTES,
        max_segments: int = _MAX_SEGMENTS,
    ) -> None:
        if max_segment_bytes <= 0:
            raise ValueError("max_segment_bytes must be positive")
        if max_segments <= 0:
            raise ValueError("max_segments must be positive")
        self._max_segment_bytes = max_segment_bytes
        self._max_segments = max_segments
        self._segments: list[_CapturedSegment] = []
        self._current = bytearray()
        self._current_truncated = False

    def on_dispatched_audio(self, mulaw: bytes) -> None:
        if not mulaw or len(self._segments) >= self._max_segments:
            return
        remaining = self._max_segment_bytes - len(self._current)
        if remaining <= 0:
            self._current_truncated = True
            return
        self._current.extend(mulaw[:remaining])
        if len(mulaw) > remaining:
            self._current_truncated = True

    def on_checkpoint(self, checkpoint: str) -> None:
        self._finalize(
            checkpoint=checkpoint,
            cancelled=False,
        )

    def on_clear(self) -> None:
        self._finalize(
            checkpoint="",
            cancelled=True,
        )

    def finish(self) -> None:
        # Teardown with uncheckpointed dispatched audio is an interrupted
        # segment. Preserve its transcript evidence but never call it complete.
        if self._current:
            self._finalize(
                checkpoint="",
                cancelled=True,
            )

    def _finalize(
        self,
        *,
        checkpoint: str,
        cancelled: bool,
    ) -> None:
        if not self._current:
            self._current_truncated = False
            return
        if len(self._segments) >= self._max_segments:
            self._current.clear()
            self._current_truncated = False
            return
        self._segments.append(
            _CapturedSegment(
                index=len(self._segments) + 1,
                audio=self._current,
                checkpoint=checkpoint,
                cancelled=cancelled,
                truncated=self._current_truncated,
            )
        )
        self._current = bytearray()
        self._current_truncated = False

    @property
    def segment_count(self) -> int:
        return len(self._segments)

    async def transcribe_and_save(
        self,
        call_id: str,
        *,
        worker_python: Optional[str] = None,
        model_dir: Optional[str] = None,
    ) -> Optional[Path]:
        self.finish()
        if not self._segments:
            return None

        python_path = (
            worker_python
            or os.getenv("SHUO_LOCAL_STT_PYTHON", "")
        ).strip()
        model_path = (
            model_dir
            or os.getenv("SHUO_MALAYALAM_STT_MODEL_DIR", "")
        ).strip()

        if not python_path or not Path(python_path).expanduser().is_file():
            raise RuntimeError(
                "TTS outbound transcript verification requires "
                "SHUO_LOCAL_STT_PYTHON"
            )
        if not model_path or not Path(model_path).expanduser().is_dir():
            raise RuntimeError(
                "TTS outbound transcript verification requires "
                "SHUO_MALAYALAM_STT_MODEL_DIR"
            )

        worker = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "local_malayalam_speech_worker.py"
        )

        proc = await asyncio.create_subprocess_exec(
            str(Path(python_path).expanduser()),
            "-u",
            str(worker),
            "--model-dir",
            str(Path(model_path).expanduser()),
            "--verify-only",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None

        results = []
        try:
            ready = await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=_STARTUP_TIMEOUT_SECONDS,
            )
            if ready.rstrip(b"\n") != b"READY":
                raise RuntimeError(
                    "TTS transcript verifier did not become ready"
                )

            for segment in self._segments:
                payload = base64.b64encode(
                    bytes(segment.audio)
                ).decode("ascii")
                proc.stdin.write(
                    (
                        f"V\t{segment.index}\t{payload}\n"
                    ).encode("ascii")
                )
                await proc.stdin.drain()

                raw = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=_VERIFY_TIMEOUT_SECONDS,
                )
                line = raw.decode(
                    "utf-8",
                    errors="replace",
                ).rstrip("\n")
                parts = line.split("\t")
                if len(parts) < 4 or parts[0] != "VERIFY":
                    raise RuntimeError(
                        "TTS transcript verifier returned invalid protocol"
                    )
                if int(parts[1]) != segment.index:
                    raise RuntimeError(
                        "TTS transcript verifier returned wrong segment"
                    )
                transcript = base64.b64decode(
                    parts[2],
                    validate=True,
                ).decode("utf-8")

                metadata = dict(
                    field.split("=", 1)
                    for field in parts[3:]
                    if "=" in field
                )
                buffered_audio_ms = int(
                    metadata["buffered_audio_ms"]
                )

                results.append(
                    {
                        "segment": segment.index,
                        "checkpoint": segment.checkpoint,
                        "cancelled": segment.cancelled,
                        "truncated": segment.truncated,
                        "buffered_audio_ms": buffered_audio_ms,
                        "transcript": transcript,
                    }
                )

                # Raw bytes are no longer needed after this segment verifies.
                segment.audio.clear()

            proc.stdin.write(b"Q\n")
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()
            await asyncio.wait_for(
                proc.wait(),
                timeout=5.0,
            )

        finally:
            for segment in self._segments:
                segment.audio.clear()
            self._current.clear()
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

        safe_id = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in call_id
        ) or "unknown"
        out_dir = (
            Path(tempfile.gettempdir())
            / "shuo"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        path = (
            out_dir
            / f"{safe_id}-tts-outbound-transcript.json"
        )
        path.write_text(
            json.dumps(
                {
                    "call_id": call_id,
                    "boundary": (
                        "post-player-mulaw8k-pre-bluetooth-codec"
                    ),
                    "segments": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info(
            "TTS outbound transcript trace saved "
            f"segments={len(results)} path={path}"
        )
        return path
