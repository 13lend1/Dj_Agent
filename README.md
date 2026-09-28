# DJ Agent

**Tired of the same songs on repeat? Let a DJ handle it.**

DJ Agent is not your ordinary music player. It's built for **background music**: the soundtrack for a long drive, a coding session, a study block or a workout. You pick a place (`car`, `study`, `focus_coding`, `rave`...), and the DJ takes it from there. It discovers songs, learns what you like in each place, and cuts every track at its best moment, so you never sit through the same playlist twice and never have to touch the controls.

> Windows only for now (global hotkeys + setup steps). Runs locally, and the Gemini API key works on the free tier.

<!-- Add a demo GIF or screenshot of the UI here: ![demo](docs/demo.gif) -->

## Why it's different

- **Made to run in the background.** Start it, minimize it, forget it. Global hotkeys let you skip, like or dislike from any window.
- **Fresh music, not the same loop.** Songs are discovered from YouTube Music and filtered by the genres of your active place, so a `study` session never plays party tracks.
- **It learns you, per place.** Each place has its own preference model, trained from your likes and dislikes. What you like in the car isn't what you like while coding.
- **Hook-based playback.** Each song's hook comes from its YouTube replay heatmap — the seconds real listeners scrub back to — so the DJ plays the part worth hearing instead of guessing a chorus or playing tracks end to end.
- **No waiting.** The next song is preloaded in memory and a background thread keeps a preprocessed pool full, so playback never stalls.

## How it works

1. **Prefill / refill** (`Music/songs.py`): searches YouTube Music and saves songs plus audio specs (tempo, energy, key, ...) into `Database/music.db`.
2. **Preprocessing** (`Music/audio_specs.py`): decodes a short clip of each candidate and extracts features. The best candidates go into the `Preprocessed` pool. Nothing is warmed up ahead of time — the pool is filled on demand, targeted at the place you just clicked. Until that place is audible the filler only aims for one batch's worth of songs, and once playback has started it keeps the pool near its high-water mark, replacing what has been consumed. Audio-feature results are cached on disk and written in batches, so a fill is not dominated by JSON serialization.
3. **Scoring** (`Model/linear_regression.py`): a per-place model (`models/<place>.pkl`) predicts how much you'll like each track and retrains automatically as like/dislike records accumulate. With too few records, selection falls back to random.
4. **Choosing** (`DJ/agent.py`): every song's hook window is resolved in three steps — measured from its YouTube replay heatmap (`Music/heatmap.py`, cached), then, for videos that publish no heatmap, estimated by a separate hook-only Gemini request (`Music/gemini_hooks.py`), and only then falling back to the song's middle section. A Gemini agent then builds the next playlist around those windows — picking the track order, the transition effects, and what each transition blends into. Hooks are resolved before ordering and never re-guessed.
5. **Playing** (`Music/preloaded_player.py`, `Music/dj.py`): songs are preloaded so the next track starts instantly. The click that picks a place queues a first batch immediately through the fast path — deterministic middle-section hooks, no model and no AI calls — and that batch is the buffer covering the slow second batch, so the deck keeps playing while scoring, hooks and Gemini ordering catch up. Skips land on the saved hook point (no full song unless you ask). On a skip, the server waits up to ~0.8 s for the swap and returns the new song's state in the `POST /api/control/skip` response, so the UI updates with no extra poll.

## Quick start (Windows)

### 1. Install uv (manages Python too)

```powershell
irm https://astral.sh/uv/install.ps1 | iex
uv python install 3.14
```

The project pins Python 3.14 (`.python-version`); `uv sync` uses it automatically.

### 2. Install ffmpeg (required)

The DJ decodes every track and plays effect clips through ffmpeg. Get a build from https://www.gyan.dev/ffmpeg/builds/ (release essentials) and add its `bin` folder to your PATH:

```powershell
# example for an unzipped build at C:\ffmpeg\bin
[Environment]::SetEnvironmentVariable("Path", "$([Environment]::GetEnvironmentVariable('Path','User'));C:\ffmpeg\bin", "User")
```

Restart your terminal, then confirm:

```powershell
ffmpeg -version
```

### 3. Clone and install dependencies

```powershell
git clone <repo-url> Dj_Agent
cd Dj_Agent
uv sync
```

### 4. Create `.env` with your API keys

Create a file named `.env` in the project root (it's git-ignored):

```dotenv
GEMINI_API_KEY=...            # REQUIRED: get one at https://aistudio.google.com/apikey
FREESOUND_API_KEY=...         # optional: only to (re)download effects/ via Music/effects.py
DJ_YTDLP_COOKIES_FILE=...     # optional: export your browser cookies for yt-dlp (403 fixes)
GEMINI_MODEL=gemini-3.1-flash-lite
```

The DJ refuses to start without `GEMINI_API_KEY`.

### 5. (Optional) YouTube Music headers

An anonymous client works out of the box; searches are just slower to warm up. To use your own browser session instead:

```powershell
uv run python -m ytmusicapi setup
```

This writes `Music/headers_auth.json`. If the file is missing, the DJ silently uses the anonymous client.

### 6. Run

```powershell
uv run python api/server.py
```

Open http://127.0.0.1:8000, pick a place, and the DJ starts. `Database/music.db` is git-ignored and does not ship, so a fresh clone has no catalog: the schema is created automatically and the DJ discovers and preprocesses songs on demand, which makes the first few minutes slower than a warm install. The per-place models in `models/` are git-ignored as well, so a clone has no like/dislike history and picks at random until you rate a few songs.

## Places & genres

A place is just a name mapped to a genre list (see `Music/preference.py`; `DEFAULT_PLACE` is `car`). Ships with: car, home, restaurant, gym, party, study, sleep, office, beach, walk, rave, date_night, road_trip, cooking, focus_coding, gaming.

Create your own from the UI (Stop → pick → "Create a new place"); they're saved to `Database/places.json`. Songs may be borrowed from any place's pool, but an active place only ever plays songs whose genre is in **its** list.

Custom places can be deleted from the same screen with the `×` on their tile (or `DELETE /api/control/place?place=<name>`). That permanently removes the place along with its songs, its unplayed pool, its playlist history and its trained model (`models/<place>.pkl`) — so it asks for confirmation first, and the DJ must be stopped. The 16 built-in places cannot be deleted. The per-genre tables and the song-level caches (audio features, heatmaps, covers) are shared across places and are kept.

## Controls

### System-wide hotkeys (Windows)

The browser loses keystrokes to other tabs, so a small thread registers **global** hotkeys that hit the DJ's HTTP API no matter which window has focus. They start with the server and need no admin rights.

| Key          | Action                                                     |
|--------------|------------------------------------------------------------|
| `Ctrl+Alt+N` | Skip to the next song                                      |
| `Ctrl+Alt+L` | Like the current song (a full play also counts as a like)  |
| `Ctrl+Alt+F` | Play the current song in full (counts as a like)           |
| `Ctrl+Alt+P` | Step back to the previous song                             |
| `Ctrl+Alt+D` | Dislike the current song                                   |
| `Ctrl+Alt+Space` | Pause / resume (toggle)                               |

`python hotkeys.py` runs them standalone against `http://127.0.0.1:8000`.
Each press pops a small tray-balloon status near the clock ("Skipping",
"Liked", ...) so you get feedback even when another window has focus.

### Server options

```bash
python api/server.py                       # default UI + DJ
python api/server.py --resume-place rave   # skip the place gate, start now
python api/server.py --no-dj               # API/UI only (playback controls 503)
python api/server.py --speaker             # play via machine speakers, not browser
python api/server.py --no-hotkeys          # skip global Ctrl+Alt hotkeys
python api/server.py --pool-size 40 --top-n 15
python api/server.py --host 0.0.0.0 --port 8000
```

## Project layout

```
api/                FastAPI server + control endpoints (status, skip, rate...)
DJ/                 Gemini agent: playlist order, effects, responses
Model/              Per-place linear-regression preference models
Music/              Discovery, preprocessing, audio features, player, places/genres
                    (heatmap.py resolves each song's hook from YouTube;
                    gemini_hooks.py estimates hooks for videos with no heatmap)
ui/                 Browser DJ deck (vanilla JS, /app.js + /style.css)
Database/           music.db (created on first run), places.json, cover cache
effects/            Downloaded CC0/Attribution transition SFX (Freesound)
models/             Trained per-place .pkl files
hotkeys.py          Windows global Ctrl+Alt hotkey daemon
test_hook_repair.py     offline heatmap-hook + fallback regression tests
test_gemini_hooks.py    live heatmap + Gemini test (needs DJ_TESTS_LIVE=1)
```

Dependencies are managed with `pyproject.toml` + `uv.lock` (no `requirements.txt`).

## Tests

```powershell
uv run python test_gemini_hooks.py
uv run python test_hook_repair.py
```

## Troubleshooting

- **Port already in use**: an old server may still be running. Stop it, or use a different port: `uv run python api/server.py --port 8001`. The system-wide hotkeys register with whichever server runs last.
- **403 errors from YouTube**: set `DJ_YTDLP_COOKIES_FILE` to your exported browser cookies in `.env`, or regenerate `Music/headers_auth.json` with `uv run python -m ytmusicapi setup`.

## Disclaimer

DJ Agent is a personal, educational project. It streams and decodes audio locally for playback and analysis, and does not store or redistribute copyrighted music. Use it in line with the terms of the services it talks to.