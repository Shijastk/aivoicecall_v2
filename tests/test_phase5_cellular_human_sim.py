from __future__ import annotations

import argparse
import asyncio
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "16_phase5_cellular_human_sim.py"
SPEC = importlib.util.spec_from_file_location("phase5_cellular_human_sim", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_phone_validation_is_e164_only():
    assert MOD.validate_phone("+919876543210") == "+919876543210"
    assert MOD.validate_phone("+91 98765-43210") == "+919876543210"
    with pytest.raises(argparse.ArgumentTypeError):
        MOD.validate_phone("9876543210")
    with pytest.raises(argparse.ArgumentTypeError):
        MOD.validate_phone("+0123456789")


def test_phase5_duration_is_hard_capped_at_five_minutes():
    assert MOD.validate_duration("30") == 30.0
    assert MOD.validate_duration("300") == 300.0
    with pytest.raises(argparse.ArgumentTypeError):
        MOD.validate_duration("301")
    with pytest.raises(argparse.ArgumentTypeError):
        MOD.validate_duration("29")


def test_public_url_preserves_path_and_converts_websocket_scheme():
    assert (
        MOD._public_url("https://example.test", "/x?token=a", websocket=True)
        == "wss://example.test/x?token=a"
    )
    assert (
        MOD._public_url("http://127.0.0.1:3040/", "x")
        == "http://127.0.0.1:3040/x"
    )
    with pytest.raises(RuntimeError):
        MOD._public_url("ftp://example.test", "x")


def test_mulaw_frames_are_20ms_and_pad_only_final_frame_with_mulaw_silence():
    audio = bytes(range(160)) + b"abc"
    frames = MOD._frames(audio)
    assert len(frames) == 2
    assert all(len(frame) == 160 for frame in frames)
    assert frames[0] == bytes(range(160))
    assert frames[1][:3] == b"abc"
    assert frames[1][3:] == bytes([0xFF]) * 157

    with pytest.raises(RuntimeError):
        MOD._frames(b"")


def test_semantic_match_is_word_based_and_supports_alternatives():
    assert MOD._has_any("The code was blue seven.", (("blue", "seven"), ("blue", "7")))
    assert MOD._has_any("It is 5.", (("five",), ("5",)))
    assert MOD._has_any("I don't know that detail.", (("do", "not", "know"),))
    assert not MOD._has_any("I do not remember it.", (("blue", "seven"),))


def test_sanitized_report_removes_transcript_bearing_responses():
    report = {
        "raw": {
            "private_responses": {"turn": "private transcript"},
            "scenario_error": None,
            "observations": [],
        },
        "checks": [],
    }
    clean = MOD._sanitise(report)
    assert "private_responses" not in clean["raw"]
    assert "private_responses" in report["raw"]


@pytest.mark.asyncio
async def test_reference_knobs_fail_before_runner_or_live_side_effects(monkeypatch):
    class ExplodingRunner:
        def __init__(self, _):
            raise AssertionError("Runner must not be constructed for invalid locked knobs")

    monkeypatch.setattr(MOD, "Runner", ExplodingRunner)

    bad_eot = SimpleNamespace(
        eot_threshold=0.7,
        latency="120ms",
        thinking_pause=1.0,
        barge_delay=0.7,
    )
    with pytest.raises(RuntimeError, match="EOT 0.8"):
        await MOD._run(bad_eot)

    bad_latency = SimpleNamespace(
        eot_threshold=0.8,
        latency="80ms",
        thinking_pause=1.0,
        barge_delay=0.7,
    )
    with pytest.raises(RuntimeError, match="latency 120ms"):
        await MOD._run(bad_latency)


def test_output_directory_uses_platform_tempdir():
    assert MOD.DEFAULT_OUT_DIR == Path(tempfile.gettempdir()) / "shuo"


def test_harness_source_preserves_phase5_call_control_and_raw_audio_rules():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "carrier.hangup(" not in source
    assert "validate_signature(" in source
    assert "_authenticate_carrier" in source
    assert "org.ofono" not in source
    assert "busctl" not in source
    assert "write_bytes(" not in source
    assert 'Path("/tmp' not in source
    assert "time.monotonic" not in source
    assert "time.perf_counter" in source
    assert 'sys.platform != "linux"' in source
    assert 'shutil.which' in source
    assert "await self.session.checkpoint" not in source
    assert 'record=False' in source
    assert "MAX_LIVE_SECONDS = 300.0" in source
