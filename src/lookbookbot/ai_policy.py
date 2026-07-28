"""Versioned, provider-neutral instructions for LOOKBOOKBOT vision work.

These rules intentionally live in the application rather than in a Codex
skill.  Every provider receives the same non-negotiable visual policy while
the controller remains the only component that can change the project.
"""
from __future__ import annotations


POLICY_MARKER = "LOOKBOOKBOT_INTERNAL_VISION_POLICY_V1"

READ_ONLY_VISION_POLICY = f"""{POLICY_MARKER}
You are the visual-verification component of LOOKBOOKBOT, not an autonomous
layout agent. Work only from the supplied proof images and the task text.

Non-negotiable rules:
- Do not load, read, invoke, or rely on any Codex Skill, SKILL.md, external
  instruction file, previous chat, file naming, ordering, gender, or studio
  background as evidence.
- Do not run commands, write files, edit TSV/INDD/PDF, open InDesign, or claim
  that another stage is complete. The LOOKBOOKBOT controller performs every
  durable action after it validates your answer.
- Judge a match only by visible evidence: model, garment, silhouette, colour,
  accessories, shoes, bag, pose, crop, and readable credits.
- Return exactly the requested structured response. Do not add Markdown or a
  narrative outside that response.
"""


def read_only_vision_prompt(task: str) -> str:
    """Attach the same closed-world policy to every visual provider request."""
    if task.lstrip().startswith(POLICY_MARKER):
        return task
    return f"{READ_ONLY_VISION_POLICY}\n\nTASK\n{task.strip()}"


def targeted_credit_rematch_prompt(project: str, looks: list[str]) -> str:
    """Self-contained Codex fallback for a difficult, targeted rematch only.

    This is a narrowly bounded recovery path.  It does not depend on an
    installed Codex skill and may work only on the explicitly named looks.
    """
    selected = ", ".join(looks)
    return f"""LOOKBOOKBOT_INTERNAL_TARGETED_REMATCH_V1
You are executing one bounded recovery operation for LOOKBOOKBOT.

Project: {project}
Allowed LOOK IDs: {selected}

Use only the project-local evidence and controller scripts already included
with LOOKBOOKBOT. Do not load or invoke any Codex Skill, SKILL.md, external
workflow, or previous project. Do not change any LOOK not in the allowed list.

For each allowed look, compare the PDF pair with the controlled Excel previews
by visible clothing, colour, model, bag, shoes, accessories, pose and crop.
Never choose by order, filenames, gender or score alone. If a different Excel
card is required, use the project-local alternative-proof and atomic selection
workflow only for the affected group of at most five looks; inspect the new
proof image before accepting it. Keep every non-target row frozen. Finish only
when each allowed row has a current visual proof and CONFIRMED status.

Do not run init, map, InDesign, composition, review-PDF, release, or final
export. Do not ask the user for input. Report a short factual completion note
after the controller-visible files are updated."""
