"""Versioned, provider-neutral instructions for LOOKBOOKBOT vision work.

These rules intentionally live in the application rather than in a Codex
skill.  Every provider receives the same non-negotiable visual policy while
the controller remains the only component that can change the project.
"""
from __future__ import annotations


POLICY_MARKER = "LOOKBOOKBOT_INTERNAL_VISION_POLICY_V3"
CREDIT_POLICY_MARKER = "LOOKBOOKBOT_CREDIT_IDENTITY_V3"

# Credit catalogue photographs are different takes, not pixel-identical shots.
# Keep this scoped to credits: PDF -> hi-res frame verification must still
# identify the exact requested photograph, including its pose/crop.
CREDIT_IDENTITY_POLICY = f"""{CREDIT_POLICY_MARKER}
Identify the catalogue OUTFIT across different photographs, not the same pose.
The Excel catalogue can show the SAME garments worn by a DIFFERENT PERSON.
Model identity, perceived gender, hair, face, skin tone, glasses, pose and crop
are NOT vetoes for credit matching. Compare distinctive main garments (cut,
material/texture, colour, closures, collars), layering and lower-body clothing
first. Require at least three concrete
matching cues, including a distinctive main garment and another clothing cue.
Do not accept merely the same model, gender, or similar-coloured clothes.
Pose, crop, lighting, hairstyle position and accessory visibility can change
between takes. Sunglasses worn in the Excel shot can be removed in another
take: their absence alone does not prove a different outfit. Never infer that
an item is absent simply because it is cropped, concealed or turned away.
A soft tote can be folded, tucked under the arm, or held with hidden handles;
it can then resemble a clutch. Compare its leather/material, seams, proportions
when unfolded and construction before asserting a different bag. Do not invent
a bag replacement merely from the way it is held. A buttoned or folded coat
may look asymmetric: compare collar, texture, seams and visible button rows
before asserting a different garment construction.
Record genuine catalogue-identity contradictions only: clearly different main
garment/cut/colour/texture, conflicting layering or lower-body clothing.
These must reject the match even if the model is the same. If those garments
match decisively, visibly changed shoes or accessories do NOT turn the outfit
into a different catalogue card. Record an actual accessory/shoe substitution
in styling_differences for product-credit review, never hide it or invent
replacement credits. Harmless folds, scale, poses and visibility changes belong
only in note, NOT contradictions or styling_differences.
If evidence is unreadable or the main garments remain uncertain, reject.
"""

READ_ONLY_VISION_POLICY = f"""{POLICY_MARKER}
You are the visual-verification component of LOOKBOOKBOT, not an autonomous
layout agent. Work only from the supplied proof images and the task text.

Non-negotiable rules:
- Do not load, read, invoke, or rely on any Codex Skill, SKILL.md, external
  instruction file, previous chat, file naming, ordering, Excel W/M sheet
  labels, counters, or studio background as evidence.
- Do not run commands, write files, edit TSV/INDD/PDF, open InDesign, or claim
  that another stage is complete. The LOOKBOOKBOT controller performs every
  durable action after it validates your answer.
- Judge a match only by visible evidence: model, garment, silhouette, colour,
  accessories, shoes, bag, pose, crop, and readable credits.
- For credit-catalogue matching, apply LOOKBOOKBOT_CREDIT_IDENTITY_V3:
  different people can wear the same catalogue outfit; clothing determines
  the card, while real styling substitutions must be reported separately.
  For PDF-to-hires photograph verification, still require the exact requested
  model, pose and photograph. Never relax that separate photo-order check.
  A sheet label is not visual evidence.
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
