import asyncio

import pytest

from shuo.bluetooth.conversation import run_bluetooth_conversation


class BlockingSession:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.wait = asyncio.Event()

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def read(self):
        await self.wait.wait()
        raise AssertionError("unreachable")

    async def write(self, audio):
        pass

    async def clear(self):
        pass


class FakeFlux:
    def __init__(self, on_eot, on_sot, on_interim):
        self.on_eot = on_eot
        self.on_sot = on_sot
        self.on_interim = on_interim

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, audio):
        pass


class FakeAgent:
    async def start_turn(self, transcript):
        pass

    async def cancel_turn(self):
        pass

    async def cleanup(self):
        pass


@pytest.mark.asyncio
async def test_orchestrator_accepts_async_agent_factory():
    session = BlockingSession()
    built = asyncio.Event()

    def flux_factory(eot, sot, interim):
        return FakeFlux(eot, sot, interim)

    async def agent_factory(outbound, done):
        await asyncio.sleep(0)
        built.set()
        return FakeAgent()

    task = asyncio.create_task(
        run_bluetooth_conversation(
            session,
            flux_factory=flux_factory,
            agent_factory=agent_factory,
        )
    )

    await built.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.started == 1
    assert session.stopped == 1
