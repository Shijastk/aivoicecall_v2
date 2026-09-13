from __future__ import annotations

import platform
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Protocol

from .codec import AudioFormat, BLUETOOTH_PCM_FORMAT


class PipeWireSelectionError(RuntimeError):
    pass


class UnsupportedPlatformError(RuntimeError):
    pass


class StreamDirection(str, Enum):
    DOWNLINK = "downlink"
    UPLINK = "uplink"


@dataclass(frozen=True)
class PipeWireTarget:
    """
    Sanitized description of one candidate PipeWire Bluetooth call stream.

    Numeric PipeWire object IDs are deliberately excluded because Phase 1
    proved they are transient.
    """

    node_name: str
    factory_name: str
    media_class: str
    bluetooth_address: str
    bluetooth_profile: str
    bluetooth_codec: str
    audio_format: AudioFormat


class PipeWireDiscovery(Protocol):
    async def list_targets(self) -> list[PipeWireTarget]:
        ...


EXPECTED_PROFILE = "headset-audio-gateway"
EXPECTED_CODEC = "msbc"

EXPECTED_FACTORY = {
    StreamDirection.DOWNLINK: "api.bluez5.sco.source",
    StreamDirection.UPLINK: "api.bluez5.sco.sink",
}

EXPECTED_MEDIA_CLASS = {
    StreamDirection.DOWNLINK: "Stream/Output/Audio",
    StreamDirection.UPLINK: "Stream/Input/Audio",
}


def require_pipewire_platform(system_name: Optional[str] = None) -> None:
    """
    The future live PipeWire adapter is Linux-only.

    Importing this module is cross-platform and side-effect free.
    The explicit runtime check is performed only when requested.
    """

    system_name = system_name or platform.system()

    if system_name != "Linux":
        raise UnsupportedPlatformError(
            f"Bluetooth PipeWire adapter requires Linux; got {system_name}"
        )


def candidate_matches(
    target: PipeWireTarget,
    *,
    direction: StreamDirection,
    address: Optional[str] = None,
) -> bool:
    if target.factory_name != EXPECTED_FACTORY[direction]:
        return False

    if target.media_class != EXPECTED_MEDIA_CLASS[direction]:
        return False

    if target.bluetooth_profile != EXPECTED_PROFILE:
        return False

    if target.bluetooth_codec.lower() != EXPECTED_CODEC:
        return False

    if target.audio_format != BLUETOOTH_PCM_FORMAT:
        return False

    if address is not None:
        if target.bluetooth_address.casefold() != address.casefold():
            return False

    return True


def select_target(
    candidates: Iterable[PipeWireTarget],
    *,
    direction: StreamDirection,
    address: Optional[str] = None,
) -> PipeWireTarget:
    """
    Select by validated capabilities, not transient object ID or phone model.

    If selection is ambiguous we fail closed instead of routing to a default
    microphone/speaker.
    """

    matches = [
        candidate
        for candidate in candidates
        if candidate_matches(
            candidate,
            direction=direction,
            address=address,
        )
    ]

    if not matches:
        suffix = f" for {address}" if address else ""
        raise PipeWireSelectionError(
            f"no compatible Bluetooth {direction.value} target{suffix}"
        )

    if len(matches) > 1:
        names = ", ".join(sorted(item.node_name for item in matches))
        raise PipeWireSelectionError(
            f"ambiguous Bluetooth {direction.value} targets: {names}; "
            "explicit device selection is required"
        )

    return matches[0]