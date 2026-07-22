"""
Player behaviour at the carrier seam.

Covers the byte accounting, the carrier seam, and -- since Phase 5 -- the
pacing gate from rules.md section 10: 50 frames/second with less than one
frame of drift, and `played_ms` matching wall-clock within 20ms.

The pacing tests are wall-clock tests on purpose. context.md warns that
"the pacing assertions will be easy to write tautologically", so they
measure the interval between real `play_audio` calls rather than trusting
the scheduler's own arithmetic. Reverting to `sleep(0.020)` per chunk
fails them by two orders of magnitude.
"""

import asyncio
import base64
import os
import time

import pytest

from shuo.services.player import (
    AudioPlayer,
    FRAME_BYTES,
    FRAME_SECONDS,
    MULAW_SILENCE,
    _b64_decoded_len,
)


class FakeSession:
    """Minimal CarrierSession stand-in that records calls."""

    def __init__(self):
        self.played = []
        self.cleared = 0
        self.checkpoints = []

    async def play_audio(self, payload_b64: str) -> None:
        self.played.append(payload_b64)

    async def clear_audio(self) -> None:
        self.cleared += 1

    async def checkpoint(self, name: str) -> None:
        self.checkpoints.append(name)


class TimingSession(FakeSession):
    """
    Records when each frame was handed to the carrier.

    Uses `perf_counter`, not `loop.time()`: on Python 3.12 for Windows the
    latter resolves to 15.625ms, which would leave the drift measurement
    unable to see three quarters of its own 20ms budget.
    """

    def __init__(self):
        super().__init__()
        self.sent_at = []

    async def play_audio(self, payload_b64: str) -> None:
        self.sent_at.append(time.perf_counter())
        await super().play_audio(payload_b64)


def frame(n_bytes: int = 160) -> str:
    """A base64 chunk of `n_bytes` of mu-law audio."""
    return base64.b64encode(b"\xff" * n_bytes).decode("ascii")


def speech(n_bytes: int) -> str:
    """
    A base64 chunk of `n_bytes` of non-silent mu-law.

    Distinguishable from the 0xFF padding byte, so tail-padding and
    frame-boundary tests can tell real audio from filler.
    """
    return base64.b64encode(bytes((i % 251) + 1 for i in range(n_bytes))).decode("ascii")


def sent_bytes(session) -> bytes:
    """Every frame the carrier received, concatenated back together."""
    return b"".join(base64.b64decode(p) for p in session.played)


async def until(predicate, timeout: float = 2.0) -> None:
    """
    Poll until `predicate` holds, then fail if it never does.

    Bounded on purpose. An unbounded `while not ...: await sleep()` turns
    every player regression into a hung suite instead of a red test, and
    the regressions this file guards against are precisely the ones that
    stop the loop from making progress.
    """
    deadline = time.perf_counter() + timeout
    while not predicate():
        if time.perf_counter() > deadline:
            raise AssertionError(f"condition never held within {timeout}s")
        await asyncio.sleep(0.005)


# =============================================================================
# BYTE ACCOUNTING
# =============================================================================

class TestBase64Length:
    """
    played_ms = bytes_sent / 8 at 8kHz mu-law, so the byte count has to be
    exact. Computing it from the base64 length avoids decoding every chunk
    twice in the hot path.
    """

    @pytest.mark.parametrize("raw", [
        b"", b"A", b"AB", b"ABC", b"ABCD", b"\x00" * 160, b"\xff" * 159, b"\xff" * 1601,
    ])
    def test_matches_actual_decode(self, raw):
        encoded = base64.b64encode(raw).decode("ascii")
        assert _b64_decoded_len(encoded) == len(raw)

    def test_one_frame_is_160_bytes(self):
        """20ms of 8kHz mu-law."""
        assert _b64_decoded_len(frame(160)) == 160


# =============================================================================
# PLAYBACK
# =============================================================================

class TestPlayback:
    @pytest.mark.asyncio
    async def test_chunks_reach_the_carrier_session(self):
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        for _ in range(3):
            await player.send_chunk(frame())
        player.mark_tts_done()
        await player.wait_until_done()

        assert len(session.played) == 3

    @pytest.mark.asyncio
    async def test_played_ms_accounting(self):
        """
        1 byte of 8kHz mu-law is 125us, so 480 bytes is exactly 60ms.
        This is the number Phase 5 maps back to characters to truncate
        history to what the caller actually heard (Bug A).
        """
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        for _ in range(3):
            await player.send_chunk(frame(160))
        player.mark_tts_done()
        await player.wait_until_done()

        assert player.bytes_sent == 480
        assert player.queued_ms == 60

    @pytest.mark.asyncio
    async def test_checkpoint_is_sent_after_the_last_chunk(self):
        """
        The checkpoint is what the carrier acknowledges once the audio has
        actually been played out. Phase 1 wires it; Phase 5 makes it
        authoritative for turn completion.
        """
        session = FakeSession()
        player = AudioPlayer(session=session, checkpoint_name="turn-7")
        await player.start()
        await player.send_chunk(frame())
        player.mark_tts_done()
        await player.wait_until_done()

        assert session.checkpoints == ["turn-7"]

    @pytest.mark.asyncio
    async def test_no_checkpoint_when_unnamed(self):
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        await player.send_chunk(frame())
        player.mark_tts_done()
        await player.wait_until_done()

        assert session.checkpoints == []

    @pytest.mark.asyncio
    async def test_on_done_fires(self):
        session = FakeSession()
        fired = []
        player = AudioPlayer(session=session, on_done=lambda: fired.append(True))
        await player.start()
        await player.send_chunk(frame())
        player.mark_tts_done()
        await player.wait_until_done()

        assert fired == [True]


# =============================================================================
# BARGE-IN
# =============================================================================

class TestBargeIn:
    @pytest.mark.asyncio
    async def test_stop_and_clear_flushes_the_carrier_buffer(self):
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        await player.send_chunk(frame())
        await player.stop_and_clear()

        assert session.cleared == 1
        assert player.is_playing is False

    @pytest.mark.asyncio
    async def test_no_checkpoint_on_interrupt(self):
        """
        An interrupted turn never completed, so claiming the caller heard
        it would corrupt the history Phase 5 truncates against.
        """
        session = FakeSession()
        player = AudioPlayer(session=session, checkpoint_name="turn-1")
        await player.start()
        await player.send_chunk(frame())
        await player.stop_and_clear()

        assert session.checkpoints == []

    @pytest.mark.asyncio
    async def test_on_done_does_not_fire_on_interrupt(self):
        session = FakeSession()
        fired = []
        player = AudioPlayer(session=session, on_done=lambda: fired.append(True))
        await player.start()
        await player.send_chunk(frame())
        await player.stop_and_clear()

        assert fired == []

    @pytest.mark.asyncio
    async def test_bytes_sent_survives_a_barge_in(self):
        """
        After an interrupt, bytes_sent is the record of what the caller
        heard. Bug A truncates history against it, so clearing it here
        would silently discard the evidence.
        """
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        await player.send_chunk(speech(FRAME_BYTES * 5))
        await until(lambda: player.frames_sent >= 2)
        await player.stop_and_clear()

        assert player.bytes_sent >= 2 * FRAME_BYTES


# =============================================================================
# REFRAMING  (Bug B, part 1: chunk size is the vendor's business)
# =============================================================================

class TestReframing:
    """
    TTS chunks never arrive on a 20ms grid -- ElevenLabs' are ~125ms, and
    160 bytes divides no vendor's framing. The player owns the grid.
    """

    @pytest.mark.asyncio
    async def test_every_frame_is_exactly_one_20ms_frame(self):
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        # Deliberately ragged: none of these is a multiple of 160.
        for size in (1000, 37, 999, 160 * 3 + 1):
            await player.send_chunk(speech(size))
        player.mark_tts_done()
        await player.wait_until_done()

        sizes = {len(base64.b64decode(p)) for p in session.played}
        assert sizes == {FRAME_BYTES}

    @pytest.mark.asyncio
    async def test_audio_crosses_chunk_boundaries_byte_exactly(self):
        """
        The codec gate (rules.md section 10) is a byte-exact round trip.
        Reframing must not drop, reorder or resample a single sample.
        """
        session = FakeSession()
        payloads = [speech(300), speech(150), speech(410)]
        expected = b"".join(base64.b64decode(p) for p in payloads)

        player = AudioPlayer(session=session)
        await player.start()
        for p in payloads:
            await player.send_chunk(p)
        player.mark_tts_done()
        await player.wait_until_done()

        assert sent_bytes(session).startswith(expected)

    @pytest.mark.asyncio
    async def test_tail_is_padded_with_mulaw_silence_not_dropped(self):
        """
        860 bytes is 5 frames plus 60. Dropping the remainder would clip
        the last syllable of every turn; emitting a short frame would put
        the carrier off the 20ms grid.
        """
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        await player.send_chunk(speech(860))
        player.mark_tts_done()
        await player.wait_until_done()

        assert player.frames_sent == 6
        assert player.bytes_sent == 6 * FRAME_BYTES

        tail = base64.b64decode(session.played[-1])
        # The literal 0xFF, deliberately not MULAW_SILENCE: comparing the
        # output against the constant that produced it would hold for any
        # pad byte, so it would prove nothing about the choice of byte.
        assert tail[60:] == b"\xff" * 100
        assert tail[59] != 0xFF  # real audio right up to the pad

    def test_pad_byte_is_mulaw_digital_silence(self):
        """
        0xFF is digital silence in G.711 mu-law -- verified against the
        codec itself: audioop.lin2ulaw(b"\\x00" * 320, 2) == b"\\xff" * 160.
        0x00 is very nearly full-scale negative; padding with it would put
        a click at the end of every turn, and nothing else here would fail.
        """
        assert MULAW_SILENCE == b"\xff"

    @pytest.mark.asyncio
    async def test_sub_frame_utterance_still_reaches_the_caller(self):
        session = FakeSession()
        player = AudioPlayer(session=session)
        await player.start()
        await player.send_chunk(speech(40))
        player.mark_tts_done()
        await player.wait_until_done()

        assert player.frames_sent == 1
        assert len(base64.b64decode(session.played[0])) == FRAME_BYTES


# =============================================================================
# PACING  (Bug B, part 2: the rules.md section 10 gate)
# =============================================================================

# The gate is specified over 60 seconds. That is too slow for every run,
# so the default is a 3-second window -- long enough that a per-chunk
# sleep or an accumulating error is unmissable. Set SHUO_PACING_SECONDS=60
# to run the literal gate.
PACING_SECONDS = float(os.getenv("SHUO_PACING_SECONDS", "3"))


class TestPacing:
    @pytest.mark.asyncio
    async def test_fifty_frames_per_second_with_under_one_frame_of_drift(self):
        """
        rules.md section 10, Player: exactly 50 frames/second with <1 frame
        of drift.

        Measured between the first and last real `play_audio` call, so the
        scheduler cannot pass by reporting its own intentions. Under the
        old `sleep(0.020)`-per-chunk loop this whole stream left in a
        single tick.
        """
        n_frames = int(PACING_SECONDS / FRAME_SECONDS)
        session = TimingSession()
        player = AudioPlayer(session=session)

        await player.start()
        await player.send_chunk(speech(n_frames * FRAME_BYTES))
        player.mark_tts_done()
        await player.wait_until_done()

        assert player.frames_sent == n_frames
        assert player.underruns == 0, "the buffer was pre-filled; it cannot run dry"

        span = session.sent_at[-1] - session.sent_at[0]
        expected = (n_frames - 1) * FRAME_SECONDS
        drift = abs(span - expected)

        assert drift < FRAME_SECONDS, (
            f"{n_frames} frames spanned {span:.4f}s, expected {expected:.4f}s "
            f"({drift * 1000:.1f}ms drift, budget {FRAME_SECONDS * 1000:.0f}ms)"
        )

    @pytest.mark.asyncio
    async def test_played_ms_tracks_wall_clock(self):
        """
        rules.md section 10: played_ms matches wall-clock within 20ms.
        This is the property Bug A depends on -- history may only be
        truncated to audio that had time to be heard.
        """
        n_frames = int(PACING_SECONDS / FRAME_SECONDS)
        session = TimingSession()
        player = AudioPlayer(session=session)

        await player.start()
        await player.send_chunk(speech(n_frames * FRAME_BYTES))
        player.mark_tts_done()
        await player.wait_until_done()

        elapsed_ms = (session.sent_at[-1] - session.sent_at[0]) * 1000
        # The last frame is counted when sent but is heard over the
        # following 20ms, so queued_ms leads wall-clock by exactly one frame.
        assert abs(player.queued_ms - FRAME_SECONDS * 1000 - elapsed_ms) < 20

    @pytest.mark.asyncio
    async def test_no_burst_when_tts_delivers_faster_than_realtime(self):
        """
        The regression that mattered: ElevenLabs hands over ~125ms of audio
        at a time, so anything that paces per chunk empties the whole
        utterance into the carrier at ~6x realtime. Committed audio is
        unretractable on barge-in.
        """
        session = TimingSession()
        player = AudioPlayer(session=session)

        await player.start()
        for _ in range(8):  # 8 x 125ms = 1s of audio, delivered instantly
            await player.send_chunk(speech(1000))
        player.mark_tts_done()

        await asyncio.sleep(0.2)
        assert player.frames_sent <= 12, (
            f"{player.frames_sent} frames left in 200ms; the carrier is "
            f"being over-buffered"
        )

        await player.wait_until_done()
        assert player.frames_sent == 50

    @pytest.mark.asyncio
    async def test_holds_the_gate_while_the_loop_is_stalled(self):
        """
        The gate must be measured under load, not idle. rules.md section 5
        puts Silero VAD and Smart Turn v3.1 in-process on this same event
        loop -- ~12ms on a modern CPU, ~60ms on a small instance -- so an
        idle-loop measurement tests a condition production never has.

        A scheduler that absorbs every stall instead of catching up passes
        idle and misses this by 6x, because each stall is paid permanently
        and they accumulate.
        """
        n_frames = 150  # 3s
        session = TimingSession()
        player = AudioPlayer(session=session)

        async def stall():
            while True:
                await asyncio.sleep(0.5)
                time.sleep(0.030)  # blocking, like a synchronous model

        stalls = asyncio.create_task(stall())
        try:
            await player.start()
            await player.send_chunk(speech(n_frames * FRAME_BYTES))
            player.mark_tts_done()
            await player.wait_until_done()
        finally:
            stalls.cancel()

        span = session.sent_at[-1] - session.sent_at[0]
        drift = abs(span - (n_frames - 1) * FRAME_SECONDS)
        assert drift < FRAME_SECONDS, (
            f"{drift * 1000:.1f}ms drift under a stalled loop, "
            f"budget {FRAME_SECONDS * 1000:.0f}ms"
        )

    @pytest.mark.asyncio
    async def test_underrun_does_not_burst_a_catch_up(self):
        """
        A gap in TTS is a gap the caller heard. Re-anchoring is what stops
        the scheduler from "recovering" the lost time by dumping the
        backlog -- which is over-buffering by another name.
        """
        session = TimingSession()
        player = AudioPlayer(session=session)

        await player.start()
        await player.send_chunk(speech(FRAME_BYTES * 3))
        await asyncio.sleep(0.25)  # starve it well past three frames
        await player.send_chunk(speech(FRAME_BYTES * 5))
        player.mark_tts_done()
        await player.wait_until_done()

        assert player.underruns >= 1
        assert player.frames_sent == 8

        # The five post-gap frames must occupy roughly their own playing
        # time. Asserting every individual gap would be flaky: a bounded
        # catch-up is allowed to emit back-to-back frames after a stall
        # (see _sleep_until), so only the aggregate is guaranteed.
        after_gap = session.sent_at[3:]
        span = after_gap[-1] - after_gap[0]
        assert span > 3 * FRAME_SECONDS, (
            f"5 frames dumped in {span * 1000:.1f}ms after the underrun; "
            f"the backlog was bursted rather than paced"
        )


# =============================================================================
# UNDERRUN SAFETY
# =============================================================================

class TestUnderrunSuspends:
    """
    The underrun branch of `_playback_loop` has exactly one suspension
    point: `_await_audio`. If that returns while `_next_frame` still yields
    None, the loop spins without yielding and starves the whole event loop
    -- carrier reader, TTS reader, barge-in and hangup included. The
    process wedges; it does not merely play badly.

    These assert the invariant directly rather than through behaviour,
    because the failure mode is a hang: a black-box test cannot fail, it
    can only stop. Measured on the broken version: a 1000-byte chunk with
    TTS still open gave 10.2M underruns and 7 loop ticks in 250ms.
    """

    @pytest.mark.parametrize("residue", [1, 40, 159])
    @pytest.mark.asyncio
    async def test_await_audio_blocks_whenever_no_frame_can_be_formed(self, residue):
        """
        160 divides no vendor's framing, so a buffer drained mid-utterance
        holds 1-159 bytes in 159 cases out of 160.
        """
        player = AudioPlayer(session=FakeSession())
        player._running = True
        player._buffer.extend(base64.b64decode(speech(residue)))

        assert player._next_frame() is None, "precondition: no frame is formable"

        # Must not return -- returning here is the spin.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(player._await_audio(), 0.05)

    @pytest.mark.asyncio
    async def test_await_audio_returns_as_soon_as_a_frame_is_formable(self):
        player = AudioPlayer(session=FakeSession())
        player._running = True
        player._buffer.extend(base64.b64decode(speech(FRAME_BYTES)))

        await asyncio.wait_for(player._await_audio(), 0.05)
        assert player._next_frame() is not None

    @pytest.mark.asyncio
    async def test_end_of_stream_releases_a_partial_frame(self):
        """`_tts_done` must still short-circuit, or the tail never plays."""
        player = AudioPlayer(session=FakeSession())
        player._running = True
        player._buffer.extend(base64.b64decode(speech(40)))
        player.mark_tts_done()

        await asyncio.wait_for(player._await_audio(), 0.05)
        assert len(player._next_frame()) == FRAME_BYTES

    @pytest.mark.asyncio
    async def test_partial_residue_mid_stream_counts_one_underrun(self):
        """
        Six frames plus a 40-byte residue, TTS still open: the player drains
        what it has, records a single underrun, and leaves the loop alive.
        A spin showed up here as millions.
        """
        session = FakeSession()
        player = AudioPlayer(session=session)
        beats = 0

        async def heartbeat():
            nonlocal beats
            while True:
                await asyncio.sleep(0.005)
                beats += 1

        hb = asyncio.create_task(heartbeat())
        try:
            await player.start()
            await player.send_chunk(speech(1000))  # 6 frames + 40 bytes
            await asyncio.sleep(0.25)

            assert player.frames_sent == 6
            assert player.underruns == 1
            assert beats > 10, f"event loop starved: {beats} ticks in 250ms"
        finally:
            hb.cancel()
            await player.stop_and_clear()


# =============================================================================
# PRE-ROLL  (rules.md C5)
# =============================================================================

class TestPreRoll:
    @pytest.mark.asyncio
    async def test_short_utterance_is_not_held_waiting_for_a_full_preroll(self):
        """
        The pre-roll must never outlast the audio. A one-frame greeting
        would otherwise sit behind a wait for two frames that never come.
        """
        session = TimingSession()
        player = AudioPlayer(session=session)

        await player.start()
        await player.send_chunk(speech(FRAME_BYTES))
        player.mark_tts_done()
        await player.wait_until_done()

        assert player.frames_sent == 1

    @pytest.mark.asyncio
    async def test_preroll_wait_is_bounded_when_tts_dribbles(self):
        """
        A TTS that never reaches the pre-roll must not add unbounded
        latency to first audio -- the cap is the pre-roll's own duration.
        """
        session = TimingSession()
        player = AudioPlayer(session=session)

        await player.start()
        t0 = time.perf_counter()
        await player.send_chunk(speech(FRAME_BYTES))  # one frame, then nothing

        await until(lambda: bool(session.sent_at))
        first_audio = session.sent_at[0] - t0

        await player.stop_and_clear()
        assert first_audio < 0.150, f"first frame held for {first_audio * 1000:.0f}ms"
