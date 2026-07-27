"""FastAPI app: REST API + serves the static web UI."""

from __future__ import annotations

import time
from typing import Any, Optional
import requests

from fastapi import FastAPI, HTTPException, Query, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, importers, plex, recommend, tmdb, books, games
from .config import WEB_DIR, load_config

app = FastAPI(title="Praxis", version="0.1.0")

# Bump this whenever the API surface changes. The frontend compares it against
# its own expected value and warns if the running server is stale (a common
# confusion: the browser serves fresh static files while an old python process
# still answers the API and returns 405 for new routes).
SERVER_VERSION = "2026-06-07.1"

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
        res = db.upsert_media(conn, result["rows"])
    finally:
        conn.close()
    _thumb_cache.clear()
    return {"synced": res["total"], "new": res["new"], "counts": result["counts"]}


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
    type: str = "movie"  # movie | show | book | game
    year: Optional[int] = None
    author: Optional[str] = None
    verdict: Optional[str] = None  # loved | liked | disliked | None
    note: Optional[str] = None


class ChatMessageBody(BaseModel):
    content: str


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


@app.delete("/api/media/{rating_key}")
def delete_media(rating_key: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        db.delete_media(conn, rating_key)
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/media/manual")
def add_manual(body: ManualMediaBody) -> dict[str, Any]:
    """Add a title you watched elsewhere (not in Plex), optionally pre-rated."""
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title required")
    conn = db.connect()
    try:
        key = db.add_external_media(
            conn, body.title, body.type, source="manual", year=body.year, author=body.author
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


@app.post("/api/import/books-path")
def import_books_path(body: PathBody) -> StreamingResponse:
    import json
    def event_stream():
        try:
            results = []
            for event in books.scan_directory_stream(body.path):
                if event["type"] == "progress":
                    yield f"data: {json.dumps(event)}\n\n"
                elif event["type"] == "book":
                    results.append(event["data"])
                elif event["type"] == "error":
                    yield f"data: {json.dumps(event)}\n\n"
                    return
            
            conn = db.connect()
            try:
                res = db.bulk_add_external(conn, results, source="local_scan")
                yield f"data: {json.dumps({'type': 'done', 'unique': len(results), 'added': res['added'], 'items': res['items']})}\n\n"
            finally:
                conn.close()
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/import/games-path")
def import_games_path(body: PathBody) -> StreamingResponse:
    import json
    def event_stream():
        try:
            results = []
            for event in games.scan_games_dir(body.path):
                if event["type"] == "progress":
                    yield f"data: {json.dumps(event)}\n\n"
                elif event["type"] == "game":
                    results.append(event["data"])
                elif event["type"] == "error":
                    yield f"data: {json.dumps(event)}\n\n"
                    return
            
            conn = db.connect()
            try:
                res = db.bulk_add_external(conn, results, source="local_scan")
                yield f"data: {json.dumps({'type': 'done', 'unique': len(results), 'added': res['added'], 'items': res['items']})}\n\n"
            finally:
                conn.close()
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/books/search")
def search_books(q: str = Query(...)) -> dict[str, Any]:
    if not q.strip():
        return {"items": []}
    try:
        resp = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": q, "maxResults": 10},
            timeout=10
        )
        resp.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Google Books API failed: {exc}") from exc
    
    data = resp.json()
    items = []
    for item in data.get("items", []):
        vol = item.get("volumeInfo", {})
        title = vol.get("title")
        if not title:
            continue
        authors = vol.get("authors", [])
        author = authors[0] if authors else None
        date = vol.get("publishedDate", "")
        year = int(date[:4]) if date[:4].isdigit() else None
        
        # Change http to https for images
        thumb = vol.get("imageLinks", {}).get("thumbnail")
        if thumb and thumb.startswith("http:"):
            thumb = "https:" + thumb[5:]

        items.append({
            "title": title,
            "author": author,
            "year": year,
            "thumb": thumb,
        })
    return {"items": items}


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


@app.get("/api/enrich-books/status")
def enrich_books_status() -> dict[str, Any]:
    conn = db.connect()
    try:
        remaining = db.books_needing_enrichment_count(conn)
    finally:
        conn.close()
    return {"remaining": remaining}


@app.get("/api/enrich-games/status")
def enrich_games_status() -> dict[str, Any]:
    conn = db.connect()
    try:
        remaining = db.games_needing_enrichment_count(conn)
    finally:
        conn.close()
    return {"remaining": remaining}


@app.post("/api/enrich-books")
def enrich_books(body: EnrichBody) -> dict[str, Any]:
    """Search Google Books for imported books with missing covers."""
    conn = db.connect()
    try:
        batch = db.books_needing_enrichment(conn, max(1, min(body.limit, 100)))
        enriched = failed = 0
        for item in batch:
            try:
                q = f"{item['title']} {item.get('author') or ''}".strip()
                resp = requests.get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params={"q": q, "maxResults": 1},
                    timeout=5
                )
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if items:
                        vol = items[0].get("volumeInfo", {})
                        thumb = vol.get("imageLinks", {}).get("thumbnail")
                        if thumb and thumb.startswith("http:"):
                            thumb = "https:" + thumb[5:]
                        
                        data = {}
                        if thumb: data["thumb"] = thumb
                        genres = vol.get("categories", [])
                        if genres: data["genres"] = genres
                        date = vol.get("publishedDate", "")
                        if date[:4].isdigit(): data["year"] = int(date[:4])
                        
                        db.apply_enrichment(conn, item["ratingKey"], data if data else None)
                        if data:
                            enriched += 1
                        else:
                            failed += 1
                    else:
                        db.apply_enrichment(conn, item["ratingKey"], None)
                        failed += 1
                else:
                    db.apply_enrichment(conn, item["ratingKey"], None)
                    failed += 1
            except Exception:
                db.apply_enrichment(conn, item["ratingKey"], None)
                failed += 1
                
        remaining = db.books_needing_enrichment_count(conn)
    finally:
        conn.close()
    _thumb_cache.clear()
    return {"processed": len(batch), "enriched": enriched,
            "no_match": failed, "remaining": remaining}


@app.post("/api/enrich-games")
def enrich_games(body: EnrichBody) -> dict[str, Any]:
    """Search Steam Store API for imported games with missing metadata."""
    conn = db.connect()
    try:
        batch = db.games_needing_enrichment(conn, max(1, min(body.limit, 100)))
        enriched = failed = 0
        for item in batch:
            try:
                # Strip punctuation for fuzzy search (Steam API is bad at hyphens)
                clean_term = __import__('re').sub(r'[^\w\s]', '', item["title"])
                resp = requests.get(
                    "https://store.steampowered.com/api/storesearch/",
                    params={"term": clean_term, "l": "english", "cc": "US"},
                    timeout=5
                )
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if items:
                        # Grab the exact first match
                        game = items[0]
                        appid = game.get("id")
                        thumb = game.get("tiny_image")
                        if thumb:
                            thumb = __import__('re').sub(r'capsule_.*?\.jpg', 'library_600x900.jpg', thumb)
                        
                        data = {}
                        if thumb:
                            data["thumb"] = thumb
                            
                        # If we have an appid, try to fetch more details (genres, year)
                        if appid:
                            try:
                                det_resp = requests.get(
                                    f"https://store.steampowered.com/api/appdetails?appids={appid}",
                                    timeout=5
                                )
                                if det_resp.status_code == 200:
                                    det_data = det_resp.json().get(str(appid), {})
                                    if det_data.get("success"):
                                        game_data = det_data.get("data", {})
                                        
                                        # Parse genres
                                        genres = [g.get("description") for g in game_data.get("genres", [])]
                                        if genres:
                                            data["genres"] = genres
                                        
                                        # Parse year
                                        date_str = game_data.get("release_date", {}).get("date", "")
                                        year_match = __import__('re').search(r'\\b(19\\d{2}|20\\d{2})\\b', date_str)
                                        if year_match:
                                            data["year"] = int(year_match.group(1))
                                            
                                        # Parse developer as author
                                        devs = game_data.get("developers", [])
                                        if devs:
                                            data["author"] = devs[0]
                            except Exception:
                                pass # ignore detail errors and just use storesearch thumb
                                
                        db.apply_enrichment(conn, item["ratingKey"], data if data else None)
                        if data:
                            enriched += 1
                        else:
                            failed += 1
                    else:
                        db.apply_enrichment(conn, item["ratingKey"], None)
                        failed += 1
                else:
                    db.apply_enrichment(conn, item["ratingKey"], None)
                    failed += 1
            except Exception:
                db.apply_enrichment(conn, item["ratingKey"], None)
                failed += 1
                
        remaining = db.games_needing_enrichment_count(conn)
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

    thumb_val = item["thumb"]
    
    if thumb_val.startswith("local:"):
        from .config import DATA_DIR
        path = DATA_DIR / "covers" / thumb_val[6:]
        if not path.exists():
            raise HTTPException(status_code=404, detail="local poster not found")
        return FileResponse(path)

    if thumb_val.startswith("http"):
        try:
            r = requests.get(thumb_val, timeout=10)
            _thumb_cache[rating_key] = (r.content, r.headers.get("content-type", "image/jpeg"))
            return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"poster proxy failed: {exc}") from exc

    try:
        data, ctype = plex.fetch_thumb(cfg(), thumb_val)
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


@app.get("/api/chat/sessions")
def get_chat_sessions() -> dict[str, Any]:
    conn = db.connect()
    try:
        items = db.get_chat_sessions(conn)
    finally:
        conn.close()
    return {"sessions": items}


@app.post("/api/chat/sessions")
def create_chat_session() -> dict[str, Any]:
    conn = db.connect()
    try:
        sid = db.create_chat_session(conn)
    finally:
        conn.close()
    return {"session_id": sid}


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session(session_id: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        msgs = db.get_chat_messages(conn, session_id)
    finally:
        conn.close()
    return {"messages": msgs}


@app.post("/api/chat/sessions/{session_id}/message")
def send_chat_message(session_id: str, body: ChatMessageBody) -> dict[str, Any]:
    conn = db.connect()
    try:
        return recommend.chat(conn, cfg(), session_id, body.content)
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


@app.post("/api/stats/analyze")
def analyze_taste() -> dict[str, Any]:
    conn = db.connect()
    try:
        return {"analysis": recommend.analyze_taste(conn, cfg())}
    except recommend.RecommendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


# ---------------------------------------------------------------- static UI


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="web")
