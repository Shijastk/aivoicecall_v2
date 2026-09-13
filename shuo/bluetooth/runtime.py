from __future__ import annotations

from typing import Protocol, Sequence


class AsyncResource(Protocol):
    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...


class ResourceGroup:
    """
    Owns ordered asynchronous resources.

    Guarantees:
    - start in declaration order
    - rollback already-started resources if later start fails
    - stop in reverse order
    - stop is idempotent
    """

    def __init__(self, resources: Sequence[AsyncResource]):
        self._resources = tuple(resources)
        self._started: list[AsyncResource] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return

        self._started.clear()

        try:
            for resource in self._resources:
                await resource.start()
                self._started.append(resource)

        except BaseException:
            await self._rollback()
            raise

        self._running = True

    async def _rollback(self) -> None:
        for resource in reversed(self._started):
            try:
                await resource.stop()
            except Exception:
                # Preserve the original startup failure.
                pass

        self._started.clear()
        self._running = False

    async def stop(self) -> None:
        if not self._started and not self._running:
            return

        first_error: Exception | None = None

        for resource in reversed(self._started):
            try:
                await resource.stop()
            except Exception as exc:
                if first_error is None:
                    first_error = exc

        self._started.clear()
        self._running = False

        if first_error is not None:
            raise first_error