"""Filesystem locations used by the app.

Wraps ``%LOCALAPPDATA%\\FinanceApp`` so the SQLite database (and any future
per-user files) all live in one well-known place that survives reinstalls
and isn't visible in the user's regular Documents folder.
"""
from __future__ import annotations

import os
from pathlib import Path


# ----- App data directory -------------------------------------------------
def app_data_dir() -> Path:
    """Return the per-user app folder (does NOT create it — see
    :func:`ensure_app_data_exists`).

    Mirrors C# ``AppPaths.AppDataDirectory`` (LocalApplicationData/FinanceApp).
    Falls back to ``~/AppData/Local/FinanceApp`` if ``LOCALAPPDATA`` is unset.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "FinanceApp"
    # Fallback (should be rare on Windows)
    return Path.home() / "AppData" / "Local" / "FinanceApp"


# ----- Specific files -----------------------------------------------------
def database_path() -> Path:
    """Full path to the main SQLite database file."""
    return app_data_dir() / "finance.db"


def ensure_app_data_exists() -> None:
    """Create the app data folder if it doesn't exist yet."""
    app_data_dir().mkdir(parents=True, exist_ok=True)
