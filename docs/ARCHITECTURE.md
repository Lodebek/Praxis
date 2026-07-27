# Praxis — Architecture & Implementation

How Praxis turns a Plex library into a personal recommendation engine — the
pipeline, the components, and the design decisions behind them.

## The problem it solves

You've watched everything obvious. New releases miss. You keep digging into older
years to find something good, and you re-derive the same dead ends because nothing
remembers what you already rejected. Praxis fixes that: it learns your taste from
what you own and rate, then uses an LLM to surface specific, lesser-known titles
you haven't seen — and never forgets a pick.

## High-level flow

```
 Plex library ─┐
 Netflix CSV ──┼─► media (SQLite) ─► you rate it ─► taste profile ─┐
 manual adds ──┘            ▲                                       │
                            │                                       ▼
                    TMDB enrichment                        LLM (via OpenRouter)
              (year, genres, poster, IMDb id)                      │
                            ▲                                       ▼
                            └──────────── recommendations ◄────────┘
                                          (de-duped vs library, enriched,
                                           rated / queued / dismissed)
```

Everything runs locally. The only thing that leaves the machine is a recommendation
prompt (your taste profile — no credentials) to OpenRouter, and title lookups to TMDB.

## Components

| Module | Responsibility |
|---|---|
| `praxis/config.py` | Loads `config.json` over `config.example.json` defaults |
| `praxis/db.py` | SQLite schema + all queries; lightweight in-code migrations |
| `praxis/plex.py` | Reads the Plex token (Windows registry / config) and pulls the library as JSON |
| `praxis/tmdb.py` | Title lookups: year, genres, poster, IMDb id; bearer-token auth |
| `praxis/importers.py` | Netflix CSV parser (episode → series collapsing) |
| `praxis/recommend.py` | Taste profile, prompt building, OpenRouter calls, agentic chat |
| `praxis/server.py` | FastAPI: REST API + serves the static UI |
| `web/` | Vanilla HTML/CSS/JS single-page UI (no build step) |

## Data model (SQLite)

- **`media`** — one row per title from any source (`plex` / `netflix` / `manual`),
  with `enriched` flag, genres, poster, etc.
- **`ratings`** — your verdict per title (`loved` / `liked` / `disliked`); a row
  exists only once you rate it.
- **`recommendations`** — every AI suggestion ever, with status
  (`new` / `queued` / `watched` / `dismissed`) so nothing repeats or is forgotten.

## Key design decisions

**Netflix-style verdicts, not 5 stars.** Research (and Netflix's own pivot) shows
binary-ish thumbs get far more, far more honest ratings than star scales. Coverage
beats false precision — 300 honest thumbs beat 80 agonized stars. An optional
free-text note carries the nuance ("loved the first two seasons") that a number can't.

**TMDB does the metadata, the LLM does the taste.** Plex's own agents are backed by
TMDB, so we use the same source for posters/genres/IMDb ids — and TMDB's result also
tells us whether something is a movie or show, auto-correcting mislabels from the
Netflix import. The LLM is reserved for the one thing it's uniquely good at: reasoning
about taste.

**Post-hoc de-dupe, not a giant prompt.** Early on the prompt shipped the *entire*
library (1,000+ titles) as an exclusion list — ~42K tokens, ~$0.23/call. Now the code
filters owned/seen titles *after* the model responds, so the prompt is a capped taste
sample (~900 tokens, ~$0.005). 50× cheaper, same quality.

**Over-fetch to survive de-dupe.** A deep library can dedupe a batch down to zero, so
the engine requests extra picks and trims to what you asked for — you never get an
empty result just because you own the obvious ones.

**Reasoning budget capped.** Thinking models (e.g. Gemini 2.5 Pro) can burn the whole
output budget on hidden reasoning, returning empty content *and* a surprise bill. A
low `reasoning_effort` keeps replies reliable and cheap.

**Chat is agentic, grounded in your data.** The chat tab uses tool-calling: it can run
the real recommendation engine (library-excluded, enriched cards rendered inline), add
titles you've seen with a verdict, or queue things — all from plain language, with
year-disambiguation for remakes.

## The recommendation pipeline (end to end)

1. Build a compact taste profile from your ratings (liked/disliked genres, eras,
   loved directors, capped title samples with notes).
2. Compose a lean prompt; ask for `count + buffer` picks, biased hard toward
   lesser-known titles.
3. Call the model via OpenRouter (model + reasoning budget configurable).
4. Tolerantly parse the JSON (handles fenced/prose-wrapped output).
5. Enrich each pick via TMDB (poster, year, genres, IMDb id).
6. De-dupe against the whole library + every prior recommendation; trim to `count`.
7. Store and render as cards you rate, queue, or dismiss — feeding step 1 next time.

## Tech

Python 3.11+ · FastAPI · SQLite · vanilla JS · TMDB API · OpenRouter. No build step,
single command to run, ~3,900 lines.
