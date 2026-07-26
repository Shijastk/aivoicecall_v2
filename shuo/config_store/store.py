"""
Persistence for operator-editable configuration.

A single JSON document, replaced atomically. Chosen over SQLite for one
reason that matters more than any other consideration here: **Phase 2's
reader lives in the audio process.** Whatever this module writes, the agent
has to read while a call is in flight, and the read path needs to be a file
open, a `json.loads` and nothing else -- no driver, no connection, no
lock held anywhere near the 20ms player deadline.

The write path is deliberately synchronous. It blocks whatever loop calls it,
and that is safe *only* because the process calling it never carries audio
(see `shuo/config_api.py`). Do not import this into `conversation.py` or
`agent.py` and call `save_*` from a running call.

Design notes worth keeping:

- **`os.replace`, not `open(path, "w")`.** A reader that catches a truncated
  write sees a syntax error instead of a config, and a reader in the audio
  process must never be handed one. `os.replace` is atomic on Windows too
  (`MoveFileExW` + `MOVEFILE_REPLACE_EXISTING`), so a reader observes either
  the whole old document or the whole new one.
- **The replace is retried.** On Windows `os.replace` raises `PermissionError`
  if the destination is open by another process, which is exactly what the
  agent reading config looks like. The window is microseconds and the retry
  closes it; without it, a save fails with an opaque 500 whenever a call
  happens to be starting. This is the same class of dev-machine-only trap as
  Bug C and decision 20, so it is handled rather than discovered.
- **`load` never raises.** A missing, empty, truncated, hand-mangled or
  future-versioned file all resolve to "nothing configured". The agent must
  not lose a call because a config file is malformed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from ..log import get_logger
from .models import (
    CONFIG_VERSION,
    AgentConfig,
    AgentSection,
    ConfigDocument,
    KnowledgeConfig,
    KnowledgeSection,
    PersonaConfig,
    PersonaSection,
    stamp_agent,
    stamp_knowledge,
    stamp_persona,
)

logger = get_logger("shuo.config_store")


# `parents[2]` from shuo/config_store/store.py is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    """
    Where the configuration lives.

    Deliberately *not* under `tempfile.gettempdir()` the way traces are: a
    trace is disposable and this is not. Config has to survive a restart, so
    it goes in a persistent directory next to the code, overridable for
    deploys that mount a volume.

    Resolved through `pathlib` rather than a literal string, because a
    hardcoded POSIX path here would be Bug C again.
    """
    override = os.getenv("SHUO_CONFIG_PATH")
    if override:
        return Path(override)
    return _REPO_ROOT / "var" / "agent_config.json"


# How hard to try the atomic rename before giving up. Five attempts at 20ms
# covers a reader holding the file open across a scheduling hiccup; anything
# longer than that is a real problem (a file lock, a permissions error) and
# should surface as an error rather than a hang -- the panel is waiting on
# this request.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.02


class ConfigStore:
    """
    Read/write access to the configuration document.

    Cheap to construct and holds no open handles, so the reader in Phase 2
    can make one per call or one per process without caring which.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else default_config_path()

        # Serialises read-modify-write within this process. Uncontended in
        # practice -- the API's handlers are async and `_mutate` contains no
        # `await`, so two of them cannot be inside it at once -- but the
        # store is a plain object anyone can call from a thread, and a
        # lost update here is a setting that silently did not save.
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ── Read ────────────────────────────────────────────────────────

    def load(self) -> ConfigDocument:
        """
        Read the document. Never raises.

        Every failure -- absent, empty, truncated mid-write, hand-edited into
        invalid JSON, written by a future version -- resolves to an empty
        document, which `resolved_*` turns into "nothing configured". That is
        the correct degradation: the agent starts the call on its built-in
        defaults instead of not starting it.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # The ordinary state before the first save. Not a warning.
            return ConfigDocument()
        except OSError as exc:
            logger.warning(
                f"Could not read config at {self._path}: {exc} — "
                f"continuing with defaults"
            )
            return ConfigDocument()

        if not raw.strip():
            return ConfigDocument()

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                f"Config at {self._path} is not valid JSON ({exc}) — "
                f"continuing with defaults"
            )
            return ConfigDocument()

        if not isinstance(payload, dict):
            logger.warning(
                f"Config at {self._path} is not a JSON object — "
                f"continuing with defaults"
            )
            return ConfigDocument()

        version = payload.get("version")
        if version != CONFIG_VERSION:
            # Refuse to guess at a shape we do not know. A file from a newer
            # version read optimistically is how a stale prompt reaches a
            # live call while looking like it applied.
            logger.warning(
                f"Config at {self._path} has version {version!r}, expected "
                f"{CONFIG_VERSION} — continuing with defaults"
            )
            return ConfigDocument()

        try:
            return ConfigDocument.model_validate(payload)
        except Exception as exc:
            logger.warning(
                f"Config at {self._path} does not match the expected shape "
                f"({exc}) — continuing with defaults"
            )
            return ConfigDocument()

    # ── Write ───────────────────────────────────────────────────────

    def save_agent(self, config: AgentConfig) -> AgentSection:
        """Replace the agent section. Returns what was stored."""
        section = stamp_agent(config)
        self._replace_section("agent", section)
        return section

    def save_persona(self, config: PersonaConfig) -> PersonaSection:
        """Replace the persona section. Returns what was stored."""
        section = stamp_persona(config)
        self._replace_section("persona", section)
        return section

    def save_knowledge(self, config: KnowledgeConfig) -> KnowledgeSection:
        """Replace the knowledge section. Returns what was stored."""
        section = stamp_knowledge(config)
        self._replace_section("knowledge", section)
        return section

    # ── Internals ───────────────────────────────────────────────────

    def _replace_section(self, name: str, section: object) -> None:
        """
        Swap one section and rewrite the whole document.

        The document is re-read from disk inside the lock rather than from
        anything cached, so a save merges against the newest version on disk
        instead of over the top of a snapshot this object took earlier.

        Residual limit, stated rather than hidden: two *separate processes*
        both writing could still lose one section, because the read and the
        replace are not one operation across process boundaries. The
        deployment is a single API process, so this does not arise; closing
        it properly would need a lock file, and the POSIX/Windows fork that
        implies is exactly what CLAUDE.md §5 warns against carrying for a
        case that does not occur.
        """
        with self._lock:
            document = self.load()
            setattr(document, name, section)
            document.version = CONFIG_VERSION
            self._write(document)

        logger.info(f"Saved {name} config to {self._path}")

    def _write(self, document: ConfigDocument) -> None:
        """Serialise and atomically replace the file."""
        # `by_alias` so the file is camelCase -- the same shape the panel
        # sent, readable side by side with `lib/api/types.ts`.
        payload = json.dumps(
            document.model_dump(by_alias=True),
            ensure_ascii=False,
            indent=2,
        )
        _atomic_write(self._path, payload + "\n")


def _atomic_write(path: Path, payload: str) -> None:
    """
    Write `payload` to `path` so no reader can ever see a partial file.

    Write to a sibling temp file, flush, fsync, then rename over the target.
    The temp file is PID-suffixed so two processes cannot collide on it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        # newline="\n" so the file is byte-identical across the Windows dev
        # machine and the Linux target -- otherwise every save on Windows
        # shows as a whole-file diff.
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        last_error: Optional[OSError] = None
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                # Windows: the destination is open in another process. The
                # holder is almost certainly a reader that is about to close.
                last_error = exc
                if attempt < _REPLACE_ATTEMPTS - 1:
                    time.sleep(_REPLACE_BACKOFF_SECONDS)

        raise OSError(
            f"Could not replace {path} after {_REPLACE_ATTEMPTS} attempts: "
            f"{last_error}"
        )
    finally:
        # A failed replace leaves the temp file behind; a successful one has
        # already renamed it away and this is a no-op.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
