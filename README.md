# Praxis

A lightweight local hub and AI recommendation engine. It unifies your taste profile across four media types: movies, TV shows, PC games, and e-books. 

Built as a local-first alternative to cloud tracking services, Praxis automatically indexes your local libraries (Plex, game folders, and e-book directories), lets you rate everything instantly with a Netflix-style thumb system, builds a comprehensive taste profile, and uses an LLM (via OpenRouter) to give you hyper-personalized recommendations—tracking every single suggestion so nothing gets forgotten or recommended twice.

Everything runs locally on your machine. Your library data never leaves your computer, and your API keys live only in your `.git-ignored` config file.

## Screenshots

### The Discover Engine
![Discover tab](docs/screenshots/discover.png)
*Builds a personalized prompt from your taste profile and returns fresh recommendations as interactive cards.*

### Fast Rating
![Rate tab](docs/screenshots/rate.png)
*Rapidly sort and grade your library using a Netflix-style thumbs system to build your profile.*

### Watchlist
![Watchlist tab](docs/screenshots/watchlist.png)
*Tracks all your pinned recommendations. Rate them as you watch/play/read to feed your memory.*

### AI Chat & Actions
![Chat tab](docs/screenshots/chat.png)
*A conversational interface grounded in your exact taste profile. Use it for contextual requests or natural language actions.*

## Quick Start

```bash
# 1. Install dependencies
python -m pip install -r requirements.txt

# 2. Set up your config
cp config.example.json config.json   # (Windows: copy config.example.json config.json)
```

Fill in your API keys in `config.json`:
- `openrouter.api_key` → Required for AI chat and recommendations.
- `tmdb.read_access_token` → Required to fetch movie and TV posters.

```bash
# 3. Run the server
python run.py
```
Your browser will automatically open to `http://127.0.0.1:8765`. 

---

## Supported Media & Data Sources

Praxis connects to multiple external databases to automatically enrich your local files with gorgeous metadata.

### 1. Movies & TV Shows (Plex + TMDB)
- **Scanning:** Praxis automatically connects to your local Plex Media Server (`http://localhost:32400`) to sync your entire movie and TV library. (On Windows, the Plex token is read from the registry automatically).
- **Enrichment:** Queries **TMDB (The Movie Database)** to download official posters, genres, release years, and plot summaries.

### 2. PC Games (Local Folders + Steam)
- **Scanning:** Point Praxis at your local game installation directories (e.g., `D:\Games`), and it will automatically search, clean, and list every game it can find.
- **Enrichment:** Queries the public **Steam API** to fetch official game capsules (cover art), developers, and game genres. Background scanning uses intelligent batching to respect public API limits. No API key required.

### 3. E-Books (Local Folders + Google Books)
- **Scanning:** Point Praxis at your e-book folders. It natively reads metadata headers directly from `.epub`, `.mobi`, `.azw3`, and `.pdf` files to extract the exact book title and author.
- **Enrichment:** Queries the public **Google Books API** to download high-resolution book covers and literary genres. Background scanning uses intelligent batching to respect public API limits. No API key required.

### 4. Netflix History (CSV Import)
- **Import:** Upload your `NetflixViewingHistory.csv` file. Praxis smartly collapses raw episode logs into single TV show entries (e.g., *Breaking Bad: Season 1: Pilot* → *Breaking Bad*). Imports are automatically marked as "watched" so the AI doesn't recommend them.

---

## Core Features

### Fast Rating System
Praxis uses the highly efficient Netflix rating model. Designed for speed rather than agonized nuance, it lets you actually get through your backlog:
- **👍👍 Two thumbs up** = Loved it
- **👍 One thumb up** = Liked it (meh-to-good)
- **👎 Thumbs down** = Nope / Hated it
- **❌ Ignore** = Exclude this from stats and AI profiles

You can also add a short, 1-line **Note** to any rating (e.g., *"loved the combat mechanics"*, *"fell apart in Season 3"*, *"couldn't put this book down"*). This note is the single most powerful signal the AI uses to understand *why* you like things.

### The AI Recommendation Engine (Discover)
Choose how many recommendations you want, select your media types (movies, TV, games, books, or all), and optionally provide a **Vibe Steer** (e.g., *"a sci-fi RPG like Mass Effect"* or *"something short and funny"*). 

Praxis builds an enhanced prompt containing your exact taste profile and an exclusion list of *everything you already own or have rated*. It sends this to the LLM. 
The LLM returns suggestions, which Praxis automatically looks up in TMDB, Steam, and Google Books to render as real, interactive cards. You can immediately rate these suggestions or pin them to your **Watchlist**.

### AI Chat & Natural Language Actions
A fully conversational interface grounded in your exact taste profile. Because the LLM uses your unified profile to keep recommendations focused, you can ask it highly contextual questions.
The Chat also supports **Natural Language Actions**. You can simply type:
- *"Add Halo 3 and Mass Effect 2 as loved games"* → They are instantly added to your library and rated.
- *"Put Dune on my reading watchlist"* → It goes straight to your queue.

### Sorting, Filtering, and Stats
- **Filter by Source:** View only your Plex media, your PC games, your e-books, or your manually added titles.
- **Stats Dashboard:** See your verdict breakdowns, completion percentages, and automatically calculated favorite genres and eras.

---

## Configuration Reference (`config.json`)

| Key | Description |
|---|---|
| `plex.base_url` | Your Plex server address (default: `http://localhost:32400`). |
| `plex.token` | Leave blank on Windows (auto-reads registry). Set manually for macOS/Linux. |
| `openrouter.api_key` | Your OpenRouter key (required for AI features). |
| `openrouter.model` | The LLM to use (e.g., `google/gemini-2.5-pro` or `anthropic/claude-opus-4.8`). |
| `tmdb.read_access_token`| Your TMDB v4 Bearer Token for movie and TV enrichment. |
| `server.port` | The port the web UI runs on (default: 8765). |

---

## Privacy & Architecture
- **Tech Stack:** Python 3.11+, FastAPI, SQLite (`data/praxis.db`), Vanilla JS/CSS. No build step required.
- **Privacy:** Built as a local-first hub. Your database, watch history, and API keys are 100% local and git-ignored. The only data that leaves your network are anonymous media titles (e.g., querying TMDB for "The Matrix" or sending an anonymous list of your rated games to OpenRouter). No personally identifiable information, IP addresses, or library credentials ever leave your machine.
