"""
Type definitions for shuo.

All state, events, and actions are immutable dataclasses.
Minimal -- only what the main loop needs to route decisions.

Conversation history lives in Agent, not in AppState.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Union, List


# =============================================================================
# CALL CONTEXT (carrier-neutral call identity + persona routing)
# =============================================================================

class CallDirection(Enum):
    """Which way the call was placed."""
    OUTBOUND = auto()
    INBOUND = auto()


@dataclass(frozen=True)
class CallContext:
    """
    Everything the conversation needs to know about *this* call that is
    decided before the first audio frame.

    Created by the carrier at answer-time (inbound) or originate-time
    (outbound) and handed to the conversation loop. The persona layer
    (Phase 6.5) reads `persona_id` to select a system prompt, fact block,
    voice and turn-taking profile -- so a Digital Twin role swap is a
    config change, never a code change.
    """
    call_id: str
    direction: CallDirection = CallDirection.OUTBOUND
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    persona_id: str = "default"
    carrier: str = "unknown"

    # The id minted before the carrier was called, and the only identifier
    # that spans a whole attempt: `call_id` above is the carrier's, and on a
    # call nobody answers it never exists. Empty when the media socket was
    # opened without one, in which case the monitor mints its own.
    attempt_id: str = ""


# =============================================================================
# STATE
# =============================================================================

class Phase(Enum):
    """Current phase of the conversation."""
    LISTENING = auto()    # Waiting for user / user speaking
    RESPONDING = auto()   # Agent active (LLM -> TTS -> Playback)


@dataclass(frozen=True)
class AppState:
    """
    Application state -- just routing information.

    Conversation history is owned by Agent, not tracked here.

    `stream_sid` is the media-stream identifier; `call_id` is the *call*
    identifier. They are not the same thing on Vobiz: a `maxRetries`
    reconnect replays a fresh `start` with a NEW streamId on the SAME
    call, so session state must key on `call_id`.
    """
    phase: Phase = Phase.LISTENING
    stream_sid: Optional[str] = None
    call_id: Optional[str] = None


# =============================================================================
# EVENTS (inputs to the system)
# =============================================================================

@dataclass(frozen=True)
class StreamStartEvent:
    """
    Media stream started.

    `call_id` is absent on Twilio (which has only a streamSid at this
    point) and present on Vobiz.
    """
    stream_sid: str
    call_id: Optional[str] = None


@dataclass(frozen=True)
class StreamStopEvent:
    """Media stream ended."""
    pass


@dataclass(frozen=True)
class MediaEvent:
    """
    Audio data received from the carrier.

    `track` identifies which leg the audio came from. We only ever want
    to transcribe the *caller*, so anything that is not the inbound track
    is dropped by the state machine -- this guards against the carrier
    forking both legs to us (Vobiz's REST `<Stream>` path defaults
    audioTrack to "both", unlike the XML path which defaults to
    "inbound"). Without this guard the agent transcribes its own TTS
    output and talks to itself.
    """
    audio_bytes: bytes
    track: str = "inbound"


@dataclass(frozen=True)
class FluxStartOfTurnEvent:
    """Turn detector saw the user start speaking (barge-in)."""
    pass


@dataclass(frozen=True)
class FluxEndOfTurnEvent:
    """Turn detector saw the user finish speaking."""
    transcript: str


@dataclass(frozen=True)
class AgentTurnDoneEvent:
    """Agent finished speaking (playback complete)."""
    pass


@dataclass(frozen=True)
class PlaybackMarkEvent:
    """
    Carrier acknowledged that audio up to a named checkpoint was actually
    played out to the caller (Vobiz `playedStream`, Twilio `mark`).

    Phase 1 plumbs this through to the tracer only -- it is deliberately
    a no-op in the state machine. Phase 5 promotes it to the authoritative
    turn-completion signal, replacing the currently-guessed
    AgentTurnDoneEvent, and uses it to truncate history to what the caller
    actually heard (Bug A).
    """
    name: str


@dataclass(frozen=True)
class AudioClearedEvent:
    """
    Carrier acknowledged a buffer flush (Vobiz `clearedAudio`).

    Phase 1: traced only, so we can measure real barge-in flush latency.
    """
    pass


@dataclass(frozen=True)
class DtmfEvent:
    """Caller pressed a key."""
    digit: str


Event = Union[
    StreamStartEvent, StreamStopEvent, MediaEvent,
    FluxStartOfTurnEvent, FluxEndOfTurnEvent,
    AgentTurnDoneEvent,
    PlaybackMarkEvent, AudioClearedEvent, DtmfEvent,
]


# =============================================================================
# ACTIONS (outputs from the system)
# =============================================================================

@dataclass(frozen=True)
class FeedFluxAction:
    """Send audio to the STT / turn-detection stage."""
    audio_bytes: bytes


@dataclass(frozen=True)
class StartAgentTurnAction:
    """Start agent response pipeline."""
    transcript: str


@dataclass(frozen=True)
class ResetAgentTurnAction:
    """Cancel agent response and clear the carrier's audio buffer."""
    pass


Action = Union[
    FeedFluxAction,
    StartAgentTurnAction,
    ResetAgentTurnAction,
]
