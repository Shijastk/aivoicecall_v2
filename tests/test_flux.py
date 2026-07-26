"""
Deepgram Flux message handling.

These tests exist because of one live-call failure: audio flowed, Deepgram
answered with 53 messages, and the agent never took a turn. The SDK types
its socket responses as a union containing `typing.Any`, so `construct_type`
returns the decoded JSON *dict* rather than a `ListenV2TurnInfo` -- and the
handler read fields with `getattr`, which is None on a dict. Every turn was
dropped with no error anywhere.

So the contract under test is deliberately shape-agnostic: a TurnInfo must
reach the callbacks whether it arrives as a dict or as a model.
"""

import pytest

from shuo.services.flux import FluxService


def _turn_info(event: str, transcript: str = "", **extra) -> dict:
    """A TurnInfo in the exact shape the SDK hands to the message handler."""
    return {
        "type": "TurnInfo",
        "request_id": "req-1",
        "sequence_id": 3,
        "event": event,
        "turn_index": 0,
        "audio_window_start": 0.0,
        "audio_window_end": 1.2,
        "transcript": transcript,
        "words": [],
        "end_of_turn_confidence": 0.91,
        **extra,
    }


class _Model:
    """A message that arrives as an object, not a dict (future SDK)."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class Recorder:
    """Captures what the conversation loop would have been told."""

    def __init__(self):
        self.ends: list = []
        self.starts: int = 0
        self.interims: list = []

    def service(self) -> FluxService:
        return FluxService(
            on_end_of_turn=self._end,
            on_start_of_turn=self._start,
            on_interim=self._interim,
        )

    async def _end(self, transcript: str) -> None:
        self.ends.append(transcript)

    async def _start(self) -> None:
        self.starts += 1

    async def _interim(self, transcript: str) -> None:
        self.interims.append(transcript)


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


class TestTurnInfoAsDict:
    """The shape the SDK actually delivers."""

    @pytest.mark.asyncio
    async def test_end_of_turn_reaches_the_callback(self, rec):
        await rec.service()._on_message(_turn_info("EndOfTurn", "hello there"))
        assert rec.ends == ["hello there"]

    @pytest.mark.asyncio
    async def test_start_of_turn_reaches_the_callback(self, rec):
        await rec.service()._on_message(_turn_info("StartOfTurn"))
        assert rec.starts == 1

    @pytest.mark.asyncio
    async def test_transcript_is_stripped(self, rec):
        await rec.service()._on_message(_turn_info("EndOfTurn", "  padded  "))
        assert rec.ends == ["padded"]

    @pytest.mark.asyncio
    async def test_update_is_interim_not_a_turn(self, rec):
        """An Update mid-turn must not end the turn -- Flux is still listening."""
        await rec.service()._on_message(_turn_info("Update", "half a sen"))
        assert rec.ends == []
        assert rec.interims == ["half a sen"]

    @pytest.mark.asyncio
    async def test_eager_end_of_turn_does_not_end_the_turn(self, rec):
        """EagerEndOfTurn is a maybe; TurnResumed can still retract it."""
        await rec.service()._on_message(_turn_info("EagerEndOfTurn", "hello"))
        assert rec.ends == []


class TestTurnInfoAsModel:
    """The shape the SDK's type hints promise, in case a version delivers it."""

    @pytest.mark.asyncio
    async def test_end_of_turn_reaches_the_callback(self, rec):
        await rec.service()._on_message(_Model(**_turn_info("EndOfTurn", "hello")))
        assert rec.ends == ["hello"]

    @pytest.mark.asyncio
    async def test_start_of_turn_reaches_the_callback(self, rec):
        await rec.service()._on_message(_Model(**_turn_info("StartOfTurn")))
        assert rec.starts == 1


class TestOtherMessages:
    @pytest.mark.asyncio
    async def test_connected_is_ignored(self, rec):
        await rec.service()._on_message(
            {"type": "Connected", "request_id": "req-1", "sequence_id": 0}
        )
        assert (rec.ends, rec.starts, rec.interims) == ([], 0, [])

    @pytest.mark.asyncio
    async def test_fatal_error_does_not_raise(self, rec):
        await rec.service()._on_message(
            {"type": "FatalError", "description": "model not available"}
        )
        assert rec.ends == []

    @pytest.mark.asyncio
    async def test_an_empty_end_of_turn_still_reports(self, rec):
        """
        The state machine, not this service, decides that an empty
        transcript is not worth a turn. Passing it on keeps that decision
        in the one place tests cover it.
        """
        await rec.service()._on_message(_turn_info("EndOfTurn", ""))
        assert rec.ends == [""]

    @pytest.mark.asyncio
    async def test_a_malformed_message_does_not_kill_the_call(self, rec):
        await rec.service()._on_message({"type": "TurnInfo"})
        await rec.service()._on_message(None)
        assert rec.ends == []
