"""Taste-profile building + OpenRouter recommendations + prompt export/import.

The profile is intentionally compact: an LLM does the heavy lifting, so we feed
it your loved/liked/disliked titles (with notes), aggregate genre signals, and a
full exclusion list so it never suggests something you already have or were
already shown.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

import requests

from . import db, tmdb

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Debug snapshot of the most recent model call (exposed via /api/debug/last).
LAST_RESPONSE: dict[str, Any] = {}


class RecommendError(RuntimeError):
    pass


# ---------------------------------------------------------------- profile


def _decade(year: int | None) -> str | None:
    if not year:
        return None
    return f"{(year // 10) * 10}s"


def build_profile(conn) -> dict[str, Any]:
    """Summarise taste from rated media into a compact, LLM-friendly structure."""
    rated = db.rated_media(conn)
    buckets: dict[str, list[dict[str, Any]]] = {"loved": [], "liked": [], "disliked": []}
    genre_score: Counter[str] = Counter()
    decade_score: Counter[str] = Counter()
    director_love: Counter[str] = Counter()

    weight = {"loved": 2, "liked": 1, "disliked": -2}
    for m in rated:
        v = m["verdict"]
        if v not in buckets:
            continue
        entry = {
            "title": m["title"],
            "year": m["year"],
            "type": m["type"],
            "genres": m["genres"],
        }
        if m.get("note"):
            entry["note"] = m["note"]
        buckets[v].append(entry)

        for g in m["genres"]:
            genre_score[g] += weight[v]
        dec = _decade(m["year"])
        if dec:
            decade_score[dec] += weight[v]
        if v == "loved":
            for d in m["directors"]:
                director_love[d] += 1

    liked_genres = [g for g, s in genre_score.most_common() if s > 0]
    disliked_genres = [g for g, s in sorted(genre_score.items(), key=lambda x: x[1]) if s < 0]

    return {
        "counts": {k: len(v) for k, v in buckets.items()},
        "loved": buckets["loved"],
        "liked": buckets["liked"],
        "disliked": buckets["disliked"],
        "liked_genres": liked_genres[:10],
        "disliked_genres": disliked_genres[:10],
        "favorite_decades": [d for d, _ in decade_score.most_common(3)],
        "loved_directors": [d for d, n in director_love.most_common(8) if n >= 1],
    }


# ---------------------------------------------------------------- prompt


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


# Caps that keep the prompt small (and cheap) no matter how big the library grows.
# Anything the model still re-suggests is removed afterward by dedupe_and_flag(),
# so we do NOT need to send the whole library — just enough taste signal.
_MAX_LOVED = 40
_MAX_LIKED = 25
_MAX_DISLIKED = 25
_MAX_PRIOR_RECS = 80


def _compact(entries: list[dict[str, Any]], limit: int) -> str:
    """Title (year) [note] list, capped — far cheaper than JSON dumps."""
    out = []
    for e in entries[:limit]:
        s = e["title"]
        if e.get("year"):
            s += f" ({e['year']})"
        if e.get("note"):
            s += f" — {e['note']}"
        out.append(s)
    extra = len(entries) - limit
    if extra > 0:
        out.append(f"…(+{extra} more)")
    return "; ".join(out) or "none yet"


def build_prompt(conn, count: int, media_type: str, vibe: str | None) -> str:
    """Compose a LEAN recommendation prompt.

    Earlier versions dumped the entire library (1000+ titles) as an exclusion
    list, which made each request ~40K tokens / ~$0.23. We rely on the code-side
    dedupe (dedupe_and_flag) to drop anything owned/seen, so the prompt only needs
    a capped taste sample plus the recent recommendations (to avoid repeats).
    """
    profile = build_profile(conn)

    type_phrase = {
        "movie": "movies",
        "show": "TV shows",
        "both": "movies and TV shows",
        "book": "books",
        "game": "video games",
        "all": "movies, TV shows, books, and video games",
    }.get(media_type, "movies, TV shows, books, and video games")

    # Only recent recommendations go in the prompt (small); the full library is
    # excluded after the fact in code, not by bloating the prompt.
    seen = set()
    prior = []
    for t in db.existing_rec_titles(conn):
        k = _norm(t["title"])
        if k in seen:
            continue
        seen.add(k)
        prior.append(t["title"] + (f" ({t['year']})" if t.get("year") else ""))
    prior_block = "; ".join(prior[:_MAX_PRIOR_RECS]) or "none"

    vibe_block = f"\nEXTRA STEER FROM ME RIGHT NOW: {vibe.strip()}\n" if vibe and vibe.strip() else ""

    return f"""You are a sharp, opinionated curator for film, TV, books, and video games. Recommend exactly {count} \
{type_phrase} I have NOT seen that match my taste below. Strongly favor lesser-known \
or older gems over obvious blockbusters — I have a huge library and have already \
mined the popular stuff, so DO NOT suggest mainstream/obvious titles; dig for things \
I would not find on my own. Be specific about WHY each fits my taste.

MY TASTE PROFILE:
- Genres I gravitate to: {", ".join(profile["liked_genres"]) or "n/a"}
- Genres I avoid: {", ".join(profile["disliked_genres"]) or "n/a"}
- Eras I lean toward: {", ".join(profile["favorite_decades"]) or "n/a"}
- Directors I love: {", ".join(profile["loved_directors"]) or "n/a"}
- LOVED: {_compact(profile["loved"], _MAX_LOVED)}
- LIKED: {_compact(profile["liked"], _MAX_LIKED)}
- DISLIKED (steer away from these patterns): {_compact(profile["disliked"], _MAX_DISLIKED)}

Already recommended to me — do NOT repeat these: {prior_block}
{vibe_block}
Respond with ONLY a JSON array, no prose, no markdown fences. Each element:
{{"title": str, "year": int, "type": "movie"|"show"|"book"|"game", "author": str (only if a book, else omit), "reason": str (one sentence, \
specific to my taste), "where_to_watch": str (best-guess streaming service or "rent/buy" or "bookstore" or "Steam")}}
"""


# ---------------------------------------------------------------- parse & dedupe


def parse_recommendations(text: str) -> list[dict[str, Any]]:
    """Tolerantly parse a JSON array of recs from an LLM response."""
    if not text or not text.strip():
        raise RecommendError("empty response")

    cleaned = text.strip()
    # strip ```json ... ``` fences if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    # find the outermost JSON array
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RecommendError(f"could not parse JSON from response: {exc}") from exc

    if isinstance(data, dict):
        data = data.get("recommendations") or data.get("results") or [data]
    if not isinstance(data, list):
        raise RecommendError("expected a JSON array of recommendations")

    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        year = item.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        mtype = item.get("type")
        out.append({
            "title": str(item["title"]).strip(),
            "year": year,
            "type": mtype if mtype in ("movie", "show", "book", "game") else None,
            "author": (item.get("author") or "").strip() or None,
            "reason": (item.get("reason") or "").strip() or None,
            "where_to_watch": (item.get("where_to_watch") or "").strip() or None,
        })
    return out


def dedupe_and_flag(conn, recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop recs already in library or already recommended; flag library matches."""
    library = {_norm(t["title"]) for t in db.all_titles(conn)}
    prior = {_norm(t["title"]) for t in db.existing_rec_titles(conn)}

    out = []
    seen_now: set[str] = set()
    for rec in recs:
        key = _norm(rec["title"])
        if key in seen_now or key in prior:
            continue
        seen_now.add(key)
        if key in library:
            # already own it — skip rather than surface a dupe
            continue
        rec["in_library"] = False
        out.append(rec)
    return out


def enrich_recs(cfg: dict[str, Any], recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort TMDB / Google Books lookup for each rec: fills poster, genres, canonical
    title/year, and corrects movie/show type. Silently no-ops without creds
    or on any single lookup failure (the rec just keeps the AI's raw fields)."""
    tmdb_cfg = cfg.get("tmdb") or {}
    has_tmdb = tmdb.has_credentials(tmdb_cfg)
    for rec in recs:
        if rec.get("type") == "book":
            try:
                q = f"{rec['title']} {rec.get('author') or ''}".strip()
                resp = requests.get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params={"q": q, "maxResults": 1},
                    timeout=5
                )
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if items:
                        vol = items[0].get("volumeInfo", {})
                        rec["title"] = vol.get("title") or rec["title"]
                        date = vol.get("publishedDate", "")
                        if date[:4].isdigit():
                            rec["year"] = int(date[:4])
                        authors = vol.get("authors", [])
                        if authors:
                            rec["author"] = authors[0]
                        thumb = vol.get("imageLinks", {}).get("thumbnail")
                        if thumb and thumb.startswith("http:"):
                            thumb = "https:" + thumb[5:]
                        rec["thumb"] = thumb
                        rec["genres"] = vol.get("categories", [])
            except Exception:
                pass
            continue
            
        if not has_tmdb:
            continue
        try:
            data = tmdb.enrich_title(tmdb_cfg, rec["title"], prefer=rec.get("type"),
                                     with_ids=True)
        except Exception:  # noqa: BLE001 - enrichment is optional, never fail recs
            data = None
        if not data:
            continue
        rec["title"] = data.get("title") or rec["title"]
        rec["year"] = data.get("year") or rec.get("year")
        rec["type"] = data.get("type") or rec.get("type")
        rec["genres"] = data.get("genres") or []
        rec["thumb"] = data.get("thumb")
        rec["imdb_id"] = data.get("imdb_id")
        rec["tmdb_id"] = data.get("tmdb_id")
        rec["tmdb_type"] = data.get("tmdb_type")
    return recs


# ---------------------------------------------------------------- openrouter


def _extract_text(content: Any) -> str:
    """Safely extract text from OpenRouter content, which can sometimes be a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if content is None:
        return ""
    return str(content)


def _chat_request(
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    max_tokens: int | None = None,
    temperature: float = 0.8,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Low-level OpenRouter chat call. Returns (message_obj, model_used).

    message_obj is choices[0].message — it may contain 'content' and/or 'tool_calls'.
    """
    orc = cfg.get("openrouter") or {}
    api_key = (orc.get("api_key") or "").strip()
    if not api_key:
        raise RecommendError(
            "No OpenRouter API key set. Add openrouter.api_key to config.json, "
            "or use the 'Copy prompt for Claude' export path instead."
        )
    model = orc.get("model") or "anthropic/claude-opus-4.8"
    # Cap output: an uncapped request makes OpenRouter reserve the model's full
    # context ceiling, which can trip a 402 on low balances.
    if max_tokens is None:
        max_tokens = int(orc.get("max_tokens", 4000))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": orc.get("site_url", "http://localhost:8765"),
        "X-Title": orc.get("app_name", "Praxis"),
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Thinking models (Gemini 2.5 Pro, etc.) can burn the entire token budget on
    # hidden reasoning, leaving no room for the answer (empty content) and running
    # up cost. We don't need deep deliberation to list titles, so keep it minimal.
    effort = orc.get("reasoning_effort", "low")
    if effort and effort != "default":
        payload["reasoning"] = {"effort": effort}
    if tools:
        payload["tools"] = tools
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
    except requests.RequestException as exc:
        raise RecommendError(f"OpenRouter request failed: {exc}") from exc

    if resp.status_code != 200:
        raise RecommendError(f"OpenRouter error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError) as exc:
        raise RecommendError(f"Unexpected OpenRouter response: {str(data)[:300]}") from exc

    # Capture everything about the last call for debugging (/api/debug/last).
    global LAST_RESPONSE
    LAST_RESPONSE = {
        "model": model,
        "finish_reason": choice.get("finish_reason"),
        "content": msg.get("content"),
        "content_len": len(msg.get("content") or ""),
        "has_tool_calls": bool(msg.get("tool_calls")),
        "message_keys": list(msg.keys()),
        "usage": data.get("usage"),
    }
    return msg, model


def chat_completion(
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    max_tokens: int | None = None,
    temperature: float = 0.8,
) -> tuple[str, str]:
    """Convenience wrapper returning just (content, model)."""
    msg, model = _chat_request(cfg, messages, max_tokens, temperature)
    return _extract_text(msg.get("content")), model


def call_openrouter(cfg: dict[str, Any], prompt: str) -> tuple[str, str]:
    """Single-prompt convenience wrapper around :func:`chat_completion`."""
    return chat_completion(cfg, [{"role": "user", "content": prompt}])


# ---------------------------------------------------------------- conversational chat


def build_chat_system(conn) -> str:
    """System prompt grounding the chat in the user's taste + library."""
    p = build_profile(conn)
    titles = lambda lst: ", ".join(  # noqa: E731
        f"{e['title']}{' (' + str(e['year']) + ')' if e.get('year') else ''}" for e in lst
    )
    loved = titles(p["loved"][:40]) or "none yet"
    liked = titles(p["liked"][:40]) or "none yet"
    disliked = titles(p["disliked"][:40]) or "none yet"
    return f"""You are Praxis, a sharp, opinionated film, TV, book, and video game concierge for ONE user. \
You know their taste from titles they have rated in their Plex library plus \
imported watch history. Be conversational, concise, and specific. When they ask \
what to watch, give a few precise picks with a one-line reason each, lean toward \
lesser-known/older gems (they have exhausted the obvious stuff), and do NOT \
recommend things they have clearly already seen — if unsure whether they've seen \
something, just ask. Ask a clarifying question when their mood/constraints are vague.

YOU CAN TAKE ACTIONS — USE THE TOOLS, never just answer from your own head:
- When the user asks for SUGGESTIONS / RECOMMENDATIONS / "what should I watch" / \
"something like X", you MUST call get_recommendations. Do NOT list titles yourself \
— the tool runs the real engine that excludes everything already in their library \
and returns proper cards. Pass a short `vibe` capturing their request (e.g. \
"80s action-adventure shows like A-Team and MacGyver"), the right `type`, and a count.
- When the user says they've seen/loved/hated something, call add_watched_titles.
- When they want to save something for later, call add_to_watchlist.
Disambiguate remakes by year (original 1980s MacGyver = 1985, reboot = 2016). After \
get_recommendations runs, give a one-line intro — the cards render below your message, \
so do NOT re-list the titles.

THEIR TASTE PROFILE:
- Gravitates to genres: {", ".join(p["liked_genres"]) or "n/a"}
- Avoids genres: {", ".join(p["disliked_genres"]) or "n/a"}
- Favorite eras: {", ".join(p["favorite_decades"]) or "n/a"}
- Loved directors: {", ".join(p["loved_directors"]) or "n/a"}
- LOVED (two thumbs): {loved}
- LIKED (one thumb): {liked}
- DISLIKED (thumbs down): {disliked}
"""


CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_recommendations",
            "description": "Generate fresh recommendations through the real engine: it excludes everything already in the user's library or previously suggested, enriches each with poster/genres/IMDb, and returns cards. ALWAYS use this when the user wants suggestions — never invent titles yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vibe": {"type": "string", "description": "Short summary of what the user wants, e.g. '80s action-adventure shows like A-Team and MacGyver' or 'a slow-burn 70s thriller'."},
                    "type": {"type": "string", "enum": ["movie", "show", "both", "book", "game", "all"], "description": "What to recommend."},
                    "count": {"type": "integer", "description": "How many (default 6)."},
                },
                "required": ["vibe"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_watched_titles",
            "description": "Add one or more titles the user has ALREADY SEEN, each with a verdict. Adds them to the rated library so they shape the taste profile and are excluded from future recommendations. Use for 'I loved/liked/hated X' or 'add X as a loved show'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "media_type": {"type": "string", "enum": ["movie", "show", "book", "game"]},
                                "verdict": {"type": "string", "enum": ["loved", "liked", "disliked"]},
                                "year": {"type": "integer", "description": "Release year if known, to disambiguate remakes."},
                            },
                            "required": ["title", "media_type", "verdict"],
                        },
                    }
                },
                "required": ["titles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_watchlist",
            "description": "Add one or more titles the user wants to watch in the FUTURE (has not seen yet) to their Want-to-Watch list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "media_type": {"type": "string", "enum": ["movie", "show", "book", "game"]},
                                "year": {"type": "integer"},
                            },
                            "required": ["title", "media_type"],
                        },
                    }
                },
                "required": ["titles"],
            },
        },
    },
]


def _exec_add_watched(conn, cfg: dict[str, Any], titles: list[dict[str, Any]]) -> list[str]:
    tmdb_cfg = cfg.get("tmdb") or {}
    have_tmdb = tmdb.has_credentials(tmdb_cfg)
    done = []
    for it in titles:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        mtype = it.get("media_type") if it.get("media_type") in ("movie", "show", "book", "game") else "show"
        verdict = it.get("verdict") if it.get("verdict") in ("loved", "liked", "disliked") else "liked"
        year = it.get("year")
        key = db.find_media_key_by_title(conn, title)
        if key is None:
            data = None
            if have_tmdb:
                try:
                    data = tmdb.enrich_title(tmdb_cfg, title, prefer=mtype, year=year)
                except Exception:  # noqa: BLE001
                    data = None
            key = db.add_external_media(
                conn,
                (data or {}).get("title") or title,
                (data or {}).get("type") or mtype,
                source="manual",
                year=(data or {}).get("year") or year,
                genres=(data or {}).get("genres"),
                thumb=(data or {}).get("thumb"),
                enriched=bool(data),
            )
        if key:
            db.set_rating(conn, key, verdict)
            done.append(f"{title} → {verdict}")
    return done


def _exec_add_watchlist(conn, cfg: dict[str, Any], titles: list[dict[str, Any]]) -> list[str]:
    tmdb_cfg = cfg.get("tmdb") or {}
    items = []
    for it in titles:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        rec = {"title": title, "type": it.get("media_type"), "year": it.get("year"),
               "reason": "Added from chat"}
        items.append(rec)
    enrich_recs(cfg, items)
    ids = db.add_recommendations(conn, items, source="chat")
    for rid in ids:
        db.update_recommendation(conn, rid, status="queued")
    return [it["title"] for it in items]


def chat(conn, cfg: dict[str, Any], session_id: str, content: str) -> dict[str, Any]:
    """Conversation grounded in the user's taste — and able to take actions via tools."""
    # save user message
    db.add_chat_message(conn, session_id, "user", content)
    
    # fetch history
    history = db.get_chat_messages(conn, session_id)

    convo: list[dict[str, Any]] = [{"role": "system", "content": build_chat_system(conn)}]
    convo += [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    actions: list[str] = []
    recommendations: list[dict[str, Any]] = []  # cards to render in the chat
    model = ""
    for _ in range(4):  # cap tool round-trips
        msg, model = _chat_request(cfg, convo, max_tokens=2500, temperature=0.7, tools=CHAT_TOOLS)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            reply = _extract_text(msg.get("content")).strip()
            if not reply:
                reply = ("Here are some picks 👇" if recommendations
                         else "Sorry — I didn't catch that. Mind rephrasing?")
            db.add_chat_message(conn, session_id, "assistant", reply, recommendations)
            return {"reply": reply, "model": model,
                    "actions": actions, "recommendations": recommendations}

        # echo the assistant's tool-call turn back, then append each tool result
        convo.append({"role": "assistant", "content": _extract_text(msg.get("content")),
                      "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "get_recommendations":
                mtype = args.get("type") if args.get("type") in ("movie", "show", "both", "book", "game", "all") else "all"
                cnt = max(1, min(int(args.get("count") or 6), 15))
                res = recommend(conn, cfg, cnt, mtype, args.get("vibe"))
                recommendations += res.get("recommendations", [])
                names = [r["title"] for r in res.get("recommendations", [])]
                result_text = (
                    f"Generated {len(names)} recommendations (already filtered against "
                    f"the user's library + past suggestions); cards are shown to the user: "
                    + "; ".join(names)
                ) if names else "No new picks (everything matched was already in their library)."
            elif name == "add_watched_titles":
                res = _exec_add_watched(conn, cfg, args.get("titles", []))
                actions += [f"Rated: {r}" for r in res]
                result_text = "Added & rated: " + "; ".join(res) if res else "Nothing added."
            elif name == "add_to_watchlist":
                res = _exec_add_watchlist(conn, cfg, args.get("titles", []))
                actions += [f"Watchlist: {r}" for r in res]
                result_text = "Added to Want-to-Watch: " + "; ".join(res) if res else "Nothing added."
            else:
                result_text = f"Unknown tool {name}."
            convo.append({"role": "tool", "tool_call_id": call.get("id"),
                          "content": result_text})

    # Fell out of the loop still wanting tools — return a graceful summary
    reply = "Done." if (actions or recommendations) else "I wasn't able to finish that."
    db.add_chat_message(conn, session_id, "assistant", reply, recommendations)
    return {"reply": reply,
            "model": model, "actions": actions, "recommendations": recommendations}


def analyze_taste(conn, cfg: dict[str, Any]) -> str:
    """Provides a fun, opinionated AI analysis of the user's taste profile."""
    p = build_profile(conn)
    titles = lambda lst: ", ".join(  # noqa: E731
        f"{e['title']}{' (' + str(e['year']) + ')' if e.get('year') else ''}" for e in lst
    )
    loved = titles(p["loved"][:40]) or "none yet"
    liked = titles(p["liked"][:40]) or "none yet"
    disliked = titles(p["disliked"][:40]) or "none yet"

    prompt = f"""You are Praxis, a sharp, highly opinionated, slightly snarky but fun film, TV & book concierge.
I want you to analyze and 'roast' my taste profile based on my ratings. 
Write a 2-3 paragraph prose analysis of my cinematic psychology. What do my ratings say about me? What are my blind spots? Be specific, engaging, and fun.

MY TASTE PROFILE:
- Gravitates to genres: {", ".join(p["liked_genres"]) or "n/a"}
- Avoids genres: {", ".join(p["disliked_genres"]) or "n/a"}
- Favorite eras: {", ".join(p["favorite_decades"]) or "n/a"}
- Loved directors: {", ".join(p["loved_directors"]) or "n/a"}
- LOVED (two thumbs): {loved}
- LIKED (one thumb): {liked}
- DISLIKED (thumbs down): {disliked}
"""
    content, _ = call_openrouter(cfg, prompt)
    if not content:
        raise RecommendError("Failed to generate analysis.")
    return content


def recommend(conn, cfg: dict[str, Any], count: int, media_type: str,
              vibe: str | None) -> dict[str, Any]:
    """Full pipeline: prompt -> OpenRouter -> parse -> TMDB enrich -> dedupe -> store.

    Over-fetches so that titles already in the library (which dedupe drops) don't
    leave us with too few — important for big libraries where a narrow ask would
    otherwise come back empty.
    """
    overfetch = min(count + 10, 25)
    prompt = build_prompt(conn, overfetch, media_type, vibe)
    content, model = call_openrouter(cfg, prompt)
    try:
        parsed = parse_recommendations(content)
        # Force strict typing if requested; prevents LLM from hallucinating "movie" for famous books
        if media_type in ("movie", "show", "book", "game"):
            for rec in parsed:
                rec["type"] = media_type
    except RecommendError as exc:
        fr = LAST_RESPONSE.get("finish_reason")
        snippet = (content or "")[:200].replace("\n", " ")
        raise RecommendError(
            f"{exc} | model={model} finish={fr} content_len={LAST_RESPONSE.get('content_len')} "
            f"| raw starts: {snippet!r} — see /api/debug/last"
        ) from exc
    # Enrich first so dedupe uses TMDB's canonical titles (better dup detection).
    enrich_recs(cfg, parsed)
    fresh = dedupe_and_flag(conn, parsed)[:count]  # trim to what the user asked for
    ids = db.add_recommendations(conn, fresh, source=f"openrouter:{model}")
    stored = db.list_recommendations(conn, status="new")
    by_id = {r["id"]: r for r in stored}
    return {"model": model, "added": len(ids), "recommendations": [by_id[i] for i in ids if i in by_id]}
