from __future__ import annotations

import asyncio
import audioop
import os
import shutil
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from shuo.bluetooth.process import (
    AsyncioProcessRunner,
    CompletedCommand,
    ProcessRunner,
    stop_process,
)

from .android_cellular_tx import (
    AndroidBuildTools,
    discover_adb_serial,
    is_mode_in_call,
    parse_privapp_permissions,
)


class AndroidCellularRxError(RuntimeError):
    """Fail-closed error for the opt-in Android cellular RX harness."""


DEFAULT_REMOTE_RX_DEX = "/data/local/tmp/shuo-telephony-rx-bridge.dex"
_REQUIRED_RX_PERMISSIONS = (
    "android.permission.RECORD_AUDIO",
    "android.permission.CAPTURE_AUDIO_OUTPUT",
)

ANDROID_RX_SAMPLE_RATE = 48_000
ANDROID_RX_CHANNELS = 2
ANDROID_RX_SAMPLE_WIDTH = 2
ANDROID_RX_FRAME_BYTES = ANDROID_RX_CHANNELS * ANDROID_RX_SAMPLE_WIDTH


@dataclass(frozen=True)
class AndroidRxPreflight:
    serial: str
    sdk_int: int
    app_process: str
    actual_mode: str
    required_permissions: tuple[str, ...]


@dataclass(frozen=True)
class AndroidRxProbeMetrics:
    pcm_bytes: int
    pcm_duration_seconds: float
    chunk_count: int
    peak_rms: int
    average_rms: float
    mulaw_bytes: int


def _decode(command: CompletedCommand) -> str:
    return command.stdout.decode("utf-8", errors="replace").strip()


def _stderr(command: CompletedCommand) -> str:
    return command.stderr.decode("utf-8", errors="replace").strip()


async def _run_checked(
    runner: ProcessRunner,
    argv: Sequence[str],
    *,
    label: str,
) -> CompletedCommand:
    result = await runner.run(argv)
    if result.returncode != 0:
        detail = _stderr(result) or _decode(result) or f"exit={result.returncode}"
        raise AndroidCellularRxError(f"{label} failed: {detail}")
    return result


async def preflight_android_cellular_rx(
    tools: AndroidBuildTools,
    *,
    serial: Optional[str] = None,
    runner: Optional[ProcessRunner] = None,
    require_active_call: bool = True,
) -> AndroidRxPreflight:
    """Verify the reference receive prerequisites before AudioRecord starts."""

    process_runner = runner or AsyncioProcessRunner()
    selected = await discover_adb_serial(
        tools,
        requested_serial=serial,
        runner=process_runner,
    )

    def adb(*args: str) -> list[str]:
        return [tools.adb, "-s", selected, *args]

    sdk_result = await _run_checked(
        process_runner,
        adb("shell", "getprop", "ro.build.version.sdk"),
        label="read Android SDK level",
    )
    try:
        sdk_int = int(_decode(sdk_result))
    except ValueError as exc:
        raise AndroidCellularRxError("Android SDK level was not an integer") from exc

    # Direct audio capture is the reference path used by scrcpy on Android 11+.
    if sdk_int < 30:
        raise AndroidCellularRxError(
            f"Android SDK {sdk_int} is below the validated direct-capture floor (30)"
        )

    app_process_result = await _run_checked(
        process_runner,
        adb("shell", "command", "-v", "app_process"),
        label="locate app_process",
    )
    app_process = _decode(app_process_result)
    if not app_process.startswith("/"):
        raise AndroidCellularRxError("app_process was not exposed as an absolute path")

    grants_result = await _run_checked(
        process_runner,
        adb(
            "shell",
            "cmd",
            "package",
            "get-privapp-permissions",
            "com.android.shell",
        ),
        label="read shell privapp permissions",
    )
    grants = parse_privapp_permissions(_decode(grants_result))
    missing = [
        permission
        for permission in _REQUIRED_RX_PERMISSIONS
        if permission not in grants
    ]
    if missing:
        raise AndroidCellularRxError(
            "shell privapp allowlist is missing required receive permission(s): "
            + ", ".join(missing)
        )

    audio_result = await _run_checked(
        process_runner,
        adb("shell", "dumpsys", "audio"),
        label="read Android audio mode",
    )
    audio_text = _decode(audio_result)
    actual_mode = "MODE_IN_CALL" if is_mode_in_call(audio_text) else "NOT_IN_CALL"
    if require_active_call and actual_mode != "MODE_IN_CALL":
        raise AndroidCellularRxError(
            "Android is not in MODE_IN_CALL; establish and answer the cellular call manually first"
        )

    return AndroidRxPreflight(
        serial=selected,
        sdk_int=sdk_int,
        app_process=app_process,
        actual_mode=actual_mode,
        required_permissions=_REQUIRED_RX_PERMISSIONS,
    )


async def compile_android_rx_bridge(
    *,
    repo_root: Path,
    build_dir: Path,
    tools: AndroidBuildTools,
    runner: Optional[ProcessRunner] = None,
) -> Path:
    missing = tools.missing_host_tools()
    if missing:
        raise AndroidCellularRxError(
            "missing Android RX host tool(s): " + ", ".join(missing)
        )

    source = repo_root / "tools" / "android" / "TelephonyRxBridge.java"
    if not source.is_file():
        raise AndroidCellularRxError(f"Android RX bridge source is missing: {source}")

    process_runner = runner or AsyncioProcessRunner()
    classes_dir = build_dir / "rx-classes"
    dex_path = build_dir / "telephony-rx-bridge.dex"
    classes_dir.mkdir(parents=True, exist_ok=True)

    await _run_checked(
        process_runner,
        [
            tools.javac,
            "-source",
            "8",
            "-target",
            "8",
            "-cp",
            str(tools.android_jar),
            "-d",
            str(classes_dir),
            str(source),
        ],
        label="javac TelephonyRxBridge",
    )
    await _run_checked(
        process_runner,
        [
            str(tools.dx),
            "--dex",
            f"--output={dex_path}",
            str(classes_dir),
        ],
        label="dx TelephonyRxBridge",
    )
    if not dex_path.is_file():
        raise AndroidCellularRxError(f"dx completed without creating {dex_path}")
    return dex_path


async def push_android_rx_bridge(
    *,
    tools: AndroidBuildTools,
    serial: str,
    dex_path: Path,
    remote_path: str = DEFAULT_REMOTE_RX_DEX,
    runner: Optional[ProcessRunner] = None,
) -> None:
    if not dex_path.is_file():
        raise AndroidCellularRxError(f"DEX does not exist: {dex_path}")

    process_runner = runner or AsyncioProcessRunner()
    await _run_checked(
        process_runner,
        [tools.adb, "-s", serial, "push", str(dex_path), remote_path],
        label="adb push TelephonyRxBridge",
    )


class AndroidCellularRxCodec:
    """Reference Android downlink PCM -> existing SHUO mu-law/8k boundary."""

    def __init__(self) -> None:
        self._tail = bytearray()
        self._rate_state = None

    def feed(self, pcm_s16le_48k_stereo: bytes) -> bytes:
        if not pcm_s16le_48k_stereo:
            return b""

        data = bytes(self._tail) + pcm_s16le_48k_stereo
        self._tail.clear()

        remainder = len(data) % ANDROID_RX_FRAME_BYTES
        if remainder:
            self._tail.extend(data[-remainder:])
            data = data[:-remainder]

        if not data:
            return b""

        mono_48k = audioop.tomono(data, 2, 0.5, 0.5)
        mono_8k, self._rate_state = audioop.ratecv(
            mono_48k,
            2,
            1,
            ANDROID_RX_SAMPLE_RATE,
            8_000,
            self._rate_state,
        )
        return audioop.lin2ulaw(mono_8k, 2)

    def finish(self) -> bytes:
        if self._tail:
            raise AndroidCellularRxError(
                "Android RX stream ended with an incomplete stereo PCM16 frame"
            )
        return b""

    def reset(self) -> None:
        self._tail.clear()
        self._rate_state = None


class AdbTelephonyRxBridge:
    """Long-lived Android VOICE_DOWNLINK -> binary ADB stdout bridge."""

    def __init__(
        self,
        *,
        adb: str,
        serial: str,
        remote_dex: str = DEFAULT_REMOTE_RX_DEX,
        runner: Optional[ProcessRunner] = None,
        ready_timeout_seconds: float = 5.0,
        stop_timeout_seconds: float = 2.0,
        diagnostic_lines: int = 64,
    ) -> None:
        if ready_timeout_seconds <= 0 or stop_timeout_seconds <= 0:
            raise ValueError("bridge timeouts must be positive")
        if diagnostic_lines <= 0:
            raise ValueError("diagnostic_lines must be positive")

        self._adb = adb
        self._serial = serial
        self._remote_dex = remote_dex
        self._runner = runner or AsyncioProcessRunner()
        self._ready_timeout_seconds = ready_timeout_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._diagnostics: deque[str] = deque(maxlen=diagnostic_lines)
        self._proc = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._started = False
        self._closed = False
        self._pcm_bytes = 0

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)

    @property
    def pcm_bytes(self) -> int:
        return self._pcm_bytes

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and not self._closed

    def _argv(self) -> list[str]:
        command = (
            f"CLASSPATH={self._remote_dex} "
            "app_process / TelephonyRxBridge"
        )
        return [
            self._adb,
            "-s",
            self._serial,
            "shell",
            "-T",
            command,
        ]

    async def start(self) -> None:
        if self._started:
            return

        self._proc = await self._runner.spawn(
            self._argv(),
            stdin=False,
            stdout=True,
            stderr=True,
        )
        if self._proc.stdout is None or self._proc.stderr is None:
            raise AndroidCellularRxError(
                "ADB RX bridge did not expose stdout/stderr pipes"
            )

        self._started = True
        self._stderr_task = asyncio.create_task(self._read_stderr())
        ready_waiter = asyncio.create_task(self._ready.wait())
        process_waiter = asyncio.create_task(self._proc.wait())

        try:
            done, _ = await asyncio.wait(
                {ready_waiter, process_waiter},
                timeout=self._ready_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_waiter in done and self._ready.is_set():
                return
            if process_waiter in done:
                code = process_waiter.result()
                raise AndroidCellularRxError(
                    "Android Telephony Rx helper exited before STREAM_READY "
                    f"(exit={code}); diagnostics={list(self._diagnostics)!r}"
                )
            raise AndroidCellularRxError(
                "timed out waiting for Android Telephony Rx STREAM_READY; "
                f"diagnostics={list(self._diagnostics)!r}"
            )
        except Exception:
            await self.abort()
            raise
        finally:
            for task in (ready_waiter, process_waiter):
                if not task.done():
                    task.cancel()

    async def _read_stderr(self) -> None:
        assert self._proc is not None
        stream = self._proc.stderr
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            self._diagnostics.append(line)
            if line == "STREAM_READY":
                self._ready.set()

    async def read_pcm(self, max_bytes: int = 4096) -> bytes:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if not self.is_ready or self._proc is None or self._proc.stdout is None:
            raise AndroidCellularRxError("ADB Telephony Rx bridge is not ready")
        if self._proc.returncode is not None:
            raise AndroidCellularRxError(
                f"ADB Telephony Rx helper already exited (exit={self._proc.returncode})"
            )

        chunk = await self._proc.stdout.read(max_bytes)
        if not chunk:
            code = await self._proc.wait()
            raise AndroidCellularRxError(
                "Android Telephony Rx stream ended unexpectedly "
                f"(exit={code}); diagnostics={list(self._diagnostics)!r}"
            )

        self._pcm_bytes += len(chunk)
        return bytes(chunk)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._proc is not None:
            await stop_process(
                self._proc,
                timeout_seconds=self._stop_timeout_seconds,
            )

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

    async def abort(self) -> None:
        await self.close()


async def probe_android_cellular_rx(
    bridge: AdbTelephonyRxBridge,
    *,
    duration_seconds: float,
) -> AndroidRxProbeMetrics:
    """Consume RX only in memory and report content-free energy/byte metrics."""

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration_seconds
    codec = AndroidCellularRxCodec()
    total = 0
    chunks = 0
    peak_rms = 0
    rms_sum = 0
    mulaw_bytes = 0

    while loop.time() < deadline:
        remaining = max(0.05, deadline - loop.time())
        try:
            chunk = await asyncio.wait_for(
                bridge.read_pcm(),
                timeout=min(1.0, remaining),
            )
        except asyncio.TimeoutError:
            continue

        total += len(chunk)
        chunks += 1
        if len(chunk) >= 2:
            rms = audioop.rms(chunk[: len(chunk) - (len(chunk) % 2)], 2)
            peak_rms = max(peak_rms, rms)
            rms_sum += rms
        mulaw_bytes += len(codec.feed(chunk))

    codec.finish()

    return AndroidRxProbeMetrics(
        pcm_bytes=total,
        pcm_duration_seconds=(
            total
            / (
                ANDROID_RX_SAMPLE_RATE
                * ANDROID_RX_CHANNELS
                * ANDROID_RX_SAMPLE_WIDTH
            )
        ),
        chunk_count=chunks,
        peak_rms=peak_rms,
        average_rms=(rms_sum / chunks if chunks else 0.0),
        mulaw_bytes=mulaw_bytes,
    )
