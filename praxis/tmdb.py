"""TMDB (The Movie Database) metadata lookup.

Plex's default movie/TV agents are backed by TMDB, so this is the same source.
We use the free v3 API (an API key from https://www.themoviedb.org/settings/api).

Given a bare title (e.g. a Netflix import), we do a multi-search and adopt the
best match — which also tells us whether it's really a movie or a show, so
enrichment doubles as a type-correction pass.
"""

from __future__ import annotations

from typing import Any

import requests

BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w500"

# module-level genre-id -> name caches (fetched once per process)
_genre_cache: dict[str, dict[int, str]] = {}


class TMDBError(RuntimeError):
    pass


def has_credentials(tmdb_cfg: dict[str, Any]) -> bool:
    tmdb_cfg = tmdb_cfg or {}
    return bool(
        (tmdb_cfg.get("read_access_token") or "").strip()
        or (tmdb_cfg.get("api_key") or "").strip()
    )


def _auth(tmdb_cfg: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (headers, params) for auth.

    Prefers the v4 read access token (Bearer header), which TMDB recommends and
    which works across v3 endpoints; falls back to the legacy api_key query param.
    """
    tmdb_cfg = tmdb_cfg or {}
    token = (tmdb_cfg.get("read_access_token") or "").strip()
    if token:
        return {"Authorization": f"Bearer {token}", "accept": "application/json"}, {}
    api_key = (tmdb_cfg.get("api_key") or "").strip()
    if api_key:
        return {"accept": "application/json"}, {"api_key": api_key}
    raise TMDBError(
        "No TMDB credentials. Add tmdb.read_access_token (preferred) or "
        "tmdb.api_key to config.json."
    )


def _get(tmdb_cfg: dict[str, Any], path: str, **params: Any) -> dict[str, Any]:
    headers, auth_params = _auth(tmdb_cfg)
    params.update(auth_params)
    resp = requests.get(f"{BASE}{path}", params=params, headers=headers, timeout=20)
    if resp.status_code == 401:
        raise TMDBError(
            "TMDB rejected the credentials (401). Check tmdb.read_access_token / "
            "tmdb.api_key in config.json."
        )
    resp.raise_for_status()
    return resp.json()


def _genre_map(tmdb_cfg: dict[str, Any], kind: str) -> dict[int, str]:
    """kind is 'movie' or 'tv'."""
    if kind not in _genre_cache:
        data = _get(tmdb_cfg, f"/genre/{kind}/list")
        _genre_cache[kind] = {g["id"]: g["name"] for g in data.get("genres", [])}
    return _genre_cache[kind]


def check_credentials(tmdb_cfg: dict[str, Any]) -> bool:
    _get(tmdb_cfg, "/configuration")
    return True


def _year(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


def _result_year(r: dict[str, Any]) -> int | None:
    return _year(r.get("release_date") or r.get("first_air_date"))


def external_ids(tmdb_cfg: dict[str, Any], tmdb_id: int, mtype: str) -> dict[str, Any]:
    """Fetch external IDs (notably imdb_id) for a TMDB movie/show."""
    kind = "tv" if mtype == "show" else "movie"
    try:
        return _get(tmdb_cfg, f"/{kind}/{tmdb_id}/external_ids")
    except Exception:  # noqa: BLE001 - non-fatal; ids are a nice-to-have
        return {}


def enrich_title(
    tmdb_cfg: dict[str, Any], title: str, prefer: str | None = None,
    year: int | None = None, with_ids: bool = False,
) -> dict[str, Any] | None:
    """Look up a title; return enrichment dict or None if no match.

    prefer: 'movie' or 'show' to break ties when both exist; otherwise we take
    TMDB's most popular result regardless of media type.
    year:   if given, prefer a result whose year matches (±1) — disambiguates
            remakes like MacGyver 1985 vs 2016.
    """
    data = _get(tmdb_cfg, "/search/multi", query=title, include_adult="false")
    results = [
        r for r in data.get("results", [])
        if r.get("media_type") in ("movie", "tv")
    ]
    if not results:
        return None

    prefer_mt = {"movie": "movie", "show": "tv"}.get(prefer or "")
    if prefer_mt:
        same = [r for r in results if r.get("media_type") == prefer_mt]
        if same:
            results = same

    if year:
        matches = [r for r in results if (_result_year(r) or 0) and abs(_result_year(r) - year) <= 1]
        if matches:
            results = matches

    best = results[0]  # already popularity-ordered by TMDB
    mt = best["media_type"]
    if mt == "movie":
        name = best.get("title")
        year = _year(best.get("release_date"))
        gmap = _genre_map(tmdb_cfg, "movie")
        mtype = "movie"
    else:
        name = best.get("name")
        year = _year(best.get("first_air_date"))
        gmap = _genre_map(tmdb_cfg, "tv")
        mtype = "show"

    genres = [gmap[g] for g in best.get("genre_ids", []) if g in gmap]
    poster = best.get("poster_path")
    out = {
        "type": mtype,
        "title": name or title,
        "year": year,
        "genres": genres,
        "summary": best.get("overview") or None,
        "thumb": (IMG_BASE + poster) if poster else None,
        "tmdb_id": best.get("id"),
        "tmdb_type": "tv" if mtype == "show" else "movie",
        "imdb_id": None,
        "audience_rating": best.get("vote_average"),
    }
    if with_ids and best.get("id"):
        out["imdb_id"] = external_ids(tmdb_cfg, best["id"], mtype).get("imdb_id") or None
    return out
