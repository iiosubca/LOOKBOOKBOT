"""Language-neutral validation helpers for visual credit proof notes.

This module deliberately uses only the Python standard library so the map
writer and the desktop application can enforce the same evidence rule without
depending on an AI provider or an installed Codex skill.
"""
from __future__ import annotations

import re


# Proof reviewers can legitimately answer in Russian or English.  These are
# visual categories, not a language gate: a note must identify at least two
# independently visible features before it can confirm a credit card.
IDENTITY_CUE_PATTERNS: dict[str, str] = {
    "model": (
        r"(?:\b(?:model|woman|man|female|male|person)\b|"
        r"модел|женщин|мужчин|девушк|парен)"
    ),
    "garment": (
        r"(?:\b(?:coat|trench|jacket|blazer|shirt|top|dress|skirt|trouser|pants|jeans|shorts|"
        r"jumpsuit|vest|suit|cardigan|hoodie|sweater|polo|swimwear|bikini|bodysuit|gown)\b|"
        r"пальто|плащ|куртк|жакет|пиджак|рубаш|топ|плать|юбк|брюк|джинс|шорт|комбинез|жилет|"
        r"костюм|кардиган|худи|свитер|поло|купаль|боди)"
    ),
    "colour": (
        r"(?:\b(?:black|white|grey|gray|brown|beige|pink|blue|red|green|yellow|orange|purple|"
        r"gold|silver|denim|colour|color)\b|"
        r"ч[её]рн|бел|сер|корич|беж|розов|син|голуб|красн|зел[её]н|желт|оранж|фиолет|золот|серебр|"
        r"деним|цвет)"
    ),
    "bag": r"(?:\b(?:bag|tote|clutch|pouch|handbag|crossbody)\b|сумк|клатч|тоут|ридикюл)",
    "shoes": (
        r"(?:\b(?:shoe|shoes|sandal|boot|sneaker|heel|loafer|pump|ballet|trainer)\b|"
        r"туфл|босонож|ботин|сапог|кроссов|лофер|балетк|обув)"
    ),
    "accessory": (
        r"(?:\b(?:glasses|sunglasses|scarf|belt|bracelet|necklace|earring|hat|cap|jewell?ry|watch)\b|"
        r"очк|шарф|платок|рем[её]н|браслет|колье|серьг|шляп|кепк|украшен|час[ыов])"
    ),
    "pose": r"(?:\b(?:standing|seated|sitting|full[- ]length|side[- ]pose|pose)\b|в\s+полный\s+рост|сидит|стоит|поза)",
}


def visible_identity_cue_categories(note: str) -> set[str]:
    """Return the grounded visual categories named in a review note.

    Explicit labels such as ``garment=...; bag=...`` are intentionally
    recognised as well as natural-language descriptions.
    """
    lowered = str(note).casefold()
    return {
        category
        for category, pattern in IDENTITY_CUE_PATTERNS.items()
        if re.search(pattern, lowered, flags=re.IGNORECASE)
    }
