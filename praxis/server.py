"""FastAPI app: REST API + serves the static web UI."""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, importers, plex, recommend, tmdb
from .config import WEB_DIR, load_config

app = FastAPI(title="Praxis", version="0.1.0")

# Bump this whenever the API surface changes. The frontend compares it against
# its own expected value and warns if the running server is stale (a common
# confusion: the browser serves fresh static files while an old python process
# still answers the API and returns 405 for new routes).
SERVER_VERSION = "2026-06-06.2"

# simple in-process poster cache: ratingKey -> (bytes, content_type)
_thumb_cache: dict[str, tuple[bytes, str]] = {}


def cfg() -> dict[str, Any]:
    return load_config()


def _do_sync(c: dict[str, Any]) -> dict[str, Any]:
    """Fetch the Plex library and upsert it. Shared by /api/sync and startup."""
    synced_at = int(time.time())
    result = plex.fetch_library(c, synced_at)
    conn = db.connect()
    try:
        n = db.upsert_media(conn, result["rows"])
    finally:
        conn.close()
    _thumb_cache.clear()
    return {"synced": n, "counts": result["counts"]}


@app.on_event("startup")
def _startup_sync() -> None:
    """Re-index Plex on launch so new additions show up before anything else."""
    c = cfg()
    if not (c.get("plex") or {}).get("sync_on_start", True):
        return
    try:
        r = _do_sync(c)
        print(f"  [startup] Plex re-indexed: {r['counts']}")
    except Exception as exc:  # noqa: BLE001 - never block startup on Plex
        print(f"  [startup] Plex sync skipped: {exc}")


# ---------------------------------------------------------------- models


class RateBody(BaseModel):
    ratingKey: str
    verdict: Optional[str] = None  # 'loved' | 'liked' | 'disliked' | None (clear)
    note: Optional[str] = None


class RecommendBody(BaseModel):
    count: int = 8
    type: str = "both"  # movie | show | both
    vibe: Optional[str] = None


class RecUpdateBody(BaseModel):
    status: Optional[str] = None  # new | queued | watched | dismissed
    user_verdict: Optional[str] = None


class ImportBody(BaseModel):
    text: str


class ManualMediaBody(BaseModel):
    title: str
    type: str = "movie"  # movie | show
    year: Optional[int] = None
    verdict: Optional[str] = None  # loved | liked | disliked | None
    note: Optional[str] = None


class ChatBody(BaseModel):
    messages: list[dict[str, str]]


# ---------------------------------------------------------------- system


@app.get("/api/health")
def health() -> dict[str, Any]:
    c = cfg()
    out: dict[str, Any] = {"ok": True}
    try:
        out["plex"] = plex.check_connection(c)
    except Exception as exc:  # noqa: BLE001 - surface any connection problem
        out["plex"] = {"ok": False, "error": str(exc)}
    out["openrouter_configured"] = bool((c.get("openrouter") or {}).get("api_key"))
    out["model"] = (c.get("openrouter") or {}).get("model")
    out["server_version"] = SERVER_VERSION
    return out


# ---------------------------------------------------------------- sync / media


@app.post("/api/sync")
def sync() -> dict[str, Any]:
    try:
        return _do_sync(cfg())
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/media")
def get_media(
    status: str = "all",
    type: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "title",
    source: Optional[str] = None,
) -> dict[str, Any]:
    conn = db.connect()
    try:
        items = db.list_media(
            conn, status=status, media_type=type, q=q, sort=sort, source=source
        )
    finally:
        conn.close()
    return {"count": len(items), "items": items}


@app.post("/api/rate")
def rate(body: RateBody) -> dict[str, Any]:
    conn = db.connect()
    try:
        db.set_rating(conn, body.ratingKey, body.verdict, body.note)
        item = db.get_media(conn, body.ratingKey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    if item is None:
        raise HTTPException(status_code=404, detail="unknown ratingKey")
    return {"ok": True, "item": item}


@app.post("/api/media/manual")
def add_manual(body: ManualMediaBody) -> dict[str, Any]:
    """Add a title you watched elsewhere (not in Plex), optionally pre-rated."""
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title required")
    conn = db.connect()
    try:
        key = db.add_external_media(
            conn, body.title, body.type, source="manual", year=body.year
        )
        if key is None:
            raise HTTPException(status_code=409, detail="a title with that name already exists")
        if body.verdict:
            db.set_rating(conn, key, body.verdict, body.note)
        item = db.get_media(conn, key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return {"ok": True, "item": item}


def _decode_csv(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _import_netflix_text(text: str) -> dict[str, Any]:
    try:
        parsed = importers.parse_netflix_csv(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = db.connect()
    try:
        result = db.bulk_add_external(conn, parsed["items"], source="netflix")
    finally:
        conn.close()
    _thumb_cache.clear()
    return {"rows": parsed["rows"], "unique": len(parsed["items"]), **result}


@app.post("/api/import/netflix")
async def import_netflix(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    return _import_netflix_text(_decode_csv(raw))


class PathBody(BaseModel):
    path: str


@app.post("/api/import/netflix-path")
def import_netflix_path(body: PathBody) -> dict[str, Any]:
    from pathlib import Path
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"file not found: {body.path}")
    return _import_netflix_text(_decode_csv(p.read_bytes()))


@app.get("/api/enrich/status")
def enrich_status() -> dict[str, Any]:
    conn = db.connect()
    try:
        remaining = db.needs_enrichment_count(conn)
    finally:
        conn.close()
    return {
        "remaining": remaining,
        "tmdb_configured": tmdb.has_credentials(cfg().get("tmdb") or {}),
    }


class EnrichBody(BaseModel):
    limit: int = 40


@app.post("/api/enrich")
def enrich(body: EnrichBody) -> dict[str, Any]:
    """Enrich a batch of un-enriched titles via TMDB. Call repeatedly until
    ``remaining`` hits 0 (lets the UI show progress and avoids long requests)."""
    tmdb_cfg = cfg().get("tmdb") or {}
    if not tmdb.has_credentials(tmdb_cfg):
        raise HTTPException(
            status_code=400,
            detail="No TMDB credentials. Add tmdb.read_access_token to config.json "
                   "(from themoviedb.org → Settings → API).",
        )
    conn = db.connect()
    try:
        batch = db.media_needing_enrichment(conn, max(1, min(body.limit, 100)))
        enriched = failed = 0
        for item in batch:
            try:
                data = tmdb.enrich_title(tmdb_cfg, item["title"], prefer=item.get("type"))
                db.apply_enrichment(conn, item["ratingKey"], data)
                if data:
                    enriched += 1
                else:
                    failed += 1
            except tmdb.TMDBError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception:  # noqa: BLE001 - one bad title shouldn't abort the batch
                db.apply_enrichment(conn, item["ratingKey"], None)
                failed += 1
        remaining = db.needs_enrichment_count(conn)
    finally:
        conn.close()
    _thumb_cache.clear()
    return {"processed": len(batch), "enriched": enriched,
            "no_match": failed, "remaining": remaining}


@app.get("/api/thumb/{rating_key}")
def thumb(rating_key: str) -> Response:
    if rating_key in _thumb_cache:
        data, ctype = _thumb_cache[rating_key]
        return Response(content=data, media_type=ctype)

    conn = db.connect()
    try:
        item = db.get_media(conn, rating_key)
    finally:
        conn.close()
    if not item or not item.get("thumb"):
        raise HTTPException(status_code=404, detail="no poster")

    try:
        data, ctype = plex.fetch_thumb(cfg(), item["thumb"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"poster fetch failed: {exc}") from exc
    _thumb_cache[rating_key] = (data, ctype)
    return Response(content=data, media_type=ctype)


# ---------------------------------------------------------------- recommendations


@app.get("/api/profile")
def profile() -> dict[str, Any]:
    conn = db.connect()
    try:
        return recommend.build_profile(conn)
    finally:
        conn.close()


@app.post("/api/recommend")
def do_recommend(body: RecommendBody) -> dict[str, Any]:
    c = cfg()
    conn = db.connect()
    try:
        return recommend.recommend(conn, c, body.count, body.type, body.vibe)
    except recommend.RecommendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/export-prompt")
def export_prompt(body: RecommendBody) -> dict[str, Any]:
    conn = db.connect()
    try:
        prompt = recommend.build_prompt(conn, body.count, body.type, body.vibe)
    finally:
        conn.close()
    return {"prompt": prompt}


@app.post("/api/import-recommendations")
def import_recommendations(body: ImportBody) -> dict[str, Any]:
    conn = db.connect()
    try:
        parsed = recommend.parse_recommendations(body.text)
        recommend.enrich_recs(cfg(), parsed)  # TMDB posters/genres for pasted picks
        fresh = recommend.dedupe_and_flag(conn, parsed)
        ids = db.add_recommendations(conn, fresh, source="claude-export")
        stored = {r["id"]: r for r in db.list_recommendations(conn, status="new")}
    except recommend.RecommendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return {"added": len(ids), "recommendations": [stored[i] for i in ids if i in stored]}


@app.post("/api/chat")
def chat(body: ChatBody) -> dict[str, Any]:
    conn = db.connect()
    try:
        return recommend.chat(conn, cfg(), body.messages)
    except recommend.RecommendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@app.get("/api/recommendations")
def get_recommendations(status: Optional[str] = None) -> dict[str, Any]:
    conn = db.connect()
    try:
        items = db.list_recommendations(conn, status=status)
    finally:
        conn.close()
    return {"count": len(items), "items": items}


@app.post("/api/recommendations/{rec_id}")
def update_recommendation(rec_id: int, body: RecUpdateBody) -> dict[str, Any]:
    conn = db.connect()
    try:
        row = db.update_recommendation(conn, rec_id, body.status, body.user_verdict)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown recommendation id")
    return {"ok": True, "item": row}


class RecRateBody(BaseModel):
    verdict: str  # loved | liked | disliked


@app.post("/api/recommendations/{rec_id}/rate")
def rate_recommendation(rec_id: int, body: RecRateBody) -> dict[str, Any]:
    """Mark a recommended title as already-seen with a verdict: adds it to the
    rated library (shaping your profile + excluding it from future recs)."""
    conn = db.connect()
    try:
        row = db.rate_recommendation(conn, rec_id, body.verdict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown recommendation id")
    return {"ok": True, "item": row}


@app.get("/api/debug/last")
def debug_last() -> dict[str, Any]:
    """The raw details of the most recent model call — for diagnosing bad responses."""
    return recommend.LAST_RESPONSE or {"note": "no model call yet this session"}


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    conn = db.connect()
    try:
        return db.stats(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- static UI


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="web")
