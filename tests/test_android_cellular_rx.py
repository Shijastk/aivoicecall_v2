from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shuo.benchmark.android_cellular_rx import (
    ANDROID_RX_FRAME_BYTES,
    AdbTelephonyRxBridge,
    AndroidCellularRxCodec,
    AndroidCellularRxError,
    preflight_android_cellular_rx,
    probe_android_cellular_rx,
)
from shuo.benchmark.android_cellular_tx import AndroidBuildTools
from shuo.bluetooth.process import CompletedCommand


def _command(argv, stdout="", stderr="", returncode=0):
    return CompletedCommand(
        argv=tuple(argv),
        returncode=returncode,
        stdout=stdout.encode(),
        stderr=stderr.encode(),
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
async def test_rx_preflight_requires_capture_permissions_and_active_call():
    runner = FakeRunner(
        [
            {"stdout": "List of devices attached\nSERIAL device usb:3-4\n"},
            {"stdout": "33\n"},
            {"stdout": "/system/bin/app_process\n"},
            {
                "stdout": (
                    "{android.permission.RECORD_AUDIO, "
                    "android.permission.CAPTURE_AUDIO_OUTPUT}"
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

    result = await preflight_android_cellular_rx(
        tools,
        serial="SERIAL",
        runner=runner,
    )

    assert result.serial == "SERIAL"
    assert result.sdk_int == 33
    assert result.app_process == "/system/bin/app_process"
    assert result.actual_mode == "MODE_IN_CALL"
    assert result.required_permissions == (
        "android.permission.RECORD_AUDIO",
        "android.permission.CAPTURE_AUDIO_OUTPUT",
    )


@pytest.mark.asyncio
async def test_rx_preflight_fails_closed_when_capture_permission_missing():
    runner = FakeRunner(
        [
            {"stdout": "List of devices attached\nSERIAL device\n"},
            {"stdout": "33\n"},
            {"stdout": "/system/bin/app_process\n"},
            {"stdout": "{android.permission.RECORD_AUDIO}"},
        ]
    )
    tools = AndroidBuildTools(
        javac="javac",
        android_jar=Path("/unused/android.jar"),
        dx=Path("/unused/dx"),
        adb="adb",
    )

    with pytest.raises(AndroidCellularRxError, match="CAPTURE_AUDIO_OUTPUT"):
        await preflight_android_cellular_rx(
            tools,
            serial="SERIAL",
            runner=runner,
        )


@pytest.mark.asyncio
async def test_rx_preflight_rejects_non_call_mode():
    runner = FakeRunner(
        [
            {"stdout": "List of devices attached\nSERIAL device\n"},
            {"stdout": "33\n"},
            {"stdout": "/system/bin/app_process\n"},
            {
                "stdout": (
                    "{android.permission.RECORD_AUDIO, "
                    "android.permission.CAPTURE_AUDIO_OUTPUT}"
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

    with pytest.raises(AndroidCellularRxError, match="MODE_IN_CALL"):
        await preflight_android_cellular_rx(
            tools,
            serial="SERIAL",
            runner=runner,
        )


def test_rx_codec_handles_fragmented_stereo_pcm_and_preserves_core_contract():
    codec = AndroidCellularRxCodec()

    # 10ms at 48kHz, stereo, signed PCM16.
    frame = (
        (1200).to_bytes(2, "little", signed=True)
        + (-1200).to_bytes(2, "little", signed=True)
    )
    pcm = frame * 480

    first = codec.feed(pcm[:7])
    second = codec.feed(pcm[7:1001])
    third = codec.feed(pcm[1001:])
    codec.finish()

    mulaw = first + second + third
    assert len(pcm) == 1920
    assert len(mulaw) == 80


def test_rx_codec_rejects_partial_final_stereo_frame():
    codec = AndroidCellularRxCodec()
    codec.feed(b"\x00\x00\x00")

    with pytest.raises(AndroidCellularRxError, match="incomplete stereo PCM16"):
        codec.finish()

    codec.reset()
    assert codec.finish() == b""


class FakeStdout:
    def __init__(self):
        self.queue = asyncio.Queue()

    def feed(self, data):
        self.queue.put_nowait(bytes(data))

    async def read(self, max_bytes):
        data = await self.queue.get()
        if len(data) <= max_bytes:
            return data
        head = data[:max_bytes]
        tail = data[max_bytes:]
        self.queue.put_nowait(tail)
        return head


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdin = None
        self.stdout = FakeStdout()
        self.stderr = asyncio.StreamReader()
        self.done = asyncio.Event()

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
        assert stdin is False
        assert stdout is True
        assert stderr is True
        self.argv = tuple(argv)
        self.process = FakeProcess()
        self.process.stderr.feed_data(b"SHUO_TELEPHONY_RX_BRIDGE\n")
        self.process.stderr.feed_data(b"SOURCE=VOICE_DOWNLINK\n")
        self.process.stderr.feed_data(b"PCM_RATE=48000\n")
        self.process.stderr.feed_data(b"PCM_CHANNELS=2\n")
        self.process.stderr.feed_data(b"STREAM_READY\n")
        return self.process


@pytest.mark.asyncio
async def test_rx_bridge_uses_binary_stdout_without_tty_or_stdin():
    runner = FakeBridgeRunner()
    bridge = AdbTelephonyRxBridge(
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
        "CLASSPATH=/data/local/tmp/shuo-telephony-rx-bridge.dex "
        "app_process / TelephonyRxBridge",
    )

    runner.process.stdout.feed(b"\x01\x02\x03\x04" * 160)
    chunk = await bridge.read_pcm()

    assert chunk == b"\x01\x02\x03\x04" * 160
    assert bridge.pcm_bytes == len(chunk)

    await bridge.close()
    assert runner.process.returncode == -15


class ProbeBridge:
    def __init__(self, chunk):
        self.chunk = chunk
        self.pcm_bytes = 0

    async def read_pcm(self, max_bytes=4096):
        await asyncio.sleep(0)
        self.pcm_bytes += len(self.chunk)
        return self.chunk


@pytest.mark.asyncio
async def test_rx_probe_reports_content_free_energy_and_mulaw_bytes():
    frame = (
        (1000).to_bytes(2, "little", signed=True)
        + (1000).to_bytes(2, "little", signed=True)
    )
    chunk = frame * 480
    bridge = ProbeBridge(chunk)

    metrics = await probe_android_cellular_rx(
        bridge,
        duration_seconds=0.001,
    )

    assert metrics.pcm_bytes > 0
    assert metrics.chunk_count > 0
    assert metrics.peak_rms > 0
    assert metrics.average_rms > 0
    assert metrics.mulaw_bytes > 0


def test_java_rx_helper_uses_voice_downlink_and_no_audio_file_path():
    source = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "android"
        / "TelephonyRxBridge.java"
    ).read_text(encoding="utf-8")

    assert "MediaRecorder.AudioSource.VOICE_DOWNLINK" in source
    assert "CHANNEL_IN_STEREO" in source
    assert "SAMPLE_RATE = 48000" in source
    assert "FileDescriptor.out" in source
    assert 'log("STREAM_READY")' in source
    assert "new File(" not in source
    assert "FileOutputStream(FileDescriptor.out)" in source


def test_android_rx_frame_contract_is_pcm16_stereo():
    assert ANDROID_RX_FRAME_BYTES == 4
