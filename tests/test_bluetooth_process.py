import asyncio

import pytest

from shuo.bluetooth.process import stop_process


class FakeProcess:
    def __init__(self, *, exit_on_terminate=True):
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.exit_on_terminate = exit_on_terminate
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return int(self.returncode)

    def terminate(self):
        self.terminated += 1
        if self.exit_on_terminate:
            self.returncode = 0
            self._done.set()

    def kill(self):
        self.killed += 1
        self.returncode = -9
        self._done.set()


@pytest.mark.asyncio
async def test_stop_process_terminates_cleanly():
    proc = FakeProcess()

    await stop_process(proc, timeout_seconds=0.05)

    assert proc.terminated == 1
    assert proc.killed == 0


@pytest.mark.asyncio
async def test_stop_process_kills_after_timeout():
    proc = FakeProcess(exit_on_terminate=False)

    await stop_process(proc, timeout_seconds=0.001)

    assert proc.terminated == 1
    assert proc.killed == 1


@pytest.mark.asyncio
async def test_stop_process_is_idempotent_for_exited_process():
    proc = FakeProcess()
    proc.returncode = 0
    proc._done.set()

    await stop_process(proc, timeout_seconds=0.05)

    assert proc.terminated == 0
    assert proc.killed == 0
