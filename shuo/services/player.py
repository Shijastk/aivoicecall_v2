"""
Audio player -- streams audio to the caller through the carrier session.

Owns an independent playback loop that emits **exactly one 20ms frame per
20ms tick against a monotonic deadline**. That is the whole point of this
module: the carrier's buffer is fed at realtime, so the bytes we have
handed over stay a truthful measure of what the caller has heard.

Phase 5 / Bug B. The previous loop slept 20ms per *chunk* regardless of
that chunk's duration. ElevenLabs' ~125ms chunks meant it ran ~6x faster
than realtime and over-buffered the carrier, which happened to mask the
error; a provider with a different chunk size would have drifted a
different way. Pacing is now decoupled from whatever framing the TTS
vendor emits:

    TTS chunks (any size) -> byte buffer -> 160-byte frames -> carrier
                                            ^ 50/second, deadline-paced

Two consequences, both load-bearing:

  * `bytes_sent / 8` is milliseconds of audio actually delivered, so Bug A
    (truncating history to what the caller heard) has a number it can
    trust rather than a burst of pre-buffered speech.
  * A barge-in flushes about one frame plus the handset de-jitter buffer
    instead of seconds of committed audio. Nothing in the loop may emit
    two frames back-to-back, including after a stall -- see _sleep_until.

Deliberately unchanged: `on_done` fires when the last frame is
*dispatched*, not when the carrier acknowledges the checkpoint. The
`checkpoint` wired here is still the authoritative playback signal, but
rules.md V18 says `playedStream` is conditional and may never arrive, so
nothing may block on it.
"""

import asyncio
import base64
import time
from typing import List, Optional, Callable

from ..carrier.base import CarrierSession
from ..log import ServiceLogger
from ..recording import CallTape

log = ServiceLogger("Player")


# ── Frame geometry (rules.md C3) ─────────────────────────────────────
# One byte of 8kHz mu-law is 125us, so one 20ms frame is exactly 160
# bytes and 50 frames make one second.
FRAME_BYTES = 160
FRAME_SECONDS = 0.020

# G.711 mu-law encodes digital silence as 0xFF -- verified locally:
# audioop.lin2ulaw(b"\x00" * 320, 2) == b"\xff" * 160. Used only to pad
# the final partial frame, never to fill an underrun (see _playback_loop).
MULAW_SILENCE = b"\xff"

# rules.md C5: pre-roll 2-3 frames. More is unretractable barge-in
# latency, less risks an underrun on the very first tick. Three frames is
# 60ms, which buys one Windows timer quantum of margin.
PREROLL_FRAMES = 3
PREROLL_BYTES = PREROLL_FRAMES * FRAME_BYTES
PREROLL_SECONDS = PREROLL_FRAMES * FRAME_SECONDS

# A tick later than this is a stall (a GC pause, a blocking call, an
# in-process model), not jitter. Re-anchor instead of bursting the backlog
# at the carrier -- bursting is the bug this module exists to fix. Within
# the bound, catching up is what holds drift inside the gate; see
# _sleep_until for the measurements.
MAX_LATENESS_SECONDS = 5 * FRAME_SECONDS

# Cap on how long the loop blocks waiting to be woken. The event is set on
# every chunk and by mark_tts_done, so this only bounds a TTS that dies
# without saying so.
WAKE_TIMEOUT_SECONDS = 0.100

# Pacing reads this clock, NOT loop.time(). On Windows loop.time() is
# time.monotonic(), which resolves to 15.625ms -- most of a frame. Against
# a clock that coarse a tick reads as "late" roughly every third frame
# even on an idle machine, and since lateness re-anchors (see
# _sleep_until) each of those phantom stalls became permanent drift:
# 40ms over 150 frames, measured. perf_counter resolves to 100ns, so
# "late" means genuinely late and re-anchoring stays rare.
_now = time.perf_counter


class AudioPlayer:
    """
    Streams audio to the caller at exactly realtime.

    Features:
    - Independent playback loop (not affected by incoming messages)
    - Accepts TTS chunks of any size and reframes them to 20ms frames
    - Deadline-paced: 50 frames/second, error does not accumulate
    - Instant stop and clear on interrupt
    - Callback when playback completes
    """

    def __init__(
        self,
        session: CarrierSession,
        on_done: Optional[Callable[[], None]] = None,
        checkpoint_name: Optional[str] = None,
        tape: Optional["CallTape"] = None,
        *,
        preroll_frames: int = PREROLL_FRAMES,
    ):
        self._session = session
        self._on_done = on_done
        self._checkpoint_name = checkpoint_name
        if preroll_frames not in (2, 3):
            raise ValueError("preroll_frames must be 2 or 3 (rules.md C5)")
        self._preroll_frames = preroll_frames
        self._preroll_bytes = preroll_frames * FRAME_BYTES
        self._preroll_seconds = preroll_frames * FRAME_SECONDS
        # The local recording's right channel (W5b). Defaults to a disabled
        # tape so `_send_frame` stays unconditional -- a player built outside
        # a call loop records nothing rather than needing a guard on the one
        # line in this module that runs 50 times a second.
        self._tape = tape or CallTape.disabled()

        # Decoded mu-law awaiting reframing. A byte buffer rather than a
        # chunk list because frame boundaries do not line up with chunk
        # boundaries and never will -- 160 does not divide any vendor's
        # framing.
        self._buffer = bytearray()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._tts_done = False

        # Set by send_chunk and mark_tts_done. Lets the loop block instead
        # of polling, so a chunk arriving mid-underrun resumes playback on
        # the next tick rather than at the next poll.
        self._wake = asyncio.Event()

        # Bytes handed to the carrier this turn. At 8kHz mu-law one byte
        # is 125us, so played_ms = bytes_sent / 8. Phase 5 maps this back
        # to characters to truncate history to what the caller heard.
        self._bytes_sent = 0
        self._frames_sent = 0
        self._underruns = 0
        self._warned_bad_b64 = False

    @property
    def is_playing(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def bytes_sent(self) -> int:
        return self._bytes_sent

    @property
    def preroll_frames(self) -> int:
        return self._preroll_frames

    @property
    def frames_sent(self) -> int:
        """20ms frames handed to the carrier. The subject of the pacing gate."""
        return self._frames_sent

    @property
    def underruns(self) -> int:
        """Times the buffer ran dry mid-utterance -- TTS slower than realtime."""
        return self._underruns

    @property
    def queued_ms(self) -> int:
        """
        Milliseconds of 8kHz mu-law handed to the carrier so far.

        Because the loop is deadline-paced this now tracks wall-clock
        playback rather than running ahead of it, which is what makes it
        usable as `played_ms`.
        """
        return self._bytes_sent // 8

    async def start(self) -> None:
        """Start the playback loop."""
        if self.is_playing:
            await self.stop_and_clear()

        self._reset()
        self._running = True
        self._task = asyncio.create_task(self._playback_loop())

    async def send_chunk(self, chunk: str) -> None:
        """
        Add a TTS chunk to the playback queue.

        Chunk size is the vendor's business; the loop reframes to 20ms.
        """
        if not self._running:
            await self.start()

        self._buffer.extend(self._decode(chunk))
        self._wake.set()

    def mark_tts_done(self) -> None:
        """Signal that TTS is complete - no more chunks coming."""
        self._tts_done = True
        self._wake.set()

    async def play(self, chunks: List[str]) -> None:
        """Start playing a fixed list of audio chunks (legacy mode)."""
        if self.is_playing:
            await self.stop_and_clear()

        self._reset()
        for chunk in chunks:
            self._buffer.extend(self._decode(chunk))
        self._running = True
        self._tts_done = True
        self._wake.set()

        self._task = asyncio.create_task(self._playback_loop())

    async def stop_and_clear(self) -> None:
        """Stop playback immediately and flush the carrier's buffer."""
        self._running = False
        # Release the loop if it is blocked on an underrun so cancellation
        # lands promptly rather than after the wake timeout.
        self._wake.set()

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._task = None
        self._buffer.clear()
        self._tts_done = False
        self._wake.clear()

        # _bytes_sent survives deliberately: after a barge-in it is the
        # record of what the caller heard, which Bug A truncates against.
        await self._session.clear_audio()

    async def wait_until_done(self) -> None:
        """Wait for playback to complete (or be interrupted)."""
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Playback ────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._buffer.clear()
        self._tts_done = False
        self._bytes_sent = 0
        self._frames_sent = 0
        self._underruns = 0
        self._wake.clear()

    async def _playback_loop(self) -> None:
        """Emit one 20ms frame per 20ms tick, on a monotonic deadline."""
        try:
            await self._await_preroll()
            log.info(
                f"Playback start preroll_frames={self._preroll_frames} "
                f"buffered_ms={len(self._buffer) // 8}"
            )

            deadline = _now()
            while self._running:
                frame = self._next_frame()

                if frame is None:
                    if self._tts_done:
                        break  # stream finished and fully drained
                    # Ran dry mid-utterance. Injecting silence would only
                    # add bytes the caller must wait through on a barge-in,
                    # so let the gap be a gap.
                    self._underruns += 1
                    await self._await_audio()
                    # The caller heard real silence; there is no catching
                    # up to do. Re-anchor so the backlog is not bursted.
                    deadline = _now()
                    continue

                await self._send_frame(frame)
                deadline = await self._sleep_until(deadline + FRAME_SECONDS)

            if self._running:
                self._running = False
                if self._underruns:
                    log.info(
                        f"{self._underruns} underrun(s) -- TTS ran slower than realtime"
                    )
                # Ask the carrier to tell us when this audio has actually
                # reached the caller. Traced in Phase 1; still advisory,
                # because rules.md V18 says it may never arrive.
                if self._checkpoint_name:
                    await self._session.checkpoint(self._checkpoint_name)
                if self._on_done:
                    self._on_done()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Playback failed", e)
            self._running = False

    async def _await_preroll(self) -> None:
        """
        Hold the first frame until a small cushion exists (rules.md C5).

        Waiting for the first frame is unbounded -- the turn has not begun,
        and a partial frame could not be sent anyway.
        Topping up to the full pre-roll is capped at the pre-roll's own
        duration, so a TTS that dribbles cannot add latency to the number
        the caller actually perceives.
        """
        await self._await_audio()

        limit = _now() + self._preroll_seconds
        while (
            self._running
            and not self._tts_done
            and len(self._buffer) < self._preroll_bytes
        ):
            remaining = limit - _now()
            if remaining <= 0:
                break
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), remaining)
            except asyncio.TimeoutError:
                break

    async def _await_audio(self) -> None:
        """
        Block until a whole frame can be formed, or the stream is over.

        ðŸ”´ This condition MUST stay the exact complement of `_next_frame`
        returning a frame -- "< FRAME_BYTES", not "empty". The underrun
        branch in `_playback_loop` has no other suspension point, so if
        this returns while `_next_frame()` still yields None the loop spins
        without ever yielding and starves the entire event loop: the
        carrier reader, the TTS reader, barge-in, the hangup path, and
        every other call on the worker die with it, permanently.

        That is not a corner case. 160 divides no vendor's framing, so a
        buffer drained mid-utterance holds 1-159 bytes in 159 cases out of
        160. Measured before the fix: a 1000-byte ElevenLabs-shaped chunk
        with TTS still open produced 10.2 million underruns and 7 event-loop
        ticks in 250ms. Only exact multiples of 160 survived.
        """
        while self._running and len(self._buffer) < FRAME_BYTES and not self._tts_done:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), WAKE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                pass

    def _next_frame(self) -> Optional[bytes]:
        """Pop exactly one 20ms frame, or None if one cannot be formed yet."""
        if len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            return frame

        if self._tts_done and self._buffer:
            # Final partial frame. Padding to a whole frame keeps the
            # carrier on the 20ms grid; dropping it would clip the last
            # syllable of every turn.
            frame = bytes(self._buffer).ljust(FRAME_BYTES, MULAW_SILENCE)
            self._buffer.clear()
            return frame

        return None

    async def _sleep_until(self, deadline: float) -> float:
        """
        Sleep until `deadline`; return the deadline to schedule against.

        The deadline advances in exact 20ms steps, so an overshooting sleep
        shortens the next one instead of adding to a running error. That is
        what keeps 3000 frames inside 60 seconds on a platform whose timer
        quantum (Windows: ~15.6ms) is most of a frame.

        Lateness catches up, but only within MAX_LATENESS_SECONDS. This was
        argued both ways and settled by measurement, because the deploy
        target does not run this loop idle: rules.md section 5 puts Silero
        VAD and Smart Turn v3.1 in-process on it (~12ms modern CPU, ~60ms
        on a small instance). Drift over a 10s stream, 20ms budget:

                                idle      60ms/2s     12ms/500ms
            absorb lateness    13.4ms    126.7ms *      18.5ms
            catch up (<=100ms)  8.8ms      2.2ms         1.9ms

        Absorbing every stall fails the gate by 6x under the stall profile
        we will actually have, because each stall is paid permanently and
        they accumulate. The burst it was meant to prevent does not show
        up: catching up emitted 8 back-to-back frames out of 500, and the
        worst gap was identical either way (60.9ms). A bounded catch-up
        costs at most MAX_LATENESS_SECONDS of committed audio, which is
        inside the handset de-jitter buffer rules.md T4 already concedes.

        Past the bound it is a stall, not jitter, and re-anchoring is
        right -- that time is gone and cannot be recovered by bursting.
        The underrun path re-anchors unconditionally for the same reason.
        """
        delay = deadline - _now()

        if delay > 0:
            await asyncio.sleep(delay)
            return deadline

        if -delay > MAX_LATENESS_SECONDS:
            # A stall, not jitter. Emitting the backlog back-to-back would
            # re-create the over-buffering this scheduler exists to prevent.
            return _now()

        # Late but recoverable: yield to the loop and catch up next tick.
        await asyncio.sleep(0)
        return deadline

    async def _send_frame(self, frame: bytes) -> None:
        """
        Send exactly one 20ms frame through the carrier.

        The tape tee is here rather than anywhere else in this module because
        this is the last point at which the audio is still raw µ-law and is
        already known to be going out: after the reframing, after the pacing
        gate, before the base64. Recording the buffer instead would record
        bytes a barge-in was about to discard.

        It costs one `bytearray.extend` and two integers -- deliberately the
        same cost class as `call_monitor`'s publish, because this line runs 50
        times a second inside the loop decision 24 made unforgiving.
        """
        self._bytes_sent += len(frame)
        self._frames_sent += 1
        self._tape.agent(frame)
        await self._session.play_audio(base64.b64encode(frame).decode("ascii"))

    def _decode(self, payload: str) -> bytes:
        """Decode a base64 TTS chunk to raw mu-law bytes."""
        raw = base64.b64decode(payload)
        # b64decode silently drops characters outside the alphabet, so a
        # payload that is not clean base64 decodes short rather than
        # raising. One comparison catches it; this is the codec seam, and
        # rules.md section 2 is about corruption that never throws.
        if not self._warned_bad_b64 and len(raw) != _b64_decoded_len(payload):
            self._warned_bad_b64 = True
            log.error(
                f"TTS payload is not clean base64: {len(payload)} chars "
                f"decoded to {len(raw)} bytes"
            )
        return raw


def _b64_decoded_len(payload: str) -> int:
    """Decoded byte length of a base64 string, without decoding it."""
    n = len(payload)
    if n == 0:
        return 0
    padding = payload.count("=", max(0, n - 2))
    return (n * 3) // 4 - padding
