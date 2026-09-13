from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence


class ProcessError(RuntimeError):
    pass


class ProcessExitedError(ProcessError):
    pass


@dataclass(frozen=True)
class CompletedCommand:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class RunningProcess(Protocol):
    stdin: object | None
    stdout: object | None
    stderr: object | None
    returncode: int | None

    async def wait(self) -> int:
        ...

    def terminate(self) -> None:
        ...

    def kill(self) -> None:
        ...


class ProcessRunner(Protocol):
    async def run(self, argv: Sequence[str]) -> CompletedCommand:
        ...

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: bool,
        stdout: bool,
        stderr: bool,
    ) -> RunningProcess:
        ...


class AsyncioProcessRunner:
    """
    Real subprocess boundary for Phase 3.

    Importing this module has no side effects. A process is created only when
    run()/spawn() is explicitly called.
    """

    async def run(self, argv: Sequence[str]) -> CompletedCommand:
        if not argv:
            raise ValueError("argv must not be empty")

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        return CompletedCommand(
            argv=tuple(argv),
            returncode=int(proc.returncode or 0),
            stdout=stdout,
            stderr=stderr,
        )

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: bool,
        stdout: bool,
        stderr: bool,
    ) -> asyncio.subprocess.Process:
        if not argv:
            raise ValueError("argv must not be empty")

        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE if stderr else asyncio.subprocess.DEVNULL,
        )


async def stop_process(
    proc: RunningProcess,
    *,
    timeout_seconds: float,
) -> None:
    """
    Bounded, idempotent process stop.

    terminate -> bounded wait -> kill -> bounded wait.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    if proc.returncode is not None:
        return

    proc.terminate()

    try:
        await asyncio.wait_for(proc.wait(), timeout_seconds)
        return
    except asyncio.TimeoutError:
        pass

    if proc.returncode is None:
        proc.kill()

    try:
        await asyncio.wait_for(proc.wait(), timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise ProcessError("child process did not exit after terminate/kill") from exc
