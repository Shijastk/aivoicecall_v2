from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .codec import AudioFormat, BLUETOOTH_PCM_FORMAT, SampleEncoding
from .pipewire import (
    PipeWireDiscovery,
    PipeWireSelectionError,
    PipeWireTarget,
    StreamDirection,
    require_pipewire_platform,
    select_target,
)
from .process import (
    AsyncioProcessRunner,
    ProcessExitedError,
    ProcessRunner,
    RunningProcess,
    stop_process,
)
from .transport import (
    BoundedAudioQueue,
    OverflowPolicy,
    QueueClosedError,
)


class PipeWireDiscoveryError(RuntimeError):
    pass


class PipeWireStreamError(RuntimeError):
    pass


def _scalar(value: Any) -> Any:
    """
    Conservative extraction for pw-dump values that may be represented either
    as a scalar or as a PipeWire choice object containing `default`.

    Unknown shapes return None rather than being guessed.
    """
    if isinstance(value, (str, int, float)):
        return value

    if isinstance(value, dict):
        default = value.get("default")
        if isinstance(default, (str, int, float)):
            return default

    return None


def _normalize_format(value: Any) -> Optional[str]:
    value = _scalar(value)
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower().replace("_", "").replace("-", "")
    if normalized in {"s16", "s16le"}:
        return "s16le"

    return None


def _find_enum_format(node: dict[str, Any]) -> Optional[AudioFormat]:
    """
    Find an explicitly described S16LE/16k/mono EnumFormat.

    pw-dump structure varies between PipeWire versions, so this walks the
    `info.params.EnumFormat` tree conservatively. It only returns the Phase-1
    validated contract when format, rate and channel count are all explicit.
    """

    info = node.get("info")
    if not isinstance(info, dict):
        return None

    params = info.get("params")
    if not isinstance(params, dict):
        return None

    enum_format = params.get("EnumFormat")
    if enum_format is None:
        return None

    stack: list[Any] = [enum_format]

    while stack:
        current = stack.pop()

        if isinstance(current, list):
            stack.extend(current)
            continue

        if not isinstance(current, dict):
            continue

        fmt = _normalize_format(current.get("format"))
        rate = _scalar(current.get("rate"))
        channels = _scalar(current.get("channels"))

        if (
            fmt == "s16le"
            and int(rate) == 16_000 if isinstance(rate, (int, float)) else False
        ):
            if isinstance(channels, (int, float)) and int(channels) == 1:
                return BLUETOOTH_PCM_FORMAT

        stack.extend(current.values())

    return None


def _target_from_pw_dump_node(node: dict[str, Any]) -> Optional[PipeWireTarget]:
    if node.get("type") != "PipeWire:Interface:Node":
        return None

    info = node.get("info")
    if not isinstance(info, dict):
        return None

    props = info.get("props")
    if not isinstance(props, dict):
        return None

    node_name = props.get("node.name")
    factory_name = props.get("factory.name")
    media_class = props.get("media.class")
    address = props.get("api.bluez5.address")
    profile = props.get("api.bluez5.profile")
    codec = props.get("api.bluez5.codec")

    required = (node_name, factory_name, media_class, address, profile, codec)
    if not all(isinstance(value, str) and value for value in required):
        return None

    audio_format = _find_enum_format(node)

    # Some PipeWire builds expose negotiated raw audio fields directly in props.
    # Accept them only when every field is explicit.
    if audio_format is None:
        prop_format = _normalize_format(props.get("audio.format"))
        prop_rate = _scalar(props.get("audio.rate"))
        prop_channels = _scalar(props.get("audio.channels"))

        if (
            prop_format == "s16le"
            and isinstance(prop_rate, (int, float))
            and int(prop_rate) == 16_000
            and isinstance(prop_channels, (int, float))
            and int(prop_channels) == 1
        ):
            audio_format = BLUETOOTH_PCM_FORMAT

    if audio_format is None:
        return None

    return PipeWireTarget(
        node_name=node_name,
        factory_name=factory_name,
        media_class=media_class,
        bluetooth_address=address,
        bluetooth_profile=profile,
        bluetooth_codec=codec,
        audio_format=audio_format,
    )


def parse_pw_dump_targets(payload: bytes | str) -> list[PipeWireTarget]:
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")

        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipeWireDiscoveryError("pw-dump returned invalid JSON") from exc

    if not isinstance(decoded, list):
        raise PipeWireDiscoveryError("pw-dump root must be a JSON array")

    targets: list[PipeWireTarget] = []

    for item in decoded:
        if not isinstance(item, dict):
            continue

        target = _target_from_pw_dump_node(item)
        if target is not None:
            targets.append(target)

    return targets


class PwDumpDiscovery(PipeWireDiscovery):
    """
    Real Phase-3 discovery using pw-dump.

    No command runs at import time.
    """

    def __init__(
        self,
        runner: ProcessRunner,
        *,
        system_name: Optional[str] = None,
    ):
        self._runner = runner
        self._system_name = system_name

    async def list_targets(self) -> list[PipeWireTarget]:
        require_pipewire_platform(self._system_name)

        result = await self._runner.run(("pw-dump",))

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise PipeWireDiscoveryError(
                f"pw-dump failed with exit {result.returncode}: {stderr}"
            )

        return parse_pw_dump_targets(result.stdout)


@dataclass(frozen=True)
class PwCatConfig:
    """
    Explicit pw-cat stream configuration.

    `latency` has no default on purpose. Phase 3 must measure/approve the runtime
    value instead of inheriting pw-cat's implicit default as product policy.
    """

    latency: str
    stop_timeout_seconds: float = 1.0
    read_size: int = 4096
    playback_queue_chunks: int = 8
    playback_overflow_policy: OverflowPolicy = OverflowPolicy.REJECT_NEW

    def __post_init__(self) -> None:
        if not self.latency.strip():
            raise ValueError("latency must be explicit")
        if self.stop_timeout_seconds <= 0:
            raise ValueError("stop_timeout_seconds must be positive")
        if self.read_size <= 0:
            raise ValueError("read_size must be positive")
        if self.playback_queue_chunks <= 0:
            raise ValueError("playback_queue_chunks must be positive")


def build_pw_cat_command(
    target: PipeWireTarget,
    *,
    direction: StreamDirection,
    latency: str,
) -> tuple[str, ...]:
    """
    Build an explicitly-targeted raw S16/16k/mono pw-cat command.

    `s16` is the pw-cat CLI spelling for signed 16-bit PCM. The validated
    Bluetooth boundary contract remains S16LE/16k/mono.
    """

    if target.audio_format != BLUETOOTH_PCM_FORMAT:
        raise PipeWireStreamError("target audio format is not S16LE/16k/mono")

    if not latency.strip():
        raise ValueError("latency must be explicit")

    mode = "--record" if direction is StreamDirection.DOWNLINK else "--playback"

    return (
        "pw-cat",
        mode,
        "--target",
        target.node_name,
        "--format",
        "s16",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--channel-map",
        "mono",
        "--latency",
        latency,
        "--raw",
        "-",
    )


class _PwCatBase:
    def __init__(
        self,
        target: PipeWireTarget,
        runner: ProcessRunner,
        config: PwCatConfig,
        *,
        system_name: Optional[str] = None,
    ):
        self._target = target
        self._runner = runner
        self._config = config
        self._system_name = system_name
        self._proc: RunningProcess | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def stderr_tail(self) -> bytes:
        return bytes(self._stderr_tail)

    async def _start_process(
        self,
        *,
        direction: StreamDirection,
        stdin: bool,
        stdout: bool,
    ) -> None:
        require_pipewire_platform(self._system_name)

        if self.running:
            return

        command = build_pw_cat_command(
            self._target,
            direction=direction,
            latency=self._config.latency,
        )

        proc = await self._runner.spawn(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=True,
        )

        self._proc = proc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Yield once so an immediate exec/target failure can surface.
        await asyncio.sleep(0)

        if proc.returncode is not None:
            code = proc.returncode
            await self._finish_stderr_task()
            self._proc = None
            raise PipeWireStreamError(
                f"pw-cat exited during startup with code {code}: "
                f"{self.stderr_tail.decode('utf-8', errors='replace')}"
            )

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        reader = proc.stderr

        while True:
            chunk = await reader.read(1024)
            if not chunk:
                return

            self._stderr_tail.extend(chunk)

            # Keep diagnostics bounded.
            if len(self._stderr_tail) > 8192:
                del self._stderr_tail[:-8192]

    async def _finish_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None

        if task is None:
            return

        if not task.done():
            task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _stop_process(self) -> None:
        proc = self._proc
        self._proc = None

        if proc is not None:
            await stop_process(
                proc,
                timeout_seconds=self._config.stop_timeout_seconds,
            )

        await self._finish_stderr_task()


class PwCatCaptureEndpoint(_PwCatBase):
    """
    Phone downlink capture only.

    No default source is ever opened: `--target <validated node.name>` is mandatory.
    """

    async def start(self) -> None:
        await self._start_process(
            direction=StreamDirection.DOWNLINK,
            stdin=False,
            stdout=True,
        )

    async def read(self) -> bytes:
        proc = self._proc

        if proc is None or proc.stdout is None:
            raise PipeWireStreamError("capture is not started")

        chunk = await proc.stdout.read(self._config.read_size)

        if chunk:
            return chunk

        code = await proc.wait()
        raise ProcessExitedError(
            f"pw-cat capture exited with code {code}: "
            f"{self.stderr_tail.decode('utf-8', errors='replace')}"
        )

    async def stop(self) -> None:
        await self._stop_process()


class PwCatPlaybackEndpoint(_PwCatBase):
    """
    Phone uplink playback only.

    A bounded local queue exists so clear() can discard audio that has not yet
    been handed to pw-cat. Bytes already written to the OS/process pipe cannot be
    truthfully claimed as retractable.
    """

    def __init__(
        self,
        target: PipeWireTarget,
        runner: ProcessRunner,
        config: PwCatConfig,
        *,
        system_name: Optional[str] = None,
    ):
        super().__init__(
            target,
            runner,
            config,
            system_name=system_name,
        )
        self._queue: BoundedAudioQueue | None = None
        self._writer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.running:
            return

        self._queue = BoundedAudioQueue(
            max_frames=self._config.playback_queue_chunks,
            overflow_policy=self._config.playback_overflow_policy,
        )

        try:
            await self._start_process(
                direction=StreamDirection.UPLINK,
                stdin=True,
                stdout=False,
            )
            self._writer_task = asyncio.create_task(self._writer_loop())
        except BaseException:
            if self._queue is not None:
                self._queue.close()
                self._queue = None
            await self._stop_process()
            raise

    async def write(self, audio: bytes) -> None:
        if not audio:
            return

        queue = self._queue

        if not self.running or queue is None:
            raise PipeWireStreamError("playback is not started")

        accepted = queue.put_nowait(audio)

        if not accepted:
            raise PipeWireStreamError(
                "playback queue full; configured overflow policy rejected new audio"
            )

    async def clear(self) -> None:
        queue = self._queue
        if queue is not None:
            queue.clear()

    async def _writer_loop(self) -> None:
        proc = self._proc
        queue = self._queue

        if proc is None or proc.stdin is None or queue is None:
            raise PipeWireStreamError("playback process is not writable")

        writer = proc.stdin

        try:
            while True:
                data = await queue.get()
                writer.write(data)
                await writer.drain()

                if proc.returncode is not None:
                    raise ProcessExitedError(
                        f"pw-cat playback exited with code {proc.returncode}"
                    )

        except QueueClosedError:
            return

    async def stop(self) -> None:
        queue = self._queue
        self._queue = None

        if queue is not None:
            queue.close()

        task = self._writer_task
        self._writer_task = None

        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        proc = self._proc

        # Close stdin first so pw-cat can exit naturally before terminate().
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
                wait_closed = getattr(proc.stdin, "wait_closed", None)
                if wait_closed is not None:
                    await wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        await self._stop_process()


@dataclass(frozen=True)
class SelectedDuplexTargets:
    downlink: PipeWireTarget
    uplink: PipeWireTarget


async def discover_duplex_targets(
    discovery: PipeWireDiscovery,
    *,
    bluetooth_address: Optional[str],
) -> SelectedDuplexTargets:
    candidates = await discovery.list_targets()

    downlink = select_target(
        candidates,
        direction=StreamDirection.DOWNLINK,
        address=bluetooth_address,
    )
    uplink = select_target(
        candidates,
        direction=StreamDirection.UPLINK,
        address=bluetooth_address,
    )

    if downlink.bluetooth_address.casefold() != uplink.bluetooth_address.casefold():
        raise PipeWireSelectionError(
            "selected downlink/uplink belong to different Bluetooth devices"
        )

    return SelectedDuplexTargets(
        downlink=downlink,
        uplink=uplink,
    )
