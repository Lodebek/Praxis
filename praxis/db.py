"""SQLite storage for Praxis.

Three tables:
  * ``media``           — one row per Plex title (synced from the library)
  * ``ratings``         — your current verdict per title (a row exists only once
                          you rate it; no row = not yet watched / not yet rated)
  * ``recommendations`` — everything the AI has ever suggested, so nothing is
                          forgotten or suggested twice
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Any, Iterable

from .config import DATA_DIR, DB_PATH


def norm_title(title: str) -> str:
    """Normalized title key for fuzzy dedupe across sources."""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    ratingKey       TEXT PRIMARY KEY,
    source          TEXT NOT NULL DEFAULT 'plex',  -- 'plex' | 'manual' | 'netflix'
    enriched        INTEGER NOT NULL DEFAULT 0,    -- 1 once metadata is filled in
    type            TEXT NOT NULL,             -- 'movie' | 'show'
    title           TEXT NOT NULL,
    year            INTEGER,
    genres          TEXT,                      -- json array
    summary         TEXT,
    studio          TEXT,
    content_rating  TEXT,
    critic_rating   REAL,
    audience_rating REAL,
    duration_ms     INTEGER,
    directors       TEXT,                      -- json array
    writers         TEXT,                      -- json array
    cast            TEXT,                      -- json array (top ~6)
    country         TEXT,                      -- json array
    tagline         TEXT,
    thumb           TEXT,
    added_at        INTEGER,
    updated_at      INTEGER,
    last_synced     INTEGER
);

CREATE TABLE IF NOT EXISTS ratings (
    ratingKey TEXT PRIMARY KEY REFERENCES media(ratingKey) ON DELETE CASCADE,
    verdict   TEXT NOT NULL,                   -- 'loved' | 'liked' | 'disliked'
    note      TEXT,
    rated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    year           INTEGER,
    type           TEXT,                        -- 'movie' | 'show'
    source         TEXT,                        -- 'openrouter:<model>' | 'claude-export'
    reason         TEXT,
    where_to_watch TEXT,
    genres         TEXT,                        -- json array (from TMDB enrichment)
    thumb          TEXT,                        -- poster URL (from TMDB)
    status         TEXT NOT NULL DEFAULT 'new', -- new | queued | watched | dismissed
    user_verdict   TEXT,                        -- filled if you watch + rate it
    in_library     INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_type   ON media(type);
CREATE INDEX IF NOT EXISTS idx_recs_status  ON recommendations(status);
"""


def connect() -> sqlite3.Connection:
    """Open a connection with sensible defaults and ensure the schema exists."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight migrations for DBs created before a column existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN source TEXT NOT NULL DEFAULT 'plex'")
    if "enriched" not in cols:
        # plex rows arrive fully formed; external rows start un-enriched
        conn.execute("ALTER TABLE media ADD COLUMN enriched INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE media SET enriched = 1 WHERE source = 'plex'")

    rec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)").fetchall()}
    if "genres" not in rec_cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN genres TEXT")
    if "thumb" not in rec_cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN thumb TEXT")
    if "imdb_id" not in rec_cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN imdb_id TEXT")
    if "tmdb_id" not in rec_cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN tmdb_id INTEGER")
    if "tmdb_type" not in rec_cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN tmdb_type TEXT")
    conn.commit()


# ---------------------------------------------------------------- media

_MEDIA_COLS = [
    "ratingKey", "source", "enriched", "type", "title", "year", "genres", "summary", "studio",
    "content_rating", "critic_rating", "audience_rating", "duration_ms",
    "directors", "writers", "cast", "country", "tagline", "thumb",
    "added_at", "updated_at", "last_synced",
]


def upsert_media(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Insert or update media rows keyed by ``ratingKey``. Ratings are untouched."""
    placeholders = ", ".join("?" for _ in _MEDIA_COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _MEDIA_COLS if c != "ratingKey")
    sql = (
        f"INSERT INTO media ({', '.join(_MEDIA_COLS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(ratingKey) DO UPDATE SET {updates}"
    )
    count = 0
    for row in rows:
        conn.execute(sql, [row.get(c) for c in _MEDIA_COLS])
        count += 1
    conn.commit()
    return count


def _media_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a media row to a JSON-friendly dict (decoding json columns)."""
    d = dict(row)
    for col in ("genres", "directors", "writers", "cast", "country"):
        try:
            d[col] = json.loads(d[col]) if d.get(col) else []
        except (TypeError, json.JSONDecodeError):
            d[col] = []
    return d


def list_media(
    conn: sqlite3.Connection,
    status: str = "all",
    media_type: str | None = None,
    q: str | None = None,
    sort: str = "title",
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Return media joined with current rating, filtered/sorted for the UI."""
    where: list[str] = []
    params: list[Any] = []

    if media_type in ("movie", "show"):
        where.append("m.type = ?")
        params.append(media_type)

    if source in ("plex", "netflix", "manual"):
        where.append("m.source = ?")
        params.append(source)

    # A search should find a title no matter how it's rated, so when a query is
    # present we ignore the status (loved/unrated/…) filter entirely.
    if q:
        where.append("m.title LIKE ?")
        params.append(f"%{q}%")
    elif status == "unrated":
        where.append("r.verdict IS NULL")
    elif status in ("loved", "liked", "disliked"):
        where.append("r.verdict = ?")
        params.append(status)
    # 'all' -> no verdict filter

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # NULL years (un-enriched titles) always sort last
    order = {
        "title": "m.title COLLATE NOCASE ASC",
        "year_desc": "m.year IS NULL, m.year DESC, m.title COLLATE NOCASE ASC",
        "year_asc": "m.year IS NULL, m.year ASC, m.title COLLATE NOCASE ASC",
        "added": "m.added_at DESC",
        "rated": "r.rated_at DESC NULLS LAST, m.title COLLATE NOCASE ASC",
        "random": "RANDOM()",
    }.get(sort, "m.title COLLATE NOCASE ASC")

    rows = conn.execute(
        f"""
        SELECT m.*, r.verdict AS verdict, r.note AS note, r.rated_at AS rated_at
        FROM media m
        LEFT JOIN ratings r ON r.ratingKey = m.ratingKey
        {where_sql}
        ORDER BY {order}
        """,
        params,
    ).fetchall()
    return [_media_to_dict(row) for row in rows]


def get_media(conn: sqlite3.Connection, rating_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT m.*, r.verdict AS verdict, r.note AS note, r.rated_at AS rated_at
        FROM media m LEFT JOIN ratings r ON r.ratingKey = m.ratingKey
        WHERE m.ratingKey = ?
        """,
        (rating_key,),
    ).fetchone()
    return _media_to_dict(row) if row else None


def _existing_norms(conn: sqlite3.Connection) -> set[str]:
    return {norm_title(r["title"]) for r in conn.execute("SELECT title FROM media").fetchall()}


def add_external_media(
    conn: sqlite3.Connection,
    title: str,
    media_type: str,
    source: str,
    year: int | None = None,
    genres: list[str] | None = None,
    thumb: str | None = None,
    enriched: bool = False,
) -> str | None:
    """Add a title we don't own in Plex (manual entry, Netflix, or a rated rec).

    Returns the generated ratingKey, or None if a same-title row already exists
    (so we never duplicate a Plex/owned title).
    """
    if media_type not in ("movie", "show"):
        media_type = "movie"
    if norm_title(title) in _existing_norms(conn):
        return None
    key = f"{source}:{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    row = {c: None for c in _MEDIA_COLS}
    row.update({
        "ratingKey": key,
        "source": source,
        "enriched": 1 if enriched else 0,
        "type": media_type,
        "title": title.strip(),
        "year": year,
        "genres": json.dumps(genres or []),
        "directors": json.dumps([]),
        "writers": json.dumps([]),
        "cast": json.dumps([]),
        "country": json.dumps([]),
        "thumb": thumb,
        "added_at": now,
        "updated_at": now,
        "last_synced": now,
    })
    placeholders = ", ".join("?" for _ in _MEDIA_COLS)
    conn.execute(
        f"INSERT INTO media ({', '.join(_MEDIA_COLS)}) VALUES ({placeholders})",
        [row[c] for c in _MEDIA_COLS],
    )
    conn.commit()
    return key


def bulk_add_external(
    conn: sqlite3.Connection, items: Iterable[dict[str, Any]], source: str
) -> dict[str, int]:
    """Add many external titles, skipping any whose title already exists.

    Each item: {title, type, year?, genres?, verdict?}. If ``verdict`` is set,
    a rating row is created too. Returns {'added', 'skipped', 'rated'}.
    """
    existing = _existing_norms(conn)
    added = skipped = rated = 0
    now = int(time.time())
    placeholders = ", ".join("?" for _ in _MEDIA_COLS)
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            skipped += 1
            continue
        n = norm_title(title)
        if n in existing:
            skipped += 1
            continue
        existing.add(n)
        mtype = item.get("type") if item.get("type") in ("movie", "show") else "movie"
        key = f"{source}:{uuid.uuid4().hex[:12]}"
        row = {c: None for c in _MEDIA_COLS}
        row.update({
            "ratingKey": key, "source": source, "enriched": 0, "type": mtype, "title": title,
            "year": item.get("year"),
            "genres": json.dumps(item.get("genres") or []),
            "directors": json.dumps([]), "writers": json.dumps([]),
            "cast": json.dumps([]), "country": json.dumps([]),
            "added_at": now, "updated_at": now, "last_synced": now,
        })
        conn.execute(
            f"INSERT INTO media ({', '.join(_MEDIA_COLS)}) VALUES ({placeholders})",
            [row[c] for c in _MEDIA_COLS],
        )
        added += 1
        verdict = item.get("verdict")
        if verdict in ("loved", "liked", "disliked"):
            conn.execute(
                "INSERT INTO ratings (ratingKey, verdict, note, rated_at) VALUES (?, ?, ?, ?)",
                (key, verdict, item.get("note"), now),
            )
            rated += 1
    conn.commit()
    return {"added": added, "skipped": skipped, "rated": rated}


def needs_enrichment_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM media WHERE enriched = 0"
    ).fetchone()[0]


def media_needing_enrichment(
    conn: sqlite3.Connection, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ratingKey, title, type FROM media WHERE enriched = 0 LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def apply_enrichment(
    conn: sqlite3.Connection, rating_key: str, data: dict[str, Any] | None
) -> None:
    """Update a row with TMDB data and mark it enriched. ``data=None`` just marks
    it enriched (no match found) so we don't keep retrying it."""
    if data is None:
        conn.execute("UPDATE media SET enriched = 1 WHERE ratingKey = ?", (rating_key,))
        conn.commit()
        return
    conn.execute(
        """
        UPDATE media SET
            enriched = 1,
            type = COALESCE(?, type),
            title = COALESCE(?, title),
            year = COALESCE(?, year),
            genres = ?,
            summary = COALESCE(?, summary),
            audience_rating = COALESCE(?, audience_rating),
            thumb = COALESCE(?, thumb),
            updated_at = ?
        WHERE ratingKey = ?
        """,
        (
            data.get("type"),
            data.get("title"),
            data.get("year"),
            json.dumps(data.get("genres") or []),
            data.get("summary"),
            data.get("audience_rating"),
            data.get("thumb"),
            int(time.time()),
            rating_key,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------- ratings


def set_rating(
    conn: sqlite3.Connection,
    rating_key: str,
    verdict: str | None,
    note: str | None = None,
) -> None:
    """Upsert a verdict. ``verdict=None`` clears the rating (back to unrated)."""
    if verdict is None:
        conn.execute("DELETE FROM ratings WHERE ratingKey = ?", (rating_key,))
    else:
        if verdict not in ("loved", "liked", "disliked"):
            raise ValueError(f"invalid verdict: {verdict!r}")
        conn.execute(
            """
            INSERT INTO ratings (ratingKey, verdict, note, rated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ratingKey) DO UPDATE SET
                verdict = excluded.verdict,
                note    = excluded.note,
                rated_at = excluded.rated_at
            """,
            (rating_key, verdict, note, int(time.time())),
        )
    conn.commit()


def rated_media(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All media that have a verdict (for profile building)."""
    rows = conn.execute(
        """
        SELECT m.*, r.verdict AS verdict, r.note AS note
        FROM ratings r JOIN media m ON m.ratingKey = r.ratingKey
        """
    ).fetchall()
    return [_media_to_dict(row) for row in rows]


def all_titles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Lightweight (title, year, type) list of the whole library — for exclusions."""
    rows = conn.execute("SELECT title, year, type FROM media").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- recommendations


def add_recommendations(
    conn: sqlite3.Connection, recs: Iterable[dict[str, Any]], source: str
) -> list[int]:
    """Store new recommendations; returns the inserted row ids."""
    now = int(time.time())
    ids: list[int] = []
    for rec in recs:
        cur = conn.execute(
            """
            INSERT INTO recommendations
                (title, year, type, source, reason, where_to_watch, genres, thumb,
                 imdb_id, tmdb_id, tmdb_type, status, in_library, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
            """,
            (
                rec.get("title"),
                rec.get("year"),
                rec.get("type"),
                source,
                rec.get("reason"),
                rec.get("where_to_watch"),
                json.dumps(rec.get("genres") or []),
                rec.get("thumb"),
                rec.get("imdb_id"),
                rec.get("tmdb_id"),
                rec.get("tmdb_type"),
                1 if rec.get("in_library") else 0,
                now,
                now,
            ),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def _rec_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["genres"] = json.loads(d["genres"]) if d.get("genres") else []
    except (TypeError, json.JSONDecodeError):
        d["genres"] = []
    return d


def list_recommendations(
    conn: sqlite3.Connection, status: str | None = None
) -> list[dict[str, Any]]:
    if status and status != "all":
        rows = conn.execute(
            "SELECT * FROM recommendations WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recommendations ORDER BY created_at DESC"
        ).fetchall()
    return [_rec_to_dict(r) for r in rows]


def get_recommendation(conn: sqlite3.Connection, rec_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM recommendations WHERE id = ?", (rec_id,)
    ).fetchone()
    return _rec_to_dict(row) if row else None


def rate_recommendation(
    conn: sqlite3.Connection, rec_id: int, verdict: str
) -> dict[str, Any] | None:
    """Mark a recommendation as already-seen with a verdict.

    Adds it to the rated library (so it shapes the taste profile and is excluded
    from future recs) and flips the rec to 'watched' with the verdict recorded.
    """
    if verdict not in ("loved", "liked", "disliked"):
        raise ValueError(f"invalid verdict: {verdict!r}")
    rec = get_recommendation(conn, rec_id)
    if rec is None:
        return None
    key = add_external_media(
        conn,
        rec["title"],
        rec.get("type") or "movie",
        source="manual",
        year=rec.get("year"),
        genres=rec.get("genres") or [],
        thumb=rec.get("thumb"),
        enriched=True,
    )
    # key is None if the title already exists in the library; rate that one instead
    if key is None:
        key = find_media_key_by_title(conn, rec["title"])
    if key:
        set_rating(conn, key, verdict)
    return update_recommendation(conn, rec_id, status="watched", user_verdict=verdict)


def find_media_key_by_title(conn: sqlite3.Connection, title: str) -> str | None:
    """Find an existing media ratingKey by normalized title, or None."""
    target = norm_title(title)
    for r in conn.execute("SELECT ratingKey, title FROM media").fetchall():
        if norm_title(r["title"]) == target:
            return r["ratingKey"]
    return None


def update_recommendation(
    conn: sqlite3.Connection,
    rec_id: int,
    status: str | None = None,
    user_verdict: str | None = None,
) -> dict[str, Any] | None:
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [int(time.time())]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if user_verdict is not None:
        sets.append("user_verdict = ?")
        params.append(user_verdict)
    params.append(rec_id)
    conn.execute(
        f"UPDATE recommendations SET {', '.join(sets)} WHERE id = ?", params
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM recommendations WHERE id = ?", (rec_id,)
    ).fetchone()
    return dict(row) if row else None


def existing_rec_titles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT title, year, type FROM recommendations"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- stats


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    by_type = {
        r["type"]: r["n"]
        for r in conn.execute(
            "SELECT type, COUNT(*) n FROM media GROUP BY type"
        ).fetchall()
    }
    verdicts = {
        r["verdict"]: r["n"]
        for r in conn.execute(
            "SELECT verdict, COUNT(*) n FROM ratings GROUP BY verdict"
        ).fetchall()
    }
    rated = sum(verdicts.values())
    recs = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM recommendations GROUP BY status"
        ).fetchall()
    }
    return {
        "total": total,
        "by_type": by_type,
        "verdicts": verdicts,
        "rated": rated,
        "unrated": total - rated,
        "pct_rated": round(100 * rated / total, 1) if total else 0,
        "recommendations": recs,
    }
