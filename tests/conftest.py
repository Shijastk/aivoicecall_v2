"""
Shared test configuration.

Puts `scripts/` on the import path so the test suite can drive the same
fake-Vobiz protocol implementation that the CLI stub uses -- one model of
the wire format, not two that can drift apart.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

for path in (REPO_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _isolate_call_history(tmp_path, monkeypatch):
    """
    Keep the call log out of the repo's `var/`.

    Autouse and unconditional, because the write is not made by anything a
    test names: it happens in `run_conversation`'s teardown, so every test
    that drives the real loop -- test_integration, test_call_monitor,
    test_turn_completion -- archives a call whether it means to or not. That
    was found the ordinary way: 42 rows appeared in a developer's `var/`
    after a full run.

    `var/` is gitignored, so nothing was ever going to be committed. It still
    matters: those rows hold transcripts, and a test suite that quietly
    accumulates conversation text in a working directory is a test suite that
    will one day do it on a machine holding real ones.

    A test that wants to exercise the log sets the same variable itself
    (`tests/test_call_history.py`), which overrides this one for that test.
    """
    monkeypatch.setenv(
        "SHUO_CALL_HISTORY_PATH", str(tmp_path / "call_history.jsonl")
    )


@pytest.fixture(autouse=True)
def _isolate_recordings(tmp_path, monkeypatch):
    """
    Keep call recordings out of the repo's `var/` -- the same trap as the call
    log above, one turn of the screw worse.

    Found the same way, and immediately: the first full run after the tee was
    wired left a directory of WAVs in a developer's working tree. The write is
    made by nothing a test names -- `run_conversation` builds a tape, and every
    test that drives the real loop therefore records one -- so it had to be
    autouse and unconditional.

    Worse than the log because of what these are. A call log row is text about
    a call; a recording is **the audio of a conversation**, and a suite that
    accumulates those in a working directory is a suite that will one day do it
    on a machine holding real interviews.

    A test that wants to exercise recording sets the same variable itself
    (`tests/test_recording.py`), which overrides this for that test.
    """
    monkeypatch.setenv("SHUO_RECORDINGS_DIR", str(tmp_path / "recordings"))
