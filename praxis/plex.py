"""Plex Media Server access.

Reads the auth token from the Windows registry (or config override), lists the
Movies + TV Shows libraries, and maps Plex's JSON metadata onto our ``media``
columns. Posters are fetched server-side so the token never reaches the browser.
"""

from __future__ import annotations

import json
from typing import Any

import requests

TOKEN_REG_PATH = r"Software\Plex, Inc.\Plex Media Server"
TOKEN_REG_VALUE = "PlexOnlineToken"
CAST_LIMIT = 6


class PlexError(RuntimeError):
    pass


def get_token(cfg: dict[str, Any]) -> str:
    """Return the Plex token: config override if set, else the Windows registry."""
    configured = (cfg.get("plex") or {}).get("token") or ""
    if configured.strip():
        return configured.strip()

    try:
        import winreg  # stdlib, Windows only
    except ImportError as exc:  # pragma: no cover - non-Windows
        raise PlexError(
            "No Plex token in config.json and registry lookup is only available "
            "on Windows. Set plex.token in config.json."
        ) from exc

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TOKEN_REG_PATH) as key:
            value, _ = winreg.QueryValueEx(key, TOKEN_REG_VALUE)
            if value:
                return str(value)
    except OSError as exc:
        raise PlexError(
            "Could not read the Plex token from the registry. Is Plex Media "
            "Server installed and signed in? You can also paste a token into "
            "config.json under plex.token."
        ) from exc

    raise PlexError("Plex token was empty. Set plex.token in config.json.")


def _client(cfg: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return (base_url, params-with-token, json-headers)."""
    plex = cfg.get("plex") or {}
    base = (plex.get("base_url") or "http://localhost:32400").rstrip("/")
    token = get_token(cfg)
    return base, {"X-Plex-Token": token}, {"Accept": "application/json"}


def _first(item: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return default


def _tags(item: dict[str, Any], key: str, limit: int | None = None) -> list[str]:
    raw = item.get(key) or []
    tags = [t.get("tag") for t in raw if isinstance(t, dict) and t.get("tag")]
    return tags[:limit] if limit else tags


def _to_int(val: Any) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_float(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def map_item(item: dict[str, Any], media_type: str, synced_at: int) -> dict[str, Any]:
    """Map one Plex metadata object to a ``media`` row dict."""
    return {
        "ratingKey": str(item.get("ratingKey")),
        "source": "plex",
        "enriched": 1,
        "type": media_type,
        "title": _first(item, "title", default="(untitled)"),
        "year": _to_int(item.get("year")),
        "genres": json.dumps(_tags(item, "Genre")),
        "summary": item.get("summary"),
        "studio": item.get("studio"),
        "content_rating": item.get("contentRating"),
        "critic_rating": _to_float(item.get("rating")),
        "audience_rating": _to_float(item.get("audienceRating")),
        "duration_ms": _to_int(item.get("duration")),
        "directors": json.dumps(_tags(item, "Director")),
        "writers": json.dumps(_tags(item, "Writer")),
        "cast": json.dumps(_tags(item, "Role", limit=CAST_LIMIT)),
        "country": json.dumps(_tags(item, "Country")),
        "tagline": item.get("tagline"),
        "thumb": item.get("thumb"),
        "added_at": _to_int(item.get("addedAt")),
        "updated_at": _to_int(item.get("updatedAt")),
        "last_synced": synced_at,
    }


def fetch_section(cfg: dict[str, Any], section_key: int, media_type: str,
                  synced_at: int) -> list[dict[str, Any]]:
    """Fetch all items in a library section, mapped to media rows."""
    base, params, headers = _client(cfg)
    url = f"{base}/library/sections/{section_key}/all"
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise PlexError(f"Failed to fetch section {section_key}: {exc}") from exc

    container = resp.json().get("MediaContainer", {})
    items = container.get("Metadata", []) or []
    return [map_item(it, media_type, synced_at) for it in items]


def fetch_library(cfg: dict[str, Any], synced_at: int) -> dict[str, Any]:
    """Fetch Movies + TV Shows. Returns {'rows': [...], 'counts': {...}}."""
    plex = cfg.get("plex") or {}
    movie_key = int(plex.get("movie_section", 1))
    show_key = int(plex.get("show_section", 2))

    movies = fetch_section(cfg, movie_key, "movie", synced_at)
    shows = fetch_section(cfg, show_key, "show", synced_at)
    return {
        "rows": movies + shows,
        "counts": {"movie": len(movies), "show": len(shows)},
    }


def fetch_thumb(cfg: dict[str, Any], thumb_path: str) -> tuple[bytes, str]:
    """Fetch a poster image; returns (bytes, content_type)."""
    base, params, _ = _client(cfg)
    if not thumb_path:
        raise PlexError("no thumb path")
    url = f"{base}{thumb_path}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "image/jpeg")


def check_connection(cfg: dict[str, Any]) -> dict[str, Any]:
    """Lightweight identity probe for the UI's status indicator."""
    base, params, headers = _client(cfg)
    resp = requests.get(f"{base}/identity", params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    mc = resp.json().get("MediaContainer", {})
    return {"ok": True, "version": mc.get("version"), "machine": mc.get("machineIdentifier")}
