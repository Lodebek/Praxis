"""Configuration loading.

Reads ``config.json`` from the project root (falling back to
``config.example.json`` defaults for anything missing). The Plex token is
optional in the file: if blank, :func:`praxis.plex.get_token` reads it straight
from the Windows registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "praxis.db"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto ``base`` (returns a new dict)."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config() -> dict[str, Any]:
    """Load merged config: example defaults overlaid with the user's config.json."""
    defaults: dict[str, Any] = {}
    if EXAMPLE_PATH.exists():
        defaults = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))

    user: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    return _deep_merge(defaults, user)
