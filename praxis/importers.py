"""Importers for external watch data — currently Netflix CSV exports.

Netflix's ``NetflixViewingHistory.csv`` is just ``Title,Date`` where episodic
titles look like:
    "Devil May Cry: Season 1: Inferno"
    "Hulk Hogan: Real American: Limited Series: Hulkamania"
    "Man on Fire: Seven"
...but movies can also contain colons:
    "Nemesis: A Long Time Coming"

So we collapse to series titles using two signals:
  1. explicit episodic markers (": Season", ": Limited Series", ": Episode", …)
     -> the show is everything *before* that marker
  2. for the ambiguous ": Something" case, frequency — if the same prefix appears
     across several distinct rows it's a series; if it appears once it's a movie.

The full GDPR export may also include a Ratings.csv with thumbs; we auto-detect
a rating column if present and map it to our verdicts.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from typing import Any

# explicit episodic markers (case-insensitive), matched after a colon
_MARKER = re.compile(
    r":\s*(season|limited series|miniseries|series|episode|part|chapter|volume|book|"
    r"collection|special)\b",
    re.IGNORECASE,
)

_TITLE_KEYS = ("title", "title name", "name")
_RATING_KEYS = ("thumbs value", "thumbsrating", "rating", "rating value", "thumbs")

# Netflix thumbs / star -> our verdict
_VERDICT_FROM_THUMBS = {
    "1": "disliked", "2": "liked", "3": "loved",          # thumbs value
    "thumbsdown": "disliked", "thumbsup": "liked", "twothumbsup": "loved",
}
_VERDICT_FROM_STARS = {"1": "disliked", "2": "disliked", "3": "liked",
                       "4": "liked", "5": "loved"}


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _find_col(fieldnames: list[str], candidates: tuple[str, ...]) -> str | None:
    lookup = {_norm_key(f): f for f in (fieldnames or [])}
    for cand in candidates:
        if cand in lookup:
            return lookup[cand]
    return None


def _collapse_title(title: str) -> tuple[str, bool]:
    """Return (candidate_series_or_movie_title, is_certain_show)."""
    title = title.strip()
    m = _MARKER.search(title)
    if m:
        return title[: m.start()].strip(), True
    if ":" in title:
        # ambiguous — return the pre-colon prefix as a *candidate* series title
        return title.split(":", 1)[0].strip(), False
    return title, False


def parse_netflix_csv(text: str, kind: str = "auto") -> dict[str, Any]:
    """Parse a Netflix CSV (viewing history and/or ratings).

    Returns {'items': [{title, type, verdict?}], 'rows': int, 'rated': int}.
    Items are de-duplicated by resolved title.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    title_col = _find_col(fields, _TITLE_KEYS)
    rating_col = _find_col(fields, _RATING_KEYS)
    if not title_col:
        raise ValueError(
            f"Could not find a title column in CSV (headers: {fields}). "
            "Expected one of: Title, Title Name."
        )

    rows = list(reader)

    # ---- pass 1: resolve each row to (title, is_certain_show) + tally prefixes
    resolved: list[tuple[str, bool, str | None]] = []  # (title, certain_show, raw)
    prefix_counts: Counter[str] = Counter()
    certain_show_titles: set[str] = set()
    for r in rows:
        raw = (r.get(title_col) or "").strip()
        if not raw:
            continue
        cand, certain = _collapse_title(raw)
        if not cand:
            continue
        resolved.append((cand, certain, raw))
        if certain:
            certain_show_titles.add(cand.lower())
        else:
            prefix_counts[cand.lower()] += 1

    # ---- pass 2: finalize type + collapse to unique titles
    seen: dict[str, dict[str, Any]] = {}
    rated = 0
    for i, (cand, certain, raw) in enumerate(resolved):
        if certain:
            final_title, mtype = cand, "show"
        else:
            low = cand.lower()
            is_show = prefix_counts[low] >= 2 or low in certain_show_titles
            final_title = cand if is_show else raw
            mtype = "show" if is_show else "movie"

        item = seen.get(final_title.lower())
        if not item:
            item = {"title": final_title, "type": mtype}
            seen[final_title.lower()] = item

        # ratings file path
        if rating_col:
            v = _map_rating(rows[i].get(rating_col) if i < len(rows) else None)
            if v and not item.get("verdict"):
                item["verdict"] = v
                rated += 1

    return {"items": list(seen.values()), "rows": len(rows), "rated": rated}


def _map_rating(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().lower().replace(" ", "")
    if not raw:
        return None
    if raw in _VERDICT_FROM_THUMBS:
        return _VERDICT_FROM_THUMBS[raw]
    if raw in _VERDICT_FROM_STARS:
        return _VERDICT_FROM_STARS[raw]
    return None
