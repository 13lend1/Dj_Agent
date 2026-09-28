"use strict";

const $ = (id) => document.getElementById(id);

const CONTROL_URL = "/api/control";
const STATUS_URL = `${CONTROL_URL}/status`;
const PLACES_URL = `${CONTROL_URL}/places`;
const PLACE_URL = `${CONTROL_URL}/place`;

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  await checkOk(res);
  return res.json();
}

async function del(url) {
  const res = await fetch(url, { method: "DELETE" });
  await checkOk(res);
  return res.json();
}

// Throw with the server's human-readable `detail` when there is one, so a
// rejected request (e.g. "that place name is already taken") tells the user
// why instead of a bare "400 Bad Request".
async function checkOk(res) {
  if (res.ok) return;
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") message = body.detail;
  } catch (_) {
    /* non-JSON error body; keep the status line */
  }
  throw new Error(message);
}

const audio = $("audio");
let audioOn = false;
// The deck is paused (either the user clicked pause or a poll reported the
// server paused). While set, NOTHING may play or reconnect the <audio> element
// — a paused DJ must be silent, even if buffered audio or watchdog events fire.
let audioPaused = false;

const COVER_URL = `${CONTROL_URL}/cover`;
let lastSongKey = "";
let coverRequest = 0;
let lastCoverUrl = "";

const STREAM_URL = "/api/control/stream";

let audioBuffering = false;
let lastReconnectAt = 0;
let stallTimer = null;
// Timestamp of the last explicit resume: the poll right after a resume can
// still catch the server mid-transition (paused), so the defensive pause
// below must not re-freeze an element that was just told to play.
let lastResumeAt = 0;
// Previous polled pause state: lets the status poll detect "deck was paused,
// now it isn't" so a hotkey pause/resume in the background can unfreeze the
// audio element even though no click ever reached the page.
let prevPolledPaused = false;

function startAudio() {
  if (audioOn) return;
  audioPaused = false;
  audio.src = `${STREAM_URL}?_=${Date.now()}`;
  audio
    .play()
    .then(() => { audioOn = true; $("sound-cta").hidden = true; })
    .catch((e) => flash(`audio blocked: ${e.message}`));
}

function reconnectAudio(opts) {
  if (!audioOn || audioPaused) return;
  const now = Date.now();
  // `force` (deliberate user moves: skip / previous) snaps the browser to the
  // live edge EVEN if a background resync happened a moment ago — the 1.2s
  // throttle only guards the watchdog, never a real button press.
  const force = !!(opts && opts.force);
  if (!force && now - lastReconnectAt < 1200) return;
  lastReconnectAt = now;
  // Force a brand-new connection: reusing the same URL lets the browser keep
  // playing (and re-buffer) audio the DJ has already moved past, which is what
  // made skips/seeks feel seconds behind. The cache-buster guarantees a fresh
  // stream subscribed at the live edge (and pre-seeded, so it starts at once).
  // When `fresh` is set (skip / previous / full), the subscription skips the
  // pre-seed too: replaying the outgoing song's last ~0.75s read as "the next
  // song is buffering", so these cuts land cleanly on the new song instead.
  audioBuffering = true;
  const freshQ = (opts && opts.fresh) ? "&fresh=1" : "";
  audio.src = `${STREAM_URL}?_=${now}${freshQ}`;
  audio.load();
  audio.play().catch(() => {});
}

function resyncSoon() {
  // A stalled element (waiting/stalled fired but the DJ says it's still
  // playing) reconnects after a short debounce. This is the self-heal for the
  // moments nothing is being written to the sink (e.g. a slow download between
  // two songs) — show BUFFERING briefly, then snap back to the live edge.
  clearTimeout(stallTimer);
  stallTimer = setTimeout(() => { if (audioOn && !audioPaused && audioBuffering) reconnectAudio(); }, 1500);
}

function bindAudioWatchdog() {
  // A live HTTP stream has no standard "buffering" state we can trust, so the
  // element's own events drive it: the moment it reports it can't advance
  // while the DJ is still playing, snap to the live edge instead of leaving
  // the page silently stuck on a buffer.
  audio.addEventListener("waiting", () => { if (!audioPaused) { audioBuffering = true; resyncSoon(); } });
  audio.addEventListener("stalled", () => { if (!audioPaused) { audioBuffering = true; resyncSoon(); } });
  audio.addEventListener("playing", () => { audioBuffering = false; clearTimeout(stallTimer); });
  audio.addEventListener("canplay", () => { audioBuffering = false; clearTimeout(stallTimer); });
  audio.addEventListener("error", () => {
    if (audioOn && !audioPaused) setTimeout(reconnectAudio, 400);
  });
  audio.addEventListener("ended", () => {
    // Server closed the stream (sink shut down) or the connection died.
    if (audioOn && !audioPaused) reconnectAudio();
  });
}

function fmt(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "--:--";
  sec = Math.max(0, sec);
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

function setCoverImage(url) {
  const img = $("cover-art");
  const fb = $("art-fallback");
  if (!url) {
    img.hidden = true;
    fb.hidden = false;
    return;
  }
  const req = ++coverRequest;
  const loader = new Image();
  loader.onload = () => {
    if (req !== coverRequest) return;
    img.src = url;
    img.hidden = false;
    fb.hidden = true;
    img.classList.add("ready");
  };
  loader.onerror = () => {
    if (req !== coverRequest) return;
    img.hidden = true;
    fb.hidden = false;
  };
  loader.src = url;
}

async function refreshCover(song) {
  if (!song || !song.title) {
    setCoverImage(null);
    return;
  }
  const key = `${song.artist || ""}|${song.title}`;
  if (key === lastSongKey) return;
  lastSongKey = key;
  lastCoverUrl = "";
  $("cover-art").classList.remove("ready");
  try {
    const qs = new URLSearchParams({
      artist: song.artist || "",
      title: song.title || "",
    });
    const d = await getJSON(`${COVER_URL}?${qs.toString()}`);
    lastCoverUrl = d.url || "";
    setCoverImage(lastCoverUrl);
  } catch (e) {
    console.warn("cover lookup failed:", e);
    setCoverImage(null);
  }
}

function setDeviceStatus(playing) {
  document.body.classList.toggle("playing", !!playing);
}

function setPauseButton(paused) {
  const btn = $("pause-btn");
  if (!btn) return;
  btn.classList.toggle("paused", !!paused);
  btn.innerHTML = paused ? "&#9654;" : "&#9208;";
  btn.title = paused ? "Resume" : "Pause";
}

// ---------------------------------------------------------------------------
// Progress bar
//
// The server can only tell us where the deck is once a second, which makes the
// bar look frozen or jumpy. Instead we remember the last reported position and
// let the bar advance locally between polls, then re-anchor on every poll.
// ---------------------------------------------------------------------------
const progress = {
  songKey: "",
  elapsed: 0,
  durSec: null,
  paused: true,
  active: false,
  at: performance.now(),
  like: null,
};

function paintProgress() {
  const p = progress;
  let shown = p.elapsed;
  if (p.active && !p.paused) shown += (performance.now() - p.at) / 1000;
  if (p.durSec && p.durSec > 0) shown = Math.min(shown, p.durSec);
  shown = Math.max(0, shown);

  const pct = (p.durSec && p.durSec > 0) ? (shown / p.durSec) * 100 : 0;
  const width = Number.isFinite(pct) ? Math.min(100, Math.max(0, pct)) : 0;
  const bar = $("progress-bar");
  if (bar) bar.style.width = width.toFixed(1) + "%";

  const time = $("np-time");
  if (time) {
    const txt = `${fmt(shown)} / ${p.durSec ? fmt(p.durSec) : "--:--"}` +
      (p.like != null ? `  \u00b7  predicted ${p.like.toFixed(2)}` : "");
    if (time.textContent !== txt) time.textContent = txt;
  }
}

function updateProgress(song, playing, paused) {
  if (!song) {
    progress.songKey = "";
    progress.elapsed = 0;
    progress.durSec = null;
    progress.paused = true;
    progress.active = false;
    progress.like = null;
    paintProgress();
    return;
  }

  const start = song.play_start_sec;
  const end = song.play_end_sec;
  const hookDur = (start != null && end != null && end > start) ? end - start : null;
  const durSec = hookDur || (song.duration_ms ? song.duration_ms / 1000 : null);

  progress.songKey = `${song.artist || ""}|${song.title || ""}|${song.genre || ""}`;
  const serverElapsed = Number(song.elapsed_sec);
  progress.elapsed = Number.isFinite(serverElapsed) ? serverElapsed : 0;
  progress.durSec = durSec || null;
  progress.like = song.likeability != null ? song.likeability : null;
  progress.paused = paused || !playing;
  progress.active = !!playing;
  progress.at = performance.now();
  paintProgress();
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------
let statusInFlight = false;
let statusQueued = false;
let pausePending = 0;
let lastQueueSig = "";
let lastStatus = null;

function setLoading(s) {
  // The deck is up but has nothing to play yet (freshly chosen place, empty
  // pool): discovery is fetching + preprocessing the first tracks and the
  // server reports how many are ready so far. Drive the "Getting songs ready"
  // overlay (spinner + progress bar) instead of a blank STANDBY screen.
  // NOTE: every element here is null-guarded — a missing element used to throw
  // and silently froze the whole status render (bar never filled).
  const overlay = $("loading");
  if (overlay) overlay.hidden = !(s.dj_running && s.preparing && !s.stopping);
  if (!overlay || overlay.hidden) return;

  const bar = $("loading-bar");
  const fill = $("loading-fill");
  if (!bar || !fill) return;

  const n = typeof s.ready_count === "number" ? s.ready_count : null;
  const target = typeof s.ready_target === "number" && s.ready_target > 0 ? s.ready_target : 0;
  bar.classList.toggle("indeterminate", !target || !!s.trial);
  if (s.trial) {
    // Cold-place trial fast-start: songs are being fetched and the first will
    // play the moment it is downloaded. No point counting past the target here
    // (the background fetch keeps climbing) — just show an indeterminate
    // "starting playback" state.
    fill.style.width = "0%";
    $("loading-sub").textContent = "starting playback\u2026";
    return;
  }
  if (target && n != null) {
    fill.style.width = Math.min(100, Math.round((n / target) * 100)) + "%";
    $("loading-sub").textContent = n >= target
      ? `${n} tracks found \u00b7 mixing your first set\u2026`
      : `${n} / ${target} tracks found`;
  } else if (n != null) {
    fill.style.width = "0%";
    $("loading-sub").textContent = `${n} tracks found \u00b7 searching\u2026`;
  } else {
    fill.style.width = "0%";
    $("loading-sub").textContent = "searching for tracks\u2026";
  }
}

// The now-playing block (title / artist / meta / rating / cover / progress bar)
// is shared by the status poll and the SKIP response: when a skip POST returns
// after waiting for the deck to swap, it already carries the NEW song, so the
// UI switches the same instant the audio does instead of one poll behind.
function renderSongNow(song, playing, paused) {
  if (song) {
    $("np-title").textContent = song.title || "\u2014";
    $("np-artist").textContent = song.artist || "\u2014";
    let meta = [song.genre, song.place].filter(Boolean).join(" \u00b7 ");
    if (song.play_start_sec != null && song.play_end_sec != null) {
      meta += `${meta ? " \u00b7 " : ""}hook ${fmt(song.play_start_sec)}-${fmt(song.play_end_sec)}`;
    }
    $("np-meta").textContent = meta;

    $("rate-like").className = "rate up" + (song.rating === 0 ? " active" : "");
    $("rate-dislike").className = "rate down" + (song.rating === 1 ? " active" : "");
    refreshCover(song);
  } else {
    setCoverImage(null);
  }

  updateProgress(song, !!playing, !!paused);
}

function renderStatus(s) {
  lastStatus = s;
  if (!s.dj_running) {
    djAvailable = s.dj_available !== false;
    // The deck is stopped (or waiting for a place): drop the audio and show the
    // gate. Changing place is Stop -> pick -> Start, so this is the only screen
    // that can retarget the DJ.
    if (audioOn) { audio.pause(); audioOn = false; }
    audioPaused = false;
    prevPolledPaused = false;
    setDeviceStatus(false);
    setPauseButton(false);
    $("state-label2").textContent = "STANDBY";
    $("state-dot").className = "dot off";
    $("state-label").textContent = "choose a place";
    $("np-title").textContent = "\u2014";
    $("np-artist").textContent = "\u2014";
    $("np-meta").textContent = "";
    $("queue-list").innerHTML = "";
    $("queue-count").textContent = "";
    lastQueueSig = "";
    lastSongKey = "";
    setCoverImage(null);
    updateProgress(null, false, true);
    setLoading({ dj_running: false, preparing: false, stopping: false });
    openGate();
    return;
  }

  djAvailable = true;
  closeGate();
  const paused = !!s.paused;
  const transitioning = !!s.transitioning;
  // Defense-in-depth for the pause leak: even if a pause click never reached
  // us (slow POST, another tab, a poll that caught the transition), the moment
  // a poll says the deck is paused the element must freeze NOW. Buffered audio
  // must never keep pouring out of a paused deck.
  if (paused && audioOn && !audio.paused && Date.now() - lastResumeAt > 1500) {
    audioPaused = true;
    audio.pause();
  }
  // The reverse direction: a resume fired by the GLOBAL hotkey (or another tab)
  // never reaches this page's keydown handler, so the poll is the only signal.
  // If the deck just unpaused and the element is still frozen from the pause
  // above, snap it back to the live edge — otherwise the song plays server-side
  // while this tab stays mute forever.
  if (!paused && prevPolledPaused && audioPaused && audioOn) {
    audioPaused = false;
    lastResumeAt = Date.now();
    reconnectAudio({ fresh: true });
    setDeviceStatus(s.playing);
  }
  prevPolledPaused = !!paused;
  if (!pausePending) {
    setDeviceStatus(s.playing && !paused);
    setPauseButton(paused);
  }
  $("place-badge").textContent = `place ${s.place || "any"}`;
  placesData.active = s.place || placesData.active;
  renderPlaceCard();
  $("state-dot").className = "dot " + (paused ? "paused" : s.playing ? "on" : "off");
  $("state-label").textContent = paused ? "paused" : s.playing ? "playing" : s.stopping ? "stopping" : s.preparing ? "getting songs ready" : "waiting";
  $("state-label2").textContent = paused ? "PAUSED"
    : transitioning ? "MIXING"
      : s.playing ? "PLAYING" : s.stopping ? "STOPPING" : s.preparing ? "LOADING" : "STANDBY";
  // The element is gated on a live stream: while it reports buffering the DJ
  // is still really playing, so surface it instead of a PLAYLESS PLAYING.
  if (audioOn && audioBuffering && s.playing && !paused) {
    $("state-dot").className = "dot paused";
    $("state-label").textContent = "buffering\u2026";
    $("state-label2").textContent = "BUFFERING";
  }
  $("sound-cta").hidden = !(s.playing && !audioOn);

  const song = s.song;
  renderSongNow(song, s.playing, paused || pausePending > 0);

  // After renderSongNow (which resets the bar for a null song), let the
  // preparing state take over the same bar with the load-progress fill.
  setLoading(s);

  const q = s.queue || [];
  $("queue-count").textContent = (q.length || s.resume_set)
    ? `(${(s.resume_set || 0) + q.length})` : "";
  const sig = q.map((i) => `${i.title}|${i.artist || ""}`).join("\n");
  if (sig !== lastQueueSig) {
    lastQueueSig = sig;
    $("queue-list").innerHTML = q.map((item) =>
      `<li>${item.title} <span class="q-artist">${item.artist || ""}</span></li>`
    ).join("");
  }
}

async function refreshStatus() {
  if (statusInFlight) {
    // Never drop a request: remember that one is wanted and run it as soon as
    // the in-flight response lands. Dropping it is what left the UI showing the
    // old song for a whole poll after a click.
    statusQueued = true;
    return;
  }
  statusInFlight = true;
  try {
    renderStatus(await getJSON(STATUS_URL));
  } catch (e) {
    $("state-label").textContent = "offline";
    console.warn("status poll failed:", e);
  } finally {
    statusInFlight = false;
    if (statusQueued) {
      statusQueued = false;
      setTimeout(refreshStatus, 0);
    }
  }
}

// The backend applies a control a beat after the POST returns (the audio loop
// picks up the event, prepares the next track, then swaps current_song). Poll a
// few times over the next second so the UI catches the new state quickly
// instead of waiting for the next tick.
function refreshStatusSoon() {
  refreshStatus();
  for (const delay of [200, 500, 1000]) {
    setTimeout(refreshStatus, delay);
  }
}

// ---------------------------------------------------------------------------
// Places + startup gate
//
// The deck does not start until a place is chosen, and changing place is
// Stop -> pick -> Start. So the gate is simply shown whenever the DJ is not
// running; it doubles as the first-run chooser and the "change place" screen.
// ---------------------------------------------------------------------------
let placesData = { active: null, places: [] };
let placesInFlight = false;
let selectedGenres = [];   // genres picked in the create-place form
let djAvailable = true;

function labelForPlace(key) {
  if (!key) return "Any place (all genres)";
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function genresForPlace(key) {
  const p = (placesData.places || []).find((x) => x.key === key);
  return p ? p.genres || [] : [];
}

// The genre vocabulary is exactly what preference.PLACE_GENRES uses: the union
// of every place's genres, so a new place can only be built from genres that
// already exist in PLACE_GENRES.
function genreCatalog() {
  const set = new Set();
  for (const p of placesData.places || []) {
    for (const g of p.genres || []) set.add(g);
  }
  return [...set].sort((a, b) => a.localeCompare(b));
}

function buildGatePlaces() {
  const wrap = $("gate-places");
  if (!wrap) return;
  const places = placesData.places || [];
  // Keys and genre names come from the places registry and user input, so they
  // are escaped before being interpolated into innerHTML.
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  // The start control is a <div role="button"> rather than a <button> so the
  // delete button can sit inside the same tile — nesting a <button> in a
  // <button> is invalid HTML and the browser hoists it out, breaking the grid.
  wrap.innerHTML = places.map((p) =>
    `<div class="gate-tile${p.custom ? " gp-custom" : ""}">` +
    `<div class="gate-place" role="button" tabindex="0"` +
    ` data-place="${esc(p.key)}">` +
    `<span class="gp-name">${esc(labelForPlace(p.key))}</span>` +
    `<span class="gp-genres">${esc((p.genres || []).join(", "))}</span></div>` +
    (p.custom
      ? `<button type="button" class="gp-delete" data-place="${esc(p.key)}"` +
        ` aria-label="Delete ${esc(labelForPlace(p.key))}"` +
        ` title="Delete this place and all its songs">&times;</button>`
      : "") +
    `</div>`
  ).join("") || `<p class="place-hint">No places available.</p>`;
  setGateBusy(false);
}

function buildGenreChips() {
  const avail = $("gate-genres");
  const sel = $("gate-selected");
  if (!avail || !sel) return;
  avail.innerHTML = genreCatalog()
    .filter((g) => !selectedGenres.includes(g))
    .map((g) => `<button type="button" class="chip" data-genre="${g}">${g}</button>`)
    .join("");
  sel.innerHTML = selectedGenres
    .map((g) => `<button type="button" class="chip" data-genre="${g}">${g}</button>`)
    .join("");
}

function toggleGenre(genre) {
  const i = selectedGenres.indexOf(genre);
  if (i === -1) selectedGenres.push(genre);
  else selectedGenres.splice(i, 1);
  buildGenreChips();
}

function openGate() {
  const gate = $("place-gate");
  if (!gate || !gate.hidden) return;
  gate.hidden = false;
  document.body.classList.add("gated");
  if (!djAvailable) {
    $("gate-msg").textContent = "DJ playback is unavailable (server started with --no-dj).";
  }
  buildGatePlaces();
  buildGenreChips();
}

function closeGate() {
  const gate = $("place-gate");
  if (!gate || gate.hidden) return;
  gate.hidden = true;
  document.body.classList.remove("gated");
}

function setGateBusy(busy) {
  const gate = $("place-gate");
  if (!gate) return;
  for (const el of gate.querySelectorAll("button")) {
    el.disabled = busy || !djAvailable;
  }
}

function renderPlaceCard() {
  const key = placesData.active;
  $("place-current-name").textContent = key ? labelForPlace(key) : "\u2014";
  $("place-current-genres").textContent = key ? genresForPlace(key).join(", ") : "";
}

async function refreshPlaces() {
  if (placesInFlight) return;
  placesInFlight = true;
  try {
    const data = await getJSON(PLACES_URL);
    placesData = { active: data.active, places: data.places || [] };
    buildGatePlaces();
    buildGenreChips();
    renderPlaceCard();
  } catch (e) {
    console.warn("places fetch failed:", e);
  } finally {
    placesInFlight = false;
  }
}

async function gateStart(key) {
  $("gate-msg").textContent = `starting ${labelForPlace(key)}...`;
  setGateBusy(true);
  try {
    const res = await post(PLACE_URL, { place: key });
    placesData.active = res.place;
    selectedGenres = [];
    $("gate-name").value = "";
    flash(`\u25B6 ${labelForPlace(res.place)}`);
    closeGate();
    renderPlaceCard();
    refreshStatusSoon();
  } catch (e) {
    $("gate-msg").textContent = `could not start: ${e.message}`;
    await refreshPlaces();
  } finally {
    setGateBusy(false);
  }
}

async function gateDelete(key) {
  // This is destructive and irreversible: the place, its songs, its pool and
  // its trained model all go. Confirm before sending it.
  const name = labelForPlace(key);
  if (!window.confirm(
    `Delete "${name}"?\n\nThis also permanently deletes every song in it ` +
    `and its trained model. This cannot be undone.`
  )) {
    return;
  }
  $("gate-msg").textContent = `deleting ${name}...`;
  setGateBusy(true);
  try {
    const res = await del(`${PLACE_URL}?place=${encodeURIComponent(key)}`);
    const n = (res.songs || 0) + (res.preprocessed || 0);
    flash(`\u{1F5D1} deleted ${name}`);
    $("gate-msg").textContent =
      `deleted ${name} \u2014 ${res.songs || 0} song(s), ` +
      `${res.preprocessed || 0} pool row(s)` +
      (res.model ? ", model removed" : "");
    if (placesData.active === key) placesData.active = null;
    await refreshPlaces();
  } catch (e) {
    $("gate-msg").textContent = `delete failed: ${e.message}`;
  } finally {
    setGateBusy(false);
  }
}

// Mirror of Music/preference.py `normalize_place`: lowercased, non-alphanumerics
// collapsed to '_' then trimmed — 'Gym', 'GYM' and 'gym!' all collapse to 'gym',
// so each is challenged against the same key. The server re-validates anyway;
// this only avoids a round-trip for obvious duplicates.
function placeKey(name) {
  return (name || "").trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

async function gateCreate() {
  const name = $("gate-name").value.trim();
  if (!name) { $("gate-msg").textContent = "give the place a name"; return; }
  const key = placeKey(name);
  const clash = (placesData.places || []).find((p) => p.key === key);
  if (clash) {
    $("gate-msg").textContent = clash.custom
      ? `a place named '${key}' already exists - pick another name, or delete it first to change its genres`
      : `'${key}' is a built-in place - pick another name`;
    return;
  }
  if (!selectedGenres.length) { $("gate-msg").textContent = "pick at least one genre"; return; }
  $("gate-msg").textContent = `creating '${key}'...`;
  setGateBusy(true);
  try {
    const res = await post(PLACE_URL, { place: name, genres: selectedGenres });
    placesData.active = res.place;
    $("gate-name").value = "";
    selectedGenres = [];
    flash(`created ${labelForPlace(res.place)}`);
    await refreshPlaces();
    closeGate();
    renderPlaceCard();
    refreshStatusSoon();
  } catch (e) {
    $("gate-msg").textContent = `create failed: ${e.message}`;
  } finally {
    setGateBusy(false);
  }
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
// Actions that move where the DJ is playing: the <audio> element buffers
// ahead of the live edge, so without reconnecting the browser would keep
// playing the song we just skipped away from. Reconnect snaps it to "now".
// skip / full / previous also reconnect FRESH (no tail seed) so the next
// song starts cleanly instead of replaying the outgoing song's last moments.
const RECONNECT = new Set(["skip", "previous", "full", "seek-forward", "seek-backward"]);
const FRESH = new Set(["skip", "previous", "full"]);
// User moves that change which song plays: their audio must cut the instant
// the button is pressed — the browser sits a couple of seconds behind the live
// edge, so without an immediate forced reconnect you'd keep hearing the buffered
// outgoing song. Skips snap the browser to "now" at the click, then re-anchor
// once the server confirms the new song is actually playing.
const INSTANT = new Set(["skip", "previous"]);

async function act(action) {
  if (INSTANT.has(action)) {
    // Cut the outgoing song at the moment of the press, before the POST round
    // trip reaches the server: drop the ~3s of buffered audio and land on the
    // live edge. When the cut lands a moment later, the SAME stream carries the
    // transition + next song straight into the element.
    reconnectAudio({ fresh: true, force: true });
  }
  if (action === "pause") {
    const willPause = !$("pause-btn").classList.contains("paused");
    pausePending++;
    setPauseButton(willPause);
    setDeviceStatus(audioOn && !willPause);
    if (willPause) {
      // Pause the element IMMEDIATELY, before the POST round-trip: buffered
      // audio must not keep playing for the ~100ms+ it takes the backend to
      // confirm. This is the "pause is on but sound still passes" leak.
      audioPaused = true;
      if (audioOn) audio.pause();
    }
  }
  flash(`\u2192 ${action}...`);
  try {
    const res = await post(`${CONTROL_URL}/${action}`);
    if (action === "pause") {
      if (res.paused) {
        if (audioOn && !audio.paused) audio.pause();
        audioPaused = true;
        flash("\u23F8 paused");
      } else {
        // Resume: forget the paused state and snap the element back live.
        audioPaused = false;
        lastResumeAt = Date.now();
        reconnectAudio({ fresh: true });
        flash("\u25B6 resumed");
      }
    } else if (action === "stop") {
      if (audioOn) { audio.pause(); audioOn = false; }
      audioPaused = false;
      flash("\u25A0 stopped \u2014 pick a place to start again");
    } else if (action === "previous" && res.replayed === false) {
      flash("no earlier songs yet");
    } else {
      flash(`\u2713 ${res.action || action} sent`);
      if (action === "skip" && res && res.song && lastStatus && lastStatus.dj_running) {
        // The skip POST waited until the deck actually swapped, so res.song is
        // the new song measured at the same instant the audio cut. Render it
        // NOW — otherwise the new song's audio leads the UI/bar by ~1s.
        renderSongNow(res.song, true, lastStatus.paused || pausePending > 0);
      }
    }
    if (RECONNECT.has(action)) {
      // Skip / previous re-anchor FORCED so the throttle can never swallow the
      // user's move: the browser locks onto the new song's live edge.
      reconnectAudio({ fresh: FRESH.has(action), force: INSTANT.has(action) });
    }
    refreshStatusSoon();
  } catch (e) {
    flash(`control failed: ${e.message}`);
    refreshStatus();
  } finally {
    if (action === "pause") pausePending = Math.max(0, pausePending - 1);
  }
}

async function rate(rating) {
  flash(rating === 0 ? "\u2192 like..." : "\u2192 dislike...");
  // Paint the pressed state immediately; the poll corrects it if the rate failed.
  $("rate-like").classList.toggle("active", rating === 0);
  $("rate-dislike").classList.toggle("active", rating === 1);
  try {
    await post(`${CONTROL_URL}/rate`, { rating });
    flash(`\u2713 rated ${rating === 0 ? "like" : "dislike"}`);
    refreshStatusSoon();
  } catch (e) {
    flash(`rate failed: ${e.message}`);
    refreshStatus();
  }
}

function flash(msg) {
  $("control-msg").textContent = msg;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => ($("control-msg").textContent = ""), 3000);
}

function wire() {
  document.addEventListener("click", (e) => {
    const actionBtn = e.target.closest("[data-action]");
    if (actionBtn) { act(actionBtn.dataset.action); return; }
    if (e.target.id === "rate-like") { rate(0); return; }
    if (e.target.id === "rate-dislike") { rate(1); return; }
    if (e.target.id === "gate-create-btn") { gateCreate(); return; }
    // Delete is checked before start: it is a sibling of .gate-place, not a
    // child, so closest() would not shadow it — but keeping it first makes the
    // precedence obvious if the markup is ever changed.
    const delBtn = e.target.closest(".gp-delete");
    if (delBtn) { gateDelete(delBtn.dataset.place); return; }
    const placeBtn = e.target.closest(".gate-place");
    if (placeBtn) { gateStart(placeBtn.dataset.place); return; }
    const chip = e.target.closest(".chip");
    if (chip) { toggleGenre(chip.dataset.genre); return; }
    if (e.target.id === "sound-cta") { startAudio(); return; }
  });

  document.addEventListener("keydown", (e) => {
    // Enter/Space on the focused place tile starts it, so the tile is usable
    // from the keyboard now that it is a role="button" div.
    if ((e.key === "Enter" || e.key === " ") && e.target.classList
        && e.target.classList.contains("gate-place")) {
      e.preventDefault();
      gateStart(e.target.dataset.place);
      return;
    }
    // Ctrl+Alt shortcuts: N = skip, L = like, F = play this song in full,
    // Space = pause/resume.
    // NOTE: a background tab can't receive keys, so this only fires when the
    // page is focused. Run hotkeys.py to cover the background too — the
    // server debounces the overlap so a single press never acts twice.
    if (!(e.ctrlKey && e.altKey)) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;
    const key = (e.key || "").toLowerCase();
    e.preventDefault();
    if (key === "n") act("skip");
    else if (key === "l") rate(0);
    else if (key === "f") act("full");
    else if (key === " " || key === "spacebar") act("pause");
  });
}

wire();
bindAudioWatchdog();
refreshStatus();
refreshPlaces();
setInterval(refreshStatus, 500);
setInterval(paintProgress, 100);
setInterval(() => {
  // Final backstop: if the DJ status says "playing" but the <audio> element
  // has silently stopped advancing, snap it back to the live edge. Catches the
  // cases that fire no event (backgrounded tabs, wedged connections).
  // A genuinely stalled element (readyState stuck below HAVE_FUTURE_DATA while
  // still marked as buffering) counts the same way, even if the element didn't
  // pause itself — no amount of waiting fixes "no data flowing".
  if (!audioOn || audioPaused || !lastStatus || !lastStatus.playing || lastStatus.paused) return;
  const stuck = audio.paused
    ? (audio.readyState <= 2 && audio.currentTime > 0)
    : (audioBuffering && audio.readyState <= 2);
  if (stuck) reconnectAudio();
}, 2000);
