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
        r"jumpsuit|vest|suit|cardigan|hoodie|sweater|polo|swimwear|bikini|bodysuit|gown|"
        r"cape|poncho|shearling|turtleneck|pullover)\b|"
        r"пальто|плащ|куртк|жакет|пиджак|рубаш|топ|плать|юбк|брюк|джинс|шорт|комбинез|жилет|"
        r"костюм|кардиган|худи|свитер|поло|купаль|боди|накидк|пончо|дубл[её]н|водолаз|джемпер)"
    ),
    "colour": (
        r"(?:\b(?:black|white|grey|gray|brown|beige|pink|blue|red|green|yellow|orange|purple|"
        r"gold|silver|denim|colour|color|mustard|burgundy|khaki|cream|navy|ivory|camel)\b|"
        r"ч[её]рн|бел|сер|корич|беж|розов|син|голуб|красн|зел[её]н|желт|оранж|фиолет|золот|серебр|"
        r"деним|цвет|горчич|бордов|хаки|кремов|молочн|ж[её]лт)"
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
    explicit: set[str] = set()
    # A nonempty labelled visual value does not have to appear in a finite
    # English/Russian clothing dictionary. Never count bare labels or fillers.
    aliases = {"color": "colour", "accessories": "accessory", "clothing": "garment"}
    def labelled(match: re.Match[str]) -> str:
        category = aliases.get(match.group(1), match.group(1))
        value = match.group(2).strip()
        if (len(value) >= 3 and re.search(r"[^\W\d_]", value)
                and value not in {"unknown", "none", "n/a", "same", "matching", "matches", "present",
                                  "not visible", "не видно", "нет", "совпадает", "одинаковые"}
                and not re.fullmatch(r"<.*>|\[.*\]", value)):
            explicit.add(category)
            return value
        return ""
    prose = re.sub(
        r"\b(model|garment|clothing|colour|color|bag|shoes|accessory|accessories|pose)\s*[:=]\s*([^;\n]*)(?:;|$)",
        labelled, lowered,
    )
    return explicit | {
        category
        for category, pattern in IDENTITY_CUE_PATTERNS.items()
        if re.search(pattern, prose, flags=re.IGNORECASE)
    }


def visual_observation_is_specific(note: str) -> bool:
    return len(str(note).strip()) >= 28 and len(visible_identity_cue_categories(note)) >= 2
