"""
Tests for the durable call log -- the record that outlives the ring buffer.

Three things are being defended, in descending order of how badly they would
hurt if they broke:

1. **It cannot cost a call.** The write happens in the call loop's teardown,
   and every entry point swallows its own failures. A full disk loses a row
   in a table; it must not lose a conversation. `TestItCannotCostACall`.
2. **A whole call is archived, not the tail of one.** The ring buffer is
   global and holds 200 events across 3 calls, so a long call has evicted its
   own opening turns before it ends. `TestTheArchiveIsNotTheRing`.
3. **A torn write costs one record.** JSONL exists so an append cannot
   rewrite what is already there, and `load` skips a line it cannot parse
   because a killed writer is an expected state. `TestTheFileFormat`.
"""

import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from shuo import call_history
from shuo.call_monitor import (
    COMPLETED,
    ENDED,
    FAILED,
    MAX_ARCHIVED_TURNS,
    MISSED,
    CallMonitor,
    CallRecorder,
)


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    """Point the history at a throwaway file, not the repo's `var/`."""
    path = tmp_path / "call_history.jsonl"
    monkeypatch.setenv("SHUO_CALL_HISTORY_PATH", str(path))
    return path


@pytest.fixture
def monitor():
    return CallMonitor()


def a_call(monitor, *, call_id="MZ-1", persona="candidate"):
    """A recorder for a call that has been identified by the carrier."""
    recorder = monitor.begin(persona=persona, direction="outbound")
    recorder.identify(call_id)
    return recorder


# =============================================================================
# ROUND TRIP
# =============================================================================

class TestARoundTrip:
    def test_a_finished_call_can_be_read_back(self, log_path, monitor):
        recorder = a_call(monitor)
        recorder.configured("prompt=operator (812 chars)  voice=George [JBF]")
        recorder.caller_said("So, tell me about yourself.")
        recorder.agent_said("Sure ji — backend, mostly payments.", turn=1)
        recorder.timing("llm_first_token", 312, turn=1)
        recorder.ended("the call ended")

        call_history.append(recorder.record())

        [stored] = call_history.load()
        assert stored["callId"] == "MZ-1"
        assert stored["persona"] == "candidate"
        assert stored["status"] == COMPLETED
        assert stored["config"].startswith("prompt=operator")
        assert [turn["speaker"] for turn in stored["transcript"]] == [
            "caller",
            "agent",
        ]
        assert stored["transcript"][1]["text"] == "Sure ji — backend, mostly payments."
        assert stored["milestones"] == [
            {"name": "llm_first_token", "ms": 312, "turn": 1}
        ]

    def test_the_newest_call_comes_first(self, log_path, monitor):
        for index in range(3):
            recorder = a_call(monitor, call_id=f"MZ-{index}")
            recorder.caller_said(f"call {index}")
            recorder.ended("the call ended")
            call_history.append(recorder.record())

        assert [row["callId"] for row in call_history.load()] == [
            "MZ-2",
            "MZ-1",
            "MZ-0",
        ]

    def test_limit_takes_the_newest(self, log_path, monitor):
        for index in range(5):
            recorder = a_call(monitor, call_id=f"MZ-{index}")
            recorder.caller_said("hello")
            recorder.ended("the call ended")
            call_history.append(recorder.record())

        assert [row["callId"] for row in call_history.load(limit=2)] == [
            "MZ-4",
            "MZ-3",
        ]

    def test_no_calls_yet_is_an_empty_list_not_an_error(self, log_path):
        """
        The ordinary state of a fresh install. It must not read as a failure,
        or the panel shows a red banner to every operator on day one.
        """
        assert call_history.load() == []
        assert call_history.count() == 0

    def test_a_disabled_recorder_has_nothing_to_archive(self):
        """
        What an `Agent` built outside a call loop holds. `None`, not an empty
        dict -- writing a row for a call that never happened is worse than
        writing nothing, and the caller in `conversation.py` checks.
        """
        assert CallRecorder.disabled().record() is None


# =============================================================================
# THE OUTCOME
# =============================================================================

class TestTheStatus:
    """
    Three coarse outcomes, derived rather than tracked. None of them is a
    state the pipeline is ever *in* -- they are readings of how it finished.
    """

    def test_a_call_with_speech_is_completed(self, monitor):
        recorder = a_call(monitor)
        recorder.caller_said("Hello?")
        recorder.ended("the call ended")

        assert recorder.record()["status"] == COMPLETED

    def test_a_call_where_nobody_spoke_is_missed(self, monitor):
        recorder = a_call(monitor)
        recorder.ended("the call ended")

        assert recorder.record()["status"] == MISSED

    def test_a_call_that_never_got_a_start_frame_is_failed(self, monitor):
        """
        Never identified means the carrier's `start` never arrived -- a
        transport failure. Calling that "missed" would blame the person who
        did not answer a phone that never rang.
        """
        recorder = monitor.begin(persona="candidate", direction="outbound")
        recorder.ended("the call ended")

        assert recorder.record()["status"] == FAILED

    def test_a_loop_failure_is_failed_even_with_a_transcript(self, monitor):
        recorder = a_call(monitor)
        recorder.caller_said("Hello?")
        recorder.ended("the call loop failed — see the server log")

        assert recorder.record()["status"] == FAILED


# =============================================================================
# THE DURATION
# =============================================================================

class TestTheDuration:
    def test_the_clock_stops_when_the_call_ends(self, monitor):
        """
        🔴 Without the freeze, `elapsed_ms` keeps counting for as long as the
        process stays up, so a two-minute call archived during a slow teardown
        is recorded as however long ago it happened to be written. Teardown is
        not instantaneous -- the TTS pool, Deepgram and the trace all close
        between `ended()` and the write.
        """
        recorder = a_call(monitor)
        recorder.caller_said("Hello?")
        recorder.ended("the call ended")

        first = recorder.record()["durationSeconds"]
        # Stand in for the rest of teardown.
        import time

        time.sleep(0.05)
        second = recorder.record()["durationSeconds"]

        assert first == second

    def test_a_live_call_still_reports_a_running_duration(self, monitor):
        """The freeze must not break the live view, which shares the field."""
        recorder = a_call(monitor)
        assert monitor.snapshot()["durationMs"] >= 0


# =============================================================================
# THE ARCHIVE IS NOT THE RING
# =============================================================================

class TestTheArchiveIsNotTheRing:
    def test_a_long_call_keeps_its_opening_turns(self, log_path):
        """
        🔴 The whole reason the archive is a separate list.

        The ring holds 200 events across all calls. A call longer than that
        has evicted its own first question by the time it ends, so an archive
        derived from the buffer at teardown would save the tail of a
        conversation and call it the conversation.
        """
        monitor = CallMonitor(max_events=10)
        recorder = a_call(monitor)

        for index in range(40):
            recorder.caller_said(f"question {index}")
            recorder.agent_said(f"answer {index}", turn=index + 1)

        recorder.ended("the call ended")
        call_history.append(recorder.record())

        [stored] = call_history.load()
        assert stored["transcript"][0]["text"] == "question 0"
        assert len(stored["transcript"]) == 80

        # The live view, meanwhile, has kept only the tail -- which is correct
        # for it and is exactly why the two are separate.
        assert len(monitor.snapshot()["events"]) <= 10

    def test_one_runaway_call_cannot_grow_without_bound(self, monitor):
        recorder = a_call(monitor)
        for index in range(MAX_ARCHIVED_TURNS + 50):
            recorder.caller_said(f"line {index}")
        recorder.ended("the call ended")

        assert len(recorder.record()["transcript"]) == MAX_ARCHIVED_TURNS

    def test_a_barged_in_answer_is_flagged(self, monitor):
        """
        The text is the whole generation, not the part the caller heard (Bug
        A is not solved). The flag is what stops the panel presenting an
        unheard sentence as something the twin said.
        """
        recorder = a_call(monitor)
        recorder.agent_said("I was in the middle of—", turn=1, interrupted=True)
        recorder.ended("the call ended")

        assert recorder.record()["transcript"][0]["interrupted"] is True

    def test_a_caller_line_carries_no_interrupted_flag(self, monitor):
        """
        Barge-in is something the caller does, not something done to them. A
        `false` there would read as a claim about the caller.
        """
        recorder = a_call(monitor)
        recorder.caller_said("Hello?")
        recorder.ended("the call ended")

        assert "interrupted" not in recorder.record()["transcript"][0]


# =============================================================================
# THE FILE FORMAT
# =============================================================================

class TestTheFileFormat:
    def test_a_torn_final_line_costs_one_record_and_no_more(self, log_path, monitor):
        """
        🔴 The reason this is JSONL and the reason `load` skips.

        A writer killed mid-append leaves half a line. Refusing to serve the
        other records because of it turns a lost row into a broken screen.
        """
        for index in range(3):
            recorder = a_call(monitor, call_id=f"MZ-{index}")
            recorder.caller_said("hello")
            recorder.ended("the call ended")
            call_history.append(recorder.record())

        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write('{"id": "call-4", "callId": "MZ-tor')

        assert [row["callId"] for row in call_history.load()] == [
            "MZ-2",
            "MZ-1",
            "MZ-0",
        ]

    def test_a_write_after_a_torn_line_is_not_lost_with_it(self, log_path, monitor):
        """
        🔴 A torn tail must cost the torn record and **not the next one**.

        Without healing the missing newline, the next append lands on the same
        line and fuses the two into one unparseable record -- so a crash costs
        two rows, and the one lost is the *live* one somebody is waiting on
        rather than the historical one. That contradicts the guarantee the file
        format exists to provide.

        Found by W5d: a revision lost this way is a push notification that never
        arrives, which is a failure with no symptom at all.
        """
        recorder = a_call(monitor, call_id="MZ-before")
        recorder.ended("the call ended")
        call_history.append(recorder.record())

        # A writer killed mid-append: half a line, no terminating newline.
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write('{"id": "att-torn", "status": "ringi')

        # The process restarts and records the next thing that happens.
        call_history.append(
            call_history.revision("att-after", status="missed", callId="MZ-after")
        )

        recovered = [row.get("callId") for row in call_history.load()]
        assert "MZ-after" in recovered, "the record written after the tear was lost"
        assert "MZ-before" in recovered

    def test_the_file_is_one_json_object_per_line(self, log_path, monitor):
        recorder = a_call(monitor)
        recorder.caller_said("hello")
        recorder.ended("the call ended")
        call_history.append(recorder.record())

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["callId"] == "MZ-1"

    def test_line_endings_are_lf_on_every_platform(self, log_path, monitor):
        """
        CLAUDE.md §5: the dev machine is Windows and the target is Linux. A
        file that differs byte-for-byte between them is a diff nobody can
        read.
        """
        recorder = a_call(monitor)
        recorder.ended("the call ended")
        call_history.append(recorder.record())

        assert b"\r\n" not in log_path.read_bytes()

    def test_a_trim_keeps_the_newest_and_drops_the_oldest(
        self, log_path, monitor, monkeypatch
    ):
        monkeypatch.setattr(call_history, "TRIM_ABOVE_BYTES", 1)
        monkeypatch.setattr(call_history, "MAX_RECORDS", 2)

        for index in range(4):
            recorder = a_call(monitor, call_id=f"MZ-{index}")
            recorder.caller_said("hello")
            recorder.ended("the call ended")
            call_history.append(recorder.record())

        assert [row["callId"] for row in call_history.load()] == ["MZ-3", "MZ-2"]


# =============================================================================
# IT CANNOT COST A CALL
# =============================================================================

class TestItCannotCostACall:
    def test_an_unwritable_path_does_not_raise(self, tmp_path, monkeypatch, monitor):
        """
        The write is in the call loop's `finally`. An exception there would
        propagate out of teardown, and a call log that can break a hangup is
        worse than no call log.
        """
        # A directory where the file should be: `open(..., "a")` fails.
        blocked = tmp_path / "call_history.jsonl"
        blocked.mkdir()
        monkeypatch.setenv("SHUO_CALL_HISTORY_PATH", str(blocked))

        recorder = a_call(monitor)
        recorder.ended("the call ended")

        call_history.append(recorder.record())  # must not raise

    def test_an_unreadable_log_is_an_empty_one(self, tmp_path, monkeypatch):
        blocked = tmp_path / "call_history.jsonl"
        blocked.mkdir()
        monkeypatch.setenv("SHUO_CALL_HISTORY_PATH", str(blocked))

        assert call_history.load() == []
        assert call_history.count() == 0

    def test_it_imports_nothing_from_the_pipeline(self):
        """
        The config API reads this file directly (decision 32's seam, with the
        direction reversed). That only stays true while this module depends on
        nothing that could drag the audio pipeline into :3041.
        """
        import shuo.call_history as module

        imported = {
            name
            for name, value in vars(module).items()
            if getattr(value, "__name__", "").startswith(("shuo", "."))
            or name in ("json", "os", "Path")
        }
        assert "conversation" not in imported
        assert "call_monitor" not in imported


# =============================================================================
# END TO END THROUGH THE REAL LOOP
# =============================================================================

class TestARealCallIsRecorded:
    """
    Driven through `run_conversation`, because the thing that breaks is the
    *wiring*. A history module with perfect unit tests and no call to it in
    teardown is an empty call-log screen that nothing reports as broken.
    """

    @pytest.mark.asyncio
    async def test_a_call_lands_in_the_log(self, log_path, monkeypatch):
        import shuo.conversation as conv
        from shuo.call_monitor import MONITOR
        from shuo.carrier import get_carrier, reset_carrier_cache
        from shuo.types import CallContext, CallDirection

        from tests.test_integration import (
            MULAW_SILENCE_FRAME,
            StubFlux,
            StubTTSPool,
            VobizProtocol,
        )

        for key, value in {
            "CARRIER": "vobiz",
            "VOBIZ_AUTH_ID": "MA_TEST",
            "VOBIZ_AUTH_TOKEN": "test-auth-token",
            "VOBIZ_PHONE_NUMBER": "+911234567890",
            "PUBLIC_URL": "https://shuo.test",
            "PERSONA": "candidate",
            "GROQ_API_KEY": "test-key-not-used",
            "RECORD_CALLS": "false",
        }.items():
            monkeypatch.setenv(key, value)

        reset_carrier_cache()
        StubFlux.instances.clear()
        StubTTSPool.instances.clear()

        class DrainingWebSocket:
            def __init__(self, frames):
                self._frames = list(frames)
                self.sent = []

            async def receive_text(self):
                if self._frames:
                    await asyncio.sleep(0)
                    return json.dumps(self._frames.pop(0))
                await asyncio.sleep(0.1)
                raise WebSocketDisconnect(code=1006)

            async def send_text(self, text):
                self.sent.append(json.loads(text))

        class SpeakingFlux(StubFlux):
            async def send(self, audio_bytes):
                await super().send(audio_bytes)
                if len(self.fed) == 1:
                    await self.on_end_of_turn("So, tell me about yourself.")

        class QuietAgent:
            def __init__(self, **kwargs):
                self.recorder = kwargs.get("recorder")

            async def start_turn(self, transcript):
                self.recorder.agent_said("Sure, ji.", turn=1)
                self.recorder.timing("llm_first_token", 300, turn=1)

            async def cancel_turn(self):
                pass

            async def cleanup(self):
                pass

        MONITOR.reset()
        monkeypatch.setattr(conv, "FluxService", SpeakingFlux)
        monkeypatch.setattr(conv, "TTSPool", StubTTSPool)
        monkeypatch.setattr(conv, "Agent", QuietAgent)

        ws = DrainingWebSocket([
            VobizProtocol.start("s1", "MZ-live"),
            VobizProtocol.media("s1", MULAW_SILENCE_FRAME, seq=1, chunk=1),
        ])
        ctx = CallContext(
            call_id="",
            direction=CallDirection.OUTBOUND,
            persona_id="candidate",
            carrier="vobiz",
        )

        try:
            await asyncio.wait_for(
                conv.run_conversation(ws, ctx, get_carrier("vobiz")), timeout=5.0
            )
        finally:
            reset_carrier_cache()

        [stored] = call_history.load()

        assert stored["callId"] == "MZ-live", "the call never reached the log"
        assert stored["persona"] == "candidate"
        assert stored["direction"] == "outbound"
        assert stored["status"] == COMPLETED
        assert stored["config"], "the provenance line was not archived with the call"
        assert [turn["text"] for turn in stored["transcript"]] == [
            "So, tell me about yourself.",
            "Sure, ji.",
        ]
        assert stored["milestones"][0]["name"] == "llm_first_token"

        # The live view is unchanged by any of this.
        assert MONITOR.snapshot()["state"] == ENDED
