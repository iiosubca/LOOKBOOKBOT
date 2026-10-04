from lookbookbot.codex_catalog import _parse_model


def test_catalog_uses_account_specific_reasoning_levels_and_filters_non_image_models() -> None:
    visible = _parse_model({
        "model": "gpt-6-luna", "displayName": "GPT-6 Luna", "inputModalities": ["text", "image"],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"}, {"reasoningEffort": "medium"}, {"reasoningEffort": "max"},
        ],
    })

    assert visible is not None
    assert visible.model == "gpt-6-luna"
    assert visible.reasoning_efforts == ("low", "medium", "max")
    assert visible.default_reasoning_effort == "medium"
    assert _parse_model({"model": "text-only", "inputModalities": ["text"]}) is None
    assert _parse_model({"model": "hidden", "hidden": True}) is None
