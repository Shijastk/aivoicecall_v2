"""
Pure state machine for shuo.

The process_event function is the heart of the system:
    (State, Event) -> (State, List[Action])

With the turn detector handling end-of-turn, this is a thin
conversation controller (~30 lines of logic).

INVARIANT: this module performs no I/O and imports nothing that does.
Every new capability enters as a new Event or Action, never as a side
effect in here. That purity is what makes tests/test_update.py meaningful.
"""

from dataclasses import replace
from typing import List, Tuple

from .types import (
    AppState, Phase,
    Event, StreamStartEvent, StreamStopEvent, MediaEvent,
    FluxStartOfTurnEvent, FluxEndOfTurnEvent, AgentTurnDoneEvent,
    PlaybackMarkEvent, AudioClearedEvent, DtmfEvent,
    Action, FeedFluxAction, StartAgentTurnAction, ResetAgentTurnAction,
)

# Only the caller's audio is ever transcribed. See MediaEvent.track.
INBOUND_TRACK = "inbound"


def process_event(state: AppState, event: Event) -> Tuple[AppState, List[Action]]:
    """
    Pure state machine: (State, Event) -> (State, Actions)

    - MediaEvent           -> feed caller audio to STT
    - FluxEndOfTurnEvent   -> start agent response
    - FluxStartOfTurnEvent -> interrupt (barge-in)
    - AgentTurnDoneEvent   -> back to listening
    """
    if isinstance(event, StreamStartEvent):
        # A reconnect can replay `start` mid-response. Forcing LISTENING
        # without cancelling leaves the Agent still generating and playing
        # into a stream that no longer exists, so the state machine and the
        # Agent silently disagree for the rest of the call. Every other
        # exit from RESPONDING emits this; so must this one.
        actions: List[Action] = (
            [ResetAgentTurnAction()] if state.phase == Phase.RESPONDING else []
        )
        return replace(
            state,
            stream_sid=event.stream_sid,
            # Never let a missing call_id clobber one we already have.
            call_id=event.call_id or state.call_id,
            phase=Phase.LISTENING,
        ), actions

    if isinstance(event, StreamStopEvent):
        stop_actions: List[Action] = []
        if state.phase == Phase.RESPONDING:
            stop_actions.append(ResetAgentTurnAction())
        return state, stop_actions

    if isinstance(event, MediaEvent):
        # Drop anything that is not the caller. If the carrier forks both
        # legs to us, feeding the outbound leg to STT makes the agent
        # transcribe its own speech and reply to itself.
        if event.track != INBOUND_TRACK:
            return state, []
        return state, [FeedFluxAction(audio_bytes=event.audio_bytes)]

    if isinstance(event, FluxEndOfTurnEvent):
        if event.transcript and state.phase == Phase.LISTENING:
            new_state = replace(state, phase=Phase.RESPONDING)
            return new_state, [StartAgentTurnAction(transcript=event.transcript)]
        return state, []

    if isinstance(event, FluxStartOfTurnEvent):
        if state.phase == Phase.RESPONDING:
            return replace(state, phase=Phase.LISTENING), [ResetAgentTurnAction()]
        return state, []

    if isinstance(event, AgentTurnDoneEvent):
        if state.phase == Phase.RESPONDING:
            return replace(state, phase=Phase.LISTENING), []
        return state, []

    # ── Phase 1: observed and traced, but deliberately inert ──────────
    #
    # PlaybackMarkEvent is the carrier's authoritative "the caller has now
    # heard up to here" signal. Phase 5 promotes it to drive turn
    # completion (replacing the guessed AgentTurnDoneEvent) and history
    # truncation (Bug A). Wiring it now means Phase 5 is a state-machine
    # change only, with the transport already proven.
    if isinstance(event, (PlaybackMarkEvent, AudioClearedEvent, DtmfEvent)):
        return state, []

    return state, []
