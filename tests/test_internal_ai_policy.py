from __future__ import annotations

from lookbookbot.ai_policy import POLICY_MARKER, read_only_vision_prompt, targeted_credit_rematch_prompt


def test_read_only_policy_is_embedded_and_idempotent() -> None:
    prompt = read_only_vision_prompt("Return JSON only.")

    assert prompt.startswith(POLICY_MARKER)
    assert "Do not load, read, invoke, or rely on any Codex Skill" in prompt
    assert read_only_vision_prompt(prompt) == prompt


def test_targeted_rematch_is_self_contained_and_skill_free() -> None:
    prompt = targeted_credit_rematch_prompt(r"C:\project", ["LOOK_004", "LOOK_008"])

    assert "LOOK_004, LOOK_008" in prompt
    assert "Do not load or invoke any Codex Skill" in prompt
    assert "control/work/ui-overrides" not in prompt
    assert "lookbook-layout" not in prompt.casefold()
