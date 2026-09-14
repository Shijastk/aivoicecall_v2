from __future__ import annotations

from typing import Protocol


class OutboundMediaSession(Protocol):
    """Minimal SHUO -> caller media surface.

    CarrierSession already satisfies this protocol structurally. Bluetooth
    implements the same three operations without importing carrier code into
    the Bluetooth boundary.

    Important: ``checkpoint()`` means "dispatch boundary requested". Whether
    there is an authoritative playback acknowledgement is transport-specific
    and MUST be decided by the conversation orchestrator.
    """

    async def play_audio(self, payload_b64: str) -> None:
        """Dispatch one base64-encoded SHUO mu-law/8k audio chunk."""

    async def clear_audio(self) -> None:
        """Discard transport-owned audio that is still retractable."""

    async def checkpoint(self, name: str) -> None:
        """Place a named dispatch checkpoint if the transport supports it."""
