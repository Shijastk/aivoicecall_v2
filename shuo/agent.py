"""
Agent -- self-contained LLM -> TTS -> Player pipeline.

Encapsulates the entire agent response lifecycle.
Owns conversation history across turns.

    start_turn(transcript) -> add to history -> LLM -> TTS -> Player -> Twilio
    cancel_turn()          -> cancel all, keep history

TTS connections are managed by TTSPool (see services/tts_pool.py).
"""

import asyncio
import time
from typing import Optional, Callable, List, Dict

from .call_monitor import CallRecorder
from .carrier.base import CarrierSession
from .recording import CallTape
from .runtime_config import CallSettings
from .services.llm import LLMService, PreparedLLMResponse
from .services.tts import TTSService
from .services.tts_pool import TTSPool
from .services.player import AudioPlayer
from .tracer import Tracer
from .log import ServiceLogger

log = ServiceLogger("Agent")


def _ms_since(t0: float) -> int:
    """Milliseconds elapsed since t0."""
    return int((time.monotonic() - t0) * 1000)


class Agent:
    """
    Self-contained agent response pipeline.

    LLM is persistent (keeps conversation history across turns).
    TTS connections come from TTSPool (pre-connected, with TTL eviction).
    Player is created fresh per turn.
    """

    def __init__(
        self,
        session: CarrierSession,
        on_done: Callable[[Optional[str]], None],
        tts_pool: TTSPool,
        tracer: Tracer,
        persona_id: str = "default",
        settings: Optional[CallSettings] = None,
        recorder: Optional[CallRecorder] = None,
        tape: Optional[CallTape] = None,
    ):
        self._session = session
        self._on_done = on_done
        self._tts_pool = tts_pool
        self._tracer = tracer

        # The local recording (W5b). Held only to hand to each turn's player,
        # which is the thing that sees outbound frames -- a fresh `AudioPlayer`
        # is built per turn, so the tape has to be threaded rather than
        # attached once. Disabled by default for the same reason `recorder`
        # is: an Agent built outside a call loop records nothing.
        self._tape = tape or CallTape.disabled()

        # The operator's live view of this call (W3). Defaults to a disabled
        # recorder so every publish site below can be unconditional -- an
        # Agent built outside a call loop records nothing rather than
        # needing a guard at each of the seven call sites.
        self._recorder = recorder or CallRecorder.disabled()
        # Phase 1 threads the persona through and logs it. Phase 6.5
        # (shuo/persona/) uses it to select the system prompt, fact block,
        # voice and turn-taking profile.
        self._persona_id = persona_id

        # This call's resolved configuration, read once at StreamStart by
        # `runtime_config.load_call_settings`. `builtin()` rather than reading
        # the store here: the agent is constructed inside the call loop, and a
        # disk read at that point is exactly what the config/audio process
        # split exists to prevent. A caller that passes nothing gets built-in
        # defaults, not a surprise file open.
        self._settings = settings or CallSettings.builtin()

        # Persistent LLM -- keeps conversation history across turns
        self._llm = LLMService(
            on_token=self._on_llm_token,
            on_done=self._on_llm_done,
            system_prompt=self._settings.system_prompt,
        )

        # Active per-turn services (set during start, cleared on cancel)
        self._tts: Optional[TTSService] = None
        self._player: Optional[AudioPlayer] = None
        self._active = False

        # Checkpoint the player will ask the carrier to acknowledge for
        # this turn. Handed to `on_done` so the loop can match the
        # `playedStream` that comes back -- an ack for a turn we have
        # already abandoned must not end the one now running.
        self._checkpoint: Optional[str] = None

        # Current turn number (for tracer)
        self._turn: int = 0

        # This turn's tokens, for the panel's transcript. A list append per
        # token and one join per turn: the token loop below is the streaming
        # chain the whole latency argument rests on (CLAUDE.md rule 2), and a
        # read-only preview does not get to put anything heavier in it.
        self._response: List[str] = []

        # Latency milestones (monotonic timestamps, reset each turn)
        self._t0: float = 0.0
        self._t_tts_conn: float = 0.0
        self._t_first_token: float = 0.0
        self._t_first_audio: float = 0.0
        self._got_first_token = False
        self._got_first_audio = False

    @property
    def is_turn_active(self) -> bool:
        return self._active

    @property
    def history(self) -> List[Dict[str, str]]:
        """Read-only access to conversation history (owned by LLM)."""
        return self._llm.history

    # ── Turn Lifecycle ──────────────────────────────────────────────

    async def start_turn(
        self,
        transcript: str,
        prepared_response: Optional[PreparedLLMResponse] = None,
    ) -> None:
        """Start a new agent turn."""
        if self._active:
            await self.cancel_turn()

        self._active = True
        self._t0 = time.monotonic()
        self._got_first_token = False
        self._got_first_audio = False
        self._response = []

        # Begin tracing this turn
        self._turn = self._tracer.begin_turn(transcript)
        self._recorder.turn_started(self._turn)
        self._tracer.begin(self._turn, "tts_pool")

        # Get TTS from pool (instant if warm, blocks if cold)
        self._tts = await self._tts_pool.get(
            on_audio=self._on_tts_audio,
            on_done=self._on_tts_done,
        )
        self._t_tts_conn = time.monotonic()
        self._tracer.end(self._turn, "tts_pool")

        # Create player
        self._checkpoint = f"turn-{self._turn}"
        self._player = AudioPlayer(
            session=self._session,
            on_done=self._on_playback_done,
            checkpoint_name=self._checkpoint,
            tape=self._tape,
        )

        # Start LLM. Phase 4C may reuse one exact-match provider stream;
        # validation failure immediately falls back to the normal final-EOT path.
        self._tracer.begin(self._turn, "llm")
        reused = False
        if prepared_response is not None:
            reused = await self._llm.start_prepared(transcript, prepared_response)
        if not reused:
            await self._llm.start(transcript)
        else:
            log.info(f"Lifecycle: turn={self._turn} prepared_response_reused")

        tts_ms = int((self._t_tts_conn - self._t0) * 1000)
        log.info(f"Turn started  (TTS {tts_ms}ms = {tts_ms}ms setup)")

    async def cancel_turn(self) -> None:
        """Cancel current turn, preserve history."""
        if not self._active:
            return

        elapsed = _ms_since(self._t0) if self._t0 else 0
        self._active = False

        # Mark turn as cancelled (ends all open spans)
        self._tracer.cancel_turn(self._turn)

        # Publish what was generated before the barge-in, flagged. Without
        # this the panel shows a caller line with no reply against it, which
        # reads as the twin having failed to answer rather than having been
        # interrupted -- and interruption handling is precisely what the
        # candidate persona exists to stress (CLAUDE.md §4.4).
        self._publish_response(interrupted=True)

        # Cancel in order: LLM -> TTS -> Player
        log.info(f"Lifecycle: turn={self._turn} cancel_stage=llm_begin")
        await self._llm.cancel()
        log.info(f"Lifecycle: turn={self._turn} cancel_stage=llm_returned")

        if self._tts:
            log.info(f"Lifecycle: turn={self._turn} cancel_stage=tts_begin")
            await self._tts.cancel()
            log.info(f"Lifecycle: turn={self._turn} cancel_stage=tts_returned")
            self._tts = None

        if self._player:
            if self._player.is_playing:
                log.info(f"Lifecycle: turn={self._turn} cancel_stage=player_begin dispatched_bytes={self._player.bytes_sent}")
                await self._player.stop_and_clear()
                log.info(f"Lifecycle: turn={self._turn} cancel_stage=player_returned")
            self._player = None

        # rules.md V18: the barge-in that got us here voids this turn's
        # checkpoint permanently. Forget it so a late ack cannot be
        # mistaken for the *next* turn finishing.
        self._checkpoint = None

        log.info(f"Turn cancelled at +{elapsed}ms (history preserved)")

    async def cleanup(self) -> None:
        """Final cleanup when call ends."""
        if self._active:
            await self.cancel_turn()

    # ── Internal Callbacks ──────────────────────────────────────────

    async def _on_llm_token(self, token: str) -> None:
        """LLM produced a token -> feed to TTS."""
        if not self._active or not self._tts:
            return

        if not self._got_first_token:
            self._got_first_token = True
            self._t_first_token = time.monotonic()
            self._tracer.mark(self._turn, "llm_first_token")
            self._tracer.begin(self._turn, "tts")
            ttft = _ms_since(self._t0)
            log.info(f"⏱  LLM first token  +{ttft}ms")
            self._recorder.timing("llm_first_token", ttft, turn=self._turn)

        self._response.append(token)
        await self._tts.send(token)

    async def _on_llm_done(self) -> None:
        """LLM finished -> flush TTS."""
        if not self._active or not self._tts:
            return
        self._tracer.end(self._turn, "llm")
        await self._tts.flush()

    async def _on_tts_audio(self, audio_base64: str) -> None:
        """TTS produced audio -> send to player."""
        if not self._active or not self._player:
            return

        if not self._got_first_audio:
            self._got_first_audio = True
            self._t_first_audio = time.monotonic()
            self._tracer.mark(self._turn, "tts_first_audio")
            self._tracer.begin(self._turn, "player")
            ttft = _ms_since(self._t0)
            since_token = int((self._t_first_audio - self._t_first_token) * 1000) if self._got_first_token else 0
            log.info(f"⏱  TTS first audio  +{ttft}ms  (TTS latency {since_token}ms) turn={self._turn}")
            self._recorder.timing("tts_first_audio", ttft, turn=self._turn)

        await self._player.send_chunk(audio_base64)

    async def _on_tts_done(self) -> None:
        """TTS finished -> tell player no more chunks coming."""
        if not self._active or not self._player:
            return
        self._tracer.end(self._turn, "tts")
        self._player.mark_tts_done()

        if not self._got_first_audio:
            # 🔴 TTS ended having produced nothing: a refused generation, a
            # dropped socket, a connection that died in the pool. The player
            # was never started -- `send_chunk` is its only entry point --
            # so `_playback_loop` does not exist, `_on_playback_done` can
            # never fire, and `mark_tts_done` above just sets a flag nobody
            # reads. Left here the turn stays `_active` forever: the caller
            # hears silence, the machine sits in RESPONDING, and the only
            # exit is the caller giving up and barging in. That is precisely
            # the "silent hang" signature, and it is a *pipeline* failure
            # wearing a *conversation* failure's clothes.
            #
            # End the turn here instead. No checkpoint goes with it: not one
            # frame was dispatched, so there is nothing for the carrier to
            # acknowledge, and `_TurnCompletion.arm(None)` finishes
            # immediately rather than burning the grace window.
            log.error(
                f"TTS produced no audio at +{_ms_since(self._t0)}ms -- "
                f"ending turn (the caller heard silence)"
            )
            # Say it in the panel too. Per decision 29 this failure is not
            # something the operator can hear -- it is silence -- so a panel
            # that showed only the transcript would make a vendor entitlement
            # problem look like the twin having nothing to say.
            self._recorder.note(
                "TTS produced no audio for this turn — the caller heard "
                "silence. Check the voice entitlement."
            )
            self._end_turn(checkpoint=None)

    def _on_playback_done(self) -> None:
        """
        Player dispatched its last frame.

        This is *not* the caller having heard the turn -- the carrier and
        the handset are still draining. The checkpoint name goes out with
        the callback so the loop can hold the turn open until the carrier
        acknowledges it (see conversation._TurnCompletion). The agent
        itself is done either way: nothing here waits on the ack, because
        rules.md V18 says it may never arrive.
        """
        if not self._active:
            return

        self._tracer.end(self._turn, "player")

        total = _ms_since(self._t0)
        log.info(f"⏱  Playback dispatched  +{total}ms total turn={self._turn}")
        self._recorder.timing("playback_dispatched", total, turn=self._turn)

        self._end_turn(checkpoint=self._checkpoint)

    def _end_turn(self, checkpoint: Optional[str]) -> None:
        """
        Release the turn and hand the loop its completion signal.

        The single exit for a turn that *finished*, as opposed to one that
        was cancelled. Both routes here -- playback dispatched, and TTS
        having produced nothing -- must drop the per-turn services and clear
        `_active` in exactly the same way, so they share one body rather
        than two that drift apart.
        """
        self._active = False
        self._tts = None
        self._player = None
        self._checkpoint = None

        self._publish_response(interrupted=False)

        self._on_done(checkpoint)

    def _publish_response(self, *, interrupted: bool) -> None:
        """
        Hand this turn's generated text to the operator's panel, once.

        Joined here rather than accumulated as a string in `_on_llm_token`:
        repeated `+=` on a growing string is O(n²) over a 20-45s answer, and
        that cost would land in the token loop. The list is cleared so a
        second call on the same turn -- `cancel_turn` after `_end_turn`, say
        -- cannot publish the answer twice.
        """
        if not self._response:
            return
        text, self._response = "".join(self._response), []
        self._recorder.agent_said(text, turn=self._turn, interrupted=interrupted)
