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

    def service(self, *, eager_eot_threshold=None) -> FluxService:
        return FluxService(
            on_end_of_turn=self._end,
            on_start_of_turn=self._start,
            on_interim=self._interim,
            eager_eot_threshold=eager_eot_threshold,
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
    async def test_eager_end_of_turn_is_ignored_when_feature_is_off(self, rec):
        """Feature-off must preserve the historical ignore behavior exactly."""
        service = rec.service()
        await service._on_message(_turn_info("EagerEndOfTurn", "hello"))
        await service._on_message(_turn_info("TurnResumed", "hello again"))
        assert rec.ends == []
        assert service._eager_started_at is None
        assert service._eager_transcript is None

    @pytest.mark.asyncio
    async def test_enabled_eager_end_of_turn_does_not_end_the_turn(self, rec):
        """EagerEndOfTurn is a maybe; TurnResumed can still retract it."""
        service = rec.service(eager_eot_threshold=0.4)
        await service._on_message(_turn_info("EagerEndOfTurn", "hello"))
        assert rec.ends == []
        assert service._eager_started_at is not None

    @pytest.mark.asyncio
    async def test_eager_then_final_commits_only_the_final_turn(self, rec):
        service = rec.service(eager_eot_threshold=0.4)
        await service._on_message(_turn_info("EagerEndOfTurn", "hello"))
        await service._on_message(_turn_info("EndOfTurn", "hello"))

        assert rec.ends == ["hello"]
        assert service._eager_started_at is None
        assert service._eager_transcript is None

    @pytest.mark.asyncio
    async def test_turn_resumed_clears_eager_candidate_without_committing(self, rec):
        service = rec.service(eager_eot_threshold=0.4)
        await service._on_message(_turn_info("EagerEndOfTurn", "hello"))
        await service._on_message(_turn_info("TurnResumed", "hello again"))

        assert rec.ends == []
        assert service._eager_started_at is None
        assert service._eager_transcript is None


class TestEagerConfiguration:
    def test_measurement_threshold_accepts_documented_safe_range(self, rec):
        assert rec.service(eager_eot_threshold=0.3)._eager_eot_threshold == 0.3
        assert rec.service(eager_eot_threshold=0.7)._eager_eot_threshold == 0.7

    @pytest.mark.parametrize("value", [0.29, 0.71])
    def test_measurement_threshold_rejects_values_that_change_final_eot_assumption(
        self, rec, value
    ):
        with pytest.raises(ValueError, match="between 0.3 and 0.7"):
            rec.service(eager_eot_threshold=value)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("model_shape", [False, True])
async def test_opt_in_empty_update_reaches_shadow_invalidation_without_final(rec, model_shape):
    service = FluxService(
        on_end_of_turn=rec._end, on_start_of_turn=rec._start,
        on_interim=rec._interim, include_empty_interims=True,
    )
    for text in ("early full transcript", "early full transcript", ""):
        message = _turn_info("Update", text)
        await service._on_message(_Model(**message) if model_shape else message)
    assert rec.interims == ["early full transcript", "early full transcript", ""]
    assert rec.ends == []
    assert rec.starts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostics,callback,empty,include_empty", [
    (False, True, False, False),
    (True, False, False, False),
    (True, True, False, False),
    (True, True, True, False),
    (True, True, True, True),
])
async def test_content_free_update_receipt_and_callback_diagnostics(
    rec, caplog, diagnostics, callback, empty, include_empty,
):
    caplog.set_level("INFO")
    service = FluxService(
        rec._end, rec._start, rec._interim if callback else None,
        diagnose_updates=diagnostics, include_empty_interims=include_empty,
    )
    text = "" if empty else "synthetic private phrase"
    await service._on_message(_turn_info("Update", text))
    await service._on_message(_Model(**_turn_info("Update", text)))
    await service._on_message(_turn_info("EndOfTurn", text))
    forwarded = callback and (not empty or include_empty)
    assert rec.interims == ([text, text] if forwarded else [])
    assert rec.ends == [text]
    assert "synthetic" not in caplog.text
    if diagnostics:
        assert f"event=Update update_count=2 callback_present={callback}" in caplog.text
        assert ("event=Update_callback_returned" in caplog.text) is forwarded
        assert ("event=Update_empty_filtered" in caplog.text) is (callback and empty and not include_empty)
        assert "event=EndOfTurn update_count=2" in caplog.text
    else:
        assert "BTShadowFlux" not in caplog.text
