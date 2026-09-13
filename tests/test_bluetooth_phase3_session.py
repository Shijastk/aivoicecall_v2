import pytest

from shuo.bluetooth.phase3_session import Phase3AiOnlySession, Phase3AiOnlySessionParts
from shuo.bluetooth.pipewire_live import SelectedDuplexTargets


class FakeResource:
    def __init__(self, name, events, *, fail_start=False):
        self.name = name
        self.events = events
        self.fail_start = fail_start

    async def start(self):
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError(self.name)

    async def stop(self):
        self.events.append(f"stop:{self.name}")


class FakeCapture(FakeResource):
    async def read(self):
        return b"capture"


class FakePlayback(FakeResource):
    def __init__(self, name, events, *, fail_start=False):
        super().__init__(name, events, fail_start=fail_start)
        self.writes = []
        self.clears = 0

    async def write(self, audio):
        self.writes.append(audio)

    async def clear(self):
        self.clears += 1


@pytest.mark.asyncio
async def test_session_starts_routes_before_media_and_restores_routes_last():
    events = []
    routes = FakeResource("routes", events)
    capture = FakeCapture("capture", events)
    playback = FakePlayback("playback", events)

    session = Phase3AiOnlySession(
        Phase3AiOnlySessionParts(
            targets=SelectedDuplexTargets(downlink=object(), uplink=object()),
            routes=routes,
            capture=capture,
            playback=playback,
        )
    )

    await session.start()
    assert await session.read() == b"capture"
    await session.write(b"tts")
    await session.clear()
    await session.stop()

    assert events == [
        "start:routes",
        "start:capture",
        "start:playback",
        "stop:playback",
        "stop:capture",
        "stop:routes",
    ]
    assert playback.writes == [b"tts"]
    assert playback.clears == 1


@pytest.mark.asyncio
async def test_session_rolls_back_routes_when_capture_start_fails():
    events = []
    routes = FakeResource("routes", events)
    capture = FakeCapture("capture", events, fail_start=True)
    playback = FakePlayback("playback", events)

    session = Phase3AiOnlySession(
        Phase3AiOnlySessionParts(
            targets=SelectedDuplexTargets(downlink=object(), uplink=object()),
            routes=routes,
            capture=capture,
            playback=playback,
        )
    )

    with pytest.raises(RuntimeError):
        await session.start()

    assert events == [
        "start:routes",
        "start:capture",
        "stop:routes",
    ]
