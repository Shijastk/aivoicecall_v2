"""
State machine transitions added in Phase 1 (transport swap).

tests/test_update.py is deliberately left untouched -- it passing
unchanged is the proof that the Phase 1 type extensions were additive.
Everything new lives here.
"""

import pytest

from shuo.types import (
    AppState, Phase,
    StreamStartEvent, StreamStopEvent, MediaEvent,
    FluxStartOfTurnEvent, FluxEndOfTurnEvent, AgentTurnDoneEvent,
    PlaybackMarkEvent, AudioClearedEvent, DtmfEvent,
    FeedFluxAction, StartAgentTurnAction, ResetAgentTurnAction,
)
from shuo.state import process_event


@pytest.fixture
def listening_state() -> AppState:
    return AppState(phase=Phase.LISTENING, stream_sid="stream-1", call_id="call-1")


@pytest.fixture
def responding_state() -> AppState:
    return AppState(phase=Phase.RESPONDING, stream_sid="stream-1", call_id="call-1")


# =============================================================================
# CALL ID
# =============================================================================

class TestCallId:
    def test_stream_start_records_call_id(self):
        state, actions = process_event(
            AppState(), StreamStartEvent(stream_sid="s1", call_id="c1")
        )
        assert state.stream_sid == "s1"
        assert state.call_id == "c1"
        assert actions == []

    def test_stream_start_without_call_id_is_allowed(self):
        """Twilio has no callSid at this point in some flows."""
        state, _ = process_event(AppState(), StreamStartEvent(stream_sid="s1"))
        assert state.stream_sid == "s1"
        assert state.call_id is None

    def test_reconnect_updates_stream_id_but_keeps_call_id(self):
        """
        A maxRetries reconnect replays `start` with a NEW streamId on the
        SAME call. Session state keys on call_id, so it must survive.
        """
        state = AppState(phase=Phase.RESPONDING, stream_sid="s1", call_id="c1")
        state, _ = process_event(state, StreamStartEvent(stream_sid="s2", call_id="c1"))
        assert state.stream_sid == "s2"
        assert state.call_id == "c1"
        assert state.phase == Phase.LISTENING

    def test_reconnect_mid_response_cancels_the_orphaned_turn(self):
        """
        Forcing LISTENING without cancelling leaves the Agent generating
        and playing into a stream that no longer exists, so the state
        machine and the Agent disagree for the rest of the call. Every
        other exit from RESPONDING emits ResetAgentTurnAction; so must
        this one.
        """
        state = AppState(phase=Phase.RESPONDING, stream_sid="s1", call_id="c1")
        _, actions = process_event(state, StreamStartEvent(stream_sid="s2", call_id="c1"))
        assert [type(a) for a in actions] == [ResetAgentTurnAction]

    def test_first_start_does_not_cancel_anything(self):
        """No turn is running at the top of a call."""
        _, actions = process_event(AppState(), StreamStartEvent(stream_sid="s1", call_id="c1"))
        assert actions == []

    def test_missing_call_id_on_reconnect_does_not_clobber(self):
        state = AppState(stream_sid="s1", call_id="c1")
        state, _ = process_event(state, StreamStartEvent(stream_sid="s2"))
        assert state.call_id == "c1"


# =============================================================================
# TRACK FILTERING
# =============================================================================

class TestTrackFiltering:
    def test_inbound_audio_is_fed_to_stt(self, listening_state):
        _, actions = process_event(
            listening_state, MediaEvent(audio_bytes=b"\xff" * 160, track="inbound")
        )
        assert len(actions) == 1
        assert isinstance(actions[0], FeedFluxAction)

    def test_track_defaults_to_inbound(self, listening_state):
        """Carriers that omit `track` are assumed to send caller audio."""
        _, actions = process_event(listening_state, MediaEvent(audio_bytes=b"\xff" * 160))
        assert len(actions) == 1
        assert isinstance(actions[0], FeedFluxAction)

    def test_outbound_audio_is_dropped(self, listening_state):
        """
        Feeding our own TTS back into STT makes the agent transcribe
        itself and reply to itself. Vobiz's REST <Stream> path defaults
        audioTrack to "both", so this is a live risk, not a hypothetical.
        """
        state, actions = process_event(
            listening_state, MediaEvent(audio_bytes=b"\xff" * 160, track="outbound")
        )
        assert actions == []
        assert state == listening_state

    def test_outbound_audio_dropped_while_responding(self, responding_state):
        """The agent's own audio arrives precisely while it is speaking."""
        _, actions = process_event(
            responding_state, MediaEvent(audio_bytes=b"\xff" * 160, track="outbound")
        )
        assert actions == []


# =============================================================================
# PHASE 1 TRACE-ONLY EVENTS
# =============================================================================

class TestInertEvents:
    """
    These are plumbed through to the tracer but must not touch state yet.
    Phase 5 promotes PlaybackMarkEvent to drive turn completion.
    """

    @pytest.mark.parametrize("event", [
        PlaybackMarkEvent(name="turn-1"),
        AudioClearedEvent(),
        DtmfEvent(digit="5"),
    ])
    def test_inert_while_listening(self, listening_state, event):
        state, actions = process_event(listening_state, event)
        assert state == listening_state
        assert actions == []

    @pytest.mark.parametrize("event", [
        PlaybackMarkEvent(name="turn-1"),
        AudioClearedEvent(),
        DtmfEvent(digit="5"),
    ])
    def test_inert_while_responding(self, responding_state, event):
        state, actions = process_event(responding_state, event)
        assert state == responding_state
        assert actions == []

    def test_playback_mark_does_not_end_the_turn(self, responding_state):
        """
        Phase 1 still ends turns on the guessed AgentTurnDoneEvent.
        If this test starts failing, Phase 5 has landed -- update it
        rather than deleting it.
        """
        state, _ = process_event(responding_state, PlaybackMarkEvent(name="turn-1"))
        assert state.phase == Phase.RESPONDING


# =============================================================================
# REGRESSION: CORE LOOP UNCHANGED
# =============================================================================

class TestCoreLoopUnchanged:
    def test_full_turn_cycle(self):
        state = AppState()
        state, _ = process_event(state, StreamStartEvent(stream_sid="s1", call_id="c1"))
        assert state.phase == Phase.LISTENING

        state, actions = process_event(state, FluxEndOfTurnEvent(transcript="hello"))
        assert state.phase == Phase.RESPONDING
        assert isinstance(actions[0], StartAgentTurnAction)

        state, actions = process_event(state, AgentTurnDoneEvent())
        assert state.phase == Phase.LISTENING
        assert actions == []

    def test_barge_in_still_resets(self, responding_state):
        state, actions = process_event(responding_state, FluxStartOfTurnEvent())
        assert state.phase == Phase.LISTENING
        assert isinstance(actions[0], ResetAgentTurnAction)

    def test_stop_while_responding_resets(self, responding_state):
        _, actions = process_event(responding_state, StreamStopEvent())
        assert isinstance(actions[0], ResetAgentTurnAction)

    def test_purity_state_is_not_mutated(self, listening_state):
        """process_event must never mutate its input."""
        before = AppState(**listening_state.__dict__)
        process_event(listening_state, FluxEndOfTurnEvent(transcript="hi"))
        assert listening_state == before
