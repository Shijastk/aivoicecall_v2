from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TelephonySnapshot:
    """
    Hardware-neutral telephony observation.

    Phase 2 deliberately exposes no answer/hangup/dial operations.
    """

    gateway_path: str
    bluetooth_address: str
    transport_state: str
    codec_value: int | None
    call_paths: tuple[str, ...] = ()


class TelephonyObserver(Protocol):
    async def snapshot(self) -> TelephonySnapshot:
        ...


class FakeTelephonyObserver:
    """
    Hardware-free test implementation.

    No D-Bus import or access occurs anywhere in Phase 2.
    """

    def __init__(self, snapshot: TelephonySnapshot):
        self._snapshot = snapshot
        self.calls = 0

    async def snapshot(self) -> TelephonySnapshot:
        self.calls += 1
        return self._snapshot