"use strict";

const $ = (id) => document.getElementById(id);

const CONTROL_URL = "/api/control";
const STATUS_URL = `${CONTROL_URL}/status`;
const MODELS_URL = "/api/models";

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
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

const audio = $("audio");
let audioOn = false;

function startAudio() {
  if (audioOn) return;
  audio.src = "/api/control/stream";
  audio
    .play()
    .then(() => { audioOn = true; $("sound-cta").hidden = true; })
    .catch((e) => flash(`audio blocked: ${e.message}`));
}

function reconnectAudio() {
  if (!audioOn) return;
  const src = audio.src;
  audio.src = "";
  audio.load();
  audio.src = src;
  audio.play().catch(() => {});
}

function fmt(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "--:--";
  sec = Math.max(0, sec);
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

function renderStatus(s) {
  if (!s.dj_running) {
    $("state-dot").className = "dot off";
    $("state-label").textContent = "DJ not running";
    $("np-title").textContent = "—";
    $("np-artist").textContent = "—";
    $("np-meta").textContent = "";
    $("progress-bar").style.width = "0%";
    $("np-time").textContent = "";
    $("queue-list").innerHTML = "";
    return;
  }

  $("place-badge").textContent = `place ${s.place || "any"}`;
  $("state-dot").className = "dot " + (s.playing ? "on" : "off");
  $("state-label").textContent = s.playing ? "playing" : s.stopping ? "stopping" : "waiting";
  $("sound-cta").hidden = s.playing && !audioOn ? false : true;

  const song = s.song;
  if (song) {
    $("np-title").textContent = song.title || "—";
    $("np-artist").textContent = song.artist || "—";
    const meta = [song.genre, song.place].filter(Boolean).join(" \u00b7 ");
    $("np-meta").textContent = meta ? meta : "";
    $("np-meta").textContent += song.play_start_sec != null && song.play_end_sec != null
      ? ` \u00b7 hook ${fmt(song.play_start_sec)}-${fmt(song.play_end_sec)}`
      : "";

    const hookDur = (song.play_end_sec != null && song.play_start_sec != null)
      ? song.play_end_sec - song.play_start_sec
      : null;
    const durSec = hookDur || (song.duration_ms ? song.duration_ms / 1000 : null);
    let pct = 0;
    if (durSec) pct = (song.elapsed_sec / durSec) * 100;
    $("progress-bar").style.width = Math.min(100, Math.max(0, pct)).toFixed(1) + "%";
    $("np-time").textContent = `${fmt(song.elapsed_sec)} / ${fmt(durSec)}` +
      (song.likeability != null ? `  \u00b7  predicted ${song.likeability.toFixed(2)}` : "");

    $("rate-like").className = "rate up" + (song.rating === 0 ? " active" : "");
    $("rate-dislike").className = "rate down" + (song.rating === 1 ? " active" : "");
  }

  const q = s.queue || [];
  $("queue-count").textContent = q.length ? `(${(s.resume_set || 0) + q.length})` : "";
  $("queue-list").innerHTML = q.map((item) =>
    `<li>${item.title} <span class="q-artist">${item.artist || ""}</span></li>`
  ).join("");
}

function renderModels(data) {
  const models = data.models || [];
  const rows = models.map((m) => {
    let tag, cls = "tag";
    if (!m.ready) { tag = `waiting: ${m.records}/${m.min_required}`; cls += " waiting"; }
    else if (m.needs_retrain) {
      tag = m.has_model ? "needs retrain" : "not trained yet";
      cls += " retrain";
    } else {
      const left = m.retrain_after - (m.new_records_since_train ?? 0);
      tag = `trained \u00b7 ${left} new until retrain`;
      cls += " ready";
    }
    const action = m.ready
      ? `<button type="button" data-train="${m.place}">Retrain</button>`
      : `<span class="tag">${m.records} recs</span>`;
    return `<tr>
      <td><strong>${m.place}</strong><br><span class="q-artist">${m.genres.join(", ")}</span></td>
      <td>${m.records}</td>
      <td><span class="${cls}">${tag}</span></td>
      <td>${action}</td>
    </tr>`;
  }).join("");
  $("models-body").innerHTML = rows;
}

// Actions that move where the DJ is playing: the <audio> element buffers
// ahead of the live edge, so without reconnecting the browser would keep
// playing the song we just skipped away from. Reconnect snaps it to "now".
const RECONNECT = new Set(["skip", "previous", "restart", "seek-forward", "seek-backward"]);

async function act(action) {
  flash(`\u2192 ${action}...`);
  try {
    const res = await post(`${CONTROL_URL}/${action}`);
    if (action === "previous" && res.replayed === false) {
      flash("no previous song yet");
    } else {
      flash(`\u2713 ${res.action || action} sent`);
    }
    if (RECONNECT.has(action)) {
      reconnectAudio();
    }
    refreshStatus();
  } catch (e) {
    flash(`control failed: ${e.message}`);
  }
}

async function rate(rating) {
  flash(rating === 0 ? "\u2192 like..." : "\u2192 dislike...");
  try {
    const res = await post(`${CONTROL_URL}/rate`, { rating });
    flash(`\u2713 rated ${rating === 0 ? "like" : "dislike"}`);
  } catch (e) {
    flash(`rate failed: ${e.message}`);
  }
}

async function trainPlace(place) {
  $("models-msg").textContent = `training '${place}'...`;
  try {
    const res = await post(`/api/models/train/${place}`);
    $("models-msg").textContent = `'${res.place}' trained.`;
    refreshModels();
  } catch (e) {
    $("models-msg").textContent = `training failed: ${e.message}`;
  }
}

async function trainAll() {
  $("models-msg").textContent = "training ready places...";
  try {
    await post("/api/models/train-all");
    $("models-msg").textContent = "training pass finished.";
    refreshModels();
  } catch (e) {
    $("models-msg").textContent = `training failed: ${e.message}`;
  }
}

function flash(msg) {
  $("control-msg").textContent = msg;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => ($("control-msg").textContent = ""), 3000);
}

async function refreshStatus() {
  try {
    const s = await getJSON(STATUS_URL);
    renderStatus(s);
  } catch (e) {
    $("state-label").textContent = "offline";
    console.warn("status poll failed:", e);
  }
}

async function refreshModels() {
  try {
    renderModels(await getJSON(MODELS_URL));
  } catch (e) {
    console.warn("models poll failed:", e);
  }
}

function wire() {
  document.addEventListener("click", (e) => {
    const actionBtn = e.target.closest("[data-action]");
    if (actionBtn) { act(actionBtn.dataset.action); return; }
    const trainBtn = e.target.closest("[data-train]");
    if (trainBtn) { trainPlace(trainBtn.dataset.train); return; }
    if (e.target.id === "rate-like") { rate(0); return; }
    if (e.target.id === "rate-dislike") { rate(1); return; }
    if (e.target.id === "train-all") { trainAll(); return; }
    if (e.target.id === "sound-cta") { startAudio(); return; }
  });
}

wire();
refreshStatus();
refreshModels();
setInterval(refreshStatus, 1500);
setInterval(refreshModels, 5000);