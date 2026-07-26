"""
What the twin runs with on this call.

The **reader** half of the configuration seam. `shuo/config_store/` is
written by the operator's control panel in a separate process (context.md
decision 32), and this module is the only place the *audio* process reads it
back. Everything the panel can set arrives through `load_call_settings` and
leaves as the two values the pipeline actually consumes: one system prompt
and one provider voice ID.

Assembly lives here rather than in `agent.py` for two reasons. Prompt
composition is policy and `agent.py` is pipeline -- mixing them means the
streaming chain grows opinions about markdown headings. And Phase 6.5
(`shuo/persona/`) has to merge persona defaults with operator overrides at
exactly this point, so there should be one seam for it to land on, not two.

Three rules this module keeps:

- **Called once per call, at call setup, never per turn.** A prompt that can
  change between turns is a twin that contradicts what it said three turns
  ago -- failure mode 3 in CLAUDE.md, caused by our own plumbing. The
  snapshot also means an operator saving mid-call cannot alter the
  conversation already in progress.
- **Called before the TTS pool warms.** ElevenLabs binds the voice into the
  `stream-input` URL at connect time, and `TTSPool` pre-connects. Read the
  config after the pool has started and turn 1 speaks in the environment's
  voice while the panel shows the operator's pick -- which looks like the
  config API failing to save.
- **Never raises.** `ConfigStore.load` already degrades every failure to "no
  configuration", and the rest of this module is wrapped to match. A
  malformed config file must cost the twin its personality, not its call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config_store import ConfigStore, rejection_reason, resolve_voice
from .log import get_logger
from .services.llm import SYSTEM_PROMPT as BUILTIN_SYSTEM_PROMPT
from .services.tts import env_voice_id

logger = get_logger("shuo.runtime_config")


# Headings the operator's three fields are joined under. Markdown in a spoken
# prompt looks odd, but it is addressed to the model rather than spoken by it,
# and a labelled section is markedly harder for the model to blur into the
# prose above it than a bare newline join is. The built-in prompt already
# tells the model not to *speak* markdown.
CONSTRAINTS_HEADING = "## Constraints"
KNOWLEDGE_HEADING = "## What you know about yourself"


def assemble_system_prompt(
    *, base: str, rules: str, knowledge: str
) -> str:
    """
    Join the operator's three fields into one system message.

    Order is deliberate and fixed: **prompt, then constraints, then facts.**
    The prompt establishes who the twin is, the constraints bound how it may
    speak, and the fact block is the material it is allowed to draw on. Facts
    go last because that is the block most likely to be long, and burying the
    behavioural rules underneath a 50,000-character résumé is how the rules
    stop being followed.

    One message rather than three: history is appended after it, and a
    multi-message preamble puts the operator's fields at varying distances
    from the conversation depending on which of them happen to be empty.

    Empty sections contribute nothing -- not an empty heading. An operator who
    cleared the knowledge field asked for no knowledge block, and a bare
    "## What you know about yourself" followed by nothing reads to the model
    as "you know nothing about yourself", which is a different instruction.
    """
    blocks = [base.strip()]

    if rules.strip():
        blocks.append(f"{CONSTRAINTS_HEADING}\n{rules.strip()}")

    if knowledge.strip():
        blocks.append(f"{KNOWLEDGE_HEADING}\n{knowledge.strip()}")

    return "\n\n".join(block for block in blocks if block)


@dataclass(frozen=True)
class CallSettings:
    """
    One call's resolved configuration.

    Frozen because it is a snapshot by definition -- something that mutated
    mid-call would defeat the whole point of taking it once. The provenance
    fields (`prompt_source`, `voice_source`) exist only for `describe`, and
    they are not decoration: without them the answer to "did my save apply?"
    has to be guessed from how the twin sounded.
    """

    system_prompt: str
    voice_id: str

    # Provenance, for the one log line at call setup.
    prompt_source: str
    voice_source: str
    rules_chars: int
    knowledge_chars: int

    @classmethod
    def builtin(cls) -> "CallSettings":
        """
        What to run when there is no configuration to read.

        Pure: it touches the environment but never the disk, so it is safe to
        use as a default argument anywhere in the audio process -- including
        as `Agent`'s fallback, which must not perform I/O.

        Note this deliberately does *not* apply DEFAULT_PERSONA_RULES. This is
        the "we could not read the store at all" path, and the store is what
        owns that default; adding it here would mean two modules answering the
        same question differently. The ordinary unconfigured install goes
        through `load_call_settings`, reads a document with no persona
        section, and gets the ruleset from `resolved_persona_rules` as
        designed.
        """
        return cls(
            system_prompt=BUILTIN_SYSTEM_PROMPT,
            voice_id=env_voice_id(),
            prompt_source="built-in",
            voice_source="environment",
            rules_chars=0,
            knowledge_chars=0,
        )

    def describe(self) -> str:
        """A single line an operator can check a save against."""
        return (
            f"prompt={self.prompt_source} ({len(self.system_prompt)} chars, "
            f"rules {self.rules_chars}, knowledge {self.knowledge_chars})  "
            f"voice={self.voice_source} [{self.voice_id}]"
        )


def _resolve_voice_id(raw: Optional[str]) -> tuple[str, str]:
    """
    Turn the saved catalogue ID into a provider ID. Returns (id, provenance).

    Falls back to the environment on every failure rather than refusing the
    call. Decisions 29/31 already settled the ranking -- an off-accent voice
    is a bad turn, no audio is no conversation -- and this path exists
    precisely because the catalogue tracks ElevenLabs *entitlements*, which
    change under a config file that nobody touched. `AgentSection`'s validator
    is lax on read for the same reason: a voice that stopped being
    synthesisable must not take the system prompt down with it.
    """
    if not raw:
        return env_voice_id(), "environment"

    reason = rejection_reason(raw)
    if reason is None:
        voice = resolve_voice(raw)
        # `rejection_reason` returning None means this resolved and is
        # available, so both of these hold. Checked rather than asserted
        # because the cost of being wrong is an empty voice ID in a URL, and
        # the symptom of that is a silent call.
        if voice is not None and voice.provider_voice_id:
            return voice.provider_voice_id, f"{voice.name} ({voice.id})"
        reason = "resolved to a catalogue entry with no provider voice ID"

    logger.error(
        f"Configured voice {raw!r} {reason}. Falling back to the environment "
        f"voice so the call still connects -- the twin will speak in the "
        f"wrong voice until this is fixed in the panel."
    )
    return env_voice_id(), f"environment (fallback from {raw!r})"


def load_call_settings(store: Optional[ConfigStore] = None) -> CallSettings:
    """
    Read the config store and resolve it into this call's settings.

    **Synchronous, and call it synchronously.** `ConfigStore.load` is one file
    open and one `json.loads` -- no driver, no connection, no lock -- which is
    the whole reason config is a JSON document and not SQLite (decision 33).
    Tens of microseconds once per call, before any audio is flowing, is
    nowhere near the 20ms player deadline; that deadline only binds once
    playback has started.

    Do **not** wrap this in `asyncio.to_thread` inside the call path. It was
    tried: the await it introduces is a cancellation point between accepting
    the media socket and building the `Agent`, so a call that ends in that
    window tears down with no settings resolved at all. The thread hop buys
    nothing a 1.5KB read needs and costs the one thing this must not have.

    Never raises. A configuration that cannot be read or resolved costs the
    twin its personality for that call, and nothing more.
    """
    try:
        document = (store or ConfigStore()).load()

        configured_prompt = document.resolved_system_prompt
        rules = document.resolved_persona_rules
        knowledge = document.resolved_knowledge

        voice_id, voice_source = _resolve_voice_id(document.resolved_voice_model)

        return CallSettings(
            system_prompt=assemble_system_prompt(
                base=configured_prompt or BUILTIN_SYSTEM_PROMPT,
                rules=rules,
                knowledge=knowledge,
            ),
            voice_id=voice_id,
            prompt_source="operator" if configured_prompt else "built-in",
            voice_source=voice_source,
            rules_chars=len(rules),
            knowledge_chars=len(knowledge),
        )

    except Exception as exc:
        # `load` is documented never to raise and `_resolve_voice_id` handles
        # its own failures, so arriving here means something changed. It still
        # must not cost a call: the twin takes it on built-in defaults and
        # says so loudly enough to find in the log afterwards.
        logger.error(
            f"Could not resolve configuration ({exc}) — taking this call on "
            f"built-in defaults"
        )
        return CallSettings.builtin()
