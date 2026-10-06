"""Bluetooth-only local Malayalam STT + turn-detection adapter.

The heavy Silero/ONNX runtime stays in an isolated Python environment.
This module itself adds no project-venv dependency.

Input contract:
    G.711 mu-law / 8 kHz / mono

Callbacks intentionally match the subset of FluxService used by the
Bluetooth conversation loop:
    StartOfTurn
    EndOfTurn(transcript)
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import sys
from typing import Awaitable, Callable, Optional

from ..log import ServiceLogger


log = ServiceLogger("LocalMalayalamSpeech")


class LocalMalayalamSpeechError(RuntimeError):
    pass


# The local Malayalam runtime is deliberately external to the main
# SHUO virtualenv. Paths are operator configuration, never source defaults.
# This preserves core/carrier portability and avoids adding the ONNX/Silero
# stack to requirements.txt merely to keep the optional Bluetooth path usable.


class LocalMalayalamSpeechService:
    def __init__(
        self,
        on_end_of_turn: Callable[
            [str], Awaitable[None]
        ],
        on_start_of_turn: Callable[
            [], Awaitable[None]
        ],
        on_interim: Optional[
            Callable[[str], Awaitable[None]]
        ] = None,
        *,
        worker_python: Optional[str] = None,
        model_dir: Optional[str] = None,
        threshold: float = 0.5,
        min_silence_ms: int = 200,
        speech_pad_ms: int = 30,
        preroll_frames: int = 8,
        commit_silence_ms: int = 320,
        startup_timeout_seconds: float = 20.0,
        stop_timeout_seconds: float = 2.0,
    ) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError(
                "threshold must be between 0 and 1"
            )
        if min_silence_ms <= 0:
            raise ValueError(
                "min_silence_ms must be positive"
            )
        if speech_pad_ms < 0:
            raise ValueError(
                "speech_pad_ms must be non-negative"
            )
        if preroll_frames <= 0:
            raise ValueError(
                "preroll_frames must be positive"
            )
        if commit_silence_ms <= 0:
            raise ValueError(
                "commit_silence_ms must be positive"
            )

        self._on_end_of_turn = on_end_of_turn
        self._on_start_of_turn = on_start_of_turn
        self._on_interim = on_interim

        self._worker_python = (
            worker_python
            or os.getenv("SHUO_LOCAL_STT_PYTHON", "")
        ).strip()

        self._model_dir = (
            model_dir
            or os.getenv(
                "SHUO_MALAYALAM_STT_MODEL_DIR",
                "",
            )
        ).strip()

        self._threshold = threshold
        self._min_silence_ms = min_silence_ms
        self._speech_pad_ms = speech_pad_ms
        self._preroll_frames = preroll_frames
        self._commit_silence_ms = commit_silence_ms
        self._startup_timeout_seconds = (
            startup_timeout_seconds
        )
        self._stop_timeout_seconds = (
            stop_timeout_seconds
        )

        self._proc = None
        self._reader_task = None
        self._stderr_task = None
        self._ready = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._running = False
        self._stderr_lines = 0

    @property
    def is_active(self) -> bool:
        return (
            self._running
            and self._proc is not None
            and self._proc.returncode is None
            and self._ready.is_set()
        )

    def _worker_path(self) -> Path:
        return Path(__file__).with_name(
            "local_malayalam_speech_worker.py"
        )

    def _validate_runtime(self) -> None:
        if not self._worker_python:
            raise LocalMalayalamSpeechError(
                "SHUO_LOCAL_STT_PYTHON is required when "
                "speech_provider=local-malayalam"
            )

        if not self._model_dir:
            raise LocalMalayalamSpeechError(
                "SHUO_MALAYALAM_STT_MODEL_DIR is required when "
                "speech_provider=local-malayalam"
            )

        python = Path(
            self._worker_python
        ).expanduser()

        model_dir = Path(
            self._model_dir
        ).expanduser()

        worker = self._worker_path()

        if not python.is_file():
            raise LocalMalayalamSpeechError(
                "Local STT Python does not exist: "
                f"{python}"
            )

        if not model_dir.is_dir():
            raise LocalMalayalamSpeechError(
                "Malayalam STT model directory "
                f"does not exist: {model_dir}"
            )

        if not worker.is_file():
            raise LocalMalayalamSpeechError(
                f"Local STT worker missing: {worker}"
            )

        self._worker_python = str(python)
        self._model_dir = str(model_dir)

    async def start(self) -> None:
        if self._running:
            return

        self._validate_runtime()
        self._ready.clear()

        self._proc = (
            await asyncio.create_subprocess_exec(
                self._worker_python,
                "-u",
                str(self._worker_path()),
                "--model-dir",
                self._model_dir,
                "--threshold",
                str(self._threshold),
                "--min-silence-ms",
                str(self._min_silence_ms),
                "--speech-pad-ms",
                str(self._speech_pad_ms),
                "--preroll-frames",
                str(self._preroll_frames),
                "--commit-silence-ms",
                str(self._commit_silence_ms),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )

        self._running = True

        self._reader_task = asyncio.create_task(
            self._read_stdout()
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr()
        )

        ready_task = asyncio.create_task(
            self._ready.wait()
        )
        exit_task = asyncio.create_task(
            self._proc.wait()
        )

        try:
            done, _ = await asyncio.wait(
                {ready_task, exit_task},
                timeout=self._startup_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if ready_task in done:
                log.info(
                    "Local Malayalam STT ready"
                )
                return

            if exit_task in done:
                raise LocalMalayalamSpeechError(
                    "Local STT worker exited "
                    f"during startup with code "
                    f"{self._proc.returncode}"
                )

            raise LocalMalayalamSpeechError(
                "Timed out waiting for local "
                "Malayalam STT worker"
            )

        except BaseException:
            await self.stop()
            raise

        finally:
            for task in (
                ready_task,
                exit_task,
            ):
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                ready_task,
                exit_task,
                return_exceptions=True,
            )

    async def send(
        self,
        audio_bytes: bytes,
    ) -> None:
        if not audio_bytes:
            return

        if not self.is_active:
            raise LocalMalayalamSpeechError(
                "Local Malayalam STT is not active"
            )

        if (
            self._reader_task is not None
            and self._reader_task.done()
        ):
            exc = self._reader_task.exception()
            raise LocalMalayalamSpeechError(
                "Local STT worker reader stopped"
            ) from exc

        assert self._proc is not None
        assert self._proc.stdin is not None

        payload = base64.b64encode(
            audio_bytes
        )

        message = (
            b"A\t"
            + payload
            + b"\n"
        )

        async with self._send_lock:
            self._proc.stdin.write(
                message
            )
            await self._proc.stdin.drain()

    async def stop(self) -> None:
        self._running = False

        proc = self._proc
        self._proc = None

        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(b"Q\n")
                    await proc.stdin.drain()
                except (
                    BrokenPipeError,
                    ConnectionResetError,
                ):
                    pass

                try:
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except Exception:
                    pass

            if proc.returncode is None:
                try:
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=self._stop_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    proc.terminate()

                    try:
                        await asyncio.wait_for(
                            proc.wait(),
                            timeout=self._stop_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()

        for task in (
            self._reader_task,
            self._stderr_task,
        ):
            if task is not None and not task.done():
                task.cancel()

        await asyncio.gather(
            *[
                task
                for task in (
                    self._reader_task,
                    self._stderr_task,
                )
                if task is not None
            ],
            return_exceptions=True,
        )

        self._reader_task = None
        self._stderr_task = None
        self._ready.clear()

    async def _read_stdout(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None

        while True:
            raw = await self._proc.stdout.readline()

            if not raw:
                return

            line = raw.decode(
                "utf-8",
                errors="replace",
            ).rstrip("\n")

            await self._handle_worker_line(
                line
            )

    async def _handle_worker_line(
        self,
        line: str,
    ) -> None:
        if line == "READY":
            self._ready.set()
            return

        if line == "START":
            await self._on_start_of_turn()
            return

        if line.startswith("END\t"):
            parts = line.split("\t")
            if len(parts) < 2:
                raise LocalMalayalamSpeechError(
                    "Local STT worker returned "
                    "an invalid transcript frame"
                )

            encoded = parts[1]

            try:
                transcript = (
                    base64.b64decode(
                        encoded,
                        validate=True,
                    )
                    .decode("utf-8")
                    .strip()
                )
            except Exception as exc:
                raise LocalMalayalamSpeechError(
                    "Local STT worker returned "
                    "an invalid transcript frame"
                ) from exc

            metadata = {}
            for field in parts[2:]:
                if "=" not in field:
                    raise LocalMalayalamSpeechError(
                        "Local STT worker returned "
                        "invalid diagnostic metadata"
                    )
                key, value = field.split("=", 1)
                metadata[key] = value

            if metadata:
                try:
                    audio_ms = int(metadata["audio_ms"])
                    asr_ms = float(metadata["asr_ms"])
                    peak = int(metadata["peak"])
                    rms = float(metadata["rms"])
                    reason = metadata["reason"]
                except (KeyError, ValueError) as exc:
                    raise LocalMalayalamSpeechError(
                        "Local STT worker returned "
                        "invalid diagnostic metadata"
                    ) from exc

                if reason not in {
                    "vad_silence",
                    "max_speech",
                }:
                    raise LocalMalayalamSpeechError(
                        "Local STT worker returned "
                        "invalid end reason"
                    )

                log.info(
                    "Local Malayalam EndOfTurn "
                    f"transcript_chars={len(transcript)} "
                    f"audio_ms={audio_ms} "
                    f"asr_ms={asr_ms:.1f} "
                    f"peak={peak} "
                    f"rms={rms:.1f} "
                    f"reason={reason}"
                )
            else:
                # Backward-compatible protocol handling for injected tests and
                # older workers. Production worker emits the metadata above.
                log.info(
                    "Local Malayalam EndOfTurn "
                    f"transcript_chars={len(transcript)}"
                )

            await self._on_end_of_turn(
                transcript
            )
            return

        raise LocalMalayalamSpeechError(
            "Unexpected local STT worker "
            "protocol message"
        )

    async def _drain_stderr(self) -> None:
        assert self._proc is not None
        assert self._proc.stderr is not None

        while True:
            line = await self._proc.stderr.readline()

            if not line:
                return

            # Content-free only. Do not mirror arbitrary
            # worker stderr into the call log.
            self._stderr_lines += 1

            if self._stderr_lines == 1:
                log.info(
                    "Local STT worker emitted "
                    "stderr; content suppressed"
                )
