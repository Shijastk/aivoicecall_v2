"""
Tests for the operator configuration store.

Two things carry most of the weight here, because both fail silently:

- **Never-written vs. written-empty.** If those collapse into one state, an
  operator who deliberately clears the persona rules gets the default written
  back over them on the next read, and the agent runs constraints they
  removed on purpose.
- **`load` never raising.** Phase 2 puts this read in the audio process. A
  malformed config file must cost the twin its configuration, not the call.
"""

import json
import os

import pytest

from shuo.config_store import (
    CONFIG_VERSION,
    DEFAULT_PERSONA_RULES,
    MAX_KNOWLEDGE_CHARS,
    MAX_PERSONA_RULES_CHARS,
    MAX_SYSTEM_PROMPT_CHARS,
    AgentConfig,
    ConfigStore,
    KnowledgeConfig,
    PersonaConfig,
)

# See the note on the same constant in `test_config_api.py`.
VOICE = "el-george"


@pytest.fixture
def store(tmp_path):
    """A store on a throwaway path, in a directory that does not exist yet."""
    return ConfigStore(tmp_path / "nested" / "agent_config.json")


# =============================================================================
# EMPTY STATE
# =============================================================================

class TestUnconfigured:
    def test_load_on_a_missing_file_returns_an_empty_document(self, store):
        document = store.load()

        assert document.agent is None
        assert document.persona is None
        assert document.knowledge is None

    def test_an_unconfigured_install_still_gets_the_anti_robotic_ruleset(self, store):
        """
        The load-bearing default. An install nobody has configured must not
        run with zero persona constraints -- robotic delivery is one of the
        four failure modes the project exists to hunt.
        """
        assert store.load().resolved_persona_rules == DEFAULT_PERSONA_RULES

    def test_prompt_and_voice_resolve_to_none_rather_than_an_invented_value(self, store):
        document = store.load()

        assert document.resolved_system_prompt is None
        assert document.resolved_voice_model is None

    def test_knowledge_resolves_to_empty_never_to_a_fabricated_block(self, store):
        assert store.load().resolved_knowledge == ""

    def test_load_does_not_create_the_file(self, store):
        store.load()
        assert not store.path.exists()


# =============================================================================
# ROUND TRIP
# =============================================================================

class TestRoundTrip:
    def test_agent_config_survives_a_save_and_load(self, store):
        store.save_agent(
            AgentConfig(voice_model=VOICE, system_prompt="Be brief.")
        )

        document = store.load()
        assert document.resolved_voice_model == VOICE
        assert document.resolved_system_prompt == "Be brief."

    def test_persona_rules_survive_a_save_and_load(self, store):
        store.save_persona(PersonaConfig(rules="Never say certainly."))

        assert store.load().resolved_persona_rules == "Never say certainly."

    def test_knowledge_survives_a_save_and_load(self, store):
        store.save_knowledge(KnowledgeConfig(context="Five years at Infosys."))

        assert store.load().resolved_knowledge == "Five years at Infosys."

    def test_a_save_is_visible_to_a_second_store_on_the_same_path(self, store):
        """
        The cross-process contract. The panel writes in the API process and
        the agent reads in another, so a save has to be durable on disk
        rather than cached in the object that made it.
        """
        store.save_persona(PersonaConfig(rules="Short turns."))

        assert ConfigStore(store.path).load().resolved_persona_rules == "Short turns."

    def test_saving_one_section_leaves_the_others_alone(self, store):
        store.save_agent(AgentConfig(voice_model=VOICE, system_prompt="prompt"))
        store.save_knowledge(KnowledgeConfig(context="facts"))
        store.save_persona(PersonaConfig(rules="rules"))

        document = store.load()
        assert document.resolved_voice_model == VOICE
        assert document.resolved_system_prompt == "prompt"
        assert document.resolved_knowledge == "facts"
        assert document.resolved_persona_rules == "rules"

    def test_a_save_returns_what_was_stored_with_a_timestamp(self, store):
        section = store.save_knowledge(KnowledgeConfig(context="facts"))

        assert section.context == "facts"
        assert section.updated_at is not None
        # Timezone-aware, so a stale-config question has an answer that does
        # not depend on which machine wrote the file.
        assert section.updated_at.endswith("+00:00")

    def test_every_section_is_stamped_not_just_one(self, store):
        """
        All three, because the stamps are three separate call sites and a
        mutation dropping one of them survived a version of this suite that
        only checked `knowledge`. "Was this saved before that call" has to be
        answerable for whichever section is under suspicion.
        """
        sections = (
            store.save_agent(AgentConfig(voice_model=VOICE, system_prompt="p")),
            store.save_persona(PersonaConfig(rules="r")),
            store.save_knowledge(KnowledgeConfig(context="c")),
        )

        for section in sections:
            assert section.updated_at is not None, type(section).__name__
            assert section.updated_at.endswith("+00:00"), type(section).__name__

        document = store.load()
        assert document.agent.updated_at is not None
        assert document.persona.updated_at is not None
        assert document.knowledge.updated_at is not None

    def test_a_later_save_replaces_the_earlier_one(self, store):
        store.save_persona(PersonaConfig(rules="first"))
        store.save_persona(PersonaConfig(rules="second"))

        assert store.load().resolved_persona_rules == "second"


# =============================================================================
# CLEARED IS NOT UNSET
# =============================================================================

class TestClearedStaysCleared:
    def test_explicitly_cleared_rules_do_not_revert_to_the_default(self, store):
        """
        The distinction the whole never-written/written-empty split exists
        for. An operator who selected all and deleted has made a decision,
        and re-substituting the default would silently overrule it.
        """
        store.save_persona(PersonaConfig(rules=""))

        assert store.load().resolved_persona_rules == ""

    def test_a_cleared_section_is_still_a_section_on_disk(self, store):
        store.save_persona(PersonaConfig(rules=""))

        document = store.load()
        assert document.persona is not None
        assert document.persona.rules == ""

    def test_clearing_after_setting_sticks(self, store):
        store.save_persona(PersonaConfig(rules=DEFAULT_PERSONA_RULES))
        store.save_persona(PersonaConfig(rules=""))

        assert store.load().resolved_persona_rules == ""


# =============================================================================
# TEXT FIDELITY
# =============================================================================

class TestTextIsStoredAsWritten:
    def test_interior_blank_lines_are_preserved(self, store):
        """
        The ruleset is paragraph-separated prose. A normalising pass that
        collapsed these would rewrite an operator's constraints into a wall
        of text, and nothing would report it.
        """
        rules = "First rule.\n\nSecond rule.\n\n\nThird rule."
        store.save_persona(PersonaConfig(rules=rules))

        assert store.load().resolved_persona_rules == rules

    def test_the_default_ruleset_round_trips_byte_for_byte(self, store):
        """The ordinary submit: the panel sends this text verbatim."""
        store.save_persona(PersonaConfig(rules=DEFAULT_PERSONA_RULES))

        assert store.load().resolved_persona_rules == DEFAULT_PERSONA_RULES

    def test_leading_and_trailing_whitespace_is_trimmed(self, store):
        store.save_knowledge(KnowledgeConfig(context="  facts  \n\n"))

        assert store.load().resolved_knowledge == "facts"

    def test_non_ascii_survives_the_round_trip(self, store):
        """
        Hinglish register and Indian punctuation are persona requirements
        (CLAUDE.md §4.3), and the default ruleset itself contains em dashes.
        An ensure_ascii escape or a cp1252 write would corrupt both.
        """
        rules = "Say “theek hai” — नमस्ते, ₹5 lakh."
        store.save_persona(PersonaConfig(rules=rules))

        assert store.load().resolved_persona_rules == rules


# =============================================================================
# ON-DISK SHAPE
# =============================================================================

class TestOnDiskShape:
    def test_the_file_is_camel_case_matching_the_wire(self, store):
        """
        So "what did the panel actually save" is answerable with `cat`, and
        so there is no second field mapping to drift from the aliases.
        """
        store.save_agent(AgentConfig(voice_model=VOICE, system_prompt="p"))

        payload = json.loads(store.path.read_text(encoding="utf-8"))
        assert payload["agent"]["voiceModel"] == VOICE
        assert payload["agent"]["systemPrompt"] == "p"
        assert "updatedAt" in payload["agent"]

    def test_the_file_records_its_version(self, store):
        store.save_persona(PersonaConfig(rules="r"))

        payload = json.loads(store.path.read_text(encoding="utf-8"))
        assert payload["version"] == CONFIG_VERSION

    def test_the_parent_directory_is_created(self, store):
        assert not store.path.parent.exists()

        store.save_persona(PersonaConfig(rules="r"))

        assert store.path.exists()

    def test_no_temporary_file_is_left_behind(self, store):
        store.save_persona(PersonaConfig(rules="r"))

        leftovers = list(store.path.parent.glob("*.tmp"))
        assert leftovers == []

    def test_the_file_uses_unix_newlines_on_every_platform(self, store):
        """
        Otherwise every save on the Windows dev machine shows as a whole-file
        diff against the Linux target.
        """
        store.save_persona(PersonaConfig(rules="a\nb"))

        assert b"\r\n" not in store.path.read_bytes()


# =============================================================================
# TOLERATING A BAD FILE
# =============================================================================

class TestLoadNeverRaises:
    """
    Phase 2 puts this read in the audio process. Every case below must
    degrade to "nothing configured" rather than propagate -- a malformed file
    should cost the twin its configuration, not the call.
    """

    def _write(self, store, text: str) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(text, encoding="utf-8")

    def test_an_empty_file_loads_as_unconfigured(self, store):
        self._write(store, "")

        assert store.load().agent is None

    def test_whitespace_only_loads_as_unconfigured(self, store):
        self._write(store, "   \n\n ")

        assert store.load().agent is None

    def test_invalid_json_loads_as_unconfigured(self, store):
        self._write(store, "{ this is not json")

        assert store.load().resolved_persona_rules == DEFAULT_PERSONA_RULES

    def test_a_truncated_document_loads_as_unconfigured(self, store):
        """What a non-atomic writer would leave behind mid-crash."""
        self._write(store, '{"version": 1, "agent": {"voiceMod')

        assert store.load().agent is None

    def test_a_json_array_loads_as_unconfigured(self, store):
        self._write(store, "[1, 2, 3]")

        assert store.load().agent is None

    def test_a_future_version_is_refused_rather_than_guessed_at(self, store):
        """
        Reading an unknown shape optimistically is how a stale prompt reaches
        a live call while looking like it applied.
        """
        self._write(
            store,
            json.dumps({"version": 999, "agent": {"voiceModel": VOICE, "systemPrompt": "p"}}),
        )

        assert store.load().agent is None

    def test_a_missing_version_is_refused(self, store):
        self._write(store, json.dumps({"agent": {"voiceModel": VOICE, "systemPrompt": "p"}}))

        assert store.load().agent is None

    def test_a_section_of_the_wrong_shape_loads_as_unconfigured(self, store):
        self._write(store, json.dumps({"version": CONFIG_VERSION, "agent": "a string"}))

        assert store.load().agent is None

    def test_unknown_top_level_keys_are_ignored_not_fatal(self, store):
        """
        The document tolerates additions; only the *request bodies* forbid
        extras. A file written by a newer minor version with a section this
        build does not know about should still yield the sections it does.
        """
        self._write(
            store,
            json.dumps(
                {
                    "version": CONFIG_VERSION,
                    "persona": {"rules": "r"},
                    "somethingNew": {"x": 1},
                }
            ),
        )

        assert store.load().resolved_persona_rules == "r"

    def test_a_directory_where_the_file_should_be_loads_as_unconfigured(self, store):
        store.path.mkdir(parents=True)

        assert store.load().agent is None

    def test_a_bad_file_is_survivable_and_overwritable(self, store):
        """A corrupt file must not wedge the panel -- the next save fixes it."""
        self._write(store, "garbage")

        store.save_persona(PersonaConfig(rules="recovered"))

        assert store.load().resolved_persona_rules == "recovered"


# =============================================================================
# VALIDATION
# =============================================================================

class TestValidation:
    def test_a_system_prompt_at_the_limit_is_accepted(self):
        AgentConfig(voice_model=VOICE, system_prompt="x" * MAX_SYSTEM_PROMPT_CHARS)

    def test_a_system_prompt_over_the_limit_is_refused(self):
        with pytest.raises(ValueError, match="the limit is 16,000"):
            AgentConfig(voice_model=VOICE, system_prompt="x" * (MAX_SYSTEM_PROMPT_CHARS + 1))

    def test_rules_over_the_limit_are_refused(self):
        with pytest.raises(ValueError, match="The ruleset is 16,001 characters"):
            PersonaConfig(rules="x" * (MAX_PERSONA_RULES_CHARS + 1))

    def test_knowledge_over_the_limit_is_refused(self):
        with pytest.raises(ValueError, match="the limit is 50,000"):
            KnowledgeConfig(context="x" * (MAX_KNOWLEDGE_CHARS + 1))

    def test_an_empty_voice_model_is_refused(self):
        with pytest.raises(ValueError, match="Pick a voice model"):
            AgentConfig(voice_model="", system_prompt="p")

    def test_a_whitespace_only_voice_model_is_refused(self):
        with pytest.raises(ValueError, match="Pick a voice model"):
            AgentConfig(voice_model="   ", system_prompt="p")

    def test_an_absurd_voice_model_is_refused(self):
        with pytest.raises(ValueError, match="voice model ID"):
            AgentConfig(voice_model="x" * 200, system_prompt="p")

    def test_an_empty_system_prompt_is_allowed(self):
        """
        The panel requires a voice but not a prompt. Rejecting an empty
        prompt here would make the backend stricter than the screen, which
        surfaces as a save that fails for no visible reason.
        """
        assert AgentConfig(voice_model=VOICE, system_prompt="").system_prompt == ""

    def test_empty_rules_and_knowledge_are_allowed(self):
        assert PersonaConfig(rules="").rules == ""
        assert KnowledgeConfig(context="").context == ""

    def test_the_length_limit_counts_the_trimmed_value(self, store):
        """
        Trailing whitespace must not be able to push a legitimate value over
        the ceiling, since the value that gets stored is the trimmed one.
        """
        AgentConfig(
            voice_model=VOICE,
            system_prompt="x" * MAX_SYSTEM_PROMPT_CHARS + "\n\n   ",
        )


# =============================================================================
# PATH RESOLUTION
# =============================================================================

class TestPathResolution:
    def test_the_default_path_is_persistent_not_temporary(self):
        """
        Traces go to the temp directory because they are disposable. Config
        is not: it has to survive a restart.
        """
        import tempfile

        from shuo.config_store import default_config_path

        assert tempfile.gettempdir() not in str(default_config_path())

    def test_the_default_path_is_overridable(self, monkeypatch, tmp_path):
        from shuo.config_store import default_config_path

        target = tmp_path / "elsewhere.json"
        monkeypatch.setenv("SHUO_CONFIG_PATH", str(target))

        assert default_config_path() == target

    def test_the_default_path_is_not_posix_only(self):
        """Bug C, and decision 20, were both this mistake."""
        from shuo.config_store import default_config_path

        assert not str(default_config_path()).startswith("/tmp")
        assert os.path.isabs(default_config_path())
