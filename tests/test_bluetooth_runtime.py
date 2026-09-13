import pytest

from shuo.bluetooth.runtime import ResourceGroup


class FakeResource:
    def __init__(self, name: str, events: list[str], fail_start: bool = False):
        self.name = name
        self.events = events
        self.fail_start = fail_start
        self.started = False
        self.stop_count = 0

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")

        if self.fail_start:
            raise RuntimeError(f"failed:{self.name}")

        self.started = True

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        self.started = False
        self.stop_count += 1


@pytest.mark.asyncio
async def test_resources_start_in_order_and_stop_in_reverse():
    events: list[str] = []

    a = FakeResource("a", events)
    b = FakeResource("b", events)

    group = ResourceGroup([a, b])

    await group.start()
    await group.stop()

    assert events == [
        "start:a",
        "start:b",
        "stop:b",
        "stop:a",
    ]


@pytest.mark.asyncio
async def test_partial_start_failure_rolls_back_started_resources():
    events: list[str] = []

    a = FakeResource("a", events)
    b = FakeResource("b", events, fail_start=True)

    group = ResourceGroup([a, b])

    with pytest.raises(RuntimeError, match="failed:b"):
        await group.start()

    assert events == [
        "start:a",
        "start:b",
        "stop:a",
    ]

    assert group.running is False
    assert a.started is False


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    events: list[str] = []

    resource = FakeResource("a", events)
    group = ResourceGroup([resource])

    await group.start()
    await group.stop()
    await group.stop()

    assert resource.stop_count == 1