from __future__ import annotations

"""Keep provider secrets outside the project database and delivery folders."""

import os

try:
    import keyring
except ImportError:  # The application still works with GOOGLE_API_KEY during setup.
    keyring = None  # type: ignore[assignment]


_SERVICE = "LOOKBOOKBOT"
_GOOGLE_ACCOUNT = "google-ai-studio"


def get_google_api_key() -> str:
    """Read the key from Windows Credential Manager, then environment as a fallback."""
    if keyring is not None:
        try:
            saved = keyring.get_password(_SERVICE, _GOOGLE_ACCOUNT)
            if saved:
                return saved.strip()
        except Exception:
            pass
    return os.environ.get("GOOGLE_API_KEY", "").strip()


def save_google_api_key(value: str) -> bool:
    """Persist a non-empty key in Windows Credential Manager when available."""
    key = value.strip()
    if not key:
        return False
    os.environ["GOOGLE_API_KEY"] = key
    if keyring is None:
        return False
    try:
        keyring.set_password(_SERVICE, _GOOGLE_ACCOUNT, key)
    except Exception:
        return False
    return True
