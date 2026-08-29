"""
Tests for the configuration REST API.

The contract under test is the frontend's, taken from
`shuo-frontend/lib/api/transport.ts` and `lib/api/types.ts`:

    PUT /v1/agent/config     { voiceModel, systemPrompt }
    PUT /v1/agent/persona    { rules }
    PUT /v1/agent/knowledge  { context }

and, on failure, a JSON body carrying `message` -- which the panel renders
verbatim in the save row. `TestErrorEnvelope` is the most valuable class in
this file: FastAPI's default `detail` shape would leave every rejection
reading "The agent service returned 422", which is the reason lost.
"""

import json

import pytest
from fastapi.testclient import TestClient

import shuo.config_api as config_api
from shuo.config_store import (
    DEFAULT_PERSONA_RULES,
    MAX_KNOWLEDGE_CHARS,
    MAX_SYSTEM_PROMPT_CHARS,
    ConfigStore,
    selectable_voices,
)

# A voice that is really in the catalogue. Since the catalogue became the
# authority, `voiceModel` is no longer a free string, so a test that wants a
# save to *succeed* has to send something real. Held as a constant because
# a re-sweep can retire an ID and this should then fail in one place.
VOICE = "el-george"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the app's module-level store at a throwaway file."""
    replacement = ConfigStore(tmp_path / "agent_config.json")
    monkeypatch.setattr(config_api, "store", replacement)
    return replacement


@pytest.fixture
def client(store, monkeypatch):
    """A client with auth off -- the default loopback posture."""
    monkeypatch.delenv("SHUO_CONFIG_API_TOKEN", raising=False)
    with TestClient(config_api.app) as test_client:
        yield test_client


# =============================================================================
# THE THREE WRITES
# =============================================================================

class TestAgentConfigEndpoint:
    def test_a_save_returns_200_and_echoes_the_stored_value(self, client):
        response = client.put(
            "/v1/agent/config",
            json={"voiceModel": VOICE, "systemPrompt": "Be brief."},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["voiceModel"] == VOICE
        assert body["systemPrompt"] == "Be brief."

    def test_a_save_reaches_the_store(self, client, store):
        client.put(
            "/v1/agent/config",
            json={"voiceModel": VOICE, "systemPrompt": "Answer as a candidate."},
        )

        document = store.load()
        assert document.resolved_voice_model == VOICE
        assert document.resolved_system_prompt == "Answer as a candidate."

    def test_the_response_carries_the_stamp(self, client):
        response = client.put(
            "/v1/agent/config", json={"voiceModel": VOICE, "systemPrompt": "p"}
        )

        assert response.json()["updatedAt"].endswith("+00:00")

    def test_an_empty_voice_model_is_refused_with_the_panels_wording(self, client):
        response = client.put(
            "/v1/agent/config", json={"voiceModel": "", "systemPrompt": "p"}
        )

        assert response.status_code == 400
        assert response.json()["message"] == "Pick a voice model before saving."

    def test_an_empty_system_prompt_is_accepted(self, client):
        response = client.put(
            "/v1/agent/config", json={"voiceModel": VOICE, "systemPrompt": ""}
        )

        assert response.status_code == 200

    def test_an_over_long_prompt_is_refused_with_a_countable_reason(self, client):
        response = client.put(
            "/v1/agent/config",
            json={
                "voiceModel": VOICE,
                "systemPrompt": "x" * (MAX_SYSTEM_PROMPT_CHARS + 500),
            },
        )

        assert response.status_code == 400
        assert response.json()["message"] == (
            "The system prompt is 16,500 characters — the limit is 16,000. "
            "Nothing was saved."
        )

    def test_a_refused_save_stores_nothing(self, client, store):
        client.put("/v1/agent/config", json={"voiceModel": "", "systemPrompt": "p"})

        assert store.load().agent is None


class TestPersonaEndpoint:
    def test_the_anti_robotic_ruleset_round_trips_byte_for_byte(self, client, store):
        """
        The ordinary submit. The panel ships this text as the textarea's
        `defaultValue`, so this exact string is what an untouched save sends.
        """
        response = client.put(
            "/v1/agent/persona", json={"rules": DEFAULT_PERSONA_RULES}
        )

        assert response.status_code == 200
        assert response.json()["rules"] == DEFAULT_PERSONA_RULES
        assert store.load().resolved_persona_rules == DEFAULT_PERSONA_RULES

    def test_paragraph_structure_is_not_normalised_away(self, client, store):
        rules = 'Never say "certainly".\n\nUse contractions.\n\n\nKeep turns short.'

        client.put("/v1/agent/persona", json={"rules": rules})

        assert store.load().resolved_persona_rules == rules

    def test_clearing_the_rules_is_accepted(self, client):
        response = client.put("/v1/agent/persona", json={"rules": ""})

        assert response.status_code == 200
        assert response.json()["rules"] == ""

    def test_cleared_rules_are_not_silently_replaced_with_the_default(
        self, client, store
    ):
        """
        The one thing this endpoint must never do. An operator who cleared
        the field has expressed "no constraints"; writing the default back
        would overrule them and look like a successful save.
        """
        client.put("/v1/agent/persona", json={"rules": ""})

        assert store.load().resolved_persona_rules == ""

    def test_hinglish_and_indian_punctuation_survive(self, client, store):
        rules = "Use “ji” and “theek hai”. Amounts in ₹ lakh — never dollars."

        client.put("/v1/agent/persona", json={"rules": rules})

        assert store.load().resolved_persona_rules == rules

    def test_an_over_long_ruleset_is_refused(self, client):
        response = client.put("/v1/agent/persona", json={"rules": "x" * 16_001})

        assert response.status_code == 400
        assert "The ruleset is 16,001 characters" in response.json()["message"]


class TestKnowledgeEndpoint:
    def test_unstructured_text_is_stored_as_one_opaque_block(self, client, store):
        context = (
            "Name: Priya Sharma\n"
            "5 years at Infosys, Bengaluru (2021-2026).\n\n"
            "Expected CTC: 28 lakh. Notice period: 60 days."
        )

        response = client.put("/v1/agent/knowledge", json={"context": context})

        assert response.status_code == 200
        assert store.load().resolved_knowledge == context

    def test_a_large_but_legal_block_is_accepted(self, client):
        response = client.put(
            "/v1/agent/knowledge", json={"context": "x" * MAX_KNOWLEDGE_CHARS}
        )

        assert response.status_code == 200

    def test_an_over_long_block_is_refused(self, client):
        response = client.put(
            "/v1/agent/knowledge", json={"context": "x" * (MAX_KNOWLEDGE_CHARS + 1)}
        )

        assert response.status_code == 400
        assert "the limit is 50,000" in response.json()["message"]

    def test_clearing_the_knowledge_is_accepted(self, client, store):
        client.put("/v1/agent/knowledge", json={"context": "facts"})
        client.put("/v1/agent/knowledge", json={"context": ""})

        assert store.load().resolved_knowledge == ""


# =============================================================================
# THE ERROR ENVELOPE
# =============================================================================

class TestErrorEnvelope:
    """
    Every failure path must carry `message`, because that is the only field
    the panel reads. A rejection that arrives as `detail` renders as "The
    agent service returned 4xx" and the actual reason never reaches the
    person who can fix it.
    """

    def test_a_validation_failure_uses_message_not_detail(self, client):
        response = client.put("/v1/agent/persona", json={})

        assert "message" in response.json()
        assert "detail" not in response.json()

    def test_a_missing_field_names_the_field(self, client):
        response = client.put("/v1/agent/persona", json={})

        assert response.status_code == 400
        assert response.json()["message"] == (
            'The field "rules" is missing from the request. Nothing was saved.'
        )

    def test_an_unexpected_field_is_refused_rather_than_dropped(self, client):
        """
        Silently discarding an unknown field is the worst outcome available:
        the panel reports success and the agent runs the old value.
        """
        response = client.put(
            "/v1/agent/persona", json={"rules": "r", "temperature": 0.9}
        )

        assert response.status_code == 400
        assert '"temperature" is not a field this endpoint accepts' in (
            response.json()["message"]
        )

    def test_a_wrong_type_is_reported_readably(self, client):
        response = client.put("/v1/agent/persona", json={"rules": 42})

        assert response.status_code == 400
        assert 'The field "rules"' in response.json()["message"]

    def test_a_malformed_body_is_reported_readably(self, client):
        response = client.put(
            "/v1/agent/persona",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json()["message"] == (
            "The request body was not valid JSON. Nothing was saved."
        )

    def test_an_undecodable_body_is_reported_in_this_apis_voice(self, client):
        """
        Invalid UTF-8 reaches FastAPI as a bare HTTPException, not a
        validation error, so it bypasses the handler above and arrives
        worded by the framework. Found by sending one to the running
        service. It matters because the framework's wording omits "Nothing
        was saved" — the only part that tells the operator whether they need
        to retype what they had.
        """
        response = client.put(
            "/v1/agent/persona",
            content=b'{"rules":"\xff\xfe"}',
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 400
        message = response.json()["message"]
        assert "UTF-8" in message
        assert message.endswith("Nothing was saved.")

    def test_every_rejection_says_what_happened_to_the_data(self, client):
        """
        The envelope's whole promise. An operator staring at a failed save
        needs to know whether to retype the text they just lost, and that
        answer has to be in the sentence -- there is nowhere else for it to
        appear.
        """
        rejections = [
            client.put("/v1/agent/persona", json={}),
            client.put("/v1/agent/persona", json={"rules": 42}),
            client.put("/v1/agent/persona", json={"rules": "r", "extra": 1}),
            client.put("/v1/agent/persona", json={"rules": "x" * 16_001}),
            client.put("/v1/agent/config", json={"voiceModel": "", "systemPrompt": ""}),
            client.put(
                "/v1/agent/persona",
                content=b"{not json",
                headers={"content-type": "application/json"},
            ),
        ]

        for response in rejections:
            assert response.status_code >= 400
            message = response.json()["message"]
            # "Pick a voice model before saving." is the one deliberate
            # exception: it is an instruction, and nothing was sent to save.
            assert message.endswith("Nothing was saved.") or message.endswith(
                "before saving."
            ), message

    def test_an_unknown_route_uses_message_not_detail(self, client):
        response = client.get("/v1/nope")

        assert response.status_code == 404
        assert "message" in response.json()
        assert "detail" not in response.json()

    def test_a_wrong_method_uses_message_not_detail(self, client):
        response = client.post("/v1/agent/persona", json={"rules": "r"})

        assert response.status_code == 405
        assert "message" in response.json()

    def test_a_storage_failure_is_a_500_that_leaks_nothing(self, store, monkeypatch):
        """
        A full disk or a permissions error on the config path. The operator
        gets a sentence; the path and the traceback stay in the logs.
        """
        def _explode(_config):
            raise OSError(r"C:\secrets\agent_config.json: disk full")

        monkeypatch.setattr(store, "save_persona", _explode)
        monkeypatch.delenv("SHUO_CONFIG_API_TOKEN", raising=False)

        with TestClient(config_api.app, raise_server_exceptions=False) as client:
            response = client.put("/v1/agent/persona", json={"rules": "r"})

        assert response.status_code == 500
        body = response.json()
        assert "message" in body
        assert "secrets" not in body["message"]
        assert "disk full" not in body["message"]


# =============================================================================
# READ AND HEALTH
# =============================================================================

class TestReadEndpoints:
    def test_health_reports_where_config_lives(self, client, store):
        body = client.get("/health").json()

        assert body["status"] == "ok"
        assert body["config_path"] == str(store.path)
        assert body["configured"] is False

    def test_health_reports_configured_once_something_is_saved(self, client):
        client.put("/v1/agent/persona", json={"rules": "r"})

        assert client.get("/health").json()["configured"] is True

    def test_the_read_endpoint_separates_stored_from_resolved(self, client):
        """
        `stored` is what is on disk; `resolved` is what the agent would
        actually run with. On an unconfigured install they differ exactly
        where the persona default applies -- which is the point of showing
        both.
        """
        body = client.get("/v1/config").json()

        assert body["stored"]["persona"] is None
        assert body["resolved"]["personaRules"] == DEFAULT_PERSONA_RULES
        assert body["resolved"]["systemPrompt"] is None

    def test_the_read_endpoint_reflects_a_save(self, client):
        client.put("/v1/agent/config", json={"voiceModel": VOICE, "systemPrompt": "p"})

        resolved = client.get("/v1/config").json()["resolved"]
        assert resolved["voiceModel"] == VOICE
        assert resolved["systemPrompt"] == "p"

    def test_the_read_endpoint_resolves_the_voice_to_something_speakable(
        self, client
    ):
        """
        The question this curl is actually asked to answer is "will the next
        call produce audio", and a stored `el-george` does not answer it.
        `resolved.voice` does, because it is looked up rather than echoed.
        """
        client.put("/v1/agent/config", json={"voiceModel": VOICE, "systemPrompt": "p"})

        voice = client.get("/v1/config").json()["resolved"]["voice"]
        assert voice["providerVoiceId"] == "JBFqnCBsd6RMkjVDRZzb"
        assert voice["available"] is True

    def test_an_unconfigured_install_resolves_no_voice_rather_than_erroring(
        self, client
    ):
        assert client.get("/v1/config").json()["resolved"]["voice"] is None


# =============================================================================
# THE NOTIFIER IN THE APP (W5d)
# =============================================================================

class TestTheNotifierIsWiredButOff:
    """
    W5d lives in *this* process, and the property worth pinning is that it is
    **off**: an unconfigured install must create no task and send nothing. The
    notifier's own behaviour is `tests/test_notify.py`; this is the seam.
    """

    def test_health_reports_it_off_on_a_fresh_install(self, client, monkeypatch):
        monkeypatch.delenv("SHUO_NOTIFY_URL", raising=False)

        block = client.get("/health").json()["notifications"]

        assert block["enabled"] is False
        assert block["target"] == ""
        assert block["sent"] == 0

    def test_no_poller_runs_when_it_is_not_configured(self, store, monkeypatch):
        monkeypatch.delenv("SHUO_NOTIFY_URL", raising=False)
        from shuo import notify

        notify.NOTIFIER.reset()
        with TestClient(config_api.app) as client:
            assert client.get("/health").json()["notifications"]["running"] is False

    def test_the_lifespan_starts_and_stops_the_poller(self, store, monkeypatch):
        monkeypatch.setenv("SHUO_NOTIFY_URL", "https://ntfy.sh/a-secret-topic")
        from shuo import notify

        notify.NOTIFIER.reset()
        with TestClient(config_api.app) as client:
            body = client.get("/health").json()["notifications"]
            assert body["enabled"] is True
            assert body["running"] is True

        # Stopped on the way out, so a reload does not leave a task behind.
        #
        # Asserted on `_task is None` rather than only on `running`, because a
        # closing event loop cancels pending tasks by itself -- so `running`
        # goes false whether or not the lifespan ever called `stop()`, and a
        # test that checked only that would pass with the shutdown deleted.
        # Releasing the pooled HTTP client is the part nothing else does.
        assert notify.NOTIFIER._task is None
        assert notify.NOTIFIER.stats()["running"] is False

    def test_health_never_carries_the_topic(self, store, monkeypatch):
        """
        🔴 On ntfy the topic in the URL *is* the credential, and `/health` is
        the endpoint people paste into issues.
        """
        monkeypatch.setenv("SHUO_NOTIFY_URL", "https://ntfy.sh/a-secret-topic")
        from shuo import notify

        notify.NOTIFIER.reset()
        with TestClient(config_api.app) as client:
            assert "a-secret-topic" not in client.get("/health").text


# =============================================================================
# THE THREE PER-SCREEN READS
# =============================================================================

class TestTheThreeReads:
    """
    The read half of the three form screens.

    What these defend is the reload: before they existed the panel came back
    from a refresh with three empty textareas, which does not mean "nothing is
    configured", it means "this screen does not know". An operator who then
    pressed Save wrote that emptiness over a working prompt.
    """

    def test_each_read_answers_in_the_shape_its_write_returns(self, client):
        """
        One shape per screen, not two. The panel already types the PUT
        response; a read that answered in a different shape would need a
        second type on that side and would be free to drift from this one.
        """
        client.put(
            "/v1/agent/config", json={"voiceModel": VOICE, "systemPrompt": "Be brief."}
        )

        body = client.get("/v1/agent/config").json()
        assert set(body) == {"voiceModel", "systemPrompt", "updatedAt"}
        assert body["voiceModel"] == VOICE
        assert body["systemPrompt"] == "Be brief."
        assert body["updatedAt"]

    def test_a_saved_prompt_survives_a_reload(self, client):
        client.put(
            "/v1/agent/config",
            json={"voiceModel": VOICE, "systemPrompt": "Answer as a candidate."},
        )

        assert (
            client.get("/v1/agent/config").json()["systemPrompt"]
            == "Answer as a candidate."
        )

    def test_a_saved_knowledge_block_survives_a_reload(self, client):
        client.put("/v1/agent/knowledge", json={"context": "Three years at X."})

        body = client.get("/v1/agent/knowledge").json()
        assert set(body) == {"context", "updatedAt"}
        assert body["context"] == "Three years at X."

    def test_nothing_configured_reads_as_empty_strings_not_null(self, client):
        """
        🔴 React renders `null` in a `defaultValue` as a field with no value
        -- the same blank box this endpoint exists to prevent, reached by a
        different route. An empty string is a textarea showing its
        placeholder, which is what "nothing configured" should look like.
        """
        body = client.get("/v1/agent/config").json()

        assert body["voiceModel"] == ""
        assert body["systemPrompt"] == ""
        assert body["updatedAt"] is None

        assert client.get("/v1/agent/knowledge").json()["context"] == ""

    def test_unconfigured_persona_rules_read_as_the_default(self, client):
        """
        The screen has to show the constraints that are actually in force, and
        on an install nobody has configured those are the ruleset -- the same
        answer `runtime_config` gets, from the same property, so the panel and
        the call cannot disagree.
        """
        body = client.get("/v1/agent/persona").json()

        assert set(body) == {"rules", "updatedAt"}
        assert body["rules"] == DEFAULT_PERSONA_RULES
        assert body["updatedAt"] is None

    def test_a_deliberately_cleared_ruleset_reads_back_empty(self, client):
        """
        🔴 The asymmetry that makes the whole read path safe. "Never saved"
        and "saved empty" are different states, and only the first resolves to
        the default. If a clear read back as the ruleset, a reload would
        resurrect the rules and the next Save would write them to disk --
        undoing an operator's decision with a page load.
        """
        client.put("/v1/agent/persona", json={"rules": ""})

        body = client.get("/v1/agent/persona").json()
        assert body["rules"] == ""
        assert body["updatedAt"], "the clear was still a save and is stamped"

    def test_a_read_never_says_nothing_was_saved(self, client):
        """
        The outcome sentence has to describe what happened to the operator's
        data. On a read nothing happened to it, and "Nothing was saved."
        describes a save they never asked for.
        """
        response = client.get("/v1/agent/nonexistent")

        assert response.status_code == 404
        assert "Nothing was saved" not in response.json()["message"]

    def test_an_unresolvable_saved_voice_is_returned_unchanged(
        self, client, store, monkeypatch
    ):
        """
        Entitlements lapse under a file nobody touched. Substituting a working
        voice here would make that look like a save that never happened; the
        picker simply will not find it and falls back to its own default.
        """
        from shuo.config_store import AgentSection

        document = store.load()
        document.agent = AgentSection(
            voice_model="el-retired", system_prompt="p", updated_at="2026-01-01T00:00:00+00:00"
        )
        store._write(document)

        assert client.get("/v1/agent/config").json()["voiceModel"] == "el-retired"


# =============================================================================
# THE CALL LOG
# =============================================================================

class TestCallHistoryEndpoint:
    """
    Read straight off disk here rather than proxied from :3040 -- history is
    a file, not live state, so the log loads with `main.py` stopped and no
    route is added to the app that paces 20ms audio frames.
    """

    @pytest.fixture
    def history(self, tmp_path, monkeypatch):
        path = tmp_path / "call_history.jsonl"
        monkeypatch.setenv("SHUO_CALL_HISTORY_PATH", str(path))
        return path

    def _write(self, path, *records):
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    def test_no_calls_yet_is_an_empty_list_not_an_error(self, client, history):
        response = client.get("/v1/calls/history")

        assert response.status_code == 200
        assert response.json() == {"calls": [], "count": 0}

    def test_it_serves_the_newest_call_first(self, client, history):
        self._write(
            history,
            {"id": "call-1", "callId": "MZ-1", "status": "completed"},
            {"id": "call-2", "callId": "MZ-2", "status": "missed"},
        )

        body = client.get("/v1/calls/history").json()
        assert [row["callId"] for row in body["calls"]] == ["MZ-2", "MZ-1"]
        assert body["count"] == 2

    def test_limit_is_honoured_and_bounded(self, client, history):
        self._write(
            history,
            *[{"id": f"call-{i}", "callId": f"MZ-{i}"} for i in range(5)],
        )

        assert client.get("/v1/calls/history?limit=2").json()["count"] == 2
        # Above the ceiling is a rejection, not a silent clamp: a panel asking
        # for ten thousand rows has a bug, and serving 200 hides it.
        assert client.get("/v1/calls/history?limit=100000").status_code == 400

    def test_a_torn_record_does_not_break_the_screen(self, client, history):
        history.write_text(
            json.dumps({"id": "call-1", "callId": "MZ-1"}) + "\n" + '{"id": "call-2',
            encoding="utf-8",
        )

        body = client.get("/v1/calls/history").json()
        assert [row["callId"] for row in body["calls"]] == ["MZ-1"]

    def test_health_says_where_the_call_log_is(self, client, history):
        """
        An empty call-log screen has two very different causes -- no calls
        yet, or the two processes disagreeing about the path. This is the only
        place that tells them apart.
        """
        body = client.get("/health").json()

        assert body["call_history_path"] == str(history)
        assert body["calls_recorded"] == 0


# =============================================================================
# THE VOICE CATALOGUE
# =============================================================================

class TestVoicesEndpoint:
    def test_it_lists_the_voices(self, client):
        response = client.get("/v1/voices")

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(body["voices"]) > 0

    def test_every_entry_carries_what_the_picker_renders(self, client):
        for voice in client.get("/v1/voices").json()["voices"]:
            for key in ("id", "name", "provider", "description", "locale"):
                assert voice[key]

    def test_nothing_unusable_is_offered(self, client):
        for voice in client.get("/v1/voices").json()["voices"]:
            assert voice["available"] is True
            assert voice["providerVoiceId"]

    def test_the_old_fixture_ids_are_gone_from_the_list(self, client):
        listed = {voice["id"] for voice in client.get("/v1/voices").json()["voices"]}

        assert not listed & {"el-rachel-v2", "el-adam-turbo", "oa-alloy", "ct-sonic-hi"}

    def test_it_needs_the_token_when_one_is_configured(self, store, monkeypatch):
        """The guard is middleware precisely so a route added later cannot
        forget it. This is that check, for the route added later."""
        monkeypatch.setenv("SHUO_CONFIG_API_TOKEN", "s3cret")

        with TestClient(config_api.app) as client:
            assert client.get("/v1/voices").status_code == 401


class TestThePickerAndTheValidatorAgree:
    """
    The one invariant that makes the catalogue worth having.

    Open question 11 was not "the IDs are wrong" -- it was that the list the
    operator picks from and the check the save runs were two different tables
    that nobody had ever compared. They are now one table, and this is the
    test that keeps them one.
    """

    def test_every_offered_voice_can_actually_be_saved(self, client):
        for voice in client.get("/v1/voices").json()["voices"]:
            response = client.put(
                "/v1/agent/config",
                json={"voiceModel": voice["id"], "systemPrompt": "p"},
            )
            assert response.status_code == 200, (
                f'GET /v1/voices offers {voice["id"]} but PUT refuses it: '
                f'{response.json().get("message")}'
            )

    def test_a_voice_that_is_not_offered_is_refused(self, client):
        response = client.put(
            "/v1/agent/config",
            json={"voiceModel": "ct-sonic-hi", "systemPrompt": "p"},
        )

        assert response.status_code == 400
        assert "only synthesises through ElevenLabs" in response.json()["message"]

    def test_the_refusal_reaches_the_save_row_as_a_sentence(self, client):
        """
        The panel renders `message` verbatim. A rejected voice has to explain
        itself there or the operator is left with a red row and no next step
        -- and the alternative to explaining it here is finding out on a call
        that goes silent (decision 29).
        """
        message = client.put(
            "/v1/agent/config",
            json={"voiceModel": "el-krish", "systemPrompt": "p"},
        ).json()["message"]

        assert message.startswith('"el-krish" (Krish) is')
        assert "Upgrade the plan" in message
        assert message.endswith("Nothing was saved.")

    def test_a_refused_voice_stores_nothing(self, client, store):
        client.put(
            "/v1/agent/config",
            json={"voiceModel": "oa-alloy", "systemPrompt": "p"},
        )

        assert store.load().agent is None


# =============================================================================
# GUARDS
# =============================================================================

class TestTokenGuard:
    def test_no_token_is_required_by_default(self, client):
        assert client.put("/v1/agent/persona", json={"rules": "r"}).status_code == 200

    def test_a_configured_token_is_enforced(self, store, monkeypatch):
        monkeypatch.setenv("SHUO_CONFIG_API_TOKEN", "s3cret")

        with TestClient(config_api.app) as client:
            response = client.put("/v1/agent/persona", json={"rules": "r"})

        assert response.status_code == 401
        assert response.json()["message"] == "Not authorised. Nothing was saved."

    def test_the_right_token_is_accepted(self, store, monkeypatch):
        monkeypatch.setenv("SHUO_CONFIG_API_TOKEN", "s3cret")

        with TestClient(config_api.app) as client:
            response = client.put(
                "/v1/agent/persona",
                json={"rules": "r"},
                headers={"x-shuo-config-token": "s3cret"},
            )

        assert response.status_code == 200

    def test_a_wrong_token_is_refused(self, store, monkeypatch):
        monkeypatch.setenv("SHUO_CONFIG_API_TOKEN", "s3cret")

        with TestClient(config_api.app) as client:
            response = client.put(
                "/v1/agent/persona",
                json={"rules": "r"},
                headers={"x-shuo-config-token": "wrong"},
            )

        assert response.status_code == 401

    def test_the_token_gate_covers_reads_too(self, store, monkeypatch):
        monkeypatch.setenv("SHUO_CONFIG_API_TOKEN", "s3cret")

        with TestClient(config_api.app) as client:
            # /v1/config hands out the prompt and the fact block.
            assert client.get("/v1/config").status_code == 401


class TestBodySizeGuard:
    def test_an_oversized_body_is_refused_before_it_is_parsed(self, client):
        response = client.put(
            "/v1/agent/knowledge",
            content=b"x" * (config_api.MAX_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 413
        assert "too large" in response.json()["message"]

    def test_the_largest_legal_payload_still_fits(self, client):
        """
        The ceiling must sit above a full 50,000-character knowledge block,
        or the body guard would reject a payload the field validator accepts.
        """
        payload = json.dumps({"context": "x" * MAX_KNOWLEDGE_CHARS})
        assert len(payload.encode("utf-8")) < config_api.MAX_BODY_BYTES

        response = client.put(
            "/v1/agent/knowledge",
            content=payload.encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200


class TestExposurePolicy:
    """
    Decision 16 says operator endpoints fail closed. Here that is enforced
    through the bind address: loopback needs no token because there is no
    remote caller to authenticate, and anything else does.
    """

    def test_loopback_needs_no_token(self, monkeypatch):
        monkeypatch.delenv("SHUO_CONFIG_API_TOKEN", raising=False)

        config_api.enforce_exposure_policy("127.0.0.1")
        config_api.enforce_exposure_policy("localhost")
        config_api.enforce_exposure_policy("::1")

    def test_binding_to_all_interfaces_without_a_token_is_refused(self, monkeypatch):
        monkeypatch.delenv("SHUO_CONFIG_API_TOKEN", raising=False)

        with pytest.raises(RuntimeError, match="SHUO_CONFIG_API_TOKEN"):
            config_api.enforce_exposure_policy("0.0.0.0")

    def test_binding_to_a_lan_address_without_a_token_is_refused(self, monkeypatch):
        monkeypatch.delenv("SHUO_CONFIG_API_TOKEN", raising=False)

        with pytest.raises(RuntimeError):
            config_api.enforce_exposure_policy("192.168.1.20")

    def test_a_token_unlocks_a_public_bind(self, monkeypatch):
        monkeypatch.setenv("SHUO_CONFIG_API_TOKEN", "s3cret")

        config_api.enforce_exposure_policy("0.0.0.0")


# =============================================================================
# ISOLATION FROM THE AUDIO PIPELINE
# =============================================================================

def _imported_modules(module) -> set:
    """
    The module names a source file actually imports.

    Parsed from the AST rather than grepped from the text, because a text
    scan is wrong in both directions here: it trips over prose in a docstring
    that *names* a forbidden module, and it would miss a real
    `importlib.import_module` call. Relative imports contribute the module
    part, so `from ..services.player import X` shows up as `services.player`.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
        elif isinstance(node, ast.Call):
            # importlib.import_module("shuo.conversation") would evade the
            # two branches above.
            func = node.func
            attr = getattr(func, "attr", None) or getattr(func, "id", None)
            if attr in {"import_module", "__import__"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
    return names


# Modules that carry, pace or terminate live audio. None of them may be
# reachable from the config layer -- that is what keeps a Save off the event
# loop the player's 20ms deadline runs on.
_REALTIME_MODULES = {
    "conversation",
    "server",
    "agent",
    "state",
    "carrier",
    # W3's live view lives in the *call* process. The config API reads it
    # over HTTP through `call_client.py`; importing it here would mean this
    # process holding a monitor that no call ever publishes into, which fails
    # as an empty panel rather than as an error.
    "call_monitor",
    "services",
    "services.player",
    "services.tts",
    "services.tts_pool",
    "services.llm",
    "services.flux",
}


class TestIsolation:
    """
    The architectural guarantee, pinned rather than described. A later change
    that mounts these routes on the call-serving app, or pulls a pipeline
    module into this one, should fail here.
    """

    def test_the_config_api_does_not_import_the_audio_pipeline(self):
        imported = _imported_modules(config_api)

        offenders = {
            name
            for name in imported
            if any(
                name == forbidden or name.startswith(forbidden + ".")
                for forbidden in _REALTIME_MODULES
            )
        }
        assert not offenders, (
            f"{sorted(offenders)} reached shuo/config_api.py — the config API "
            f"must stay decoupled from the real-time path"
        )

    def test_the_store_does_not_import_the_audio_pipeline(self):
        from shuo.config_store import models as models_module
        from shuo.config_store import store as store_module

        for module in (store_module, models_module):
            imported = _imported_modules(module)
            offenders = {
                name
                for name in imported
                if any(
                    name == forbidden or name.startswith(forbidden + ".")
                    for forbidden in _REALTIME_MODULES
                )
            }
            assert not offenders, f"{sorted(offenders)} reached {module.__name__}"

    def test_the_call_server_has_no_config_routes(self):
        """
        These must not appear on the app that shares an event loop with the
        media WebSocket. A blocking fsync there is permanent stream delay for
        the rest of the call (decision 24).
        """
        from shuo.server import app as call_app

        paths = {route.path for route in call_app.routes}
        assert "/v1/agent/config" not in paths
        assert "/v1/agent/persona" not in paths
        assert "/v1/agent/knowledge" not in paths

    def test_the_two_apps_are_different_objects(self):
        from shuo.server import app as call_app

        assert config_api.app is not call_app
