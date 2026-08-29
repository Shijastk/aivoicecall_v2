"""
A local stereo recording of every call, teed out of the pipeline.

The audio is already in memory in exactly the form we want it: the carrier
hands us the caller's µ-law and the player hands the carrier ours. Recording
is therefore not a capture problem, it is a **bookkeeping** problem -- copy two
byte streams somewhere and remember where in time each byte belonged.

    caller  conversation.py, at the FeedFluxAction dispatch  ->  left channel
    agent   player.py, in _send_frame before the base64      ->  right channel

Both are observers at a dispatch boundary, the same category of thing as
`Tracer` and `CallRecorder`: the state machine never learns this exists
(CLAUDE.md rule 1), and the token-level streaming chain is untouched
(rule 2). Nothing here is in the path of a frame reaching the caller.

### Why local, and not the carrier's recording

Vobiz can record, and `conversation.py` still starts that when `RECORD_CALLS`
is on -- it is the instrument for the three open unknowns on
`vobiz.start_recording`. But it is the wrong thing to *build the panel on*: the
file lives on the carrier, retrieving it needs carrier credentials in whichever
process fetches it, recording and storage are billable, and it captures
whatever the carrier's bridge captured rather than what this pipeline
actually sent. The tee costs nothing, works on any carrier, and records
exactly the bytes the twin produced.

### Four rules, and they are the whole design

- **The tee is one `bytearray.extend` and two integers.** No disk, no lock, no
  `await`, no allocation beyond the append. Same standard as
  `call_monitor._append`, and for the same reason: decision 24 makes a stall on
  that loop permanent stream delay, not one late frame.
- **Nothing is padded on the hot path either.** A turn that starts after ten
  seconds of listening needs ten seconds of silence in the agent track, and
  memsetting 80KB inside `_send_frame` would be a strange place to do it. Each
  flushed chunk instead carries *the offset it belongs at*, and the writer
  thread pads. The hot path never allocates more than the frame it was given.
- **Memory is bounded by flushing, not by a cap on the call.** Every ~5s of
  audio the buffer is handed to the spool and a fresh one started, so a
  45-minute interview holds the same ~80KB as a 20-second test call.
- **It never raises.** A recording that can end a call is worse than no
  recording. Every entry point swallows; a failed tee degrades to a shorter
  file, and a failed conversion to a row with `recording.available: false`.

### The timeline

The caller's byte count is the clock. The carrier streams inbound audio
continuously for the whole call -- `process_event` emits a `FeedFluxAction` for
every inbound media frame regardless of phase, because barge-in requires
listening while the twin speaks -- so the caller track's length *is* the call's
duration. The agent track is written at the caller offset current when each
run of frames began, and the gaps between are silence, which is exactly what
they were.

Stereo rather than mixed, caller left and agent right, because the reason to
listen to one of these is to hear who was talking over whom.
"""

from __future__ import annotations

import array
import os
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import call_history
from .log import get_logger
from .spool import SPOOL, Spool

logger = get_logger("shuo.recording")


# =============================================================================
# GEOMETRY AND LIMITS
# =============================================================================

# rules.md C3, and the same constants the player works in: one byte of 8kHz
# µ-law is 125µs, so a second is 8000 bytes.
SAMPLE_RATE = 8000
BYTES_PER_SECOND = SAMPLE_RATE

# G.711 µ-law digital silence, verified in `player.py` against
# `audioop.lin2ulaw(b"\x00" * 320, 2)`. Used to pad the gaps between turns,
# where the twin genuinely was silent.
MULAW_SILENCE = 0xFF

# How much audio accumulates before a chunk is handed to the spool. Five
# seconds is 40KB per track: small enough that a crash loses almost nothing,
# large enough that a call generates a handful of writes a minute rather than
# fifty a second.
FLUSH_BYTES = 5 * BYTES_PER_SECOND

# Ceiling on one call's recording. An hour matches
# VOBIZ_RECORDING_TIME_LIMIT's default, and exists for the stuck stream rather
# than for the long interview: past it the tee stops and the row says so,
# instead of a socket nobody closed filling a disk.
MAX_RECORDING_SECONDS = 3600
MAX_TRACK_BYTES = MAX_RECORDING_SECONDS * BYTES_PER_SECOND

# Retention. Pruned oldest-first after each finalise, on the spool's worker.
MAX_RECORDINGS = 200
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

# How much is decoded at a time when converting to WAV. Bounded so a
# 45-minute call does not materialise 90MB of PCM on the worker thread.
CONVERT_CHUNK_BYTES = 64 * 1024

_REPO_ROOT = Path(__file__).resolve().parents[1]


def local_recording_enabled() -> bool:
    """
    Whether to tee call audio to disk.

    Separate from `RECORD_CALLS`, which governs the *carrier's* recording, on
    purpose: they are different mechanisms with different costs, and an
    operator turning off the billable one should not lose the free one.
    """
    raw = os.getenv("SHUO_LOCAL_RECORDING", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def default_recordings_dir() -> Path:
    """
    Where recordings live. Next to the call log, for the same reasons.

    `parents[1]` rather than a literal POSIX path -- Bug C, and the same
    resolution `call_history` and `config_store` use.
    """
    override = os.getenv("SHUO_RECORDINGS_DIR")
    if override:
        return Path(override)
    return _REPO_ROOT / "var" / "recordings"


# Part files live in a subdirectory rather than beside the finished WAVs, so
# that "what recordings exist" is a directory listing and never has to know
# about half-written ones.
_PARTS_DIRNAME = ".parts"


def recording_path(call_ref: str, root: Optional[Path] = None) -> Optional[Path]:
    """
    The WAV for a call, or `None` if the reference could not name one.

    🔴 The only place a call id becomes a filesystem path, and therefore the
    only place that has to care that ids arrive from a carrier's URL and from a
    browser's address bar.

    Two independent checks, because either alone has been enough to be wrong
    somewhere: the reference must match the shape ids actually have, *and* the
    resolved path must still be inside the recordings directory. The second
    catches whatever the first did not think of.
    """
    reference = (call_ref or "").strip()
    if not reference or len(reference) > 64:
        return None
    # Belt: no separators, no traversal, no surprises.
    if not all(char.isalnum() or char in "-_" for char in reference):
        return None

    base = Path(root) if root is not None else default_recordings_dir()
    candidate = (base / f"{reference}.wav").resolve()

    # Braces: whatever the shape check missed, this catches.
    try:
        candidate.relative_to(base.resolve())
    except ValueError:  # pragma: no cover - unreachable given the shape check
        logger.warning(f"Refused a recording path outside the root: {reference!r}")
        return None

    return candidate


# =============================================================================
# THE TEE (call process, hot path)
# =============================================================================

class CallTape:
    """
    One call's two µ-law tracks on their way to disk.

    Held by `run_conversation` and handed to `Agent`, which hands it to each
    turn's `AudioPlayer` -- the same shape as `CallRecorder`. Every method is
    fire-and-forget: nothing returns a value the caller must check, and nothing
    raises.

    `CallTape.disabled()` no-ops on every call, so no tee site needs a guard.
    """

    __slots__ = (
        "_id",
        "_spool",
        "_root",
        "_caller",
        "_agent",
        "_caller_bytes",
        "_agent_offset",
        "_agent_bytes",
        "_closed",
        "_truncated",
        "_warned",
    )

    def __init__(
        self,
        call_ref: str,
        *,
        spool: Optional[Spool] = None,
        root: Optional[Path] = None,
    ):
        self._id = call_ref
        self._spool = spool if spool is not None else SPOOL
        self._root = Path(root) if root is not None else default_recordings_dir()

        self._caller = bytearray()
        self._agent = bytearray()
        self._caller_bytes = 0
        self._agent_offset = 0
        self._agent_bytes = 0
        self._closed = False
        self._truncated = False
        self._warned = False

    @classmethod
    def disabled(cls) -> "CallTape":
        """A tape that records nothing. What every site falls back to."""
        tape = cls.__new__(cls)
        tape._id = ""
        tape._spool = None
        tape._root = None
        tape._caller = None
        tape._agent = None
        tape._caller_bytes = 0
        tape._agent_offset = 0
        tape._agent_bytes = 0
        tape._closed = True
        tape._truncated = False
        tape._warned = False
        return tape

    @property
    def enabled(self) -> bool:
        return not self._closed and self._id != ""

    @property
    def seconds(self) -> int:
        return self._caller_bytes // BYTES_PER_SECOND

    # ── The two tee points ──────────────────────────────────────────

    def caller(self, audio: bytes) -> None:
        """
        Inbound µ-law, straight off the carrier. One `extend` and one add.

        Called from `conversation.py`'s `FeedFluxAction` branch, which runs for
        every inbound media frame in every phase -- which is what makes this
        track a usable clock for the other one.
        """
        if self._closed or not audio:
            return
        try:
            if self._caller_bytes >= MAX_TRACK_BYTES:
                self._truncate()
                return
            self._caller.extend(audio)
            self._caller_bytes += len(audio)
            if len(self._caller) >= FLUSH_BYTES:
                self._flush_caller()
        except Exception as exc:  # pragma: no cover - defensive
            self._warn(exc)

    def agent(self, frame: bytes) -> None:
        """
        One 20ms µ-law frame on its way to the carrier.

        Called from `AudioPlayer._send_frame`, before the base64 encode, so
        this sees the same bytes the caller will hear and pays nothing to get
        them.

        The offset is claimed when a buffer starts rather than per frame:
        within a turn the frames are contiguous, so one offset describes the
        whole run and the hot path never pads.
        """
        if self._closed or not frame:
            return
        try:
            if self._agent_bytes >= MAX_TRACK_BYTES:
                self._truncate()
                return
            if not self._agent:
                self._agent_offset = self._caller_bytes
            self._agent.extend(frame)
            self._agent_bytes += len(frame)
            if len(self._agent) >= FLUSH_BYTES:
                self._flush_agent()
        except Exception as exc:  # pragma: no cover - defensive
            self._warn(exc)

    # ── Teardown ────────────────────────────────────────────────────

    def close(self) -> None:
        """
        Flush what is left and queue the conversion to WAV.

        Called from the call loop's teardown. Both the flush and the
        conversion go through the spool, so this returns immediately and the
        decode happens on a worker thread while teardown carries on.

        The row is revised by the conversion job itself rather than here,
        because only it knows whether a file was produced -- and claiming a
        recording that does not exist gives the panel a play button that 404s.
        """
        if self._closed:
            return
        self._closed = True

        try:
            self._flush_caller()
            self._flush_agent()
            self._spool.submit(
                _finalise,
                self._id,
                self._root,
                truncated=self._truncated,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._warn(exc)

    # ── Internals ───────────────────────────────────────────────────

    def _flush_caller(self) -> None:
        if not self._caller:
            return
        payload = bytes(self._caller)
        self._caller.clear()
        # The caller track has no gaps by construction, so its offset is
        # simply where it left off.
        self._spool.submit(
            _write_part,
            self._root,
            self._id,
            "caller",
            self._caller_bytes - len(payload),
            payload,
        )

    def _flush_agent(self) -> None:
        if not self._agent:
            return
        payload = bytes(self._agent)
        self._agent.clear()
        offset = self._agent_offset
        self._agent_offset += len(payload)
        self._spool.submit(
            _write_part, self._root, self._id, "agent", offset, payload
        )

    def _truncate(self) -> None:
        if self._truncated:
            return
        self._truncated = True
        logger.warning(
            f"Recording for {self._id} hit the {MAX_RECORDING_SECONDS}s ceiling "
            f"and stopped. The call is unaffected; the recording is short."
        )

    def _warn(self, exc: Exception) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning(
            f"Could not tee audio for {self._id} ({exc!r}). The call is "
            f"unaffected; the recording will be incomplete."
        )


# =============================================================================
# THE WRITER (spool worker thread)
# =============================================================================

def _part_path(root: Path, call_ref: str, track: str) -> Path:
    return Path(root) / _PARTS_DIRNAME / f"{call_ref}.{track}.ulaw"


def _write_part(
    root: Path, call_ref: str, track: str, offset: int, payload: bytes
) -> None:
    """
    Append one chunk to a raw track, padding the gap before it. Never raises.

    Padding here rather than at the tee is the point of carrying an offset:
    the silence between two turns can be minutes long, and this is a thread
    that has nothing else to do.

    A chunk that claims an offset *behind* the file is appended where the file
    already is rather than seeking back. Small overlaps are possible when the
    two clocks disagree by a frame, and an overlap that overwrote would put a
    hole in audio that exists; a few milliseconds of drift will not be heard.
    """
    try:
        path = _part_path(root, call_ref, track)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "ab") as handle:
            written = handle.tell()
            gap = offset - written
            if gap > 0:
                handle.write(bytes([MULAW_SILENCE]) * gap)
            handle.write(payload)
    except Exception as exc:
        logger.warning(
            f"Could not write the {track} track for {call_ref} ({exc}). The "
            f"call is unaffected; the recording will be incomplete."
        )


# G.711 µ-law -> signed 16-bit PCM, precomputed once.
#
# A table rather than `audioop.ulaw2lin`, which was **removed in Python 3.13**
# along with the rest of the `audioop` module. The deploy target's interpreter
# is not something a recording feature should have an opinion about.
# `tests/test_recording.py` checks every one of the 256 entries against
# `audioop` where the interpreter still has it.
def _build_ulaw_table() -> List[int]:
    table = []
    for byte in range(256):
        value = ~byte & 0xFF
        magnitude = ((value & 0x0F) << 3) + 0x84
        magnitude <<= (value & 0x70) >> 4
        magnitude -= 0x84
        table.append(-magnitude if value & 0x80 else magnitude)
    return table


_ULAW_TO_PCM = _build_ulaw_table()


def _finalise(call_ref: str, root: Path, truncated: bool = False) -> None:
    """
    Turn the two raw tracks into one stereo WAV, then tidy up. Never raises.

    Runs on the spool's worker thread, after the last chunk of both tracks --
    the spool has one consumer, so "after" is guaranteed rather than hoped for.
    """
    caller_part = _part_path(root, call_ref, "caller")
    agent_part = _part_path(root, call_ref, "agent")
    target = recording_path(call_ref, root=root)

    if target is None:
        logger.warning(f"Refusing to write a recording for {call_ref!r}")
        return

    try:
        if not caller_part.exists() and not agent_part.exists():
            # A call that dropped before any audio flowed. Not a failure, and
            # not worth a row claiming a recording.
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        frames = _write_wav(caller_part, agent_part, target)

        for part in (caller_part, agent_part):
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

        size = target.stat().st_size
        seconds = round(frames / SAMPLE_RATE, 1)
        logger.info(
            f"Recorded {call_ref}  {seconds:g}s  {size / 1024:.0f}KB  -> {target.name}"
        )

        # The row learns about the recording only now, from the code that
        # actually produced the file.
        call_history.append(
            call_history.revision(
                call_ref,
                recording={
                    "available": True,
                    "seconds": seconds,
                    "bytes": size,
                    # A path on :3041, not on disk. The panel is a browser and
                    # has no filesystem; the API resolves this back to a file.
                    "url": f"/v1/calls/{call_ref}/recording",
                    **({"truncated": True} if truncated else {}),
                },
            )
        )

        _prune(Path(root))

    except Exception as exc:
        logger.warning(
            f"Could not finish the recording for {call_ref} ({exc}). The call "
            f"is unaffected; there will be no audio for it in the panel."
        )


def _write_wav(caller_part: Path, agent_part: Path, target: Path) -> int:
    """
    Interleave two µ-law tracks into one 8kHz 16-bit stereo WAV.

    Streamed in chunks rather than read whole: a 45-minute call is 21MB per
    track raw and 86MB as PCM, and materialising that on a worker thread to
    write a file would be a strange way to avoid blocking an event loop.

    The shorter track is padded with silence. They differ routinely -- the
    agent's ends when it stops speaking, the caller's when the line drops.

    The decode and the interleave both stay out of the interpreter: `map` over
    the lookup table and an extended-slice assignment on an `array` are C
    loops, where the obvious per-sample Python loop is not. A 45-minute call is
    21 million samples, and that difference is the difference between a
    conversion that finishes while teardown is still running and one that does
    not.
    """
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    frames = 0
    lookup = _ULAW_TO_PCM.__getitem__
    silence = bytes([MULAW_SILENCE])

    with open_or_empty(caller_part) as left, open_or_empty(agent_part) as right:
        with wave.open(str(tmp), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)

            while True:
                left_chunk = left.read(CONVERT_CHUNK_BYTES)
                right_chunk = right.read(CONVERT_CHUNK_BYTES)
                if not left_chunk and not right_chunk:
                    break

                span = max(len(left_chunk), len(right_chunk))
                interleaved = array.array("h", bytes(span * 4))
                interleaved[0::2] = array.array(
                    "h", map(lookup, left_chunk.ljust(span, silence))
                )
                interleaved[1::2] = array.array(
                    "h", map(lookup, right_chunk.ljust(span, silence))
                )

                # `array` is native-endian and WAV is little-endian. A no-op
                # on every machine this will run on, and correct on the one
                # where it is not.
                if sys.byteorder == "big":  # pragma: no cover - x86/arm are LE
                    interleaved.byteswap()

                out.writeframes(interleaved.tobytes())
                frames += span

    # Atomic, like the config store's: a reader in the *other* process must
    # never be handed a half-written WAV.
    os.replace(tmp, target)
    return frames


class _EmptyReader:
    """Stands in for a track that was never written. Reads as end-of-file."""

    def read(self, _size: int) -> bytes:
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def open_or_empty(path: Path):
    """The file, or something that reads empty. A one-sided call is normal."""
    try:
        return open(path, "rb")
    except OSError:
        return _EmptyReader()


def _prune(root: Path) -> None:
    """
    Keep the newest recordings within both budgets. Never raises.

    Two limits because they bind at different times: a machine making short
    test calls hits the count first, and one recording long interviews hits
    the bytes first.
    """
    try:
        files = sorted(
            (path for path in root.glob("*.wav") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return

    total = 0
    doomed: List[Path] = []
    for index, path in enumerate(files):
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if index >= MAX_RECORDINGS or total > MAX_TOTAL_BYTES:
            doomed.append(path)

    for path in doomed:
        try:
            path.unlink()
        except OSError:
            continue

    if doomed:
        logger.info(f"Pruned {len(doomed)} old recording(s) from {root}")


# =============================================================================
# READ (config process)
# =============================================================================

def describe(call_ref: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """
    What :3041 can say about a call's audio without opening it.

    Answers from the filesystem rather than from the call log, so a recording
    deleted by the retention budget stops being offered even though the row
    still remembers it -- a play button that 404s is worse than no button.
    """
    path = recording_path(call_ref, root=root)
    if path is None or not path.exists():
        return {"available": False}

    try:
        size = path.stat().st_size
    except OSError:
        return {"available": False}

    # 44-byte canonical header, 4 bytes per stereo 16-bit frame.
    seconds = round(max(0, size - 44) / (SAMPLE_RATE * 4), 1)
    return {
        "available": True,
        "bytes": size,
        "seconds": seconds,
        "url": f"/v1/calls/{call_ref}/recording",
    }
