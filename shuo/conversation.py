"""
The main event loop for shuo.

This is the explicit, readable loop that drives the entire system:

    while connected:
        event = receive()                               # I/O (from queue)
        state, actions = process_event(state, event)    # PURE
        for action in actions:
            dispatch(action)                            # I/O

Events come from:
- The carrier WebSocket (audio packets, playback acks, DTMF)
- The turn detector (turn events)
- The agent (playback dispatched) gated on the carrier's ack -- see
  _TurnCompletion

The loop is carrier-agnostic: it holds a CarrierSession and never
mentions Twilio or Vobiz.
"""

import json
import asyncio
from typing import Callable, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .types import (
    AppState, CallContext, Phase,
    Event, StreamStartEvent, StreamStopEvent,
    FluxStartOfTurnEvent, FluxEndOfTurnEvent, AgentTurnDoneEvent,
    PlaybackMarkEvent, AudioClearedEvent, DtmfEvent,
    FeedFluxAction, StartAgentTurnAction, ResetAgentTurnAction,
)
from .state import process_event
from . import config as cfg
from .call_monitor import MONITOR, LISTENING, SPEAKING
from .carrier import Carrier, get_carrier
from .runtime_config import load_call_settings
from .services.flux import FluxService
from .services.tts_pool import TTSPool
from .agent import Agent
from .tracer import Tracer
from .log import Logger, get_logger, log_level

logger = get_logger("shuo.conversation")


class _TurnCompletion:
    """
    Decides *when* a turn is over -- the second half of Bug B.

    The player's completion callback fires when the last frame was
    **dispatched**. The caller has not heard it yet: the carrier still
    holds the pre-roll and the handset its de-jitter buffer. The carrier's
    `playedStream` is the signal that they have, and it is the one that
    should end the turn.

    rules.md V18 is what makes this a gate and not a wait: `playedStream`
    is conditional and **may never arrive** -- a `clearAudio`, a barge-in
    or a disconnect voids every pending checkpoint permanently. So:

        ack matching the armed checkpoint  -> turn over (authoritative)
        grace window expires               -> turn over anyway (V18)
        checkpoint voided                  -> wait dropped, turn already
                                              over by another route

    Nothing blocks. The longest a missing ack can cost is the grace
    window, and `SHUO_CHECKPOINT_GRACE_MS=0` reverts to the old guess.

    Exactly one AgentTurnDoneEvent per armed turn, by construction: every
    exit clears `_name` before emitting, and `_expire` re-checks `_name`
    after its sleep, so a superseded timer resolves to nothing.

    Deliberately *not* in the state machine: a grace window is a timer,
    and CLAUDE.md rule 1 keeps `process_event` pure. The machine still
    sees one AgentTurnDoneEvent per turn and is unchanged.
    """

    def __init__(
        self,
        queue: "asyncio.Queue[Event]",
        grace_seconds: float,
        on_timeout: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._queue = queue
        self._grace = grace_seconds
        self._on_timeout = on_timeout
        self._name: Optional[str] = None
        self._timer: Optional[asyncio.Task] = None
        self._warned_missing_ack = False

    @property
    def pending(self) -> Optional[str]:
        """Checkpoint currently being waited on, if any."""
        return self._name

    def arm(self, checkpoint: Optional[str]) -> None:
        """
        The player dispatched its last frame. Wait for the carrier.

        Called straight from the Agent's on_done, so it must not block and
        must not raise.
        """
        self._cancel_timer()

        if not checkpoint or self._grace <= 0:
            # No checkpoint to be acked, or the gate is switched off.
            # Fall back to the dispatch-time guess.
            self._name = None
            self._finish()
            return

        self._name = checkpoint
        self._timer = asyncio.create_task(self._expire(checkpoint))

    def ack(self, name: str) -> bool:
        """
        A `playedStream` arrived. True if it completed the pending turn.

        A name we are not waiting on is ignored on purpose: a late ack for
        an abandoned turn must never end the turn now running.
        """
        if self._name is None or name != self._name:
            return False

        self._cancel_timer()
        self._name = None
        self._finish()
        return True

    def void(self, reason: str) -> None:
        """
        Drop the pending wait **without** ending a turn.

        For every route out of RESPONDING that is not playback finishing:
        a barge-in, a reconnect, a new turn, the call ending. The state
        machine has already left RESPONDING by the time we get here, so
        emitting would either be a no-op or -- if a new turn has started
        -- would cut that new turn off. rules.md V18: those events void
        the checkpoint permanently, so there is nothing left to wait for.
        """
        if self._name is None and self._timer is None:
            return

        name, self._name = self._name, None
        self._cancel_timer()
        if name:
            logger.debug(f"Checkpoint {name} voided ({reason})")

    async def close(self) -> None:
        """Tear down at call end. Never emits."""
        self._name = None
        timer, self._timer = self._timer, None
        if timer and not timer.done():
            timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass

    # ── Internals ───────────────────────────────────────────────────

    def _finish(self) -> None:
        self._queue.put_nowait(AgentTurnDoneEvent())

    async def _expire(self, name: str) -> None:
        """The V18 escape hatch: end the turn on a timer, not on faith."""
        await asyncio.sleep(self._grace)

        # Re-check under the same tick that woke us: arm/ack/void all
        # clear `_name` synchronously, so a stale timer sees a mismatch.
        if self._name != name:
            return

        self._name = None
        self._timer = None

        if self._on_timeout:
            self._on_timeout(name)

        if not self._warned_missing_ack:
            self._warned_missing_ack = True
            # First one is a warning because it answers a live question:
            # whether this carrier sends `playedStream` at all. After that
            # it is just noise on every turn.
            logger.warning(
                f"No playedStream for {name} within "
                f"{int(self._grace * 1000)}ms -- ending the turn on the "
                f"dispatch-time guess (rules.md V18)"
            )
        else:
            logger.debug(f"No playedStream for {name}; ended on the grace window")

        self._finish()

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer and not timer.done():
            timer.cancel()


async def run_conversation(
    websocket: WebSocket,
    context: CallContext,
    carrier: Optional[Carrier] = None,
) -> None:
    """
    Main event loop for a single call.

    1. Create shared event queue and a carrier session
    2. Create the turn detector (always-on STT + turn detection)
    3. Start the carrier reader
    4. On StreamStart, create Agent
    5. Process events through the pure state machine
    6. Dispatch actions inline
    """
    carrier = carrier or get_carrier()
    session = carrier.new_session(websocket, context)

    # Per-frame media logging is 50 lines a second, so it rides on
    # SHUO_LOG_LEVEL=DEBUG rather than being on by default.
    event_log = Logger(verbose=log_level() <= 10)  # logging.DEBUG
    event_queue: asyncio.Queue[Event] = asyncio.Queue()
    tracer = Tracer()

    agent: Optional[Agent] = None
    # Built at StreamStart rather than here. The pool is scoped to one voice
    # (ElevenLabs binds it at connect time) and the voice is not known until
    # the config store has been read, which happens when the stream starts.
    tts_pool: Optional[TTSPool] = None
    recording_started = False
    background_tasks: set = set()

    # Turn completion is the carrier's `playedStream`, bounded by a grace
    # window because rules.md V18 says it may never come.
    completion = _TurnCompletion(
        queue=event_queue,
        grace_seconds=cfg.checkpoint_grace_seconds(),
        on_timeout=lambda name: _trace_playback_timeout(tracer, name),
    )

    # The operator's live view of this call (W3). An observer at the dispatch
    # boundary, exactly like `tracer` above: it takes no part in the loop, the
    # state machine never learns it exists, and every method on it is a
    # non-blocking append that cannot raise. See `call_monitor.py`.
    recorder = MONITOR.begin(
        persona=context.persona_id,
        direction=context.direction.name.lower(),
    )

    logger.info(
        f"Call starting  carrier={carrier.name}  direction={context.direction.name.lower()}  "
        f"persona={context.persona_id}  call_id={context.call_id}"
    )

    # ── Turn detector callbacks (push events to queue) ──────────────

    async def on_flux_end_of_turn(transcript: str) -> None:
        await event_queue.put(FluxEndOfTurnEvent(transcript=transcript))

    async def on_flux_start_of_turn() -> None:
        await event_queue.put(FluxStartOfTurnEvent())

    async def on_flux_interim(transcript: str) -> None:
        """
        In-progress caller text, for the panel only.

        Deliberately **not** an event. Interim text is not a fact the
        conversation may act on -- acting on it is how you answer half a
        question -- so it never reaches `process_event`, never becomes an
        Action, and the state machine stays exactly as it was. It is a single
        string assignment on the monitor's summary, replaced in place, which
        is also why it cannot flood the ring buffer.
        """
        recorder.caller_partial(transcript)

    flux = FluxService(
        on_end_of_turn=on_flux_end_of_turn,
        on_start_of_turn=on_flux_start_of_turn,
        on_interim=on_flux_interim,
    )

    # ── Carrier WebSocket reader ────────────────────────────────────

    frames_in = 0

    async def read_carrier() -> None:
        """Background task to read from the carrier and push to the queue."""
        nonlocal frames_in
        try:
            while True:
                raw = await websocket.receive_text()
                frames_in += 1
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON frame from carrier")
                    continue

                # One frame can yield several events: a `start` also
                # drains any media that arrived ahead of it.
                #
                # A frame we cannot parse is one lost 20ms of audio, not a
                # reason to hang up on the caller. Only socket-level
                # failures (the outer handlers) end the call.
                try:
                    events = session.parse_message(data)
                except Exception as e:
                    logger.warning(f"Skipping unparseable carrier frame: {e}")
                    continue

                stop = False
                for event in events:
                    await event_queue.put(event)
                    if isinstance(event, StreamStopEvent):
                        stop = True
                if stop:
                    break

        except WebSocketDisconnect as e:
            # 1006 is an abnormal mid-call media drop and is otherwise
            # invisible -- always log the code. The frame count goes with
            # it: a call that ends having received one frame got the
            # `start` and no audio at all, which is a carrier-side
            # problem, not a pipeline one.
            code = getattr(e, "code", None)
            logger.info(f"Carrier sent {frames_in} WebSocket frame(s) this call")
            if code == 1006:
                logger.warning("Carrier WebSocket closed abnormally (1006) -- mid-call media drop")
            else:
                logger.info(f"Carrier WebSocket closed (code {code})")
            await event_queue.put(StreamStopEvent())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            event_log.error("Carrier reader", e)
            await event_queue.put(StreamStopEvent())

    # ── Initialize ──────────────────────────────────────────────────

    state = AppState(call_id=context.call_id)
    reader_task = asyncio.create_task(read_carrier())

    # The operator's configuration, read exactly once per call.
    #
    # Position and synchronicity are both deliberate:
    #
    #   after the reader task   nothing may delay the point at which we start
    #                           draining the carrier's socket.
    #   before the loop         it has to beat the TTS pool built at
    #                           `StreamStart`. ElevenLabs binds the voice into
    #                           the connection at connect time, so a pool
    #                           warmed ahead of this read serves turn 1 in the
    #                           environment's voice while the panel shows the
    #                           operator's pick.
    #   synchronous             `await` here would be a *cancellation point*
    #                           between accepting the socket and building the
    #                           agent. A call that ends in that window --
    #                           which a carrier that dials and hangs up does,
    #                           and which the transport tests do deliberately
    #                           -- would tear down with `settings` unassigned.
    #
    # Blocking is affordable because decision 33 made it affordable: the read
    # is one file open and one `json.loads`, no driver and no lock, which is
    # why config is a JSON document and not SQLite. It costs tens of
    # microseconds once per call, before any audio is flowing, so it is
    # nowhere near the 20ms player deadline -- that deadline only binds once
    # playback has started. `load_call_settings` never raises.
    settings = load_call_settings()
    logger.info(f"Config  {settings.describe()}")
    # The same provenance line the log gets, in the panel. It is the answer
    # to "did my save apply?" -- asked *while the call is running*, which is
    # the only time it is cheap to answer.
    recorder.configured(settings.describe())

    ended_reason = "the call ended"

    try:
        while True:
            # ─── RECEIVE ────────────────────────────────────────────
            event = await event_queue.get()

            event_log.event(event)

            # Initialize services on stream start
            if isinstance(event, StreamStartEvent):
                # The carrier's own call id, which is only knowable now. Not
                # the `request_uuid` that `originate` returned -- see the
                # `/hangup` handler in server.py -- and hanging up from the
                # panel is exactly where being wrong about that would matter.
                recorder.identify(session.call_id or event.call_id or "")

                # Dual-channel recording cannot be expressed in Vobiz's
                # answer XML (its <Record> element has no channel
                # attribute), so it starts over REST as soon as the call
                # id is known -- which is exactly now.
                if (
                    carrier.records_via_rest
                    and cfg.record_calls()
                    and session.call_id
                    and not recording_started
                ):
                    recording_started = True
                    # Keep a reference: a bare create_task can be garbage
                    # collected mid-flight, silently cancelling itself.
                    task = asyncio.create_task(
                        _start_recording(carrier, session.call_id)
                    )
                    background_tasks.add(task)
                    task.add_done_callback(background_tasks.discard)

                if agent is None:
                    await flux.start()
                    # The voice has to be known *before* the pool warms: it is
                    # bound into the TTS connection at connect time, so a pool
                    # started ahead of this read would serve turn 1 in the
                    # environment's voice while the panel showed the
                    # operator's pick.
                    tts_pool = TTSPool(
                        pool_size=1, ttl=8.0, voice_id=settings.voice_id
                    )
                    await tts_pool.start()
                    agent = Agent(
                        session=session,
                        on_done=completion.arm,
                        tts_pool=tts_pool,
                        tracer=tracer,
                        persona_id=context.persona_id,
                        settings=settings,
                        recorder=recorder,
                    )
                else:
                    # A reconnect replays `start` on the same call. Keep
                    # the agent and its history; only the stream id moved.
                    logger.info("Stream restarted on the same call -- history preserved")

            # ─── CARRIER SIGNALS ────────────────────────────────────
            if isinstance(event, PlaybackMarkEvent):
                _trace_playback_mark(tracer, event.name)
                # The authoritative end of a turn. Queues the
                # AgentTurnDoneEvent, which lands on the next iteration --
                # after this event has been through the machine.
                if completion.ack(event.name):
                    logger.debug(f"Turn ended on the carrier ack for {event.name}")
            elif isinstance(event, AudioClearedEvent):
                logger.debug("Carrier acknowledged audio flush")
                # rules.md V18 names clearAudio as a checkpoint voider.
                # The barge-in that caused it has already voided ours;
                # this is the belt to that pair of braces.
                completion.void("the carrier flushed its buffer")
            elif isinstance(event, DtmfEvent):
                logger.info(f"DTMF: {event.digit}")

            # ─── UPDATE (pure) ──────────────────────────────────────
            old_phase = state.phase
            state, actions = process_event(state, event)
            event_log.transition(old_phase, state.phase)

            # Phase -> panel state. Read *after* the machine has run and
            # never written back into it: the mapping is one-way, so the
            # panel cannot become an input to the conversation. The pipeline
            # has two phases and the panel has four; `connecting` and `ended`
            # are the edges of the call, which the machine has no opinion on.
            recorder.phase(SPEAKING if state.phase == Phase.RESPONDING else LISTENING)

            # ─── DISPATCH (side effects) ────────────────────────────
            for action in actions:
                event_log.action(action)
                try:
                    if isinstance(action, FeedFluxAction):
                        await flux.send(action.audio_bytes)

                    elif isinstance(action, StartAgentTurnAction):
                        # Second of two guards against the same hazard: a
                        # checkpoint from an abandoned turn still pending
                        # when a new turn starts. Its ack would then end
                        # the *new* turn -- truncating a live answer
                        # mid-sentence, with no error anywhere.
                        #
                        # Mutation testing: either guard alone holds, and
                        # removing both is caught by
                        # test_a_barge_in_voids_the_checkpoint. Kept
                        # doubled because Phase 3 rewrites the machine to
                        # three phases, and this failure is silent.
                        completion.void("a new turn started")
                        # Published here rather than inside the Agent so the
                        # caller's line reaches the panel even on a call
                        # where the agent failed to start the turn.
                        recorder.caller_said(action.transcript)
                        if agent:
                            await agent.start_turn(action.transcript)

                    elif isinstance(action, ResetAgentTurnAction):
                        # Barge-in, reconnect or hangup -- rules.md V18
                        # voids the pending checkpoint permanently. First
                        # of the two guards; see StartAgentTurnAction.
                        completion.void("the turn was interrupted")
                        if agent:
                            await agent.cancel_turn()

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # A vendor hiccup should cost one turn, not the call.
                    # Drop back to LISTENING so the caller can simply
                    # repeat themselves.
                    event_log.error(f"Action {type(action).__name__} failed", e)
                    recorder.note(
                        f"{type(action).__name__} failed — this turn was "
                        f"dropped. The caller can repeat themselves."
                    )
                    if isinstance(action, StartAgentTurnAction):
                        if agent:
                            try:
                                await agent.cancel_turn()
                            except Exception:
                                pass
                        # No audio was played, so no checkpoint will ever
                        # be acked. End the turn directly, and drop any
                        # wait so this stays one done event per turn.
                        completion.void("the turn failed to start")
                        event_queue.put_nowait(AgentTurnDoneEvent())

            # ─── EXIT CHECK ─────────────────────────────────────────
            if isinstance(event, StreamStopEvent):
                break

    except Exception as e:
        event_log.error("Call loop", e)
        ended_reason = "the call loop failed — see the server log"
        raise

    finally:
        # First thing in teardown: the panel should go grey the moment the
        # call is over, not after the TTS pool and Deepgram have been closed.
        recorder.ended(ended_reason)

        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

        # The call is over; a pending checkpoint has nothing left to
        # complete. Awaited so its task cannot outlive the loop.
        await completion.close()

        # Let in-flight background work finish before teardown. Abandoning
        # a recording-start request mid-flight loses the recording for the
        # whole call, and a short call can otherwise exit before the
        # request has even been sent.
        if background_tasks:
            await asyncio.wait(set(background_tasks), timeout=5.0)

        if agent:
            await agent.cleanup()

        # Guarded: a call that dropped before `start` ever arrived never built
        # a pool. That is not hypothetical -- a carrier that dials the socket
        # and hangs up, or a rejected stream, both land here with `tts_pool`
        # still None.
        if tts_pool:
            await tts_pool.stop()
        await flux.stop()

        # Tell the carrier the stream is over *before* anything tears the
        # call down over REST, or a phantom reconnect can overwrite the
        # transcript with empty data.
        await session.send_stop()

        # Traces are keyed on call_id, not stream_id: a reconnect changes
        # the stream id mid-call and would otherwise split the trace.
        tracer.save(session.call_id or context.call_id or "unknown")

        Logger.websocket_disconnected()


async def _start_recording(carrier: Carrier, call_id: str) -> None:
    """
    Kick off dual-channel recording without blocking the call.

    Failure is logged, never raised: losing a recording is bad for review
    but must not drop a live conversation.
    """
    try:
        await carrier.start_recording(
            call_id, callback_url=cfg.recording_callback_url() or None
        )
    except Exception as e:
        logger.error(f"Could not start recording for {call_id}: {e}")


def _turn_number(name: str) -> Optional[int]:
    """Turn number out of a checkpoint name. The player names them "turn-<n>"."""
    if not name.startswith("turn-"):
        return None
    try:
        return int(name.split("-", 1)[1])
    except (ValueError, IndexError):
        return None


def _trace_playback_mark(tracer: Tracer, name: str) -> None:
    """Record a carrier playback acknowledgement against its turn."""
    turn = _turn_number(name)
    if turn is None:
        return
    tracer.mark(turn, "carrier_playback_done")
    logger.debug(f"Carrier confirmed playback of {name}")


def _trace_playback_timeout(tracer: Tracer, name: str) -> None:
    """
    Record that a turn ended without its ack.

    Open question 8 and rules.md V18 both turn on whether Vobiz sends
    `playedStream` at all. A marker in the trace answers that from the
    saved file, without needing the live log.
    """
    turn = _turn_number(name)
    if turn is None:
        return
    tracer.mark(turn, "carrier_playback_timeout")
