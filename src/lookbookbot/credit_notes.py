"""Use the exact bundled controller note contract before accepting AI output."""
from functools import lru_cache
import importlib.util

from .config import bundled_engine_scripts


@lru_cache(maxsize=1)
def _rules():
    source = bundled_engine_scripts() / "credit_note_rules.py"
    spec = importlib.util.spec_from_file_location("lookbookbot_credit_note_rules", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Bundled credit-note validator is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def visual_observation_is_specific(note: str) -> bool:
    return _rules().visual_observation_is_specific(note)
