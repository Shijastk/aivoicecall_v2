"""
Tests for the local stereo recording.

What is being defended, in descending order of how badly it would hurt:

1. **The tee cannot cost a call.** Both tee points are inside the loop that
   paces 20ms frames; a blocking write or a raised exception there is a
   degraded call, not a degraded recording. `TestItCannotCostACall`.
2. **The audio is correct.** A recording that plays back wrong is worse than
   none, because it is trusted. The µ-law table is checked against `audioop`
   entry by entry, and the geometry against the WAV header. `TestTheAudio`.
3. **The two channels line up.** The point of recording a call is hearing who
   spoke over whom, which a track that drifts cannot answer.
   `TestTheTimeline`.
4. **An id never becomes an arbitrary path.** These arrive from a carrier's
   callback URL and from a browser's address bar. `TestPathsAreNotTrusted`.
"""

import wave

import pytest

from shuo import recording
from shuo.recording import BYTES_PER_SECOND, MULAW_SILENCE, CallTape
from shuo.spool import Spool


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "recordings"
    monkeypatch.setenv("SHUO_RECORDINGS_DIR", str(path))
    return path


@pytest.fixture
def spool():
    """A spool of its own, so a test drains only its own writes."""
    return Spool(name="recording-test")


def tape(root, spool, call_ref="att-test01"):
    return CallTape(call_ref, spool=spool, root=root)


def read_wav(path):
    with wave.open(str(path), "rb") as handle:
        return handle, handle.readframes(handle.getnframes())


def channels(path):
    """The two channels of a stereo WAV, as lists of samples."""
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = [
        int.from_bytes(raw[at:at + 2], "little", signed=True)
        for at in range(0, len(raw), 2)
    ]
    return samples[0::2], samples[1::2]


TONE = bytes(range(0, 256, 8)) * 20   # 640 bytes of varied µ-law
SILENCE = bytes([MULAW_SILENCE])


# =============================================================================
# THE ROUND TRIP
# =============================================================================

class TestARoundTrip:
    @pytest.mark.asyncio
    async def test_a_call_becomes_one_stereo_wav(self, root, spool):
        recorder = tape(root, spool)
        recorder.caller(TONE)
        recorder.agent(TONE)
        recorder.close()
        await spool.drain()

        path = recording.recording_path("att-test01", root=root)
        assert path.exists(), "no recording was produced"

        with wave.open(str(path), "rb") as handle:
            assert handle.getnchannels() == 2
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 8000
            # Twice `TONE`, not once, and that is the alignment working: the
            # twin spoke *after* the caller's 640 bytes, so its audio starts
            # 640 samples in and the file runs to 1280. A recording where
            # both turns began at zero would be the bug.
            assert handle.getnframes() == len(TONE) * 2

    @pytest.mark.asyncio
    async def test_the_caller_is_left_and_the_agent_is_right(self, root, spool):
        """
        Stereo rather than mixed, because the reason to listen to one of these
        is to hear who was talking over whom.
        """
        recorder = tape(root, spool)
        recorder.caller(bytes([0x00]) * 160)   # loud on the left
        recorder.agent(SILENCE * 160)          # silent on the right
        recorder.close()
        await spool.drain()

        left, right = channels(recording.recording_path("att-test01", root=root))
        assert any(sample != 0 for sample in left), "the caller is not on the left"
        assert all(sample == 0 for sample in right), "the agent is not on the right"

    @pytest.mark.asyncio
    async def test_the_parts_are_cleaned_up(self, root, spool):
        recorder = tape(root, spool)
        recorder.caller(TONE)
        recorder.close()
        await spool.drain()

        assert not list((root / ".parts").glob("*.ulaw")), (
            "raw tracks were left behind"
        )

    @pytest.mark.asyncio
    async def test_a_call_with_no_audio_produces_no_file(self, root, spool):
        """
        A call that dropped before anything flowed. Not a failure, and not
        worth a row claiming a recording that would 404 when pressed.
        """
        recorder = tape(root, spool)
        recorder.close()
        await spool.drain()

        assert not recording.recording_path("att-test01", root=root).exists()

    @pytest.mark.asyncio
    async def test_one_sided_audio_still_records(self, root, spool):
        """
        Routine, not exceptional: a missed call has a caller track and no
        agent track at all, and it is exactly the call somebody wants to hear.
        """
        recorder = tape(root, spool)
        recorder.caller(TONE)
        recorder.close()
        await spool.drain()

        left, right = channels(recording.recording_path("att-test01", root=root))
        assert len(left) == len(right) == len(TONE)
        assert all(sample == 0 for sample in right)


# =============================================================================
# THE AUDIO
# =============================================================================

class TestTheAudio:
    def test_the_ulaw_table_matches_the_reference(self):
        """
        🔴 All 256 entries against `audioop`, which is where this decode came
        from and which was **removed in Python 3.13** -- the reason there is a
        table here at all. Skipped rather than dropped where the interpreter
        no longer has it: the check is worth running wherever it can run.
        """
        audioop = pytest.importorskip(
            "audioop", reason="removed in 3.13 — which is why we have a table"
        )

        for byte in range(256):
            expected = int.from_bytes(
                audioop.ulaw2lin(bytes([byte]), 2), "little", signed=True
            )
            assert recording._ULAW_TO_PCM[byte] == expected, (
                f"µ-law 0x{byte:02x} decodes to "
                f"{recording._ULAW_TO_PCM[byte]}, not {expected}"
            )

    def test_mulaw_silence_decodes_to_zero(self):
        """
        The constant the gaps are padded with. If this were not zero, every
        pause in every recording would be a tone.
        """
        assert recording._ULAW_TO_PCM[MULAW_SILENCE] == 0

    @pytest.mark.asyncio
    async def test_the_samples_survive_the_round_trip(self, root, spool):
        recorder = tape(root, spool)
        recorder.caller(TONE)
        recorder.close()
        await spool.drain()

        left, _ = channels(recording.recording_path("att-test01", root=root))
        assert left == [recording._ULAW_TO_PCM[byte] for byte in TONE]

    @pytest.mark.asyncio
    async def test_a_second_of_audio_is_a_second_long(self, root, spool):
        """
        Geometry, checked rather than assumed: 8000 µ-law bytes is one second,
        and a recording that reports the wrong duration makes every latency
        number read off it wrong too.
        """
        recorder = tape(root, spool)
        recorder.caller(SILENCE * BYTES_PER_SECOND)
        recorder.close()
        await spool.drain()

        path = recording.recording_path("att-test01", root=root)
        with wave.open(str(path), "rb") as handle:
            assert handle.getnframes() / handle.getframerate() == 1.0

        assert recording.describe("att-test01", root=root)["seconds"] == 1.0


# =============================================================================
# THE TIMELINE
# =============================================================================

class TestTheTimeline:
    @pytest.mark.asyncio
    async def test_the_agent_lands_where_it_spoke(self, root, spool):
        """
        🔴 The whole reason a chunk carries an offset.

        The twin answers after the caller has been speaking for a second. Its
        audio has to appear a second into the file, not at the start -- a
        recording that stacks both turns at zero cannot show an interruption,
        which is the main thing these are reviewed for.
        """
        recorder = tape(root, spool)
        recorder.caller(SILENCE * BYTES_PER_SECOND)   # one second of caller
        recorder.agent(bytes([0x00]) * 160)           # then the twin speaks
        recorder.caller(SILENCE * 160)
        recorder.close()
        await spool.drain()

        _, right = channels(recording.recording_path("att-test01", root=root))

        assert all(sample == 0 for sample in right[:BYTES_PER_SECOND]), (
            "the twin's audio was placed before it spoke"
        )
        assert any(sample != 0 for sample in right[BYTES_PER_SECOND:]), (
            "the twin's audio never landed"
        )

    @pytest.mark.asyncio
    async def test_a_gap_is_filled_with_silence_not_zero_bytes(self, root, spool):
        """
        µ-law silence is 0xFF, not 0x00. Padding a gap with zero bytes would
        fill every pause with the loudest sample the codec has.
        """
        recorder = tape(root, spool)
        recorder.caller(SILENCE * BYTES_PER_SECOND)
        recorder.agent(SILENCE * 160)
        recorder.close()
        await spool.drain()

        part = root / ".parts"
        # Inspect before cleanup by re-running the write path directly.
        recording._write_part(root, "att-gap", "agent", 800, SILENCE * 160)
        raw = (part / "att-gap.agent.ulaw").read_bytes()
        assert raw[:800] == SILENCE * 800, "the gap was not µ-law silence"

    @pytest.mark.asyncio
    async def test_the_two_tracks_are_the_same_length(self, root, spool):
        """The shorter is padded, so the channels cannot drift apart."""
        recorder = tape(root, spool)
        recorder.caller(SILENCE * 4000)
        recorder.agent(SILENCE * 160)
        recorder.close()
        await spool.drain()

        left, right = channels(recording.recording_path("att-test01", root=root))
        assert len(left) == len(right)

    @pytest.mark.asyncio
    async def test_a_long_call_flushes_rather_than_growing(self, root, spool):
        """
        Memory is bounded by flushing, not by a cap on the call: a 45-minute
        interview must hold the same buffer as a 20-second test call.
        """
        recorder = tape(root, spool)
        for _ in range(40):  # 40 seconds
            recorder.caller(SILENCE * BYTES_PER_SECOND)

        assert len(recorder._caller) < recording.FLUSH_BYTES, (
            "the buffer grew with the call instead of flushing"
        )
        recorder.close()
        await spool.drain()

    @pytest.mark.asyncio
    async def test_a_runaway_stream_stops_at_the_ceiling(self, root, spool, monkeypatch):
        monkeypatch.setattr(recording, "MAX_TRACK_BYTES", BYTES_PER_SECOND)

        recorder = tape(root, spool)
        for _ in range(5):
            recorder.caller(SILENCE * BYTES_PER_SECOND)

        assert recorder._truncated
        assert recorder.seconds <= 2, "the ceiling did not stop the tee"
        recorder.close()
        await spool.drain()


# =============================================================================
# IT CANNOT COST A CALL
# =============================================================================

class TestItCannotCostACall:
    def test_the_tee_never_touches_the_disk(self, root, spool, monkeypatch):
        """
        🔴 The property the whole design rests on.

        Both tee points are inside the loop that paces 20ms frames, where
        decision 24 makes a stall permanent stream delay rather than one late
        frame. Any real file operation on that path fails this test rather
        than being discovered on a live call.
        """
        import builtins

        recorder = tape(root, spool)

        def forbidden(*args, **kwargs):
            raise AssertionError("the tee opened a file on the hot path")

        monkeypatch.setattr(builtins, "open", forbidden)

        for _ in range(200):  # four seconds of a real call, both directions
            recorder.caller(SILENCE * 160)
            recorder.agent(SILENCE * 160)

    def test_an_unwritable_root_does_not_raise(self, tmp_path, spool):
        blocked = tmp_path / "recordings"
        blocked.write_text("not a directory", encoding="utf-8")

        recorder = CallTape("att-test01", spool=spool, root=blocked)
        recorder.caller(TONE)
        recorder.close()  # must not raise

    @pytest.mark.asyncio
    async def test_a_failed_conversion_leaves_the_call_alone(
        self, root, spool, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(recording, "_write_wav", boom)

        recorder = tape(root, spool)
        recorder.caller(TONE)
        recorder.close()
        await spool.drain()  # must not raise

        assert not recording.recording_path("att-test01", root=root).exists()

    def test_a_disabled_tape_no_ops(self):
        """What every site falls back to, so no site needs a guard."""
        recorder = CallTape.disabled()

        recorder.caller(TONE)
        recorder.agent(TONE)
        recorder.close()

        assert not recorder.enabled

    def test_closing_twice_is_harmless(self, root, spool):
        recorder = tape(root, spool)
        recorder.caller(TONE)
        recorder.close()
        recorder.close()

    @pytest.mark.asyncio
    async def test_audio_after_close_is_ignored(self, root, spool):
        """
        Teardown and a last in-flight frame can cross. A late frame must not
        reopen a file the conversion has already consumed.
        """
        recorder = tape(root, spool)
        recorder.caller(TONE)
        recorder.close()
        recorder.caller(TONE)
        recorder.agent(TONE)
        await spool.drain()

        left, _ = channels(recording.recording_path("att-test01", root=root))
        assert len(left) == len(TONE)


# =============================================================================
# PATHS ARE NOT TRUSTED
# =============================================================================

class TestPathsAreNotTrusted:
    @pytest.mark.parametrize(
        "supplied",
        [
            "../../../etc/passwd",
            "..\\..\\windows\\system32\\config\\sam",
            "att-1/../../secret",
            "att-1\x00.wav",
            "",
            "   ",
            "a" * 100,
            "att 1",
            "att-1;rm -rf /",
        ],
    )
    def test_a_hostile_id_names_no_file(self, root, supplied):
        assert recording.recording_path(supplied, root=root) is None

    def test_a_real_id_names_a_file_inside_the_root(self, root):
        path = recording.recording_path("att-0123456789ab", root=root)

        assert path is not None
        assert path.parent == root.resolve()
        assert path.name == "att-0123456789ab.wav"

    def test_the_monitors_own_ids_are_accepted(self, root):
        """`call-3`, which is what a socket opened without an attempt id gets."""
        assert recording.recording_path("call-3", root=root) is not None

    def test_describe_answers_from_the_disk_not_from_hope(self, root):
        assert recording.describe("att-missing", root=root) == {"available": False}


# =============================================================================
# RETENTION
# =============================================================================

class TestRetention:
    def test_the_oldest_recordings_are_pruned(self, root, monkeypatch):
        monkeypatch.setattr(recording, "MAX_RECORDINGS", 3)
        root.mkdir(parents=True, exist_ok=True)

        import os
        import time

        for index in range(6):
            path = root / f"att-{index:012d}.wav"
            path.write_bytes(b"x" * 100)
            # Explicit mtimes: the writes are faster than the clock's
            # resolution, so "oldest" would otherwise be undefined.
            stamp = time.time() - (6 - index) * 10
            os.utime(path, (stamp, stamp))

        recording._prune(root)

        survivors = sorted(path.name for path in root.glob("*.wav"))
        assert len(survivors) == 3
        assert survivors == [
            "att-000000000003.wav",
            "att-000000000004.wav",
            "att-000000000005.wav",
        ]

    def test_a_bytes_budget_prunes_too(self, root, monkeypatch):
        monkeypatch.setattr(recording, "MAX_RECORDINGS", 100)
        monkeypatch.setattr(recording, "MAX_TOTAL_BYTES", 250)
        root.mkdir(parents=True, exist_ok=True)

        import os
        import time

        for index in range(5):
            path = root / f"att-{index:012d}.wav"
            path.write_bytes(b"x" * 100)
            stamp = time.time() - (5 - index) * 10
            os.utime(path, (stamp, stamp))

        recording._prune(root)

        assert len(list(root.glob("*.wav"))) == 2

    def test_pruning_an_empty_directory_is_harmless(self, root):
        recording._prune(root)


# =============================================================================
# THE SWITCH
# =============================================================================

class TestTheSwitch:
    def test_it_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("SHUO_LOCAL_RECORDING", raising=False)
        assert recording.local_recording_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
    def test_it_can_be_switched_off(self, monkeypatch, value):
        monkeypatch.setenv("SHUO_LOCAL_RECORDING", value)
        assert recording.local_recording_enabled() is False

    def test_it_is_independent_of_the_carriers_recording(self, monkeypatch):
        """
        `RECORD_CALLS` governs the carrier's billable recording. Turning that
        off to stop paying for a second copy must not take the free local one
        with it.
        """
        monkeypatch.setenv("RECORD_CALLS", "false")
        monkeypatch.delenv("SHUO_LOCAL_RECORDING", raising=False)

        assert recording.local_recording_enabled() is True


# =============================================================================
# RETRIEVAL (:3041)
# =============================================================================

class TestTheEndpoint:
    """
    `GET /v1/calls/{id}/recording`, on the config API and deliberately not on
    the call server: streaming a ten-megabyte file off a disk is exactly the
    work that must not happen on the loop pacing 20ms frames (decision 32).
    """

    @pytest.fixture
    def client(self, root, spool, monkeypatch):
        from fastapi.testclient import TestClient

        import shuo.config_api as config_api

        # One real recording to fetch.
        recorder = CallTape("att-0123456789ab", spool=spool, root=root)
        recorder.caller(SILENCE * BYTES_PER_SECOND)
        recorder.close()

        with TestClient(config_api.app) as test_client:
            yield test_client

    def test_it_serves_the_wav(self, client):
        response = client.get("/v1/calls/att-0123456789ab/recording")

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content[:4] == b"RIFF"
        assert response.content[8:12] == b"WAVE"

    def test_it_plays_in_place_rather_than_downloading(self, client):
        """
        `inline`, so the panel's <audio> element plays it where it is instead
        of the browser offering to save a file nobody asked for.
        """
        response = client.get("/v1/calls/att-0123456789ab/recording")

        assert "inline" in response.headers.get("content-disposition", "")

    def test_it_answers_a_range_request(self, client):
        """
        🔴 What makes the endpoint useful rather than merely present.

        A browser seeking inside an <audio> element asks for byte ranges.
        Without a 206 the operator can play a recording but cannot skip to the
        part of it worth hearing.
        """
        response = client.get(
            "/v1/calls/att-0123456789ab/recording",
            headers={"Range": "bytes=0-99"},
        )

        assert response.status_code == 206, "range requests are not supported"
        assert len(response.content) == 100
        assert "bytes" in response.headers.get("content-range", "")

    def test_a_missing_recording_is_a_404_with_a_sentence(self, client):
        response = client.get("/v1/calls/att-ffffffffffff/recording")

        assert response.status_code == 404
        message = response.json()["message"]
        assert "no recording" in message.lower()
        # The envelope this API promises everywhere (decision 35).
        assert "detail" not in response.json()

    def test_a_traversal_attempt_gets_no_file(self, client):
        for supplied in ("..%2F..%2Fetc%2Fpasswd", "att-1%00", "not+an+id"):
            response = client.get(f"/v1/calls/{supplied}/recording")
            assert response.status_code in (400, 404), supplied
            assert response.headers["content-type"].startswith("application/json")

    def test_the_history_reports_what_is_on_disk(self, client):
        """
        Answered from the filesystem, not from what the row remembers: a
        recording deleted by the retention budget must stop being offered, or
        the panel grows a play button that 404s.
        """
        from shuo import call_history

        call_history.append(
            call_history.revision("att-0123456789ab", status="completed")
        )
        call_history.append(call_history.revision("att-gone", status="completed"))

        calls = {row["id"]: row for row in client.get("/v1/calls/history").json()["calls"]}

        assert calls["att-0123456789ab"]["recording"]["available"] is True
        assert calls["att-0123456789ab"]["recording"]["url"] == (
            "/v1/calls/att-0123456789ab/recording"
        )
        assert calls["att-gone"]["recording"] == {"available": False}


# =============================================================================
# THE LATENCY BUDGET
# =============================================================================

class TestItDoesNotCostLatency:
    """
    The tee runs inside the loop that paces 20ms frames, so "it should be
    cheap" is not good enough -- it is measured here, both directly and
    through the real player's own pacing gate.
    """

    def test_the_tee_costs_microseconds_per_frame(self, root, spool):
        """
        A 20ms frame is a 20,000µs budget. This asserts the tee stays under
        1% of it -- two orders of magnitude of headroom -- so a regression
        that made it *forty times* slower would still not miss a deadline,
        and would still fail here.
        """
        import time

        recorder = tape(root, spool)
        frame = SILENCE * 160
        rounds = 2000

        start = time.perf_counter()
        for _ in range(rounds):
            recorder.caller(frame)
            recorder.agent(frame)
        elapsed = time.perf_counter() - start

        per_frame_us = (elapsed / rounds) * 1_000_000
        assert per_frame_us < 200, (
            f"the tee costs {per_frame_us:.1f}µs per 20ms frame — that is "
            f"{per_frame_us / 20_000:.2%} of the player's budget"
        )
        recorder.close()

    @pytest.mark.asyncio
    async def test_the_player_still_paces_with_a_tape_attached(self, root, spool):
        """
        🔴 The gate that matters, run through the real `AudioPlayer`.

        rules.md section 10: exactly 50 frames a second with under one frame
        of drift, measured between the first and last real `play_audio`. The
        tee sits inside `_send_frame`, so if it cost anything the drift would
        show up here rather than on a live call.
        """
        from tests.test_player import (
            FRAME_BYTES,
            FRAME_SECONDS,
            PACING_SECONDS,
            TimingSession,
            speech,
        )
        from shuo.services.player import AudioPlayer

        n_frames = int(PACING_SECONDS / FRAME_SECONDS)
        session = TimingSession()
        recorder = tape(root, spool)
        player = AudioPlayer(session=session, tape=recorder)

        await player.start()
        await player.send_chunk(speech(n_frames * FRAME_BYTES))
        player.mark_tts_done()
        await player.wait_until_done()

        assert player.frames_sent == n_frames
        assert player.underruns == 0

        span = session.sent_at[-1] - session.sent_at[0]
        drift = abs(span - (n_frames - 1) * FRAME_SECONDS)

        assert drift < FRAME_SECONDS, (
            f"{n_frames} frames drifted {drift * 1000:.1f}ms with the tape "
            f"attached (budget {FRAME_SECONDS * 1000:.0f}ms)"
        )

        # And it actually recorded, rather than passing by doing nothing.
        assert recorder._agent_bytes == n_frames * FRAME_BYTES
        recorder.close()
        await spool.drain()
