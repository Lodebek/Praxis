"use strict";

// ----------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// Bump in lockstep with SERVER_VERSION in praxis/server.py.
const EXPECTED_SERVER_VERSION = "2026-06-06.2";

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    // 404/405 on an /api route almost always means a stale server process
    // (fresh static files, old python). Make that explicit instead of cryptic.
    if ((res.status === 405 || res.status === 404) && path.startsWith("/api/")) {
      showStaleBanner();
      throw new Error("Server is out of date — stop it and run `python run.py` again.");
    }
    const detail = (data && data.detail) || res.statusText;
    throw new Error(detail);
  }
  return data;
}

function showStaleBanner() {
  const b = $("#stale-banner");
  if (b) b.classList.remove("hidden");
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ----------------------------------------------------------------- state
const state = {
  status: "unrated",
  type: "",
  source: "",
  sort: "title",
  q: "",
  items: [],
  focusIndex: -1,
  recStatus: "queued", // Watchlist defaults to only what you chose to "Watch later"
};

// ----------------------------------------------------------------- tabs
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#" + tab.dataset.tab).classList.add("active");
    if (tab.dataset.tab === "watchlist") loadWatchlist();
    if (tab.dataset.tab === "stats") loadStats();
    if (tab.dataset.tab === "chat") { renderChat(); $("#chat-input").focus(); }
    if (tab.dataset.tab === "add") loadEnrichStatus();
    if (tab.dataset.tab === "discover") initDiscover();
  });
});

// ----------------------------------------------------------------- health / sync
async function checkHealth() {
  const dot = $("#plex-status");
  try {
    const h = await api("/api/health");
    if (h.plex && h.plex.ok) {
      dot.className = "dot ok";
      dot.title = `Plex OK (v${h.plex.version || "?"})`;
    } else {
      dot.className = "dot bad";
      dot.title = "Plex: " + ((h.plex && h.plex.error) || "unreachable");
    }
    // Detect a stale server process (old code answering, fresh files served).
    if (h.server_version !== EXPECTED_SERVER_VERSION) {
      showStaleBanner();
      console.warn("[praxis] server version", h.server_version, "expected", EXPECTED_SERVER_VERSION);
    }
  } catch (e) {
    dot.className = "dot bad";
    dot.title = "API error: " + e.message;
  }
}

$("#sync-btn").addEventListener("click", async () => {
  const btn = $("#sync-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Syncing…';
  try {
    const r = await api("/api/sync", { method: "POST" });
    toast(`Synced ${r.counts.movie} movies + ${r.counts.show} shows`);
    await loadMedia();
  } catch (e) {
    toast("Sync failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Sync Plex";
  }
});

// ----------------------------------------------------------------- RATE tab
$$("#status-filters .chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $$("#status-filters .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.status = chip.dataset.status;
    loadMedia();
  }));

$$("#type-filters .chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $$("#type-filters .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.type = chip.dataset.type;
    loadMedia();
  }));

$$("#source-filters .chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $$("#source-filters .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.source = chip.dataset.source;
    loadMedia();
  }));

$("#sort-select").addEventListener("change", (e) => {
  state.sort = e.target.value;
  loadMedia();
});

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    loadMedia();
  }, 250);
});

async function loadMedia() {
  const params = new URLSearchParams({ status: state.status, sort: state.sort });
  if (state.type) params.set("type", state.type);
  if (state.source) params.set("source", state.source);
  if (state.q) params.set("q", state.q);
  const grid = $("#grid");
  grid.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const data = await api("/api/media?" + params.toString());
    state.items = data.items;
    state.focusIndex = data.items.length ? 0 : -1;
    renderGrid();
    await loadStatsProgress();
  } catch (e) {
    grid.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function renderGrid() {
  const grid = $("#grid");
  const empty = $("#rate-empty");
  if (!state.items.length) {
    grid.innerHTML = "";
    empty.classList.remove("hidden");
    empty.textContent =
      state.status === "unrated"
        ? "Nothing unrated here — try Sync Plex, or switch the filter. 🎉"
        : "No titles match.";
    return;
  }
  empty.classList.add("hidden");
  grid.innerHTML = state.items.map((m, i) => cardHTML(m, i)).join("");
  bindCards();
  setFocus(state.focusIndex);
}

function cardHTML(m, i) {
  const genres = (m.genres || []).slice(0, 3).join(" · ");
  const v = m.verdict;
  // Plex posters proxy through /api/thumb (keeps token server-side); TMDB posters
  // are public URLs we can use directly.
  const src = m.thumb
    ? (m.thumb.startsWith("http") ? m.thumb : "/api/thumb/" + encodeURIComponent(m.ratingKey))
    : null;
  const poster = src
    ? `<img class="poster" loading="lazy" src="${esc(src)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'poster placeholder',textContent:${JSON.stringify(m.title)}}))" />`
    : `<div class="poster placeholder">${esc(m.title)}</div>`;
  const badge = m.source && m.source !== "plex"
    ? `<span class="src-badge">${m.source === "netflix" ? "Netflix" : "Added"}</span>` : "";
  return `
  <div class="card" data-key="${esc(m.ratingKey)}" data-index="${i}">
    ${badge}
    ${poster}
    <div class="card-body">
      <div class="card-title">${esc(m.title)}</div>
      <div class="card-meta">${m.year || ""}${m.type === "show" ? " · TV" : ""}${genres ? " · " + esc(genres) : ""}</div>
      <div class="verdicts">
        <button class="verdict-btn loved ${v === "loved" ? "active" : ""}" data-v="loved" title="Love it (2)">👍👍</button>
        <button class="verdict-btn liked ${v === "liked" ? "active" : ""}" data-v="liked" title="Like it (1)">👍</button>
        <button class="verdict-btn disliked ${v === "disliked" ? "active" : ""}" data-v="disliked" title="Nope (3)">👎</button>
      </div>
      <textarea class="note-input" placeholder="note (optional)…" data-key="${esc(m.ratingKey)}">${esc(m.note || "")}</textarea>
    </div>
  </div>`;
}

function bindCards() {
  $$(".card").forEach((card) => {
    const key = card.dataset.key;
    card.addEventListener("mousedown", () => setFocus(+card.dataset.index));
    $$(".verdict-btn", card).forEach((btn) =>
      btn.addEventListener("click", () => {
        const cur = state.items[+card.dataset.index].verdict;
        const next = cur === btn.dataset.v ? null : btn.dataset.v; // toggle off
        rate(key, next);
      }));
    const note = $(".note-input", card);
    note.addEventListener("blur", () => {
      const m = state.items.find((x) => x.ratingKey === key);
      if (!m) return;
      if ((note.value || "") === (m.note || "")) return;
      // saving a note only matters if there's a verdict; keep verdict as-is
      rate(key, m.verdict, note.value, true);
    });
  });
}

async function rate(key, verdict, note = undefined, silent = false) {
  const m = state.items.find((x) => x.ratingKey === key);
  const body = { ratingKey: key, verdict };
  if (note !== undefined) body.note = note;
  else if (m) body.note = m.note || null;
  try {
    const r = await api("/api/rate", { method: "POST", body: JSON.stringify(body) });
    if (m) { m.verdict = r.item.verdict; m.note = r.item.note; }
    // update buttons in place
    const card = $(`.card[data-key="${CSS.escape(key)}"]`);
    if (card) {
      $$(".verdict-btn", card).forEach((b) =>
        b.classList.toggle("active", b.dataset.v === r.item.verdict));
    }
    if (!silent) {
      // if filtering by unrated and we just rated, drop it from view
      if (state.status === "unrated" && r.item.verdict) {
        removeCard(key);
      } else if (["loved", "liked", "disliked"].includes(state.status) &&
                 r.item.verdict !== state.status) {
        removeCard(key);
      }
    }
    loadStatsProgress();
  } catch (e) {
    toast("Rate failed: " + e.message);
  }
}

function removeCard(key) {
  const idx = state.items.findIndex((x) => x.ratingKey === key);
  if (idx === -1) return;
  state.items.splice(idx, 1);
  if (state.focusIndex >= state.items.length) state.focusIndex = state.items.length - 1;
  renderGrid();
}

function setFocus(i) {
  state.focusIndex = i;
  $$(".card").forEach((c) => c.classList.remove("focused"));
  if (i < 0) return;
  const card = $(`.card[data-index="${i}"]`);
  if (card) {
    card.classList.add("focused");
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

// keyboard shortcuts for rating
document.addEventListener("keydown", (e) => {
  if (!$("#rate").classList.contains("active")) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (state.focusIndex < 0 || !state.items.length) return;
  const m = state.items[state.focusIndex];
  const map = { "2": "loved", "1": "liked", "3": "disliked", "0": null };
  if (e.key in map) {
    e.preventDefault();
    rate(m.ratingKey, map[e.key]);
  } else if (e.key === "n" || e.key === "ArrowRight" || e.key === "ArrowDown") {
    e.preventDefault();
    setFocus(Math.min(state.focusIndex + 1, state.items.length - 1));
  } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
    e.preventDefault();
    setFocus(Math.max(state.focusIndex - 1, 0));
  }
});

async function loadStatsProgress() {
  try {
    const s = await api("/api/stats");
    const pct = s.pct_rated || 0;
    $("#progress-bar").style.width = pct + "%";
    $("#progress-text").textContent = `${s.rated} / ${s.total} rated (${pct}%)`;
  } catch { /* ignore */ }
}

// ----------------------------------------------------------------- DISCOVER tab
const GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
  "Drama", "Fantasy", "History", "Horror", "Mystery", "Romance", "Sci-Fi",
  "Thriller", "War", "Western"];

// label -> instruction the LLM understands
const MOODS = {
  "🔦 Hidden gems": "Strongly favor lesser-known, under-the-radar titles I'm unlikely to find on my own.",
  "🍿 Light & fun": "Keep the tone light, fun, and easy to watch.",
  "🌑 Dark & heavy": "A darker, heavier, more intense tone is welcome.",
  "🐢 Slow burn": "Prefer slow-burn, deliberate, atmospheric pacing.",
  "⏱️ Short": "Prefer shorter commitments — movies under ~110 min or tight limited series.",
  "📺 Bingeable": "Prefer bingeable multi-season series.",
  "📼 Older (pre-2000)": "Prefer titles released before 2000.",
  "🆕 Recent": "Prefer titles from the last ~5 years.",
  "🌍 Foreign": "Include great foreign-language / subtitled titles.",
};

let discoverInited = false;
function initDiscover() {
  if (!discoverInited) {
    const gc = $("#genre-chips");
    gc.innerHTML = GENRES.map((g) => `<button class="chip" data-chip="${g}">${g}</button>`).join("");
    const mc = $("#mood-chips");
    mc.innerHTML = Object.keys(MOODS).map((m) => `<button class="chip" data-chip="${esc(m)}">${esc(m)}</button>`).join("");
    [gc, mc].forEach((box) => $$(".chip", box).forEach((c) =>
      c.addEventListener("click", () => c.classList.toggle("active"))));
    discoverInited = true;
  }
  // taste summary + hint so "Get recommendations" isn't a black box
  api("/api/profile").then((p) => {
    const top = (p.liked_genres || []).slice(0, 5);
    const eras = (p.favorite_decades || []).slice(0, 2);
    const bits = [];
    if (top.length) bits.push("genres like <b>" + top.map(esc).join(", ") + "</b>");
    if (eras.length) bits.push("the <b>" + eras.map(esc).join(" & ") + "</b>");
    $("#taste-summary").innerHTML = bits.length
      ? "Based on your ratings, your taste skews toward " + bits.join(" and ") +
        ". Pick filters below to narrow it, or just hit a button."
      : "Rate some titles first so I can learn your taste.";
  }).catch(() => {});
  $("#discover-hint").textContent = "Get recommendations = uses your ratings + filters. 🎲 Surprise me = ignores filters and takes a swing.";
}

function chipValues(boxId) {
  return $$("#" + boxId + " .chip.active").map((c) => c.dataset.chip);
}

function buildVibe(extra) {
  const parts = [];
  const genres = chipValues("genre-chips");
  if (genres.length) parts.push("Focus on these genres: " + genres.join(", ") + ".");
  const moods = chipValues("mood-chips").map((m) => MOODS[m]).filter(Boolean);
  if (moods.length) parts.push(moods.join(" "));
  const free = $("#rec-vibe").value.trim();
  if (free) parts.push(free);
  if (extra) parts.push(extra);
  return parts.join(" ") || null;
}

function recBody(extra) {
  return {
    count: Math.max(1, Math.min(25, +$("#rec-count").value || 8)),
    type: $("#rec-type").value,
    vibe: buildVibe(extra),
  };
}

async function runRecommend(body, btn) {
  const msg = $("#discover-msg");
  const buttons = [$("#recommend-btn"), $("#surprise-btn")];
  const original = btn.innerHTML;
  buttons.forEach((b) => (b.disabled = true));
  btn.innerHTML = '<span class="spinner"></span> Thinking…';
  msg.className = "msg";
  msg.textContent = "Asking the AI for picks (this can take 20-40s)…";
  try {
    const r = await api("/api/recommend", { method: "POST", body: JSON.stringify(body) });
    msg.className = "msg ok";
    msg.textContent = `Got ${r.added} new pick${r.added === 1 ? "" : "s"} via ${r.model}. Rate them below — 👍 if you've seen it, 📌 to watch later.`;
    renderRecs($("#discover-results"), r.recommendations, true);
  } catch (e) {
    msg.className = "msg error";
    msg.textContent = e.message;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
    btn.innerHTML = original;
  }
}

$("#recommend-btn").addEventListener("click", () =>
  runRecommend(recBody(), $("#recommend-btn")));

$("#surprise-btn").addEventListener("click", () =>
  runRecommend(
    recBody("Surprise me — take a creative swing. Pick something I probably wouldn't think of myself but that genuinely fits my taste; mix up genres and eras."),
    $("#surprise-btn"),
  ));

$("#copy-prompt-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/export-prompt", { method: "POST", body: JSON.stringify(recBody()) });
    await navigator.clipboard.writeText(r.prompt);
    toast("Prompt copied — paste it to Claude, then bring the answer back below.");
  } catch (e) {
    toast("Copy failed: " + e.message);
  }
});

$("#import-btn").addEventListener("click", async () => {
  const text = $("#import-text").value.trim();
  if (!text) { toast("Paste Claude's answer first."); return; }
  const msg = $("#discover-msg");
  try {
    const r = await api("/api/import-recommendations", {
      method: "POST", body: JSON.stringify({ text }),
    });
    msg.className = "msg ok";
    msg.textContent = `Imported ${r.added} new picks.`;
    $("#import-text").value = "";
    renderRecs($("#discover-results"), r.recommendations, true);
  } catch (e) {
    msg.className = "msg error";
    msg.textContent = e.message;
  }
});

// ----------------------------------------------------------------- WATCHLIST tab
$$("#rec-status-filters .chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $$("#rec-status-filters .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.recStatus = chip.dataset.rstatus;
    loadWatchlist();
  }));

async function loadWatchlist() {
  const list = $("#watchlist-list");
  list.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const params = state.recStatus !== "all" ? "?status=" + state.recStatus : "";
    const data = await api("/api/recommendations" + params);
    if (!data.items.length) {
      const msg = {
        queued: 'Your Want-to-Watch list is empty. On a recommendation in <b>Discover</b>, hit 📌 <b>Watch later</b> to add it here.',
        watched: "Nothing marked Seen yet.",
        dismissed: "Nothing dismissed.",
        new: "No unsorted suggestions — you've triaged them all. 🎉",
        all: "No recommendations yet. Head to Discover.",
      }[state.recStatus] || "Nothing here.";
      list.innerHTML = `<div class="empty">${msg}</div>`;
      return;
    }
    renderRecs(list, data.items, false);
  } catch (e) {
    list.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function renderRecs(container, recs, isDiscover) {
  if (!recs || !recs.length) {
    if (isDiscover) container.innerHTML = '<div class="empty">No new picks (all were dupes). Try again or tweak the vibe.</div>';
    return;
  }
  container.innerHTML = recs.map((r) => recHTML(r)).join("");
  $$(".rec", container).forEach((el) => {
    const id = +el.dataset.id;
    $$("[data-verdict]", el).forEach((btn) =>
      btn.addEventListener("click", () => recRate(id, btn.dataset.verdict, el)));
    $$("[data-action]", el).forEach((btn) =>
      btn.addEventListener("click", () => recAction(id, btn.dataset.action, el)));
  });
}

const STATUS_LABEL = {
  new: "New", queued: "📌 Want to watch", watched: "Seen", dismissed: "Dismissed",
};

// "Find out more" links for a recommendation. IMDb has no free API, but TMDB
// gives us the IMDb id, so we deep-link straight to the IMDb title page when we
// have it (falling back to an IMDb search). Plus TMDB + JustWatch (where to stream).
function recLinks(r) {
  const q = encodeURIComponent(`${r.title} ${r.year || ""}`.trim());
  const links = [];
  links.push(r.imdb_id
    ? `<a href="https://www.imdb.com/title/${encodeURIComponent(r.imdb_id)}/" target="_blank" rel="noopener">IMDb ↗</a>`
    : `<a href="https://www.imdb.com/find/?q=${q}" target="_blank" rel="noopener">IMDb search ↗</a>`);
  if (r.tmdb_id && r.tmdb_type) {
    links.push(`<a href="https://www.themoviedb.org/${r.tmdb_type}/${r.tmdb_id}" target="_blank" rel="noopener">TMDB ↗</a>`);
  }
  links.push(`<a href="https://www.justwatch.com/us/search?q=${q}" target="_blank" rel="noopener">Where to stream ↗</a>`);
  return links.join(" · ");
}

function recHTML(r) {
  const seen = r.status === "watched" && r.user_verdict;
  const stateText = seen
    ? `Seen · ${({ loved: "👍👍 Loved", liked: "👍 Liked", disliked: "👎 Nope" })[r.user_verdict] || r.user_verdict}`
    : (STATUS_LABEL[r.status] || r.status);
  const badge = `<span class="status-badge ${r.status}">${stateText}</span>`;
  const type = r.type ? `<span class="type-badge">${r.type === "show" ? "TV" : "movie"}</span>` : "";
  const genres = (r.genres || []).slice(0, 3).join(" · ");
  const poster = r.thumb
    ? `<img class="rec-poster" loading="lazy" src="${esc(r.thumb)}" alt="" onerror="this.style.display='none'" />`
    : `<div class="rec-poster placeholder">no poster</div>`;
  return `
  <div class="rec" data-id="${r.id}">
    ${poster}
    <div class="rec-main">
      <h3>${esc(r.title)} ${r.year ? `<span class="yr">(${r.year})</span>` : ""}${type}</h3>
      ${genres ? `<div class="rec-genres muted">${esc(genres)}</div>` : ""}
      ${r.reason ? `<div class="reason">${esc(r.reason)}</div>` : ""}
      ${r.where_to_watch ? `<div class="watch">▶ ${esc(r.where_to_watch)}</div>` : ""}
      <div class="rec-links">${recLinks(r)}</div>
      <div class="src">${esc(r.source || "")}</div>
    </div>
    <div class="rec-actions">
      ${badge}
      <div class="rec-verdicts">
        <button class="verdict-btn loved ${r.user_verdict === "loved" ? "active" : ""}" data-verdict="loved" title="Seen it — loved it">👍👍</button>
        <button class="verdict-btn liked ${r.user_verdict === "liked" ? "active" : ""}" data-verdict="liked" title="Seen it — liked it">👍</button>
        <button class="verdict-btn disliked ${r.user_verdict === "disliked" ? "active" : ""}" data-verdict="disliked" title="Seen it — nope">👎</button>
      </div>
      <button class="btn ${r.status === "queued" ? "primary" : ""}" data-action="queued">📌 Watch later</button>
      <button class="btn" data-action="dismissed">✕ Dismiss</button>
    </div>
  </div>`;
}

// "I've seen this" — rate it, which adds it to your library and shapes the profile.
async function recRate(id, verdict, el) {
  try {
    await api("/api/recommendations/" + id + "/rate", {
      method: "POST", body: JSON.stringify({ verdict }),
    });
    toast("Logged as seen — added to your library");
    if ($("#watchlist").classList.contains("active")) { loadWatchlist(); }
    else { fadeAndNote(el, verdict); }
    loadStatsProgress();
  } catch (e) {
    toast("Failed: " + e.message);
  }
}

async function recAction(id, status, el) {
  try {
    await api("/api/recommendations/" + id, {
      method: "POST", body: JSON.stringify({ status }),
    });
    toast(status === "queued" ? "Added to Want to Watch" : "Dismissed");
    if ($("#watchlist").classList.contains("active")) loadWatchlist();
    else {
      const badge = $(".status-badge", el);
      badge.className = "status-badge " + status;
      badge.textContent = STATUS_LABEL[status] || status;
      if (status === "dismissed") { el.style.opacity = 0.5; }
    }
  } catch (e) {
    toast("Update failed: " + e.message);
  }
}

function fadeAndNote(el, verdict) {
  const badge = $(".status-badge", el);
  badge.className = "status-badge watched";
  badge.textContent = "Seen · " + ({ loved: "👍👍 Loved", liked: "👍 Liked", disliked: "👎 Nope" })[verdict];
  $$(".verdict-btn", el).forEach((b) => b.classList.toggle("active", b.dataset.verdict === verdict));
  el.style.opacity = 0.6;
}

// ----------------------------------------------------------------- STATS tab
async function loadStats() {
  const el = $("#stats-content");
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const [s, profile] = await Promise.all([api("/api/stats"), api("/api/profile")]);
    el.innerHTML = statsHTML(s, profile);
  } catch (e) {
    el.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function bars(obj, max) {
  const m = max || Math.max(1, ...Object.values(obj));
  return Object.entries(obj)
    .map(([k, v]) => `
      <div class="bar-row">
        <span class="label">${esc(k)}</span>
        <span class="bar"><span style="width:${(100 * v) / m}%"></span></span>
        <span class="val">${v}</span>
      </div>`).join("") || '<div class="muted">—</div>';
}

function statsHTML(s, p) {
  const verdicts = {
    "👍👍 loved": s.verdicts.loved || 0,
    "👍 liked": s.verdicts.liked || 0,
    "👎 nope": s.verdicts.disliked || 0,
  };
  const recs = s.recommendations || {};
  return `
    <div class="stat-card">
      <h3>Library rated</h3>
      <div class="big-num">${s.pct_rated}%</div>
      <div class="muted">${s.rated} of ${s.total} titles (${s.unrated} to go)</div>
      <div style="margin-top:10px">${bars({ Movies: s.by_type.movie || 0, TV: s.by_type.show || 0 })}</div>
    </div>
    <div class="stat-card">
      <h3>Your verdicts</h3>
      ${bars(verdicts)}
    </div>
    <div class="stat-card">
      <h3>Genres you gravitate to</h3>
      <div class="muted">${(p.liked_genres || []).map(esc).join(" · ") || "rate more to populate"}</div>
      <h3 style="margin-top:16px">Genres you avoid</h3>
      <div class="muted">${(p.disliked_genres || []).map(esc).join(" · ") || "—"}</div>
    </div>
    <div class="stat-card">
      <h3>Loved directors</h3>
      <div class="muted">${(p.loved_directors || []).map(esc).join(" · ") || "—"}</div>
      <h3 style="margin-top:16px">Favorite eras</h3>
      <div class="muted">${(p.favorite_decades || []).map(esc).join(" · ") || "—"}</div>
    </div>
    <div class="stat-card">
      <h3>Recommendation funnel</h3>
      ${bars({ New: recs.new || 0, Queued: recs.queued || 0, Watched: recs.watched || 0, Dismissed: recs.dismissed || 0 })}
    </div>`;
}

// ----------------------------------------------------------------- CHAT tab
let chatHistory = []; // {role, content}

function renderChat() {
  const log = $("#chat-log");
  if (!chatHistory.length) {
    log.innerHTML = '<div class="chat-hint muted">Talk to Praxis — it knows your ratings. Try "what\'s a short, funny thing for tonight?" or "I\'m in the mood for a paranoid 70s thriller."</div>';
    return;
  }
  log.innerHTML = chatHistory
    .map((m) => `<div class="bubble ${m.role}">${esc(m.content)}</div>`)
    .join("");
  log.scrollTop = log.scrollHeight;
}

async function sendChat() {
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  chatHistory.push({ role: "user", content: text });
  input.value = "";
  input.style.height = "auto";
  renderChat();
  const log = $("#chat-log");
  const thinking = document.createElement("div");
  thinking.className = "bubble assistant thinking";
  thinking.textContent = "thinking…";
  log.appendChild(thinking);
  log.scrollTop = log.scrollHeight;
  $("#chat-send").disabled = true;
  try {
    const r = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ messages: chatHistory }),
    });
    chatHistory.push({ role: "assistant", content: r.reply });
    renderChat();
    // If the chat took actions (added/rated/queued titles), reflect it everywhere.
    if (r.actions && r.actions.length) {
      toast(r.actions.length + " change" + (r.actions.length > 1 ? "s" : "") + " applied");
      loadStatsProgress();
      loadMedia(); // refresh the Rate grid so added/rated titles show up
    }
  } catch (e) {
    thinking.remove();
    chatHistory.push({ role: "assistant", content: "⚠ " + e.message });
    renderChat();
  } finally {
    $("#chat-send").disabled = false;
  }
}

$("#chat-send").addEventListener("click", sendChat);
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$("#chat-input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
});
// Clear empties the TEXT BOX only (it does NOT wipe the conversation).
$("#chat-clear").addEventListener("click", () => {
  const input = $("#chat-input");
  input.value = "";
  input.style.height = "auto";
  input.focus();
});

// ----------------------------------------------------------------- mic diagnostics
const micLogLines = [];
function micLog(...args) {
  const msg = args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" ");
  const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
  micLogLines.push(line);
  if (micLogLines.length > 300) micLogLines.shift();
  const pre = document.getElementById("mic-log");
  if (pre) { pre.textContent = micLogLines.join("\n"); pre.scrollTop = pre.scrollHeight; }
  console.log("[mic]", ...args);
}

function explainGUMError(name) {
  return ({
    NotReadableError: "HARDWARE/OS LOCK — the mic is held by ANOTHER APP (Discord/Zoom/Teams/OBS/Windows Voice Access) or another browser tab. THIS is the 'already in use' case. Close other mic users (and extra tabs) and retry.",
    NotAllowedError: "PERMISSION DENIED — click the camera/mic icon in the address bar (or the lock icon → Site settings) and Allow microphone, then reload.",
    NotFoundError: "NO MICROPHONE found by the system. Check it's plugged in / enabled in Windows sound settings.",
    OverconstrainedError: "No device matched the requested audio constraints.",
    SecurityError: "Blocked by browser security — must be https or localhost (you should be on 127.0.0.1, which is fine).",
    AbortError: "The OS aborted mic startup (often another app grabbed it mid-start).",
    TypeError: "getUserMedia not available — likely an insecure context or an embedded webview (use real Chrome/Edge).",
  })[name] || ("Unrecognized getUserMedia error: " + name);
}

async function probeMic() {
  micLog("================ MIC PROBE ================");
  micLog("url:", location.href, "| secureContext:", window.isSecureContext);
  micLog("userAgent:", navigator.userAgent);
  micLog("SpeechRecognition available:", !!(window.SpeechRecognition || window.webkitSpeechRecognition));
  const hasGUM = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  micLog("getUserMedia available:", hasGUM);
  if (!hasGUM) { micLog("=> " + explainGUMError("TypeError")); micLog("================ END ================"); return; }

  try {
    if (navigator.permissions && navigator.permissions.query) {
      const p = await navigator.permissions.query({ name: "microphone" });
      micLog("permission state (microphone):", p.state);
    } else { micLog("permissions API: unavailable"); }
  } catch (e) { micLog("permissions.query failed:", e.name, e.message); }

  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const ins = devs.filter((d) => d.kind === "audioinput");
    micLog("audio input devices:", ins.length);
    ins.forEach((d, i) => micLog(`  [${i}] label="${d.label || "(hidden until permission granted)"}" id=${(d.deviceId || "").slice(0, 10)}`));
  } catch (e) { micLog("enumerateDevices failed:", e.name, e.message); }

  try {
    micLog("requesting getUserMedia({audio:true}) ...");
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const t = stream.getAudioTracks()[0];
    micLog("✅ getUserMedia OK — mic IS available to this page.");
    if (t) micLog("   track label:", t.label || "(unnamed)", "| readyState:", t.readyState);
    stream.getTracks().forEach((x) => x.stop());
    micLog("   released test stream.");
    micLog("If SpeechRecognition still fails after this succeeds, it's a Web Speech / network issue, not the mic hardware.");
  } catch (e) {
    micLog("❌ getUserMedia FAILED:", e.name, "-", (e.message || ""));
    micLog("=> " + explainGUMError(e.name));
  }
  micLog("================ END ================");
}

// Voice input (browser-native; degrades gracefully).
// Chrome's SpeechRecognition gets into stuck states if you reuse one instance,
// so we build a FRESH recognizer for every listening session and abort the old
// one first — a running instance can therefore never block a new start.
function attachMic(btn, input) {
  if (!btn || !input) return;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    btn.disabled = true;
    btn.title = "Voice input isn't supported in this browser (try Chrome or Edge)";
    return;
  }
  let rec = null;       // current recognizer instance (or null)
  let want = false;     // user wants the mic on
  let finalText = "";    // text in the box + everything finalized so far
  let retries = 0;

  function killRec() {
    if (rec) {
      rec.onend = rec.onresult = rec.onerror = rec.onstart = null;
      try { rec.abort(); } catch { /* ignore */ }
      rec = null;
    }
  }

  const tag = btn.id || "mic";

  function spinUp() {
    killRec();
    const r = new SR();
    r.lang = "en-US";
    r.interimResults = true;
    r.continuous = true;
    r.onstart = () => micLog(tag, "event: start (listening)");
    r.onaudiostart = () => micLog(tag, "event: audiostart (mic opened)");
    r.onspeechstart = () => micLog(tag, "event: speechstart");
    r.onspeechend = () => micLog(tag, "event: speechend");
    r.onaudioend = () => micLog(tag, "event: audioend");
    r.onnomatch = () => micLog(tag, "event: nomatch");
    r.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      input.value = (finalText + interim).replace(/\s+/g, " ").trimStart();
      input.dispatchEvent(new Event("input"));
    };
    r.onend = () => {
      micLog(tag, "event: end", want ? "(restarting — still wanted)" : "(stopped)");
      if (want) { setTimeout(() => { if (want) spinUp(); }, 250); } // keep listening
      else btn.classList.remove("listening");
    };
    r.onerror = (e) => {
      micLog(tag, "event: ERROR ->", e.error, e.message ? ("| " + e.message) : "");
      if (e.error === "no-speech" || e.error === "aborted") return; // benign; onend handles it
      want = false;
      btn.classList.remove("listening");
      const human = {
        "not-allowed": "Mic access is blocked. Click the address-bar mic/lock icon and allow it.",
        "service-not-allowed": "Mic access is blocked by the browser/OS settings.",
        "audio-capture": "No mic available, or another app/tab is using it. Running full probe…",
        "network": "Speech service network error — check your connection.",
      }[e.error] || ("Mic error: " + e.error);
      toast(human);
      micLog(tag, "auto-running probe to get the underlying reason…");
      probeMic(); // SpeechRecognition is vague; getUserMedia gives the real cause
    };
    rec = r;
    try {
      micLog(tag, "calling start()…");
      r.start();
      retries = 0;
    } catch (err) {
      // start() can throw if the previous engine hasn't fully released yet
      micLog(tag, "start() THREW:", (err && err.name) || "", "|", (err && err.message) || "");
      if (want && retries < 5) { retries++; micLog(tag, "retry", retries, "in 300ms"); setTimeout(spinUp, 300); }
      else {
        want = false; btn.classList.remove("listening");
        toast("Couldn't start mic: " + ((err && err.name) || err) + " — see diagnostics");
        probeMic();
      }
    }
  }

  // Stop when the user clicks anywhere that ISN'T the mic button or the text
  // field. Using a document click (not the field's blur) is what makes this
  // reliable: clicking the mic button is explicitly ignored here so the button's
  // own toggle handles it, instead of the old bug where pressing the mic blurred
  // the field and instantly killed the session.
  function onDocClick(e) {
    if (btn.contains(e.target) || input.contains(e.target) || e.target === input) return;
    stop();
  }

  function start() {
    if (want) return;
    micLog(tag, "▶ user pressed mic (start)");
    finalText = input.value ? input.value.trim() + " " : "";
    want = true;
    retries = 0;
    btn.classList.add("listening");
    input.focus();
    document.addEventListener("pointerdown", onDocClick, true);
    spinUp();
  }
  function stop() {
    if (!want) return;
    micLog(tag, "■ stopping");
    want = false;
    btn.classList.remove("listening");
    document.removeEventListener("pointerdown", onDocClick, true);
    killRec();
  }

  // Click the mic to start; click again (or click outside the box) to stop.
  btn.addEventListener("click", () => (want ? stop() : start()));
}

attachMic($("#mic-btn"), $("#chat-input"));
attachMic($("#add-mic"), $("#add-title"));

// diagnostics panel buttons
$("#mic-test-btn")?.addEventListener("click", () => { $("#mic-diag").open = true; probeMic(); });
$("#mic-clearlog-btn")?.addEventListener("click", () => {
  micLogLines.length = 0;
  $("#mic-log").textContent = "(cleared)";
});
$("#mic-copy-btn")?.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(micLogLines.join("\n")); toast("Log copied — paste it to me"); }
  catch { toast("Copy failed; select the text manually"); }
});

// ----------------------------------------------------------------- ADD / IMPORT tab
$("#add-submit").addEventListener("click", async () => {
  const title = $("#add-title").value.trim();
  const msg = $("#add-msg");
  if (!title) { msg.className = "msg error"; msg.textContent = "Title required."; return; }
  const body = {
    title,
    type: $("#add-type").value,
    year: $("#add-year").value ? +$("#add-year").value : null,
    verdict: $("#add-verdict").value || null,
    note: $("#add-note").value.trim() || null,
  };
  try {
    await api("/api/media/manual", { method: "POST", body: JSON.stringify(body) });
    msg.className = "msg ok";
    msg.textContent = `Added "${title}".`;
    ["add-title", "add-year", "add-note"].forEach((id) => ($("#" + id).value = ""));
    $("#add-verdict").value = "";
    loadStatsProgress();
  } catch (e) {
    msg.className = "msg error";
    msg.textContent = e.message;
  }
});

$("#netflix-submit").addEventListener("click", async () => {
  const fileInput = $("#netflix-file");
  const msg = $("#netflix-msg");
  if (!fileInput.files.length) { msg.className = "msg error"; msg.textContent = "Choose a CSV first."; return; }
  msg.className = "msg";
  msg.innerHTML = '<span class="spinner"></span> Importing…';
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  try {
    const res = await fetch("/api/import/netflix", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    msg.className = "msg ok";
    msg.textContent = `Imported ${data.added} new titles (${data.skipped} already known) from ${data.rows} rows.`;
    loadStatsProgress();
  } catch (e) {
    msg.className = "msg error";
    msg.textContent = e.message;
  }
});

// ----------------------------------------------------------------- enrichment (TMDB)
let enrichTotal = 0;

async function loadEnrichStatus() {
  try {
    const s = await api("/api/enrich/status");
    enrichTotal = enrichTotal || s.remaining;
    const status = $("#enrich-status");
    if (!s.tmdb_configured) {
      status.innerHTML = "⚠ No TMDB credentials yet — add <code>tmdb.read_access_token</code> to config.json.";
      $("#enrich-btn").disabled = true;
    } else {
      $("#enrich-btn").disabled = s.remaining === 0;
      status.textContent = s.remaining === 0
        ? "All titles enriched. ✓"
        : `${s.remaining} titles need metadata.`;
    }
    const done = enrichTotal - s.remaining;
    $("#enrich-bar").style.width = enrichTotal ? (100 * done / enrichTotal) + "%" : "0%";
  } catch (e) {
    $("#enrich-status").textContent = e.message;
  }
}

$("#enrich-btn").addEventListener("click", async () => {
  const btn = $("#enrich-btn");
  const msg = $("#enrich-msg");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Enriching…';
  let totalEnriched = 0, totalNoMatch = 0;
  // establish the baseline total for the progress bar
  try {
    const s0 = await api("/api/enrich/status");
    enrichTotal = s0.remaining;
  } catch { /* ignore */ }
  try {
    while (true) {
      const r = await api("/api/enrich", { method: "POST", body: JSON.stringify({ limit: 40 }) });
      totalEnriched += r.enriched;
      totalNoMatch += r.no_match;
      const done = enrichTotal - r.remaining;
      $("#enrich-bar").style.width = enrichTotal ? (100 * done / enrichTotal) + "%" : "100%";
      $("#enrich-status").textContent = `${r.remaining} remaining…`;
      msg.className = "msg";
      msg.textContent = `Enriched ${totalEnriched}, ${totalNoMatch} without a match…`;
      if (r.remaining === 0 || r.processed === 0) break;
    }
    msg.className = "msg ok";
    msg.textContent = `Done. Enriched ${totalEnriched} titles (${totalNoMatch} had no TMDB match).`;
    loadMedia();
  } catch (e) {
    msg.className = "msg error";
    msg.textContent = e.message;
  } finally {
    btn.innerHTML = "Enrich now";
    loadEnrichStatus();
  }
});

// ----------------------------------------------------------------- init
checkHealth();
loadMedia();
