"""
Tests for the config reader in the audio process (W2).

This is the half of the seam that decides what a *live call* runs with, so
the failures it has to catch are the ones that look like nothing:

- **A save that silently does not apply.** The panel says "Saved", the file
  on disk is correct, and the twin keeps talking in the built-in assistant
  voice with the built-in assistant prompt. The `describe()` line exists to
  make this visible, and these tests exist to make it not happen.
- **A cleared field being overwritten by a default.** An operator who deletes
  the persona rules has made a decision. Re-adding the ruleset because the
  field is empty runs constraints they deliberately removed.
- **A voice that stopped being synthesisable taking the prompt down with
  it.** ElevenLabs entitlements change under a file nobody edited. The right
  outcome is one loud line about the voice with everything else intact --
  decision 29 established that the wrong outcome is a silently silent call.
- **The pool's cold path missing the voice.** Warm turns in the operator's
  voice and the occasional cold one in the environment's reads as a vendor
  being flaky rather than as a wiring bug.
"""

import asyncio
import json

import pytest

from shuo.config_store import (
    CONFIG_VERSION,
    DEFAULT_PERSONA_RULES,
    AgentConfig,
    ConfigStore,
    KnowledgeConfig,
    PersonaConfig,
)
from shuo.runtime_config import (
    CONSTRAINTS_HEADING,
    KNOWLEDGE_HEADING,
    CallSettings,
    assemble_system_prompt,
    load_call_settings,
)
from shuo.services.llm import SYSTEM_PROMPT as BUILTIN_SYSTEM_PROMPT
from shuo.services.tts import FALLBACK_VOICE_ID, TTSService, env_voice_id
from shuo.services.tts_pool import TTSPool

# A voice that is real, selectable, and deliberately **not** the fallback --
# "the operator's voice was used" and "we fell back" are otherwise the same
# assertion.
VOICE_ID = "el-daniel"
VOICE_PROVIDER_ID = "onwK4e9ZLuTAKqWW03F9"

# Krish: in the catalogue, right accent for the persona, and not synthesisable
# on this plan. The real de-entitlement case rather than a made-up one.
DE_ENTITLED_VOICE = "el-krish"

# A sentinel so "fell back to the environment" is unambiguous in an assertion.
ENV_VOICE = "env-voice-sentinel"


@pytest.fixture
def store(tmp_path):
    """A store on a throwaway path, in a directory that does not exist yet."""
    return ConfigStore(tmp_path / "nested" / "agent_config.json")


@pytest.fixture(autouse=True)
def env_voice(monkeypatch):
    """Pin the environment voice so fallbacks are identifiable."""
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", ENV_VOICE)


# =============================================================================
# ASSEMBLY (pure)
# =============================================================================

class TestAssembly:
    def test_order_is_prompt_then_constraints_then_facts(self):
        """
        Fixed order, and the reason is the fact block's length. Knowledge can
        run to 50,000 characters; burying the behavioural rules underneath it
        is how the rules stop being followed.
        """
        prompt = assemble_system_prompt(
            base="You are Priya.", rules="Be brief.", knowledge="Five years at Infosys."
        )

        assert prompt.index("You are Priya.") < prompt.index(CONSTRAINTS_HEADING)
        assert prompt.index(CONSTRAINTS_HEADING) < prompt.index(KNOWLEDGE_HEADING)

    def test_every_field_survives_verbatim(self):
        prompt = assemble_system_prompt(
            base="You are Priya.", rules="Be brief.", knowledge="Five years at Infosys."
        )

        assert "You are Priya." in prompt
        assert "Be brief." in prompt
        assert "Five years at Infosys." in prompt

    def test_an_empty_rules_field_contributes_no_heading(self):
        """
        A bare "## Constraints" with nothing under it is not a no-op -- it
        reads to the model as an instruction with missing content.
        """
        prompt = assemble_system_prompt(base="You are Priya.", rules="", knowledge="")

        assert CONSTRAINTS_HEADING not in prompt
        assert prompt == "You are Priya."

    def test_an_empty_knowledge_field_contributes_no_heading(self):
        prompt = assemble_system_prompt(
            base="You are Priya.", rules="Be brief.", knowledge=""
        )

        assert KNOWLEDGE_HEADING not in prompt
        assert CONSTRAINTS_HEADING in prompt

    def test_whitespace_only_counts_as_empty(self):
        prompt = assemble_system_prompt(
            base="You are Priya.", rules="   \n  ", knowledge="\t"
        )

        assert prompt == "You are Priya."

    def test_sections_are_blank_line_separated(self):
        """Run together, the fact block reads as a continuation of the rules."""
        prompt = assemble_system_prompt(
            base="You are Priya.", rules="Be brief.", knowledge="Infosys."
        )

        assert "\n\n" + CONSTRAINTS_HEADING in prompt
        assert "\n\n" + KNOWLEDGE_HEADING in prompt


# =============================================================================
# UNCONFIGURED
# =============================================================================

class TestUnconfigured:
    def test_the_builtin_prompt_is_used_when_none_was_saved(self, store):
        settings = load_call_settings(store)

        assert BUILTIN_SYSTEM_PROMPT in settings.system_prompt
        assert settings.prompt_source == "built-in"

    def test_an_unconfigured_install_still_gets_the_anti_robotic_ruleset(self, store):
        """
        The load-bearing default, carried through from the store. An install
        nobody has configured must not run with zero persona constraints --
        robotic delivery is one of the four failure modes the project exists
        to hunt.
        """
        settings = load_call_settings(store)

        assert CONSTRAINTS_HEADING in settings.system_prompt
        assert DEFAULT_PERSONA_RULES in settings.system_prompt

    def test_no_knowledge_block_is_invented(self, store):
        """A fabricated fact block is exactly what the persona rules forbid."""
        settings = load_call_settings(store)

        assert KNOWLEDGE_HEADING not in settings.system_prompt
        assert settings.knowledge_chars == 0

    def test_the_environment_voice_is_used(self, store):
        settings = load_call_settings(store)

        assert settings.voice_id == ENV_VOICE
        assert settings.voice_source == "environment"


# =============================================================================
# CONFIGURED
# =============================================================================

class TestConfigured:
    def test_the_saved_prompt_replaces_the_builtin_one(self, store):
        store.save_agent(
            AgentConfig(voice_model=VOICE_ID, system_prompt="You are Priya Sharma.")
        )

        settings = load_call_settings(store)

        assert "You are Priya Sharma." in settings.system_prompt
        assert BUILTIN_SYSTEM_PROMPT not in settings.system_prompt
        assert settings.prompt_source == "operator"

    def test_the_saved_voice_resolves_to_a_provider_id(self, store):
        """
        The store holds catalogue IDs; the synthesiser only understands
        provider IDs. Handing `el-daniel` to ElevenLabs is a 404 whose symptom
        is a call with no audio.
        """
        store.save_agent(AgentConfig(voice_model=VOICE_ID, system_prompt="p"))

        settings = load_call_settings(store)

        assert settings.voice_id == VOICE_PROVIDER_ID
        assert "Daniel" in settings.voice_source

    def test_a_raw_provider_id_in_the_file_also_resolves(self, store):
        """
        `AgentConfig` canonicalises on save, but the file is hand-editable and
        `AgentSection` is deliberately lax on read.
        """
        path = store.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "version": CONFIG_VERSION,
                "agent": {"voiceModel": VOICE_PROVIDER_ID, "systemPrompt": "p"},
            }),
            encoding="utf-8",
        )

        assert load_call_settings(store).voice_id == VOICE_PROVIDER_ID

    def test_saved_rules_and_knowledge_reach_the_prompt(self, store):
        store.save_agent(AgentConfig(voice_model=VOICE_ID, system_prompt="You are Priya."))
        store.save_persona(PersonaConfig(rules="Never say certainly."))
        store.save_knowledge(KnowledgeConfig(context="Five years at Infosys."))

        settings = load_call_settings(store)

        assert "Never say certainly." in settings.system_prompt
        assert "Five years at Infosys." in settings.system_prompt
        assert DEFAULT_PERSONA_RULES not in settings.system_prompt

    def test_the_provenance_counts_describe_what_was_loaded(self, store):
        store.save_agent(AgentConfig(voice_model=VOICE_ID, system_prompt="p"))
        store.save_persona(PersonaConfig(rules="abcde"))
        store.save_knowledge(KnowledgeConfig(context="xyz"))

        settings = load_call_settings(store)

        assert settings.rules_chars == 5
        assert settings.knowledge_chars == 3
        # The line an operator checks a save against.
        assert "operator" in settings.describe()
        assert VOICE_PROVIDER_ID in settings.describe()


class TestOperatorClearedAField:
    def test_cleared_rules_are_respected_not_replaced_by_the_default(self, store):
        """
        The one asymmetry in the store, carried into the prompt. Written-empty
        is a decision; re-adding the ruleset would run constraints the
        operator deliberately removed.
        """
        store.save_persona(PersonaConfig(rules=""))

        settings = load_call_settings(store)

        assert CONSTRAINTS_HEADING not in settings.system_prompt
        assert DEFAULT_PERSONA_RULES not in settings.system_prompt

    def test_cleared_knowledge_is_respected(self, store):
        store.save_knowledge(KnowledgeConfig(context=""))

        assert KNOWLEDGE_HEADING not in load_call_settings(store).system_prompt


# =============================================================================
# VOICE FAILURE -- the whole config must not go down with the voice
# =============================================================================

class TestVoiceFallback:
    def _write_voice(self, store, voice_model):
        """
        Write a voice ID the save path would have rejected.

        Through the file rather than `save_agent`, because that is how this
        state actually arises: `AgentConfig` validates against the catalogue,
        so a rejected ID can only get onto disk by being written when it was
        still valid -- or by hand.
        """
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps({
                "version": CONFIG_VERSION,
                "agent": {
                    "voiceModel": voice_model,
                    "systemPrompt": "You are Priya Sharma.",
                },
            }),
            encoding="utf-8",
        )

    def test_a_de_entitled_voice_falls_back_without_losing_the_prompt(
        self, store, caplog
    ):
        """
        The real regression path: an ID that was synthesisable when it was
        saved and is not any more. Entitlements change under a file nobody
        touched.
        """
        self._write_voice(store, DE_ENTITLED_VOICE)

        with caplog.at_level("ERROR"):
            settings = load_call_settings(store)

        assert settings.voice_id == ENV_VOICE
        # The point of the test: the prompt survived the voice failing.
        assert "You are Priya Sharma." in settings.system_prompt
        assert settings.prompt_source == "operator"
        assert DE_ENTITLED_VOICE in caplog.text

    def test_an_unknown_voice_falls_back_and_says_so(self, store, caplog):
        self._write_voice(store, "el-does-not-exist")

        with caplog.at_level("ERROR"):
            settings = load_call_settings(store)

        assert settings.voice_id == ENV_VOICE
        assert "el-does-not-exist" in caplog.text
        assert "fallback" in settings.voice_source

    def test_the_fallback_is_silent_when_no_voice_was_ever_saved(self, store, caplog):
        """An unconfigured install is not an error and must not log like one."""
        with caplog.at_level("ERROR"):
            settings = load_call_settings(store)

        assert settings.voice_id == ENV_VOICE
        assert caplog.text == ""


# =============================================================================
# NEVER RAISES -- a bad config costs the twin its personality, not the call
# =============================================================================

class TestNeverRaises:
    def test_invalid_json_degrades_to_defaults(self, store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json", encoding="utf-8")

        settings = load_call_settings(store)

        assert BUILTIN_SYSTEM_PROMPT in settings.system_prompt
        assert settings.voice_id == ENV_VOICE

    def test_a_future_version_degrades_to_defaults(self, store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps({
                "version": CONFIG_VERSION + 1,
                "agent": {"voiceModel": VOICE_ID, "systemPrompt": "from the future"},
            }),
            encoding="utf-8",
        )

        settings = load_call_settings(store)

        assert "from the future" not in settings.system_prompt
        assert BUILTIN_SYSTEM_PROMPT in settings.system_prompt

    def test_a_store_that_raises_still_yields_usable_settings(self, caplog):
        """
        `ConfigStore.load` is documented never to raise, so reaching this
        means something changed. It still must not cost a call.
        """
        class ExplodingStore:
            def load(self):
                raise RuntimeError("disk on fire")

        with caplog.at_level("ERROR"):
            settings = load_call_settings(ExplodingStore())

        assert settings.system_prompt == BUILTIN_SYSTEM_PROMPT
        assert settings.voice_id == ENV_VOICE
        assert "disk on fire" in caplog.text

    def test_builtin_settings_perform_no_io(self, monkeypatch):
        """
        `CallSettings.builtin()` is `Agent`'s fallback, and the agent is
        constructed inside the call loop. A file open there is precisely what
        the config/audio process split exists to prevent.
        """
        def explode(*a, **kw):
            raise AssertionError("builtin() must not touch the config store")

        monkeypatch.setattr("shuo.runtime_config.ConfigStore", explode)

        settings = CallSettings.builtin()

        assert settings.system_prompt == BUILTIN_SYSTEM_PROMPT
        assert settings.voice_id == ENV_VOICE


# =============================================================================
# THE PIPELINE ACTUALLY CONSUMES IT
# =============================================================================

class _RecordingTTS:
    """Records the voice it was constructed with. Never opens a socket."""

    instances = []

    def __init__(self, on_audio, on_done, voice_id=None):
        self.voice_id = voice_id
        self.is_active = True
        self.fatal_error = None
        _RecordingTTS.instances.append(self)

    def bind(self, on_audio, on_done):
        pass

    async def start(self):
        pass

    async def cancel(self):
        pass


@pytest.fixture
def recording_tts(monkeypatch):
    _RecordingTTS.instances.clear()
    monkeypatch.setattr("shuo.services.tts_pool.TTSService", _RecordingTTS)
    return _RecordingTTS


class TestVoiceReachesTheSynthesiser:
    def test_the_service_defaults_to_the_environment_voice(self):
        assert TTSService(None, None)._voice_id == env_voice_id()

    def test_an_explicit_voice_wins(self):
        assert TTSService(None, None, voice_id="explicit")._voice_id == "explicit"

    def test_an_empty_environment_voice_resolves_to_the_fallback(self, monkeypatch):
        """
        `os.getenv(name, default)` returns "" for a variable set to empty, and
        an empty voice ID builds a URL that 404s -- a call with no audio whose
        symptom points nowhere near the environment.
        """
        monkeypatch.setenv("ELEVENLABS_VOICE_ID", "")

        assert env_voice_id() == FALLBACK_VOICE_ID

    @pytest.mark.asyncio
    async def test_the_warm_path_carries_the_pool_voice(self, recording_tts):
        pool = TTSPool(pool_size=1, ttl=8.0, voice_id="pool-voice")
        await pool.start()
        try:
            for _ in range(100):
                if pool.available:
                    break
                await asyncio.sleep(0.01)
            assert pool.available, "the pool never pre-connected"
            assert recording_tts.instances[-1].voice_id == "pool-voice"
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_the_cold_path_carries_the_pool_voice(self, recording_tts):
        """
        The easy one to miss, and the one whose failure is hardest to read:
        warm turns in the operator's voice and the occasional cold one in the
        environment's looks like ElevenLabs being inconsistent.
        """
        pool = TTSPool(pool_size=1, ttl=8.0, voice_id="pool-voice")
        # Never started, so nothing is warm and `get` must connect fresh.
        assert pool.available == 0

        await pool.get(on_audio=None, on_done=None)

        assert recording_tts.instances[-1].voice_id == "pool-voice"


class _CapturingLLMClient:
    """Captures the request the LLM service would have sent."""

    def __init__(self):
        self.kwargs = None

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return _EmptyStream()


class _EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class TestPromptReachesTheModel:
    @pytest.mark.asyncio
    async def test_the_assembled_prompt_is_the_system_message(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
        from shuo.services.llm import LLMService

        async def noop(*a):
            pass

        service = LLMService(
            on_token=noop, on_done=noop, system_prompt="You are Priya Sharma."
        )
        client = _CapturingLLMClient()
        service._client = client

        await service.start("tell me about yourself")
        await service._task

        assert client.kwargs["messages"][0] == {
            "role": "system",
            "content": "You are Priya Sharma.",
        }

    @pytest.mark.asyncio
    async def test_the_builtin_prompt_is_used_when_none_is_injected(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
        from shuo.services.llm import LLMService

        async def noop(*a):
            pass

        service = LLMService(on_token=noop, on_done=noop)
        client = _CapturingLLMClient()
        service._client = client

        await service.start("hello")
        await service._task

        assert client.kwargs["messages"][0]["content"] == BUILTIN_SYSTEM_PROMPT


class TestAgentUsesTheSettings:
    def test_the_agent_hands_the_prompt_to_the_llm(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
        from shuo.agent import Agent
        from shuo.tracer import Tracer

        settings = CallSettings(
            system_prompt="You are Priya Sharma.",
            voice_id="v",
            prompt_source="operator",
            voice_source="test",
            rules_chars=0,
            knowledge_chars=0,
        )

        agent = Agent(
            session=None,
            on_done=lambda _c: None,
            tts_pool=None,
            tracer=Tracer(),
            settings=settings,
        )

        assert agent._llm._system_prompt == "You are Priya Sharma."

    def test_an_agent_built_without_settings_falls_back_quietly(self, monkeypatch):
        """No settings must mean built-in defaults, not a surprise file open."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
        from shuo.agent import Agent
        from shuo.tracer import Tracer

        agent = Agent(
            session=None,
            on_done=lambda _c: None,
            tts_pool=None,
            tracer=Tracer(),
        )

        assert agent._llm._system_prompt == BUILTIN_SYSTEM_PROMPT
