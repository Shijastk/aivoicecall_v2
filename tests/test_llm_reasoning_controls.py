from shuo.services.llm import _provider_extra_body


def test_qwen_38_disables_reasoning_and_hides_reasoning_output():
    assert _provider_extra_body("qwen/qwen3.8-27b") == {
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
    }


def test_qwen_36_uses_same_non_thinking_hidden_contract():
    assert _provider_extra_body("qwen/qwen3.6-27b") == {
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
    }


def test_gpt_oss_keeps_existing_reasoning_contract():
    assert _provider_extra_body("openai/gpt-oss-120b") == {
        "reasoning_effort": "low",
        "include_reasoning": False,
    }


def test_unrelated_models_get_no_provider_specific_reasoning_controls():
    assert _provider_extra_body("other/model") == {}
