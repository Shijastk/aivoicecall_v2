"""
Operator-editable runtime configuration -- the shapes.

These are the three DTOs the Next.js control panel PUTs, plus the document
they are persisted into. Reconciled field-by-field against the frontend's
own declared contract in `shuo-frontend/lib/api/types.ts`, which carries a
`TODO(api)` asking for exactly that:

    AgentConfig     { voiceModel: string, systemPrompt: string }
    PersonaConfig   { rules: string }
    KnowledgeConfig { context: string }

The wire is camelCase, Python is snake_case, and the aliases below are the
only place that mapping lives. The on-disk document is written **by alias**,
so the file is a literal record of what the panel sent -- "what did the UI
actually save" is answerable with `cat`, and there is no second mapping to
drift out of step with this one.

Nothing here imports anything from the audio pipeline, and nothing in the
audio pipeline imports this yet. Phase 2 wires `agent.py` to read it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .voices import rejection_reason, resolve_voice


# =============================================================================
# LIMITS
# =============================================================================

# Character ceilings, mirrored from the frontend's FIELD_LIMITS. Its TODO asks
# us to confirm them; consider them confirmed, and consider *this* side the
# authority. The frontend copy is a pre-flight check that saves a round trip,
# not the enforcement point -- a Next.js Server Action compiles to a public
# POST, so its validation is advisory by construction.
MAX_SYSTEM_PROMPT_CHARS = 16_000
MAX_PERSONA_RULES_CHARS = 16_000
MAX_KNOWLEDGE_CHARS = 50_000

# The voice model is an identifier, not prose. ElevenLabs voice IDs are 20
# characters; this is loose enough for any provider's scheme and tight enough
# that a pasted essay is rejected as the mistake it is.
MAX_VOICE_MODEL_CHARS = 128


# =============================================================================
# THE ANTI-ROBOTIC RULESET
# =============================================================================

# The default persona ruleset, held here because **the backend is what
# actually runs**. The frontend seeds a textarea with its own copy
# (`lib/api/defaults.ts`) so an operator who never opens `/persona` still
# submits the constraints; but an install where nobody ever pressed Save at
# all must not fall back to *no* constraints. Robotic delivery is one of the
# four failure modes this project exists to hunt (CLAUDE.md §1), so "empty
# because nothing was configured" has to resolve to the ruleset, not to
# silence.
#
# The read path has since landed (`GET /v1/agent/persona`), and it resolves
# through the same `resolved_persona_rules` the agent reads -- so the panel is
# now *served* this text rather than seeding a textarea with its own copy. The
# frontend's `defaults.ts` copy survives as one thing only: what to show when
# this service cannot be reached at all, so a page load with the backend down
# does not present an empty ruleset as though it were the configuration.
# **Keep the two in step. This one is what runs.**
#
# The delivery paragraphs (length, structure, fillers, backchannels) are the
# answer to what real test calls sounded like: rigid single blocks, no
# hesitation, uniform length regardless of the question -- failure mode 4 in
# CLAUDE.md §1. They live here, in data, rather than in the response loop,
# because that is the layer that can change without touching the streaming
# chain. Response-gap sampling and true backchannel suppression are timing,
# not text, and belong to Part 5.
DEFAULT_PERSONA_RULES = """Never open with a customer-service greeting. Do not say "Can I help you", "How can I help you today", "Thank you for calling", or "Is there anything else I can assist you with". Nothing gives a bot away faster on a personal call.

Answer the way a person answers their own phone: a short hello, who you are, then stop and let them talk.

Never use: "certainly", "absolutely", "I'd be happy to", "as an AI", "please be advised", "at this time", "reach out", "apologies for the inconvenience".

Use contractions. "I'll", "don't", "that's" — never "I will not" or "do not".

Do not read out a menu of options. Do not repeat back what the caller just said before answering. Do not thank them for their patience.

Let the question set the length. A yes/no or a one-fact question gets one sentence and a full stop — do not pad it into a paragraph. An open question ("tell me about…", "walk me through…") gets a real answer, but told in beats the way a person tells it, not delivered as one block.

Never structure a spoken answer. No "firstly", "secondly", "there are three things", no summing up at the end. Nobody talks like that on the phone, and it is the fastest way to sound generated.

Start the way people start: "yeah, so…", "hmm", "right", "okay so". Break your own sentences where a real person would — restate, back up, correct yourself. "We used Postgres — well, Postgres, and Redis for the queue." Filler is not noise here; it is what makes a pause read as thinking rather than as lag.

While they are still talking, do not talk. No "mm-hm", no "I see", no agreeing noises over them — a backchannel that arrives as a whole turn is an interruption, not encouragement. Wait, then answer.

If you do not know something, say so plainly and stop. Do not fill the gap with a policy line.

Match the caller's register. If they are brief, be brief. If they want to chat, let them.

Only state that you are an assistant if you are asked directly. Then say it plainly and move on."""


# =============================================================================
# VALIDATION HELPERS
# =============================================================================

def _reject_if_too_long(label: str, value: str, limit: int) -> str:
    """
    Enforce a character ceiling, raising a sentence rather than a code.

    The message is rendered verbatim in the save row next to the button the
    operator pressed, so it has to read like something a person wrote.
    Phrasing and `,` digit grouping deliberately match the frontend's
    `checkLength`, so a value the panel would have rejected locally produces
    the same sentence when it is rejected here instead.
    """
    if len(value) > limit:
        raise ValueError(
            f"{label} is {len(value):,} characters — the limit is "
            f"{limit:,}. Nothing was saved."
        )
    return value


def _now_iso() -> str:
    """Timezone-aware UTC stamp. Naive datetimes are not worth debugging."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Wire(BaseModel):
    """
    Base for the three request bodies.

    `extra="forbid"` on purpose. A config API that silently drops a field it
    does not recognise is the worst possible failure here: the panel says
    "Saved", the operator believes the setting took effect, and the agent
    runs the old value on a live call. A 400 that names the unexpected field
    is a contract mismatch caught in a second.

    `populate_by_name` so tests and internal callers can construct these with
    Python names while the wire keeps its camelCase.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# =============================================================================
# REQUEST BODIES
# =============================================================================

class AgentConfig(_Wire):
    """`PUT /v1/agent/config` -- what the agent says, and how it sounds."""

    # An ID from the backend-owned catalogue in `voices.py`, validated
    # against it below. It used to be stored verbatim and unchecked, which
    # was open question 11: the panel's picker shipped page-local fixtures
    # (`el-rachel-v2`, `oa-alloy`, ...), none of which names a voice this
    # agent can speak with, and an unsynthesisable voice does not present as
    # an error -- decision 29 established it presents as a silent mid-call
    # hang. Validating here moves that failure from the call to the save.
    voice_model: str = Field(alias="voiceModel")

    system_prompt: str = Field(alias="systemPrompt")

    @field_validator("voice_model")
    @classmethod
    def _check_voice_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            # Matches the panel's own wording for the same rejection.
            raise ValueError("Pick a voice model before saving.")

        # Length first, so an accidentally-pasted essay is reported as the
        # wrong-length mistake it is rather than quoted back in full inside
        # an "unknown voice" message.
        _reject_if_too_long("The voice model ID", value, MAX_VOICE_MODEL_CHARS)

        reason = rejection_reason(value)
        if reason is not None:
            raise ValueError(f'"{value}" {reason}. Nothing was saved.')

        # Canonicalise. A raw provider ID (`JBFqnCBsd6RMkjVDRZzb`, copied
        # from the ElevenLabs dashboard) is accepted as input but stored as
        # its catalogue ID, so the file holds one representation of a voice
        # and W2 has one thing to resolve.
        voice = resolve_voice(value)
        assert voice is not None  # rejection_reason returned None
        return voice.id

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, value: str) -> str:
        # Ends trimmed, interior whitespace untouched. Same rule as the
        # panel's `readTextField`, and it matters for the same reason: these
        # are paragraph-separated prose fields and collapsing blank lines
        # would quietly rewrite what the operator wrote.
        return _reject_if_too_long(
            "The system prompt", value.strip(), MAX_SYSTEM_PROMPT_CHARS
        )


class PersonaConfig(_Wire):
    """
    `PUT /v1/agent/persona` -- the behavioural constraints.

    **Empty is allowed and must stay allowed.** The panel ships the textarea
    seeded with the ruleset, so an ordinary submit carries the whole thing;
    but an operator who selects all and deletes has made a decision, and the
    one thing this endpoint must never do is quietly write the default back
    over a field they deliberately cleared. That is why "never saved" and
    "saved as empty" are different states on disk (`ConfigDocument.persona`
    is `None` for the first, a section holding `""` for the second) and only
    the first resolves to `DEFAULT_PERSONA_RULES`.
    """

    rules: str

    @field_validator("rules")
    @classmethod
    def _check_rules(cls, value: str) -> str:
        return _reject_if_too_long(
            "The ruleset", value.strip(), MAX_PERSONA_RULES_CHARS
        )


class KnowledgeConfig(_Wire):
    """
    `PUT /v1/agent/knowledge` -- free-form reference context.

    Unstructured on purpose. "It fits" is the only validation there is,
    because anything stricter would be this layer inventing a schema for text
    that goes straight to the model.
    """

    context: str

    @field_validator("context")
    @classmethod
    def _check_context(cls, value: str) -> str:
        return _reject_if_too_long(
            "The context", value.strip(), MAX_KNOWLEDGE_CHARS
        )


# =============================================================================
# PERSISTED SECTIONS
# =============================================================================
#
# Each section is its request body plus the moment it was written. Subclassing
# rather than redeclaring keeps one definition of each field, and the
# timestamp is what makes a stale config diagnosable: the question after a
# surprising call is always "was this actually saved before that call".

class AgentSection(AgentConfig):
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")

    @field_validator("voice_model")
    @classmethod
    def _check_voice_model(cls, value: str) -> str:
        """
        Deliberately laxer than the request body it inherits from: **strict
        on write, permissive on read.**

        The parent's catalogue check is right for a PUT and wrong here. This
        class is also what `ConfigStore.load` validates the file *back*
        through, and a section that fails validation takes the whole document
        with it -- `load` catches the error and returns an empty
        `ConfigDocument`, so an unresolvable voice ID would silently discard
        the system prompt, the persona rules and the knowledge block along
        with it, and the agent would take the next call on built-in defaults
        having told nobody.

        That is not hypothetical. The catalogue tracks ElevenLabs
        *entitlements*, which change under us: an ID written when it was
        valid can stop being valid without the file being touched. When that
        happens the right outcome is a loud failure about the voice at call
        setup (`resolve_voice` returning None) with the rest of the config
        intact -- not a quiet reset of everything.
        """
        return value.strip()


class PersonaSection(PersonaConfig):
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")


class KnowledgeSection(KnowledgeConfig):
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")


def stamp_agent(config: AgentConfig) -> AgentSection:
    return AgentSection(
        voice_model=config.voice_model,
        system_prompt=config.system_prompt,
        updated_at=_now_iso(),
    )


def stamp_persona(config: PersonaConfig) -> PersonaSection:
    return PersonaSection(rules=config.rules, updated_at=_now_iso())


def stamp_knowledge(config: KnowledgeConfig) -> KnowledgeSection:
    return KnowledgeSection(context=config.context, updated_at=_now_iso())


# =============================================================================
# THE DOCUMENT
# =============================================================================

# Bumped only for a shape change the loader has to branch on. The loader
# treats an unrecognised version as unreadable and falls back to defaults
# rather than guessing, so a forward-incompatible file degrades to "nothing
# configured" instead of to garbage on a live call.
CONFIG_VERSION = 1


class ConfigDocument(BaseModel):
    """
    Everything the panel can set, as one file.

    One document rather than three files so a reader takes one snapshot and
    cannot observe a half-applied change -- prompt from before a save, rules
    from after. Each PUT rewrites the whole document with one section
    replaced (see `store.ConfigStore`).

    A `None` section means *never written*, which is not the same as written
    empty. `resolved_*` below is the only place that distinction turns into a
    value, so there is exactly one answer to "what does the agent run with".
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    version: int = CONFIG_VERSION
    agent: Optional[AgentSection] = None
    persona: Optional[PersonaSection] = None
    knowledge: Optional[KnowledgeSection] = None

    # ── The reader's view (what Phase 2 will consume) ─────────────────

    @property
    def resolved_system_prompt(self) -> Optional[str]:
        """
        The configured prompt, or `None` if none was ever saved.

        `None` rather than a built-in default: the caller in Phase 2 already
        has `llm.SYSTEM_PROMPT` and is the right place to decide whether to
        fall back to it. Inventing a prompt here would put words in the
        twin's mouth that no operator wrote.
        """
        if self.agent is None or not self.agent.system_prompt:
            return None
        return self.agent.system_prompt

    @property
    def resolved_voice_model(self) -> Optional[str]:
        """The configured voice ID, or `None` if none was ever saved."""
        if self.agent is None or not self.agent.voice_model:
            return None
        return self.agent.voice_model

    @property
    def resolved_persona_rules(self) -> str:
        """
        The constraints the agent should actually run under.

        The one asymmetric resolution in this file, and the reason the
        never-written/written-empty distinction is kept:

            section is None   nobody has configured this install
                              -> DEFAULT_PERSONA_RULES, because an
                                 unconfigured twin must not be a robotic one
            rules == ""       an operator cleared the field on purpose
                              -> "", because that is what they asked for
        """
        if self.persona is None:
            return DEFAULT_PERSONA_RULES
        return self.persona.rules

    @property
    def resolved_knowledge(self) -> str:
        """Reference context, empty when unset. No default is defensible
        here -- a fabricated fact block is precisely what the persona rules
        forbid (CLAUDE.md §4.1)."""
        if self.knowledge is None:
            return ""
        return self.knowledge.context
