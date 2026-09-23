from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from shuo.bluetooth.codec import BluetoothOutboundCodec
from shuo.bluetooth.process import (
    AsyncioProcessRunner,
    CompletedCommand,
    ProcessRunner,
    stop_process,
)
from shuo.services.tts_pocket import PocketTTSService


class AndroidCellularTxError(RuntimeError):
    """Fail-closed error for the opt-in Android cellular TX harness."""


DEFAULT_ANDROID_JAR = Path("/usr/lib/android-sdk/platforms/android-23/android.jar")
DEFAULT_DX = Path("/usr/lib/android-sdk/build-tools/debian/dx")
DEFAULT_REMOTE_DEX = "/data/local/tmp/shuo-telephony-tx-bridge.dex"
_REQUIRED_PRIVAPP_PERMISSIONS = (
    "android.permission.MODIFY_PHONE_STATE",
    "android.permission.MODIFY_AUDIO_ROUTING",
)
_DEVICE_LINE = re.compile(r"^(?P<serial>\S+)\s+(?P<state>\S+)(?:\s+(?P<details>.*))?$")
_MODE_IN_CALL = re.compile(r"Actual mode\s*=\s*MODE_IN_CALL\b")


@dataclass(frozen=True)
class AndroidBuildTools:
    javac: str
    android_jar: Path
    dx: Path
    adb: str

    @classmethod
    def from_environment(cls) -> "AndroidBuildTools":
        return cls(
            javac=(os.getenv("SHUO_ANDROID_JAVAC") or shutil.which("javac") or "javac"),
            android_jar=Path(os.getenv("SHUO_ANDROID_JAR") or str(DEFAULT_ANDROID_JAR)),
            dx=Path(os.getenv("SHUO_ANDROID_DX") or str(DEFAULT_DX)),
            adb=(os.getenv("SHUO_ADB") or shutil.which("adb") or "adb"),
        )

    def missing_host_tools(self) -> tuple[str, ...]:
        missing: list[str] = []
        if shutil.which(self.javac) is None and not Path(self.javac).is_file():
            missing.append(f"javac:{self.javac}")
        if not self.android_jar.is_file():
            missing.append(f"android.jar:{self.android_jar}")
        if not self.dx.is_file():
            missing.append(f"dx:{self.dx}")
        if shutil.which(self.adb) is None and not Path(self.adb).is_file():
            missing.append(f"adb:{self.adb}")
        return tuple(missing)


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    details: str = ""


@dataclass(frozen=True)
class AndroidTxPreflight:
    serial: str
    sdk_int: int
    app_process: str
    actual_mode: str
    required_permissions: tuple[str, ...]


@dataclass(frozen=True)
class PocketStreamMetrics:
    model_ready_ms: float
    first_pcm_ms: Optional[float]
    pcm_bytes: int
    pcm_duration_seconds: float

    @property
    def model_ready_to_first_pcm_ms(self) -> Optional[float]:
        if self.first_pcm_ms is None:
            return None
        return self.first_pcm_ms - self.model_ready_ms


def parse_adb_devices(output: str) -> tuple[AdbDevice, ...]:
    devices: list[AdbDevice] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices attached"):
            continue
        match = _DEVICE_LINE.match(line)
        if match is None:
            continue
        devices.append(
            AdbDevice(
                serial=match.group("serial"),
                state=match.group("state"),
                details=(match.group("details") or ""),
            )
        )
    return tuple(devices)


def choose_adb_serial(devices: Sequence[AdbDevice], requested: Optional[str]) -> str:
    usable = [device for device in devices if device.state == "device"]
    if requested:
        matches = [device for device in usable if device.serial == requested]
        if len(matches) != 1:
            raise AndroidCellularTxError(
                f"requested ADB device {requested!r} is not uniquely connected in state=device"
            )
        return requested
    if len(usable) != 1:
        raise AndroidCellularTxError(
            "exactly one ADB device in state=device is required when --serial is omitted"
        )
    return usable[0].serial


def parse_privapp_permissions(output: str) -> frozenset[str]:
    return frozenset(re.findall(r"android\.permission\.[A-Z0-9_]+", output))


def is_mode_in_call(output: str) -> bool:
    return _MODE_IN_CALL.search(output) is not None


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
        raise AndroidCellularTxError(f"{label} failed: {detail}")
    return result


async def discover_adb_serial(
    tools: AndroidBuildTools,
    *,
    requested_serial: Optional[str] = None,
    runner: Optional[ProcessRunner] = None,
) -> str:
    process_runner = runner or AsyncioProcessRunner()
    result = await _run_checked(
        process_runner,
        [tools.adb, "devices", "-l"],
        label="adb devices",
    )
    return choose_adb_serial(parse_adb_devices(_decode(result)), requested_serial)


async def preflight_android_cellular_tx(
    tools: AndroidBuildTools,
    *,
    serial: Optional[str] = None,
    runner: Optional[ProcessRunner] = None,
    require_active_call: bool = True,
) -> AndroidTxPreflight:
    """Check prerequisites; actual routing is still verified by the Java helper."""

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
        raise AndroidCellularTxError("Android SDK level was not an integer") from exc
    if sdk_int < 23:
        raise AndroidCellularTxError(
            f"Android SDK {sdk_int} is below the API 23 AudioDeviceInfo contract"
        )

    app_process_result = await _run_checked(
        process_runner,
        adb("shell", "command", "-v", "app_process"),
        label="locate app_process",
    )
    app_process = _decode(app_process_result)
    if not app_process.startswith("/"):
        raise AndroidCellularTxError("app_process was not exposed as an absolute path")

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
        for permission in _REQUIRED_PRIVAPP_PERMISSIONS
        if permission not in grants
    ]
    if missing:
        raise AndroidCellularTxError(
            "shell privapp allowlist is missing required telephony routing permission(s): "
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
        raise AndroidCellularTxError(
            "Android is not in MODE_IN_CALL; establish and answer the cellular call manually first"
        )

    return AndroidTxPreflight(
        serial=selected,
        sdk_int=sdk_int,
        app_process=app_process,
        actual_mode=actual_mode,
        required_permissions=_REQUIRED_PRIVAPP_PERMISSIONS,
    )


async def compile_android_tx_bridge(
    *,
    repo_root: Path,
    build_dir: Path,
    tools: AndroidBuildTools,
    runner: Optional[ProcessRunner] = None,
) -> Path:
    """Compile the repository-owned Java helper without Gradle/Android Studio."""

    missing = tools.missing_host_tools()
    if missing:
        raise AndroidCellularTxError(
            "missing Android TX host tool(s): " + ", ".join(missing)
        )

    source = repo_root / "tools" / "android" / "TelephonyTxBridge.java"
    if not source.is_file():
        raise AndroidCellularTxError(f"Android TX bridge source is missing: {source}")

    process_runner = runner or AsyncioProcessRunner()
    classes_dir = build_dir / "classes"
    dex_path = build_dir / "telephony-tx-bridge.dex"
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
        label="javac TelephonyTxBridge",
    )
    await _run_checked(
        process_runner,
        [
            str(tools.dx),
            "--dex",
            f"--output={dex_path}",
            str(classes_dir),
        ],
        label="dx TelephonyTxBridge",
    )
    if not dex_path.is_file():
        raise AndroidCellularTxError(f"dx completed without creating {dex_path}")
    return dex_path


async def push_android_tx_bridge(
    *,
    tools: AndroidBuildTools,
    serial: str,
    dex_path: Path,
    remote_path: str = DEFAULT_REMOTE_DEX,
    runner: Optional[ProcessRunner] = None,
) -> None:
    if not dex_path.is_file():
        raise AndroidCellularTxError(f"DEX does not exist: {dex_path}")
    process_runner = runner or AsyncioProcessRunner()
    await _run_checked(
        process_runner,
        [tools.adb, "-s", serial, "push", str(dex_path), remote_path],
        label="adb push TelephonyTxBridge",
    )


class AdbTelephonyTxBridge:
    """Long-lived, backpressured ADB stdin -> Android Telephony Tx bridge."""

    def __init__(
        self,
        *,
        adb: str,
        serial: str,
        remote_dex: str = DEFAULT_REMOTE_DEX,
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
        self._stream_done = False
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
            "app_process / TelephonyTxBridge"
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
            stdin=True,
            stdout=False,
            stderr=True,
        )
        if self._proc.stdin is None or self._proc.stderr is None:
            raise AndroidCellularTxError(
                "ADB bridge did not expose stdin/stderr pipes"
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
                raise AndroidCellularTxError(
                    "Android Telephony Tx helper exited before STREAM_READY "
                    f"(exit={code}); diagnostics={list(self._diagnostics)!r}"
                )
            raise AndroidCellularTxError(
                "timed out waiting for Android Telephony Tx STREAM_READY; "
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
            elif line == "STREAM_DONE":
                self._stream_done = True

    async def write_pcm16(self, pcm_s16le_16k: bytes) -> None:
        if not pcm_s16le_16k:
            return
        if len(pcm_s16le_16k) % 2:
            raise AndroidCellularTxError(
                "refusing odd-length PCM16 write at the ADB Telephony Tx boundary"
            )
        if not self.is_ready or self._proc is None or self._proc.stdin is None:
            raise AndroidCellularTxError("ADB Telephony Tx bridge is not ready")
        if self._proc.returncode is not None:
            raise AndroidCellularTxError(
                f"ADB Telephony Tx helper already exited (exit={self._proc.returncode})"
            )

        self._proc.stdin.write(pcm_s16le_16k)
        await self._proc.stdin.drain()
        self._pcm_bytes += len(pcm_s16le_16k)

    async def close(self) -> int:
        if self._closed:
            if self._proc is None or self._proc.returncode is None:
                return 0
            return int(self._proc.returncode)
        self._closed = True

        if self._proc is None:
            return 0

        if self._proc.stdin is not None:
            self._proc.stdin.close()
            wait_closed = getattr(self._proc.stdin, "wait_closed", None)
            if wait_closed is not None:
                try:
                    await asyncio.wait_for(
                        wait_closed(),
                        timeout=self._stop_timeout_seconds,
                    )
                except (
                    asyncio.TimeoutError,
                    BrokenPipeError,
                    ConnectionResetError,
                ):
                    pass

        try:
            code = await asyncio.wait_for(
                self._proc.wait(),
                timeout=self._stop_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await stop_process(
                self._proc,
                timeout_seconds=self._stop_timeout_seconds,
            )
            code = int(self._proc.returncode or 0)

        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stderr_task),
                    timeout=self._stop_timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except asyncio.CancelledError:
                    pass

        if code != 0:
            raise AndroidCellularTxError(
                f"Android Telephony Tx helper exited with {code}; "
                f"diagnostics={list(self._diagnostics)!r}"
            )
        if not self._stream_done:
            raise AndroidCellularTxError(
                "Android Telephony Tx helper exited without STREAM_DONE; "
                f"diagnostics={list(self._diagnostics)!r}"
            )
        return int(code)

    async def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc is not None:
            try:
                await stop_process(
                    self._proc,
                    timeout_seconds=self._stop_timeout_seconds,
                )
            finally:
                if self._stderr_task is not None:
                    self._stderr_task.cancel()
                    try:
                        await self._stderr_task
                    except asyncio.CancelledError:
                        pass


async def stream_pocket_utterance_to_bridge(
    text: str,
    bridge: AdbTelephonyTxBridge,
    *,
    voice_source: Optional[str] = None,
    timeout_seconds: float = 45.0,
    pocket_cls=PocketTTSService,
) -> PocketStreamMetrics:
    """Stream through Pocket's SHUO mu-law contract, then the Bluetooth codec.

    Timings are process-local generation/dispatch observations only; they are
    never caller-heard or mouth-to-ear measurements.
    """

    if not text.strip():
        raise ValueError("text must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    started = time.perf_counter()
    model_ready_at: Optional[float] = None
    first_pcm_at: Optional[float] = None
    total_pcm_bytes = 0
    codec = BluetoothOutboundCodec()
    done = asyncio.Event()

    async def on_audio(audio_base64: str) -> None:
        nonlocal first_pcm_at, total_pcm_bytes
        mulaw = base64.b64decode(audio_base64, validate=True)
        pcm = codec.feed(mulaw)
        if not pcm:
            return
        if first_pcm_at is None:
            first_pcm_at = time.perf_counter()
        await bridge.write_pcm16(pcm)
        total_pcm_bytes += len(pcm)

    async def on_done() -> None:
        done.set()

    service = pocket_cls(
        on_audio=on_audio,
        on_done=on_done,
        voice_id=None,
        voice_source=voice_source,
    )
    try:
        await asyncio.wait_for(service.start(), timeout=timeout_seconds)
        model_ready_at = time.perf_counter()
        await asyncio.wait_for(service.send(text), timeout=timeout_seconds)
        await asyncio.wait_for(service.flush(), timeout=timeout_seconds)
        await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        if service.fatal_error:
            raise AndroidCellularTxError(
                "Pocket TTS failed before completing synthetic caller speech: "
                f"{service.fatal_error}"
            )
        tail = codec.finish()
        if tail:
            await bridge.write_pcm16(tail)
            total_pcm_bytes += len(tail)
    finally:
        await service.cancel()

    if model_ready_at is None:
        raise AndroidCellularTxError("Pocket TTS never reached its ready boundary")
    if total_pcm_bytes <= 0:
        raise AndroidCellularTxError("Pocket TTS produced no cellular TX PCM")

    return PocketStreamMetrics(
        model_ready_ms=(model_ready_at - started) * 1000.0,
        first_pcm_ms=(
            (first_pcm_at - started) * 1000.0
            if first_pcm_at is not None
            else None
        ),
        pcm_bytes=total_pcm_bytes,
        pcm_duration_seconds=total_pcm_bytes / (16_000 * 2),
    )
