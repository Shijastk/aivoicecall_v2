"""
Tests for the voice catalogue.

The failure this file exists to prevent has a specific shape, and it is not
"the wrong voice plays". Per decision 29, a voice this ElevenLabs plan cannot
synthesise does not raise, does not log and does not fall back -- the socket
connects, `✓ TTS connected` is printed, one frame comes back saying
`payment_required`, and the call goes **silent** until the caller gives up.
So the assertions here are mostly about *refusing early*: an ID that cannot
be spoken must not survive a PUT, and the list the panel renders must contain
nothing the PUT would refuse.

`TestThePickerAndTheValidatorAgree` is the one that matters most -- it is the
only test that pins those two halves to the same table.
"""

import pytest
from pydantic import ValidationError

from shuo.config_store import (
    ELEVENLABS,
    VOICE_CATALOGUE,
    AgentConfig,
    AgentSection,
    ConfigDocument,
    rejection_reason,
    resolve_voice,
    selectable_voices,
)

# Measured by `scripts/getfreevocies.py` on 2026-07-26 -- it synthesises one
# character through every voice on the account and counts what returns audio.
# 21 pass, 2 return 402. If this number needs changing, the sweep is what
# changes it; editing it to match the catalogue would defeat the point.
MEASURED_SYNTHESISABLE = 21

# The two the sweep gets 402 on, and the only Indian-accented voices on the
# account (decisions 29/31). Named here so an upgrade that quietly makes them
# work is noticed here rather than mid-call.
BLOCKED_BY_PLAN = ("MmiGAbOYCaIFzgNItUWa", "4O1sYUnmtThcBoSBrri7")

# What the picker shipped before the catalogue existed. A stale browser tab or
# an old saved config can still send any of these.
RETIRED_FIXTURES = ("el-rachel-v2", "el-adam-turbo", "oa-alloy", "ct-sonic-hi")


# =============================================================================
# THE TABLE ITSELF
# =============================================================================

class TestCatalogueIntegrity:
    def test_the_measured_count_is_what_is_offered(self):
        assert len(selectable_voices()) == MEASURED_SYNTHESISABLE

    def test_public_ids_are_unique(self):
        ids = [voice.id for voice in VOICE_CATALOGUE]
        assert len(ids) == len(set(ids))

    def test_provider_ids_are_unique(self):
        """
        Two entries sharing a provider ID would make `resolve_voice` return
        whichever the dict happened to keep, so a raw-ID lookup would silently
        pick a different entry than the same voice's public ID does.
        """
        provider_ids = [
            voice.provider_voice_id
            for voice in VOICE_CATALOGUE
            if voice.provider_voice_id
        ]
        assert len(provider_ids) == len(set(provider_ids))

    def test_every_offered_voice_has_a_provider_id_to_synthesise_with(self):
        for voice in selectable_voices():
            assert voice.provider_voice_id, f"{voice.id} has nothing to speak with"

    def test_every_offered_voice_is_a_provider_we_actually_have(self):
        """
        Decision 21 closed the vendor question on ElevenLabs and
        `services/tts.py` is an ElevenLabs client. Offering a Cartesia or
        OpenAI voice would be offering one nothing can speak.
        """
        for voice in selectable_voices():
            assert voice.provider == ELEVENLABS

    def test_every_offered_voice_has_a_preview_to_play(self):
        """
        `_PREVIEWS` is keyed by provider ID and looked up with `.get`, so a
        voice added to the catalogue without a matching preview entry gets a
        silent `None` -- a dead play button rather than an error. This is the
        only thing that notices.
        """
        for voice in selectable_voices():
            assert voice.preview_url, f"{voice.id} has no preview to play"

    def test_the_plan_blocked_voices_can_still_be_auditioned(self):
        """
        Previews are hosted files, not synthesis, so Krish and Maya preview
        fine on a plan that cannot speak them. Kept on purpose: hearing what
        the upgrade buys is the argument for decision 31.
        """
        for blocked in ("el-krish", "el-maya"):
            assert resolve_voice(blocked).preview_url

    def test_every_refusal_explains_itself(self):
        for voice in VOICE_CATALOGUE:
            if not voice.available:
                assert voice.unavailable_reason

    def test_the_voices_the_plan_blocks_are_not_offered(self):
        offered = {voice.provider_voice_id for voice in selectable_voices()}
        for provider_id in BLOCKED_BY_PLAN:
            assert provider_id not in offered

    def test_the_retired_fixtures_are_not_offered(self):
        offered = {voice.id for voice in selectable_voices()}
        for fixture in RETIRED_FIXTURES:
            assert fixture not in offered

    def test_the_retired_fixtures_are_still_recognised(self):
        """
        Kept in the table rather than left to fall through to "unknown". A
        stale tab still sends `el-rachel-v2`, and "Rachel is not in this
        workspace, pick el-george" is a different message from "never heard
        of it" -- the first tells the operator what happened to a voice they
        were looking at a minute ago.
        """
        for fixture in RETIRED_FIXTURES:
            voice = resolve_voice(fixture)
            assert voice is not None, f"{fixture} would report as unknown"
            assert not voice.available

    def test_the_live_default_is_offered(self):
        """
        `services/tts.py` falls back to George's ID with no config at all
        (decision 31). A catalogue that could not express the value the agent
        already runs on would be describing a different system.
        """
        voice = resolve_voice("JBFqnCBsd6RMkjVDRZzb")
        assert voice is not None
        assert voice.available
        assert voice.id == "el-george"


class TestTheWireShape:
    def test_an_entry_carries_the_keys_the_panels_picker_reads(self):
        """
        The frontend's `VoiceModel` is {id, name, provider, description,
        locale, badge?}. `GET /v1/voices` is meant to drop into `models={...}`
        with no adapter, so a rename here is a broken picker there.
        """
        payload = selectable_voices()[0].model_dump(by_alias=True)

        for key in ("id", "name", "provider", "description", "locale"):
            assert key in payload and payload[key]

    def test_the_provider_id_is_camel_case_like_the_rest_of_the_wire(self):
        payload = resolve_voice("el-george").model_dump(by_alias=True)

        assert payload["providerVoiceId"] == "JBFqnCBsd6RMkjVDRZzb"
        assert "provider_voice_id" not in payload

    def test_the_preview_url_is_camel_case_too(self):
        """The play button reads `previewUrl`; a snake_case key here is a
        button that never appears."""
        payload = resolve_voice("el-george").model_dump(by_alias=True)

        assert payload["previewUrl"].startswith("https://")
        assert "preview_url" not in payload

    def test_a_row_with_no_preview_still_serialises_the_key(self):
        """
        Present-and-null rather than absent. The panel branches on the value;
        a missing key would make `previewUrl` `undefined` in TypeScript and
        the two are easy to confuse in a template.
        """
        payload = resolve_voice("oa-alloy").model_dump(by_alias=True)

        assert "previewUrl" in payload
        assert payload["previewUrl"] is None

    def test_an_entry_cannot_be_mutated_by_a_caller(self):
        """The catalogue is shared module state; a mutation would leak into
        every later request."""
        with pytest.raises(ValidationError):
            selectable_voices()[0].id = "el-something-else"


# =============================================================================
# RESOLUTION
# =============================================================================

class TestResolveVoice:
    def test_a_public_id_resolves(self):
        assert resolve_voice("el-george").provider_voice_id == "JBFqnCBsd6RMkjVDRZzb"

    def test_a_public_id_resolves_regardless_of_case(self):
        assert resolve_voice("EL-George").id == "el-george"

    def test_surrounding_whitespace_is_tolerated(self):
        assert resolve_voice("  el-george  ").id == "el-george"

    def test_a_raw_provider_id_resolves(self):
        """An operator who pasted the ID out of the ElevenLabs dashboard has
        not made a mistake."""
        assert resolve_voice("JBFqnCBsd6RMkjVDRZzb").id == "el-george"

    def test_a_provider_id_is_matched_case_sensitively(self):
        """
        ElevenLabs IDs are case-sensitive. Casefolding one before comparing
        would accept a corrupted ID as valid and then hand the corruption
        to the synthesiser, which is the silent-hang path again.
        """
        assert resolve_voice("jbfqncbsd6rmkjvdrzzb") is None

    def test_an_unknown_id_is_none(self):
        assert resolve_voice("el-nobody") is None

    def test_an_empty_value_is_none(self):
        assert resolve_voice("") is None

    def test_a_blocked_voice_resolves_but_is_not_available(self):
        """
        The distinction W2 needs: Krish is a real voice with a real ID that
        this plan cannot speak. Returning None would lose the reason.
        """
        krish = resolve_voice("el-krish")

        assert krish is not None
        assert krish.provider_voice_id == "MmiGAbOYCaIFzgNItUWa"
        assert not krish.available


class TestRejectionReason:
    def test_an_offered_voice_has_no_reason_to_refuse_it(self):
        assert rejection_reason("el-george") is None

    def test_an_unknown_id_is_pointed_at_the_endpoint_that_lists_them(self):
        reason = rejection_reason("el-nobody")

        assert "GET /v1/voices" in reason
        assert str(MEASURED_SYNTHESISABLE) in reason

    def test_a_plan_blocked_voice_names_the_upgrade_not_a_typo(self):
        """
        The single most expensive message in this file to get wrong. Krish is
        a *correct* choice for an Indian-accented persona that this plan
        cannot honour, so the refusal has to say "upgrade", or the operator
        reasonably concludes they mistyped and picks the wrong thing.
        """
        reason = rejection_reason("el-krish")

        assert "payment_required" in reason
        assert "Upgrade the plan" in reason

    def test_another_providers_voice_says_which_provider_we_have(self):
        assert "only synthesises through ElevenLabs" in rejection_reason("oa-alloy")

    def test_a_retired_fixture_names_its_replacement(self):
        assert "el-adam" in rejection_reason("el-adam-turbo")
        assert "el-george" in rejection_reason("el-rachel-v2")


# =============================================================================
# VALIDATION ON THE WAY IN
# =============================================================================

class TestAgentConfigValidation:
    def test_an_offered_voice_is_accepted(self):
        assert AgentConfig(voice_model="el-george", system_prompt="p").voice_model

    def test_an_unknown_voice_is_refused(self):
        with pytest.raises(ValidationError, match="GET /v1/voices"):
            AgentConfig(voice_model="el-nobody", system_prompt="p")

    def test_the_panels_old_default_is_refused(self):
        """
        `el-rachel-v2` was the picker's `defaultValue`, so before the
        catalogue the *most likely save in the world* stored a voice that
        does not exist in this workspace.
        """
        with pytest.raises(ValidationError, match="el-george"):
            AgentConfig(voice_model="el-rachel-v2", system_prompt="p")

    def test_a_plan_blocked_voice_is_refused(self):
        with pytest.raises(ValidationError, match="payment_required"):
            AgentConfig(voice_model="el-krish", system_prompt="p")

    def test_a_raw_provider_id_is_stored_as_its_catalogue_id(self):
        """Accepted as input, canonicalised on the way to disk, so the file
        holds one representation of a voice and W2 resolves one thing."""
        config = AgentConfig(voice_model="JBFqnCBsd6RMkjVDRZzb", system_prompt="p")

        assert config.voice_model == "el-george"

    def test_case_is_canonicalised_too(self):
        assert AgentConfig(voice_model="EL-GEORGE", system_prompt="p").voice_model == (
            "el-george"
        )

    def test_an_empty_voice_still_gets_the_panels_wording(self):
        """The catalogue check must not have swallowed the empty case -- it
        has its own message, matched to the panel's."""
        with pytest.raises(ValidationError, match="Pick a voice model before saving"):
            AgentConfig(voice_model="", system_prompt="p")

    def test_an_enormous_value_is_reported_as_a_length_problem(self):
        """
        Length is checked before the catalogue on purpose. Otherwise the
        "unknown voice" message quotes the whole rejected value back, and a
        pasted essay becomes a 20KB sentence in the save row.
        """
        with pytest.raises(ValidationError, match="the limit is 128"):
            AgentConfig(voice_model="x" * 200, system_prompt="p")


class TestTheReadPathStaysPermissive:
    """
    Strict on write, permissive on read -- and the asymmetry is load-bearing.

    `ConfigStore.load` validates the file back through `AgentSection`, and
    `load` turns *any* validation failure into an empty document. If the
    section rejected an unresolvable voice, one stale voice ID would take the
    system prompt, the persona rules and the knowledge block down with it,
    and the agent would take the next call on built-in defaults having said
    nothing about it.
    """

    def test_a_section_accepts_a_voice_that_has_left_the_catalogue(self):
        section = AgentSection(voice_model="el-retired-last-year", system_prompt="p")

        assert section.voice_model == "el-retired-last-year"

    def test_a_document_holding_one_survives_intact(self):
        document = ConfigDocument.model_validate(
            {
                "version": 1,
                "agent": {
                    "voiceModel": "el-retired-last-year",
                    "systemPrompt": "Answer as a candidate.",
                },
                "persona": {"rules": "Be brief."},
            }
        )

        # The voice is unusable and W2 must refuse to start a call on it...
        assert resolve_voice(document.resolved_voice_model) is None
        # ...but nothing else was lost on the way in.
        assert document.resolved_system_prompt == "Answer as a candidate."
        assert document.resolved_persona_rules == "Be brief."
