"""
Operator-editable runtime configuration.

The store the Next.js control panel writes and the agent will read. Two
halves, deliberately separated:

    models.py   the shapes -- reconciled against the frontend's contract
    store.py    persistence -- one JSON document, replaced atomically
    voices.py   the voice catalogue -- measured, and the only IDs that save

**This package has no dependency on the audio pipeline, and the audio
pipeline has no dependency on it.** That is the whole architectural point:
configuration is a slow, blocking, disk-bound concern and the streaming loop
is a 20ms deadline, so they do not share a process, a loop, or an import.

Phase 2 (reader wiring) needs only:

    from shuo.config_store import ConfigStore

    document = ConfigStore().load()          # never raises
    document.resolved_system_prompt          # str | None
    document.resolved_voice_model            # str | None
    document.resolved_persona_rules          # str, defaults to the ruleset
    document.resolved_knowledge              # str

Load it once at call setup, not per turn -- a file read inside the streaming
chain is exactly the kind of blocking work this split exists to keep out.

`resolved_voice_model` is a catalogue ID, not a provider one. Turn it into
something the synthesiser accepts, and fail loudly if it will not:

    from shuo.config_store import resolve_voice

    voice = resolve_voice(document.resolved_voice_model)
    if voice is None or not voice.available:
        raise ...                  # before the call connects, not during it
    voice.provider_voice_id        # e.g. "JBFqnCBsd6RMkjVDRZzb"
"""

from .models import (
    CONFIG_VERSION,
    DEFAULT_PERSONA_RULES,
    MAX_KNOWLEDGE_CHARS,
    MAX_PERSONA_RULES_CHARS,
    MAX_SYSTEM_PROMPT_CHARS,
    MAX_VOICE_MODEL_CHARS,
    AgentConfig,
    AgentSection,
    ConfigDocument,
    KnowledgeConfig,
    KnowledgeSection,
    PersonaConfig,
    PersonaSection,
)
from .store import ConfigStore, default_config_path
from .voices import (
    ELEVENLABS,
    VOICE_CATALOGUE,
    Voice,
    rejection_reason,
    resolve_voice,
    selectable_voices,
)

__all__ = [
    "CONFIG_VERSION",
    "ELEVENLABS",
    "VOICE_CATALOGUE",
    "Voice",
    "rejection_reason",
    "resolve_voice",
    "selectable_voices",
    "DEFAULT_PERSONA_RULES",
    "MAX_KNOWLEDGE_CHARS",
    "MAX_PERSONA_RULES_CHARS",
    "MAX_SYSTEM_PROMPT_CHARS",
    "MAX_VOICE_MODEL_CHARS",
    "AgentConfig",
    "AgentSection",
    "ConfigDocument",
    "ConfigStore",
    "KnowledgeConfig",
    "KnowledgeSection",
    "PersonaConfig",
    "PersonaSection",
    "default_config_path",
]
