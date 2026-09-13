from __future__ import annotations

from dataclasses import dataclass

from .pipewire import PipeWireDiscovery
from .pipewire_live import (
    PwCatCaptureEndpoint,
    PwCatConfig,
    PwCatPlaybackEndpoint,
    SelectedDuplexTargets,
    discover_duplex_targets,
)
from .process import ProcessRunner
from .routes import AiOnlyRouteIsolation
from .runtime import ResourceGroup


@dataclass(frozen=True)
class Phase3AiOnlySessionParts:
    targets: SelectedDuplexTargets
    routes: AiOnlyRouteIsolation
    capture: PwCatCaptureEndpoint
    playback: PwCatPlaybackEndpoint


class Phase3AiOnlySession:
    """Phase-3-only Bluetooth media session with physical I/O isolation.

    This is intentionally not wired into `main.py`, `shuo/server.py`, the carrier
    path, or SHUO conversation orchestration. Phase 4 owns that integration.
    """

    def __init__(self, parts: Phase3AiOnlySessionParts) -> None:
        self.parts = parts
        self._resources = ResourceGroup(
            (
                parts.routes,
                parts.capture,
                parts.playback,
            )
        )

    @property
    def running(self) -> bool:
        return self._resources.running

    async def start(self) -> None:
        await self._resources.start()

    async def read(self) -> bytes:
        return await self.parts.capture.read()

    async def write(self, audio: bytes) -> None:
        await self.parts.playback.write(audio)

    async def clear(self) -> None:
        await self.parts.playback.clear()

    async def stop(self) -> None:
        await self._resources.stop()


async def build_phase3_ai_only_session(
    *,
    discovery: PipeWireDiscovery,
    runner: ProcessRunner,
    bluetooth_address: str | None,
    config: PwCatConfig,
    system_name: str | None = None,
) -> Phase3AiOnlySession:
    """Discover fresh targets and construct, but do not start, the session."""

    targets = await discover_duplex_targets(
        discovery,
        bluetooth_address=bluetooth_address,
    )

    routes = AiOnlyRouteIsolation(
        downlink=targets.downlink,
        uplink=targets.uplink,
        runner=runner,
    )
    capture = PwCatCaptureEndpoint(
        targets.downlink,
        runner,
        config,
        system_name=system_name,
    )
    playback = PwCatPlaybackEndpoint(
        targets.uplink,
        runner,
        config,
        system_name=system_name,
    )

    return Phase3AiOnlySession(
        Phase3AiOnlySessionParts(
            targets=targets,
            routes=routes,
            capture=capture,
            playback=playback,
        )
    )
