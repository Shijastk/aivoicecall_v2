import asyncio

import pytest

from shuo.bluetooth.codec import BLUETOOTH_PCM_FORMAT
from shuo.bluetooth.pipewire import PipeWireTarget
from shuo.bluetooth.pipewire_live import PwCatCaptureEndpoint, PwCatConfig


ADDRESS = "00:C7:11:7B:84:21"


def _target():
    return PipeWireTarget(
        node_name="bluez_input.fixture.0",
        factory_name="api.bluez5.sco.source",
        media_class="Stream/Output/Audio",
        bluetooth_address=ADDRESS,
        bluetooth_profile="headset-audio-gateway",
        bluetooth_codec="msbc",
        audio_format=BLUETOOTH_PCM_FORMAT,
    )


class DrainAwareReader:
    def __init__(self, owner, chunks):
        self.owner = owner
        self.chunks = list(chunks)

    async def read(self, _n=-1):
        await asyncio.sleep(0)

        if self.chunks:
            return self.chunks.pop(0)

        if self.owner.returncode is not None:
            self.owner._done.set()

        return b""


class EmptyReader:
    async def read(self, _n=-1):
        await asyncio.sleep(0)
        return b""


class DrainAwareProcess:
    """
    Model the hardware failure: terminate marks the child exited, but wait does
    not complete until captured stdout is consumed through EOF.
    """

    def __init__(self):
        self.stdin = None
        self.returncode = None
        self._done = asyncio.Event()
        self.stdout = DrainAwareReader(
            self,
            [b"x" * 4096, b"y" * 4096, b"z" * 4096],
        )
        self.stderr = EmptyReader()
        self.terminate_calls = 0
        self.kill_calls = 0

    async def wait(self):
        await self._done.wait()
        return int(self.returncode)

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


class FakeSpawnRunner:
    def __init__(self, proc):
        self.proc = proc

    async def spawn(self, argv, *, stdin, stdout, stderr):
        return self.proc


@pytest.mark.asyncio
async def test_capture_stop_drains_unread_stdout_and_reaps_process():
    proc = DrainAwareProcess()
    endpoint = PwCatCaptureEndpoint(
        _target(),
        FakeSpawnRunner(proc),
        PwCatConfig(latency="40ms", stop_timeout_seconds=0.25),
        system_name="Linux",
    )

    await endpoint.start()

    # Deliberately do not call endpoint.read(). This reproduces the real-call
    # lifecycle test where capture stdout was left unread for the session.
    await endpoint.stop()

    assert proc.terminate_calls == 1
    assert proc.kill_calls == 0
    assert proc._done.is_set()


@pytest.mark.asyncio
async def test_capture_repeated_stop_remains_idempotent():
    proc = DrainAwareProcess()
    endpoint = PwCatCaptureEndpoint(
        _target(),
        FakeSpawnRunner(proc),
        PwCatConfig(latency="40ms", stop_timeout_seconds=0.25),
        system_name="Linux",
    )

    await endpoint.start()
    await endpoint.stop()
    await endpoint.stop()

    assert proc.terminate_calls == 1
