from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from shuo.benchmark.android_cellular_tx import (
    AdbDevice,
    AdbTelephonyTxBridge,
    AndroidBuildTools,
    AndroidCellularTxError,
    choose_adb_serial,
    is_mode_in_call,
    parse_adb_devices,
    parse_privapp_permissions,
    preflight_android_cellular_tx,
    stream_pocket_utterance_to_bridge,
)
from shuo.bluetooth.process import CompletedCommand


def _command(argv, stdout="", stderr="", returncode=0):
    return CompletedCommand(
        argv=tuple(argv),
        returncode=returncode,
        stdout=stdout.encode(),
        stderr=stderr.encode(),
    )


def test_parse_adb_devices_and_fail_closed_selection():
    devices = parse_adb_devices(
        "\n".join(
            [
                "List of devices attached",
                "SERIAL1 device usb:3-4 model:itel_P683L",
                "SERIAL2 unauthorized usb:1-1",
            ]
        )
    )

    assert devices == (
        AdbDevice("SERIAL1", "device", "usb:3-4 model:itel_P683L"),
        AdbDevice("SERIAL2", "unauthorized", "usb:1-1"),
    )
    assert choose_adb_serial(devices, None) == "SERIAL1"
    assert choose_adb_serial(devices, "SERIAL1") == "SERIAL1"

    with pytest.raises(AndroidCellularTxError, match="not uniquely connected"):
        choose_adb_serial(devices, "SERIAL2")

    with pytest.raises(AndroidCellularTxError, match="exactly one"):
        choose_adb_serial(
            (AdbDevice("A", "device"), AdbDevice("B", "device")),
            None,
        )


def test_privapp_and_call_mode_parsers_use_explicit_evidence():
    grants = parse_privapp_permissions(
        "{android.permission.MODIFY_AUDIO_ROUTING, "
        "android.permission.MODIFY_PHONE_STATE, android.permission.DUMP}"
    )
    assert "android.permission.MODIFY_AUDIO_ROUTING" in grants
    assert "android.permission.MODIFY_PHONE_STATE" in grants

    assert is_mode_in_call(
        "Audio mode:\n- Requested mode = MODE_IN_CALL\n"
        "- Actual mode = MODE_IN_CALL"
    )
    assert not is_mode_in_call(
        "Audio mode:\n- Requested mode = MODE_NORMAL\n"
        "- Actual mode = MODE_NORMAL"
    )


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def run(self, argv):
        self.calls.append(tuple(argv))
        if not self.responses:
            raise AssertionError(f"unexpected command: {argv}")
        response = self.responses.pop(0)
        return _command(argv, **response)

    async def spawn(self, argv, *, stdin, stdout, stderr):
        raise AssertionError("spawn not expected in preflight test")


@pytest.mark.asyncio
async def test_preflight_requires_device_permissions_and_active_call():
    runner = FakeRunner(
        [
            {"stdout": "List of devices attached\nSERIAL device usb:3-4\n"},
            {"stdout": "33\n"},
            {"stdout": "/system/bin/app_process\n"},
            {
                "stdout": (
                    "{android.permission.MODIFY_PHONE_STATE, "
                    "android.permission.MODIFY_AUDIO_ROUTING}"
                )
            },
            {
                "stdout": (
                    "Audio mode:\n"
                    "- Requested mode = MODE_IN_CALL\n"
                    "- Actual mode = MODE_IN_CALL\n"
                )
            },
        ]
    )
    tools = AndroidBuildTools(
        javac="javac",
        android_jar=Path("/unused/android.jar"),
        dx=Path("/unused/dx"),
        adb="adb",
    )

    result = await preflight_android_cellular_tx(
        tools,
        serial="SERIAL",
        runner=runner,
    )

    assert result.serial == "SERIAL"
    assert result.sdk_int == 33
    assert result.actual_mode == "MODE_IN_CALL"
    assert result.app_process == "/system/bin/app_process"
    assert result.required_permissions == (
        "android.permission.MODIFY_PHONE_STATE",
        "android.permission.MODIFY_AUDIO_ROUTING",
    )


@pytest.mark.asyncio
async def test_preflight_rejects_non_call_mode_before_helper_start():
    runner = FakeRunner(
        [
            {"stdout": "List of devices attached\nSERIAL device\n"},
            {"stdout": "33\n"},
            {"stdout": "/system/bin/app_process\n"},
            {
                "stdout": (
                    "{android.permission.MODIFY_PHONE_STATE, "
                    "android.permission.MODIFY_AUDIO_ROUTING}"
                )
            },
            {
                "stdout": (
                    "Audio mode:\n"
                    "- Requested mode = MODE_NORMAL\n"
                    "- Actual mode = MODE_NORMAL\n"
                )
            },
        ]
    )
    tools = AndroidBuildTools(
        javac="javac",
        android_jar=Path("/unused/android.jar"),
        dx=Path("/unused/dx"),
        adb="adb",
    )

    with pytest.raises(AndroidCellularTxError, match="MODE_IN_CALL"):
        await preflight_android_cellular_tx(
            tools,
            serial="SERIAL",
            runner=runner,
        )


class FakeStdin:
    def __init__(self, process):
        self.process = process
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        if self.closed:
            raise BrokenPipeError
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.process.stderr.feed_data(
            f"PCM_BYTES={len(self.data)}\nSTREAM_DONE\n".encode()
        )
        self.process.stderr.feed_eof()
        self.process.returncode = 0
        self.process.done.set()

    async def wait_closed(self):
        return None


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stderr = asyncio.StreamReader()
        self.stdout = None
        self.done = asyncio.Event()
        self.stdin = FakeStdin(self)

    async def wait(self):
        await self.done.wait()
        return int(self.returncode)

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15
            self.stderr.feed_eof()
            self.done.set()

    def kill(self):
        if self.returncode is None:
            self.returncode = -9
            self.stderr.feed_eof()
            self.done.set()


class FakeBridgeRunner:
    def __init__(self):
        self.process = None
        self.argv = None

    async def run(self, argv):
        raise AssertionError("run not expected")

    async def spawn(self, argv, *, stdin, stdout, stderr):
        assert stdin is True
        assert stdout is False
        assert stderr is True
        self.argv = tuple(argv)
        self.process = FakeProcess()
        self.process.stderr.feed_data(b"SHUO_TELEPHONY_TX_BRIDGE\n")
        self.process.stderr.feed_data(b"ROUTED_TYPE=18\n")
        self.process.stderr.feed_data(b"STREAM_READY\n")
        return self.process


@pytest.mark.asyncio
async def test_bridge_streams_binary_pcm_without_shell_tty():
    runner = FakeBridgeRunner()
    bridge = AdbTelephonyTxBridge(
        adb="adb",
        serial="SERIAL",
        runner=runner,
        ready_timeout_seconds=0.2,
        stop_timeout_seconds=0.2,
    )

    await bridge.start()
    assert bridge.is_ready
    assert runner.argv == (
        "adb",
        "-s",
        "SERIAL",
        "shell",
        "-T",
        "CLASSPATH=/data/local/tmp/shuo-telephony-tx-bridge.dex "
        "app_process / TelephonyTxBridge",
    )

    await bridge.write_pcm16(b"\x01\x02" * 320)
    with pytest.raises(AndroidCellularTxError, match="odd-length"):
        await bridge.write_pcm16(b"\x00")

    code = await bridge.close()

    assert code == 0
    assert bridge.pcm_bytes == 640
    assert bytes(runner.process.stdin.data) == b"\x01\x02" * 320
    assert "STREAM_READY" in bridge.diagnostics
    assert "STREAM_DONE" in bridge.diagnostics


class FakePocketService:
    def __init__(
        self,
        on_audio,
        on_done,
        voice_id=None,
        *,
        voice_source=None,
        **_kwargs,
    ):
        self.on_audio = on_audio
        self.on_done = on_done
        self.voice_source = voice_source
        self.fatal_error = None

    async def start(self):
        return None

    async def send(self, text):
        assert text
        await self.on_audio(
            base64.b64encode(b"\xff" * 160).decode("ascii")
        )

    async def flush(self):
        await self.on_done()

    async def cancel(self):
        return None


class CollectingBridge:
    def __init__(self):
        self.chunks = []

    async def write_pcm16(self, data):
        assert len(data) % 2 == 0
        self.chunks.append(bytes(data))


@pytest.mark.asyncio
async def test_pocket_stream_uses_existing_mulaw_then_bluetooth_codec_boundary():
    bridge = CollectingBridge()

    metrics = await stream_pocket_utterance_to_bridge(
        "synthetic caller test",
        bridge,
        pocket_cls=FakePocketService,
    )

    pcm = b"".join(bridge.chunks)
    assert pcm
    assert len(pcm) % 2 == 0
    assert metrics.pcm_bytes == len(pcm)
    assert metrics.pcm_duration_seconds > 0
    assert metrics.model_ready_ms >= 0
    assert metrics.first_pcm_ms is not None
    assert metrics.model_ready_to_first_pcm_ms is not None


def test_java_helper_fails_closed_on_actual_telephony_route_and_writes_no_file():
    source = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "android"
        / "TelephonyTxBridge.java"
    ).read_text(encoding="utf-8")

    assert "getRoutedDevice()" in source
    assert "AudioDeviceInfo.TYPE_TELEPHONY" in source
    assert 'log("STREAM_READY")' in source
    assert 'log("STREAM_DONE")' in source
    assert "System.exit(0)" in source
    assert "FileOutputStream" not in source
