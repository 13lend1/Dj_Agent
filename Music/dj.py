import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import queue
import time
import threading
import subprocess
import argparse
import hashlib
import tempfile
import numpy as np
import queue as _q
import yt_dlp
import shutil

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _IMPERSONATE = ImpersonateTarget.from_str('chrome')
except Exception:
    _IMPERSONATE = None

_DENO = {'deno': {'path': shutil.which('deno')}} if shutil.which('deno') else {'deno': {}}
import sounddevice as sd

from preloaded_player import PreloadedPlayer
from DJ.agent import Agent
from DJ.responses import last_run_songs, mark_song_played
from songs import (
    get_random_song,
    take_preprocessed_batch,
    delete_unscored_songs,
    save_song_metadata,
    recycle_played_songs,
    _title_matches_genre,
)

EFFECTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "effects"
)

class DJ(PreloadedPlayer):
    """
    PreloadedPlayer + the selecting/ordering Agent.

    The linear model still picks the n-best from each Preprocessed batch
    (exactly as PreloadedPlayer does). Those go to the Agent, which ranks them
    by how well each song flows after the one currently playing (genre + audio
    specs) and attaches the hook window (timestamps like 1:22 - 1:55) that
    should actually be played. Playback seeks to the hook start, cuts off at
    the hook end, and the rate/skip + save-to-Songs cycle is unchanged.
    """

    def __init__(self, pool_size=50, top_n=15, clip_length=33, resume_unplayed=None, resume_place=None):
        # Resume set state MUST exist and be loaded before super().__init__()
        # starts the background worker threads: otherwise the batch worker
        # could build a fresh run and overwrite the newest saved run (and its
        # played/unplayed flags) before it has been read.
        # 1 resum 0 no resume
        if resume_place is None:
            resume_place = os.environ.get("DJ_RESUME_PLACE", "").strip().lower() or None
        if resume_place is None:
            try:
                from preference import DEFAULT_PLACE
                resume_place = DEFAULT_PLACE if DEFAULT_PLACE else None
            except ImportError:
                pass
        self.resume_place = resume_place
        # Active place governs BOTH fresh fetching (only that place's genres
        # are pulled, tagged and played) and resume-by-place filtering. It must
        # be set before super().__init__() so the worker threads see it.
        self.place = resume_place
        if resume_unplayed is None:
            if resume_place:
                # picking a place to resume implies we want to resume
                resume_unplayed = True
            else:
                resume_unplayed = os.environ.get("DJ_RESUME_UNPLAYED", "1").strip().lower() \
                    not in {"0", "false", "no", "off"}
        self.resume_unplayed = resume_unplayed
        self._saved_set = []
        self._saved_lock = threading.Lock()
        self.arm_saved_set()

        super().__init__(pool_size=pool_size, top_n=top_n)
        self.agent = Agent()
        self.clip_length = clip_length
        self.current_clip_duration = None
        self._advance_fail_count = 0
        self._dry_started = None
        self._transition_scored = False
        self._preload_lock = threading.Lock()
        self._preloading = False
        self._play_gain = 1.0  # per-song loudness-matched gain, applied to the live stream
        self._mark_queue = queue.Queue()
        threading.Thread(target=self._mark_worker, daemon=True).start()
        self._last_transition_pair = None
        self._last_transition_at = 0.0
        self._stall_started = None
        # Local audio cache: yt-dlp downloads each track to disk (keyed by its
        # page URL) and ffmpeg decodes the file, so music requests never leave
        # ffmpeg -> no googlevideo 403 and no mid-stream session kills.
        self._media_files = {}
        self._media_lock = threading.Lock()
        self._media_dir = None
        self._hold_wait_since = None
        self._holding = False
        self._hold_inflight = False  # at most one async ring-fence prep in flight
        # A process must have exactly one stdout pump: two readers divide its
        # PCM bytes between their buffers.
        self._readers = {}
        self._readers_lock = threading.Lock()

    _CHUNK = 4096 * 2 * 2  # 4096 stereo int16 frames
    MIN_CROSSFADE = 2.0     # anything shorter sounds like a hard cut
    POOL_DRY_TIMEOUT = 120.0  # how long to keep the current song rolling while the
                             # preload/batch pipeline catches up. A dry pool NEVER
                             # shuts the DJ down — it just keeps retrying forever.
    HOLD_TIMEOUT = 5.0        # silent grace before the deck ring-fences the last
                             # known-good song so audio never goes fully dead while
                             # the pipeline refills (guarantees nonstop behavior)
    STALL_TIMEOUT = 2.0    # how long a connected-but-silent stream may sit before it
                             # is treated as dead and skipped (prevents long-session freeze)
    DEFAULT_TRANSITION = {"type": "crossfade", "crossfade_sec": 2.0, "note": "Default crossfade."}
    _effect_cache = {}
    _effect_lock = threading.Lock()

    @classmethod
    def _load_effect(cls, name):
        """Decode effects/<name>.mp3 into an int16 PCM (N, 2) buffer, once.
        Returns None (never raises) when the name is empty/'none' or the file
        is missing, so a bad effect can never break playback."""
        if not name or name == "none":
            return None
        with cls._effect_lock:
            if name in cls._effect_cache:
                return cls._effect_cache[name]
            samples = None
            path = os.path.join(EFFECTS_DIR, f"{name}.mp3")
            if os.path.isfile(path):
                try:
                    proc = subprocess.Popen(
                        ['ffmpeg', '-nostdin', '-loglevel', 'error', '-i', path,
                        '-f', 's16le', '-acodec', 'pcm_s16le',
                        '-ar', '44100', '-ac', '2', '-'],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    raw, _ = proc.communicate(timeout=15)
                    if proc.returncode != 0 or not raw:
                        pass
                        # print(f"[effect] ffmpeg failed decoding '{name}' (path={path}, "
                        #     f"returncode={proc.returncode}, bytes={len(raw or b'')})")
                    arr = np.frombuffer(raw, dtype=np.int16)
                    arr = arr[:len(arr) // 2 * 2].reshape(-1, 2).astype(np.int32)
                    if len(arr):
                        peak = int(np.abs(arr).max())
                        if peak > 0:
                            arr = (arr.astype(np.float64) / peak * 26214.0).astype(np.int32)
                        samples = arr
                    else:
                        pass
                        # print(f"[effect] '{name}' decoded to 0 samples (path={path})")
                except Exception as e:
                    # print(f"[effect] exception loading '{name}' (path={path}): {e}")
                    samples = None
            else:
                pass
                # print(f"[effect] file not found: {path}  (EFFECTS_DIR={EFFECTS_DIR})")
            cls._effect_cache[name] = samples
            return samples

    def _fetch_batch(self):
        fresh = take_preprocessed_batch(n=self.pool_size, place=getattr(self, 'place', None))

        candidates = []
        if fresh:
            try:
                from duplicates import played_ids
                played = played_ids(c.get('id') for c in fresh)
            except Exception as e:
                print("Played-check failed, treating batch as new:", e)
                played = set()
            if played:
                fresh = [c for c in fresh if c.get('id') not in played]
                print(f"Filtered {len(played)} already-played song(s) from the "
                      f"fresh batch (kept {len(fresh)}).")
            candidates = fresh

        # Nonstop guarantee: once the pool of unplayed songs is exhausted,
        # recycle already-played songs so the DJ keeps playing forever instead
        # of running dry (the catalog is finite, the set must not be).
        if len(candidates) < self.top_n:
            try:
                recycled = recycle_played_songs(
                    n=self.pool_size, place=getattr(self, 'place', None))
            except Exception as e:
                print("Recycle lookup failed:", e)
                recycled = []
            seen = {c.get('id') for c in candidates}
            recycled = [r for r in recycled if r.get('id') not in seen]
            if recycled:
                print(f"Recycling {len(recycled)} already-played song(s) to keep "
                      "the set running (pool of new songs is low).")
                candidates += recycled

        if not candidates:
            return

        unfiltered = candidates
        before = len(candidates)
        candidates = [c for c in candidates
                      if not _title_matches_genre(c.get('name') or c.get('title'), c.get('genre'))]
        if len(candidates) < before:
            print(f"Filtered {before - len(candidates)} genre-named song(s) from batch.")
        if not candidates:
            # Something is better than silence — never skip a batch entirely
            # and leave the deck with nothing to play.
            print("All candidates were genre-named songs; using the raw batch "
                  "to avoid going silent.")
            candidates = unfiltered

        try:
            delete_unscored_songs()
        except Exception as e:
            print("Cleanup failed:", e)

        try:
            self._load_model()
            self.model.fit()
            top = self.model.select_best(candidates, n=self.top_n)
            records = top.to_dict('records')
        except Exception as e:
            print("Scoring unavailable, selecting randomly instead:", e)
            records = random.sample(candidates, min(self.top_n, len(candidates)))

        # The Agent orders the n-best and picks the timestamps to play.
        # Use the actual next-to-play song (batch tail) as the predecessor
        # so transitions line up with what will actually follow in the queue;
        # fallback to the currently-playing song, then to a cold start.
        with self.batch_lock:
            current = self.batch[-1] if self.batch else None
        if not current:
            current = getattr(self, 'current_song', None)
        if not current:
            current = self.agent.last()
        playlist = self.agent.build_playlist(
            records, current=current, clip_length=self.clip_length, place=getattr(self, 'place', None)
        ) if records else []

        # Agent-approved songs are persisted to the Songs table (the linear
        # model's prediction); reorder=True moves them to the table end so
        # the table rows stay sequential in the current play order.
        for song in playlist:
            likeability = song.get('likeability')
            if likeability is not None:
                try:
                    save_song_metadata(song, likeability=likeability, reorder=True)
                except Exception as e:
                    print("Save predicted likeability failed:", e)

        with self.batch_lock:
            self.batch.extend(playlist)

        if playlist:
            first = playlist[0]
            print(f"Queued {len(playlist)} agent-ranked songs "
                  f"({first['name']} {self._fmt_hook(first)})")
        else:
            print("Agent approved no songs this batch.")

    # ---- resume set: unplayed tracks of the last Agent run -----------------

    def arm_saved_set(self):
        """Load the newest saved Agent run and keep its UNPLAYED tracks, in
        order, as the set to resume. Played tracks are skipped. When nothing
        is saved yet or every track already played, the resume set stays empty
        and the DJ simply waits for a fresh batch.

        Returns the saved list (convenience for the __main__ startup flow)."""
        if not self.resume_unplayed:
            with self._saved_lock:
                self._saved_set = []
            print("Resume disabled (--no-resume / DJ_RESUME_UNPLAYED=0) — "
                  "always selecting fresh tracks from new Agent runs.")
            return []
        from DJ.responses import last_run_songs
        try:
            _, songs = last_run_songs()
        except Exception as e:
            print("Could not load the saved Agent run:", e)
            songs = []

        playable = []
        played = 0
        skipped_place = 0
        skipped_title = 0
        for song in songs or []:
            if not isinstance(song, dict):
                continue
            if song.get("played"):
                played += 1
                continue
            if self.resume_place and song.get("place") != self.resume_place:
                skipped_place += 1
                continue
            if _title_matches_genre(song.get("name") or song.get("title"), song.get("genre")):
                skipped_title += 1
                continue
            if song.get("link"):
                playable.append(dict(song))

        with self._saved_lock:
            self._saved_set = playable

        if playable:
            where = f" for '{self.resume_place}'" if self.resume_place else ""
            print(f"Resuming {len(playable)} unplayed track(s) from the last "
                  f"Agent run{where} ({played} already played, skipped)"
                  + (f", {skipped_place} for other places" if skipped_place else "")
                  + (f", {skipped_title} genre-named" if skipped_title else "") + ".")
        elif self.resume_place and skipped_place:
            print(f"No unplayed '{self.resume_place}' tracks left in the last "
                  f"Agent run ({played} played, {skipped_place} belong to other "
                  "places) — waiting for a fresh batch.")
        # Return a copy: get_next_song() mutates the internal _saved_set by
        # popping songs off it as they are served, so callers that need the
        # full resume list (e.g. __main__) must not share that object.
        return list(playable)

    def get_next_song(self, timeout=None):
        """Serve the unplayed tracks of the saved Agent run first; once they run
        out, fall back to the normal batch queue (fresh agent-built playlists)."""
        if not self.stop_event.is_set():
            with self._saved_lock:
                if self._saved_set:
                    song = self._saved_set.pop(0)
                    self._record_genre(song)
                    return song
        return super().get_next_song(timeout)

    def _mark_worker(self):
        """Background worker that settles an outgoing song: records the genre
        (so it won't be refetched) and flags it as played in the Agent run.
        Both are database writes that grew slower as the Agent table filled up,
        so they must never run inside the audio-loop thread — a slow mark would
        freeze the stream and make skip/seek unresponsive."""
        while True:
            song = self._mark_queue.get()
            if song is None:
                break
            try:
                self._record_genre(song)
                self._mark_played(song)
            except Exception as e:
                print("Mark-played error:", e)

    def _mark_played(self, song):
        """Flag a track as played (start-to-end) in the newest Agent run so the
        next DJ start resumes from where the set actually left off."""
        song_id = song.get("id") if isinstance(song, dict) else None
        try:
            mark_song_played(song_id)
        except Exception as e:
            print("Could not flag song as played:", e)

    def stop(self):
        super().stop()
        # Give the background worker a moment to settle queued marks so the
        # resume state is accurate; the audio loop is already shutting down.
        deadline = time.time() + 10.0
        while not self._mark_queue.empty() and time.time() < deadline:
            time.sleep(0.1)

        # Tidy the downloaded scratch files; anything still held open by a
        # dying ffmpeg is skipped.
        with self._media_lock:
            paths = list(self._media_files.values())
            self._media_files.clear()
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass

    # ---- audio: download locally, then seek to the hook start ------------

    _MEDIA_MAX_FILES = 80   # bound the on-disk cache for long sessions

    def _media_cache_dir(self):
        """Local scratch dir holding yt-dlp's downloaded audio. ffmpeg decodes
        from here instead of requesting the media URL itself: googlevideo signs
        those URLs against yt-dlp's impersonated TLS/cookie session, so ffmpeg's
        own request is routinely rejected with HTTP 403 (and killed mid-stream).
        """
        d = self._media_dir
        if d and os.path.isdir(d):
            return d
        d = os.path.join(tempfile.gettempdir(), 'dj_agent_audio')
        os.makedirs(d, exist_ok=True)
        self._media_dir = d
        return d

    def _evict_media(self):
        """Bound the cache: drop the oldest downloads first. A file still open
        by a live ffmpeg cannot be unlinked on Windows, so in-use tracks are
        protected by construction; any failure is skipped."""
        with self._media_lock:
            if len(self._media_files) <= self._MEDIA_MAX_FILES:
                return
            entries = sorted(
                self._media_files.items(),
                key=lambda kv: os.path.getmtime(kv[1]) if os.path.isfile(kv[1]) else 0,
            )
            for dur, path in entries:
                if len(self._media_files) <= self._MEDIA_MAX_FILES:
                    break
                try:
                    os.remove(path)
                except OSError:
                    continue
                self._media_files.pop(dur, None)

    @staticmethod
    def _run_ytdlp(options, url):
        """yt-dlp call with a browser-cookie fallback: a missing/locked browser
        profile must never take playback down, so retry once without it."""
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
        except Exception as exc:
            if ('cookies database' in str(exc).lower()
                    and options.get('cookiesfrombrowser')):
                fallback = dict(options)
                fallback.pop('cookiesfrombrowser', None)
                print('Browser cookies unavailable; retrying without them.',
                      flush=True)
                with yt_dlp.YoutubeDL(fallback) as ydl:
                    ydl.download([url])
                return
            raise

    def _download_media(self, url, options):
        """Download the best audio track to a local file using yt-dlp's own
        (impersonated, cookie-bearing) HTTP stack, caching it by page URL so
        seeks, restarts and replays never refetch. Returns the local path."""
        with self._media_lock:
            cached = self._media_files.get(url)
            if cached and os.path.isfile(cached):
                return cached

        out_dir = self._media_cache_dir()
        key = hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]
        dl_opts = dict(options)
        dl_opts.update({
            'outtmpl': os.path.join(out_dir, key + '.%(ext)s'),
            'noprogress': True,
            'overwrites': True,
        })
        if self.stop_event.is_set():
            raise RuntimeError(f"Stopped while downloading {url}")
        self._run_ytdlp(dl_opts, url)
        if self.stop_event.is_set():
            raise RuntimeError(f"Stopped while downloading {url}")

        matches = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
                   if f.startswith(key + '.') and not f.endswith('.part')]
        if not matches:
            raise RuntimeError(f"Download produced no file for {url}")
        path = max(matches, key=os.path.getmtime)
        with self._media_lock:
            self._media_files[url] = path
        self._evict_media()
        return path

    def prepare_song(self, song, seek_to=None):
        """Seek-aware prepare: accepts a song dict (seeks to play_start_sec, or
        to `seek_to` seconds into the full track when given) or a plain URL.

        yt-dlp fetches the track to a local file first and ffmpeg decodes that
        file, so no music request ever leaves ffmpeg. Direct streaming made
        ffmpeg request a URL signed to yt-dlp's session, which googlevideo
        answered with HTTP 403 / mid-stream kills. A local file is always
        seekable, so `-ss` before `-i` is enough."""
        if isinstance(song, dict):
            url = song['link']
            seek = seek_to if seek_to is not None else song.get('play_start_sec')
        else:
            url = song
            seek = seek_to

        options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'noprogress': True,
            'noplaylist': True,
            'socket_timeout': 30,
            'retries': 3,
            'impersonate': _IMPERSONATE,
            'force_ipv4': True,
            'js_runtimes': _DENO,
            'remote_components': ['ejs:github'],
            'http_headers': {'Accept': '*/*'},
        }
        # Prefer an explicitly exported Netscape cookies.txt file. It avoids
        # Windows browser-profile locks and is more reliable for long sessions.
        cookie_file = os.environ.get('DJ_YTDLP_COOKIES_FILE', '').strip()
        if cookie_file:
            if os.path.isfile(cookie_file):
                options['cookiefile'] = cookie_file
            else:
                print(f'Cookie file not found: {cookie_file}; continuing without it.',
                      flush=True)
        else:
            # Use the user's normal browser session when it is accessible.
            # Set DJ_YTDLP_COOKIES_BROWSER= to disable it, or use firefox/edge.
            cookie_browser = os.environ.get('DJ_YTDLP_COOKIES_BROWSER', 'chrome').strip()
            if cookie_browser:
                options['cookiesfrombrowser'] = (cookie_browser,)

        media_path = self._download_media(url, options)

        # A local-file decode either starts at once or fails immediately, so a
        # failed seek is retried once without it instead of starting on silence.
        last_exc = None
        for use_seek in ([seek, None] if seek else [None]):
            if self.stop_event.is_set():
                raise RuntimeError(f"Stopped while preparing {url}")
            cmd = ['ffmpeg']
            if use_seek:
                cmd += ['-ss', str(use_seek)]
            cmd += [
                '-i', media_path,
                '-f', 's16le',
                '-acodec', 'pcm_s16le',
                '-ar', '44100',
                '-ac', '2',
                '-',
            ]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=(open(os.environ["DJ_FFMPEG_ERR"], "ab", buffering=0)
                        if os.environ.get("DJ_FFMPEG_ERR") else subprocess.DEVNULL),
                bufsize=0,
            )
            time.sleep(0.2)
            if process.poll() is None:
                return process
            last_exc = RuntimeError(
                f"ffmpeg exited early (code {process.poll()}) for {url}"
            )
            if use_seek:
                print("prepare_song retrying without seek:", last_exc, flush=True)

        raise last_exc

    def preload(self, song_info):
        if self.stop_event.is_set():
            return

        if song_info is None:
            print("No song available to preload, fetching a random one.")
            if self.stop_event.is_set():
                return
            song_info = get_random_song(place=getattr(self, 'place', None))

        if song_info is None:
            print("Could not find a song to preload.")
            return

        if self.stop_event.is_set():
            return

        try:
            process = self.prepare_song(song_info)
        except Exception as e:
            print("Preload error:", e)
            return

        if self.stop_event.is_set():
            try:
                process.kill()
            except Exception:
                pass
            return
        
        self._reader_queue(process) 

        with self._preload_procs_lock:
            self._preload_processes.add(process)

        try:
            while not self.stop_event.is_set():
                try:
                    self.song_queue.put({
                        **song_info,
                        'process': process,
                    }, timeout=1)
                    print(f"Preloaded: {song_info['name']} {self._fmt_hook(song_info)}")
                    break
                except queue.Full:
                    continue
        finally:
            with self._preload_procs_lock:
                self._preload_processes.discard(process)

        if self.stop_event.is_set():
            try:
                process.kill()
            except Exception:
                pass

    def _keep_preloaded(self, get_next_song):
        """Keep the preload buffer topped up while a song is playing: spawn at
        most one decode worker at a time so the next few songs are already
        decoding/buffered before their turn (rolling preload, not one-shot)."""
        if self.stop_event.is_set():
            return
        with self._preload_lock:
            if self._preloading:
                return
            if self.song_queue.full():
                return
            self._preloading = True
        threading.Thread(
            target=self._preload_worker,
            args=(get_next_song,),
            daemon=True,
        ).start()

    def _preload_worker(self, get_next_song):
        try:
            self.preload(get_next_song(timeout=12))
        finally:
            with self._preload_lock:
                self._preloading = False

    # ---- playback ---------------------------------------------------------

    def _begin_song(self, song, current, start_offset=0.0):
        start = song.get('play_start_sec')
        end = song.get('play_end_sec')
        with self.lock:
            self.current_process = current
            self.current_url = song['link']
            self.current_song = song
            self.current_title = song['name']
            self.current_length = song['duration']
            self.current_start_time = time.time() - start_offset
            self.current_rating = None
            self.current_clip_duration = (
                (end - start) if (start is not None and end is not None) else None
            )
        self._play_gain = 1.0
        self._transition_scored = False
        self._stall_started = None
        self.agent.track(song)
    @staticmethod
    def _transition_secs(crossfade_sec):
        try:
            return float(crossfade_sec)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _early_end(elapsed, clip):
        """True when a stream ended well before its clip window should have
        finished (dead / region-blocked / short wrong video). A legitimate full
        play of a short song (clip == real length) must NOT be flagged, so the
        bar is at most ~5s, and half the clip. Returns False when the window is
        unknown."""
        if clip is None or clip <= 0:
            return False
        return elapsed < max(5.0, 0.5 * clip)

    @staticmethod
    def _cap_crossfade(clip, crossfade_sec):
        """Never let the fade swallow the whole hook: keep at least ~5s (or half
        the clip, whichever is smaller) of solo audio before the blend starts."""
        if not clip or clip <= 0:
            return crossfade_sec
        solo = min(5.0, clip / 2.0)
        return min(crossfade_sec, max(0.0, clip - solo))

    @staticmethod
    def _soft_limit(samples):
        """Turn a float audio array into int16, soft-limiting any peaks that a
        volume boost would push past full scale (tanh) instead of hard-clipping,
        so loudness-matched songs don't turn to harsh distortion."""
        if samples.size and float(np.abs(samples).max()) > 32000.0:
            samples = np.tanh(samples / 32768.0) * 32768.0
        return np.clip(samples, -32768, 32767).astype(np.int16)

    def _score_current_song(self):
        """Likeability stays the model's prediction for the song, unless the
        audience input a 0-9 rating — then rating/9 wins. Only scores; the
        genre record + Agent-run mark are settled by the background
        _mark_worker so this never blocks the audio loop."""
        with self.lock:
            song = dict(self.current_song)
            score = self.current_rating

        if score is not None:
            likeability = round(score / 9, 2)
        else:
            predicted = song.get('likeability')
            if predicted is None:
                predicted = song.get('score') or 0.0
            likeability = round(float(predicted), 2)

        print(f"'{song['name']}' likeability: {likeability}", flush=True)
        song['score'] = likeability
        self.save_queue.put(song)

    def _crossfade(self, out_process, in_process, stream, secs, effect=None):
        """Equal-power crossfade from out_process into in_process over `secs`,
        with automatic loudness matching so the incoming song lands at roughly
        the same volume as the outgoing one.

        Returns the matched gain to keep applying to the incoming song after
        the blend ends (1.0 = no change). It is computed from the raw RMS of
        the two streams (before any fade ramp), so a quietly-mastered song
        gets boosted while a loud one gets pulled down to match its neighbour.

        Progress is measured by bytes actually written (== audio time consumed,
        since stream.write blocks on real playback), so the ramp stays correct
        regardless of how fast the pipes drain.

        The two decoders deliver chunks of different sizes at different moments.
        Both are drained into carry buffers and mixed only over the frames BOTH
        have delivered at that instant, so neither stream is ever zero-padded
        mid-stream: padding where real music will arrive next made amplitude
        steps at chunk boundaries, heard as a buzzing/clattering artefact during
        the overlap. A side is only crossed with true silence when it actually
        has no audio right now (ended or buffering).

        `effect` is an optional (N, 2) int32 PCM buffer (44100 Hz stereo) mixed
        on top of the blend for as long as it lasts, with its own fast fade-in
        and tail fade-out so it sits cleanly over the transition.
        """
        if not secs or secs <= 0:
            return 1.0
        bytes_per_sec = 44100 * 2 * 2
        fade_bytes = int(secs * bytes_per_sec)
        written = 0
        half_pi = np.pi / 2
        eff = effect if (effect is not None and len(effect)) else None
        eff_len = 0 if eff is None else len(eff)
        eff_i = 0
        eff_gain = 0.9
        fade_in_frames = int(0.08 * 44100)
        fade_out_frames = int(0.2 * 44100)

        # Side-read one chunk from the outgoing: if it has already ended there
        # is nothing to blend out of, so bring the incoming in quickly instead
        # of sitting at low volume through a long fade. Read via the reader
        # queue (the same path the play loop uses): a direct stdout.read here
        # would fight the pump thread for the same pipe. The short timeout
        # keeps a stalled outgoing from pinning the transition (see deadline
        # below): a transitional stall must never read like a long freeze.
        try:
            probe = self._read_chunk(out_process, timeout=1.0)
            if probe is None:          # stalling, not ended: fade treats it as silent
                probe = b""
        except (OSError, ValueError):
            probe = b""
        probe = probe[:len(probe) // 4 * 4]
        dead_out = len(probe) == 0
        fast_frac = min(secs, 1.0) / max(secs, 0.001)

        # Loudness matching state (raw levels, measured before any fade ramp).
        out_level = None                      # smoothed RMS of the outgoing
        in_level = None                       # smoothed RMS of the incoming
        gain_in = 1.0
        ref_level = 0.20 * 32768.0            # ~-14 dBFS anchor when outgoing is dead
        ema = 0.25
        min_gain, max_gain = 0.5, 2.0         # +/-6 dB per transition, no crazy swings

        # Frame-accurate carry buffers (see docstring: no mid-stream padding).
        a_carry = probe
        b_carry = b""

        # Hard wall-clock bound: healthy crossfades finish in ~secs (stream.write
        # blocks on real playback), so a multi-x stretch means one side is
        # stalling. Without this cap a dead-but-alive stream lets each pass eat
        # its 1s read timeout while advancing only ~0.09s of fade — a transition
        # could sit for MINUTES and read as a hard freeze. Bail and let the
        # main-loop stall watchdog handle a truly dead incoming stream.
        crossfade_deadline = time.time() + max(secs * 3.0, 30.0)
        a_real_eof = b_real_eof = False
        while not self.stop_event.is_set():
            if time.time() > crossfade_deadline:
                break
            t1 = 1.0 if fade_bytes <= 0 else min(written / fade_bytes, 1.0)

            try:
                a_chunk = self._read_chunk(out_process, timeout=1.0)
            except (OSError, ValueError):
                a_chunk = b""
            if a_chunk is None:
                a_chunk = b""          # stalled, not ended
            elif a_chunk == b"":
                a_real_eof = True      # confirmed real end
            a_carry += a_chunk[:len(a_chunk) // 4 * 4]

            try:
                b_chunk = self._read_chunk(in_process, timeout=1.0)
            except (OSError, ValueError):
                b_chunk = b""
            if b_chunk is None:
                b_chunk = b""
            elif b_chunk == b"":
                b_real_eof = True
            b_carry += b_chunk[:len(b_chunk) // 4 * 4]

            a_frames = len(a_carry) // 4
            b_frames = len(b_carry) // 4
            if a_frames == 0 and b_frames == 0:
                if a_real_eof and b_real_eof:
                    break               # both genuinely ended — nothing more to mix
                continue 

            # If only one deck has audio right now (the other ended or is
            # buffering), cross it with true silence so the device never
            # underruns — a real absence of audio, not the cadence mismatch
            # that caused the buzzing.
            if a_frames == 0:
                nframes = b_frames
                a_real = np.zeros(nframes * 2, dtype=np.int16)
                b_real = np.frombuffer(b_carry[:nframes * 4], dtype=np.int16)
            elif b_frames == 0:
                nframes = a_frames
                a_real = np.frombuffer(a_carry[:nframes * 4], dtype=np.int16)
                b_real = np.zeros(nframes * 2, dtype=np.int16)
            else:
                nframes = min(a_frames, b_frames)
                a_real = np.frombuffer(a_carry[:nframes * 4], dtype=np.int16)
                b_real = np.frombuffer(b_carry[:nframes * 4], dtype=np.int16)

            a_carry = a_carry[nframes * 4:]
            b_carry = b_carry[nframes * 4:]

            # Loudness matching on the real (unpadded) frames only.
            if a_real.size:
                rms_a = float(np.sqrt(
                    np.mean(a_real.astype(np.float64) ** 2)
                )) + 1e-8
                out_level = rms_a if out_level is None else ema * rms_a + (1 - ema) * out_level
            if b_real.size:
                rms_b = float(np.sqrt(
                    np.mean(b_real.astype(np.float64) ** 2)
                )) + 1e-8
                in_level = rms_b if in_level is None else ema * rms_b + (1 - ema) * in_level
            if in_level is not None:
                reference = out_level if out_level is not None else ref_level
                target = min(max(reference / in_level, min_gain), max_gain)
                gain_in = gain_in + 0.3 * (target - gain_in)

            # Continuous ramp across this window: each frame gets its own gain,
            # so window-size changes never step the volume.
            frame_t = np.linspace(t1, min((written + nframes * 4) / fade_bytes, 1.0), nframes)
            if dead_out:
                bt = np.minimum(1.0, frame_t / fast_frac)
                out_gain = np.zeros(nframes, dtype=np.float64)
                in_gain = np.sin(bt * half_pi)
            else:
                out_gain = np.cos(frame_t * half_pi)
                in_gain = np.sin(frame_t * half_pi)

            mix = (a_real.astype(np.float64) * out_gain.repeat(2)
                   + b_real.astype(np.float64) * (in_gain.repeat(2) * gain_in))
            if eff is not None:
                eff_chunk = eff[eff_i:eff_i + nframes]
                have = len(eff_chunk)
                if have:
                    idx = np.arange(eff_i, eff_i + have)
                    g = np.ones(have, dtype=np.float64)
                    if fade_in_frames > 0:
                        head = idx < fade_in_frames
                        g[head] *= (idx[head] + 1.0) / fade_in_frames
                    if fade_out_frames > 0:
                        tail = eff_len - idx
                        tail_mask = tail < fade_out_frames
                        g[tail_mask] *= (tail[tail_mask] + 1.0) / fade_out_frames
                    mix[:have * 2] += eff_chunk.reshape(-1) * (eff_gain * g).repeat(2)
                eff_i += nframes
            mix = self._soft_limit(mix)
            chunk = mix.tobytes()
            stream.write(chunk)
            written += nframes * 4

            if written >= fade_bytes:
                break

        return float(np.clip(gain_in, min_gain, max_gain))

    def _advance(self, current, stream, crossfade_sec, effect=None, wait=False):
        """Move from the current (outgoing) process into the next: crossfade if
        requested (optionally layered with a transition effect clip), then kill
        the old process and hand the deck to the new song.
        The outgoing song is remembered as the previously played one.

        `wait` controls how long to block for a preloaded song: the end-of-song
        path (no audio left) can afford to wait a little, while the mid-song
        clip window must fail fast and keep the current audio rolling."""
        if self.stop_event.is_set():
            return None

        with self.lock:
            outgoing = dict(self.current_song) if self.current_song is not None else None

        next_song = self.get_preloaded_song(timeout=10 if wait else 0.2)
        if next_song is None:
            return None
        next_process = next_song['process']
        matched_gain = 1.0
        consumed = 0.0
        if crossfade_sec and crossfade_sec > 0:
            # Never re-fire the same transition's effect if the same song pair
            # gets blended more than once (stalled pipeline retries). The sound
            # must play exactly once per transition, never looped/restacked.
            now = time.time()
            pair = ((outgoing or {}).get('id'), next_song.get('id'))
            if pair == self._last_transition_pair and now - self._last_transition_at < 30.0:
                effect = None
            else:
                self._last_transition_pair = pair
                self._last_transition_at = now
            effect_samples = self._load_effect(effect)
            matched_gain = self._crossfade(current, next_process, stream, crossfade_sec, effect=effect_samples)
            consumed = crossfade_sec
        try:
            current.kill()
        except Exception:
            pass
        self._begin_song(next_song, next_process, start_offset=consumed)
        self._play_gain = matched_gain
        self.last_song = outgoing
        return next_song

    def play(self, first_song, get_next_song):

        # Open the audio device FIRST: if that fails there is nothing to play,
        # and we must not pretend to be playing (no preloads, no saves).
        try:
            stream = sd.RawOutputStream(
                samplerate=44100,
                channels=2,
                dtype='int16',
                blocksize=4096
            )
            stream.__enter__()
        except Exception as e:
            print(f"\nNO AUDIO OUTPUT AVAILABLE - no sound will play.\n{e}\n",
                  flush=True)
            try:
                stream.__exit__(None, None, None)
            except Exception:
                pass
            return

        try:

            current = self.prepare_song(first_song)
            self._begin_song(first_song, current)
            self._holding = False
            print(f"Now playing: {self.current_title} {self._fmt_hook(first_song)}",
                  flush=True)
            self._keep_preloaded(get_next_song)

            print("\nDJ started!")
            print("n = next")
            print("s = stop")
            print("r = replay the song that played before this one")
            print("a = restart current song from the beginning")
            print("<- / -> = seek back / forward 5 seconds")
            print("Ctrl+C = stop\n")

            while not self.stop_event.is_set():

                if self.restart_event.is_set():

                    self.restart_event.clear()

                    with self.lock:
                        song = dict(self.current_song)

                    try:
                        current.kill()
                    except Exception:
                        pass

                    current = self.prepare_song(song)

                    self._begin_song(song, current)
                    self._holding = False

                    print(f"\nRestarting from the beginning: {self.current_title} {self._fmt_hook(song)}",
                          flush=True)
                    continue

                if self.replay_event.is_set():

                    self.replay_event.clear()

                    with self.lock:
                        outgoing = dict(self.current_song) if self.current_song is not None else None
                        replay_song = dict(self.last_song) if self.last_song else None

                    if replay_song is None:
                        print("\nNo previous song to replay.")
                        continue

                    self._score_replay(replay_song)

                    try:
                        current.kill()
                    except Exception:
                        pass

                    current = self.prepare_song(replay_song)
                    self._begin_song(replay_song, current)
                    self._holding = False
                    self.last_song = outgoing

                    print(f"\nNow playing (replayed): {self.current_title} {self._fmt_hook(replay_song)}",
                          flush=True)
                    continue

                if self.seek_forward_event.is_set() or self.seek_backward_event.is_set():

                    seek_fwd = self.seek_forward_event.is_set()
                    self.seek_forward_event.clear()
                    self.seek_backward_event.clear()

                    with self.lock:
                        song = dict(self.current_song)
                        elapsed = time.time() - self.current_start_time

                    start = song.get('play_start_sec') or 0
                    end = song.get('play_end_sec')
                    # clip-relative bounds: the hook window expressed in seconds
                    # elapsed since the clip started (0 == hook start)
                    if end is not None:
                        clip_end = end - start
                    elif song.get('duration'):
                        clip_end = song['duration'] / 1000 - start
                    else:
                        clip_end = None
                    offset = 5 if seek_fwd else -5
                    target = elapsed + offset

                    if seek_fwd:
                        if clip_end is not None and target >= clip_end:
                            print("\nAt end of song — skipping to next...")
                            self.skip()
                            continue
                        target = max(0.0, target)
                    else:
                        if elapsed <= 0.5:
                            if self.last_song is not None:
                                print("\nAt start of song — going to previous...")
                                self.replay()
                            else:
                                print("\nAt start of song — restarting...")
                                self.restart()
                            continue
                        target = max(0.0, target)

                    try:
                        current.kill()
                    except Exception:
                        pass

                    # seek_to is in full-track seconds -> hook start + clip-relative target
                    current = self.prepare_song(song, seek_to=start + target)

                    with self.lock:
                        self.current_process = current
                        self.current_start_time = time.time() - target
                        self.current_elapsed = None
                        self.current_clip_duration = (
                            (end - (start + target)) if end is not None else None
                        )
                        self._play_gain = 1.0
                    self._transition_scored = False
                    self._dry_started = None

                    print(f"\nSeeked to {target:.1f}s into the hook: "
                          f"{self.current_title} {self._fmt_hook(song)}", flush=True)
                    continue

                transitions = (
                    (getattr(self, 'current_song', None) or {}).get('transition_out')
                    or self.DEFAULT_TRANSITION
                )
                crossfade_sec = self._transition_secs(transitions.get('crossfade_sec'))
                crossfade_sec = max(crossfade_sec, self.MIN_CROSSFADE)
                clip = self.current_clip_duration
                crossfade_sec = self._cap_crossfade(clip, crossfade_sec)
                effect = transitions.get('effect')

                if self.skip_event.is_set():

                    self.skip_event.clear()

                    if self.stop_event.is_set():
                        break

                    with self.lock:
                        self.current_elapsed = time.time() - self.current_start_time

                    if not self._transition_scored:
                        self._transition_scored = True
                        self._score_current_song()

                    if not self.replay_event.is_set():
                        if transitions.get('type') != 'cut':
                            print(f"Crossfading out: {transitions.get('note', '')}")

                    next_song = self._safe_advance(current, stream, crossfade_sec, effect=effect)

                    if next_song is None:
                        # No preloaded song yet (slow preload / batch still
                        # warming up). Never shut the DJ down for that — keep
                        # the current song rolling and let the normal advance
                        # path grab the song the moment it lands.
                        if self._dry_started is None:
                            self._dry_started = time.time()
                            print("Skipping — waiting for the next song to "
                                  "preload...", flush=True)
                        elif time.time() - self._dry_started >= self.POOL_DRY_TIMEOUT:
                            # A dry pool must NEVER stop the DJ: keep the current
                            # song rolling and keep retrying the pipeline.
                            print("Pool is dry — keeping the current song rolling "
                                  "while the pipeline refills.", flush=True)
                            self._dry_started = time.time()
                        self._keep_preloaded(get_next_song)
                    else:
                        current = next_song['process']
                        self._advance_fail_count = 0
                        self._dry_started = None
                        self._hold_wait_since = None
                        self._holding = False
                        self._transition_scored = False
                        print(f"Starting next song: {self.current_title} {self._fmt_hook(next_song)}")
                        self._keep_preloaded(get_next_song)
                        continue

                # As the hook window runs out, begin the suggested crossfade
                elapsed = time.time() - self.current_start_time
                if clip is not None and elapsed >= max(0.0, clip - crossfade_sec - 0.1):

                    if self.stop_event.is_set():
                        break

                    # Score and mark the outgoing song exactly once. Every
                    # retry below re-enters this branch, so without this guard
                    # the same song would be scored / marked-played repeatedly.
                    if not self._transition_scored:
                        self._transition_scored = True
                        if not self._holding:
                            with self.lock:
                                self.current_elapsed = elapsed
                            song_for_played = dict(self.current_song) if self.current_song is not None else None
                            self._score_current_song()
                            if not self.replay_event.is_set():
                                if transitions.get('type') != 'cut':
                                    print(f"\nCrossfading to next: {transitions.get('note', '')}")
                            if song_for_played:
                                self._mark_queue.put(dict(song_for_played))

                    next_song = self._safe_advance(current, stream, crossfade_sec, effect=effect)

                    if next_song is None:
                        # Preload is still warming up / the pool is refilling.
                        # Keep the current song's tail playing while we retry:
                        # fall through to the read/write at the bottom of the
                        # loop instead of continuing (which would re-enter this
                        # branch and skip the audio write entirely).
                        if self._dry_started is None:
                            self._dry_started = time.time()
                            print("Crossfade ready — waiting for the next song to "
                                  "preload...", flush=True)
                        elif time.time() - self._dry_started >= self.POOL_DRY_TIMEOUT:
                            print("Pool is dry — keeping the current song rolling "
                                  "while the pipeline refills.", flush=True)
                            self._dry_started = time.time()
                        self._keep_preloaded(get_next_song)
                    else:
                        self._dry_started = None
                        self._advance_fail_count = 0
                        self._hold_wait_since = None
                        self._holding = False
                        current = next_song['process']
                        print(f"\nNow playing: {self.current_title} {self._fmt_hook(next_song)}")
                        self._keep_preloaded(get_next_song)
                        continue

                try:
                    data = self._read_chunk(current, timeout=1.0)
                except (OSError, ValueError):
                    data = b""

                if data is None:
                    # Producer is alive but quiet (buffering / network stall).
                    # This is NOT the end of the song: keep this song on deck
                    # and wait rather than advancing. Giving up here is what
                    # made slow streams get skipped a few seconds in.
                    if self.stop_event.is_set():
                        break
                    if self._stall_started is None:
                        self._stall_started = time.time()
                    elif time.time() - self._stall_started > self.STALL_TIMEOUT:
                        # A stream that stays connected yet silent forever is
                        # dead, not slow. Without this watchdog the deck waits
                        # on it indefinitely (the long-session freeze). Skip it.
                        print(f"Stream stalled for {self.STALL_TIMEOUT:.0f}s — "
                              "skipping to the next song.", flush=True)
                        self._stall_started = None
                        self._transition_scored = True  # stalled song: score only
                        self.skip_event.set()
                        continue
                    self._keep_preloaded(get_next_song)
                    time.sleep(0.05)
                    continue

                self._stall_started = None

                if not data:

                    if self.stop_event.is_set():
                        break

                    song_for_played = dict(self.current_song) if self.current_song is not None else None

                    # A stream that dies well before its clip window ends isn't a
                    # "finished song" — it's a dead / region-blocked / short wrong
                    # video. Don't flag it as played (so it can drift back and be
                    # refetched) and don't make it the replay target.
                    clip = self.current_clip_duration
                    with self.lock:
                        early_elapsed = time.time() - self.current_start_time
                    early = self._early_end(early_elapsed, clip)
                    prev_last = self.last_song

                    if not self._transition_scored:
                        self._transition_scored = True
                        # A ring-fenced hold song just keeps the deck warm:
                        # scoring/marking it would re-write DB rows every loop.
                        if not self._holding:
                            with self.lock:
                                self.current_elapsed = time.time() - self.current_start_time
                            self._score_current_song()

                    fade = crossfade_sec if (crossfade_sec and crossfade_sec > 0) else self.DEFAULT_TRANSITION['crossfade_sec']
                    next_song = self._safe_advance(current, stream, fade, effect=effect)

                    if next_song is None:
                        # The song has truly ended but nothing is preloaded yet.
                        # NEVER stop the DJ over an empty pipeline: keep waiting,
                        # and once the last good song has been silent long enough,
                        # ring-fence it so audio never goes fully dead.
                        if self._dry_started is None:
                            self._dry_started = time.time()
                            print("Song ended — waiting for the next song to "
                                  "preload...", flush=True)
                        if time.time() - self._dry_started >= self.POOL_DRY_TIMEOUT:
                            print("Pool is dry — keeping the last good song "
                                  "ring-fenced while the pipeline retries.",
                                  flush=True)
                            self._dry_started = time.time()
                        if self._hold_wait_since is None:
                            self._hold_wait_since = time.time()
                        elif time.time() - self._hold_wait_since >= self.HOLD_TIMEOUT:
                            if self._request_hold(song_for_played):
                                # Ring-fence prepared off the audio loop: the
                                # moment it lands in the preload queue the next
                                # _advance plays it. Never blocks playback.
                                print("Ring-fencing the last good song to keep "
                                      "audio alive while the pipeline refills.",
                                      flush=True)
                            # Don't hammer the network: one request per HOLD_TIMEOUT.
                            self._hold_wait_since = time.time()
                        self._keep_preloaded(get_next_song)
                        time.sleep(2.0)
                        continue

                    if song_for_played and not early and not self._holding:
                        self._mark_queue.put(dict(song_for_played))
                    if early:
                        self.last_song = prev_last

                    self._dry_started = None
                    self._hold_wait_since = None
                    self._holding = False
                    self._advance_fail_count = 0
                    current = next_song['process']
                    print(f"\nNow playing: {self.current_title} {self._fmt_hook(next_song)}")
                    self._keep_preloaded(get_next_song)
                    continue

                if data:
                    try:
                        if self._play_gain != 1.0:
                            raw = np.frombuffer(data, dtype=np.int16).astype(np.float64)
                            data = self._soft_limit(raw * self._play_gain).tobytes()
                        stream.write(data)
                    except Exception as wexc:
                        # A failing device must never take the DJ down: log and
                        # keep the loop alive so playback resumes the moment the
                        # output heals.
                        print("Write hiccup — recovering:", wexc, flush=True)
                        time.sleep(0.1)
                self._keep_preloaded(get_next_song)

        finally:
            try:
                stream.__exit__(None, None, None)
            except Exception:
                pass

            with self.lock:
                self.current_process = None

            try:
                current.kill()
            except:
                pass


    def _request_hold(self, song_for_played=None):
        """Ring-fence (async): when the pipeline is dry and the current stream
        has ended, re-prepare the last known-good song in the BACKGROUND and
        hand it to the preload queue so the deck keeps producing AUDIO instead
        of going silent forever.

        Everything network-bound (yt-dlp extract, ffmpeg spawn) runs off the
        audio loop — the main loop just keeps retrying _advance and picks the
        hold song up the moment it is ready. A real (fresh) song that lands in
        the queue takes over the same way. Returns True when a prep launched.

        One hold prep is allowed in flight at a time; the caller paces requests
        so a dead link can't stack a wall of stuck background threads."""
        if self.stop_event.is_set():
            return False
        if self._hold_inflight:
            return False
        anchor = song_for_played
        if not isinstance(anchor, dict) or not anchor.get('link'):
            anchor = getattr(self, 'last_song', None)
        if not isinstance(anchor, dict) or not anchor.get('link'):
            anchor = getattr(self, 'current_song', None)
        if not isinstance(anchor, dict) or not anchor.get('link'):
            return False
        self._hold_inflight = True

        def _run():
            try:
                self.preload(dict(anchor))
            except Exception as e:
                print("Hold-song prepare failed:", e)
            finally:
                self._hold_inflight = False

        threading.Thread(target=_run, daemon=True).start()
        return True

    def _safe_advance(self, current, stream, crossfade_sec, effect=None, wait=False):
        """Advance to the next preloaded song, but NEVER let an internal
        exception (crossfade math, PortAudio write, process kill) kill the
        player thread and silently stop the DJ. Returns None on failure so
        the caller retries in the next loop pass — the deck stays alive."""
        try:
            return self._advance(current, stream, crossfade_sec,
                                 effect=effect, wait=wait)
        except Exception as exc:
            print("Advance hiccup — recovering:", exc, flush=True)
            return None

    def _reader_queue(self, process, maxsize=512):
        """Lazily spawn a background thread that keeps pulling process.stdout
        into a queue, so callers never block directly on a stalled pipe.

        The queue is deliberately deep (~48s of audio at 4096 frames/chunk):
        ffmpeg can burst a whole track far faster than realtime, so a shallow
        queue made the deck run on a 0.7s cushion — any network dip starved it
        and surfaced as endless "buffering". A deep queue lets ffmpeg read far
        ahead and ride out slow spells.

        The queue is keyed by the process OBJECT — never ``id(process)``:
        id() values get recycled as soon as a dead Popen is garbage-collected
        (CPython reuses the memory address), so an id()-keyed cache collides
        once a NEW process lands on an old address. The player then starts
        reading the PREVIOUS song's dead queue and "hears" an EOF (or the
        wrong audio) mid-song — which surfaces as _read_chunk firing after a
        few song cycles. Keying by the object ties each queue to its pipe for
        life; a finished queue is marked ``_done`` (not deleted) so callers
        keep seeing its real EOF instead of spawning a fresh pump each call."""
        # Preload and playback can request a reader concurrently. Make the
        # lookup and publication atomic so they cannot spawn two pumps on the
        # same pipe (which splits chunks and causes dropouts/premature EOF).
        with self._readers_lock:
            q = self._readers.get(process)
            if q is not None:
                return q
            if len(self._readers) > 64:
                for _p, _queue in list(self._readers.items()):
                    if getattr(_queue, '_done', False):
                        del self._readers[_p]
            q = _q.Queue(maxsize=maxsize)
            q._done = False
            self._readers[process] = q
        def _pump():
            total = 0
            reason = "?"
            try:
                while True:
                    chunk = process.stdout.read(self._CHUNK)
                    if not chunk:
                        reason = "eof"
                        break
                    total += len(chunk)
                    while True:
                        try:
                            q.put(chunk, timeout=0.5)
                            break
                        except _q.Full:
                            if process.poll() is not None or self.stop_event.is_set():
                                reason = f"abandon poll={process.poll()} stop={self.stop_event.is_set()}"
                                raise _q.Full
            except _q.Full:
                pass
            except Exception as _e:
                reason = f"exc {_e!r}"
            finally:
                if os.environ.get("DJ_PUMP_DEBUG"):
                    print(f"[pump pid={process.pid}] exit reason={reason} "
                          f"read={total/(44100*4):.1f}s", flush=True)
                try:
                    q.put(b"", timeout=1.0)  # EOF marker; wait for a slot like data
                except _q.Full:
                    pass  # genuinely abandoned: nobody will read the marker either
                q._done = True
        self._readers[process] = q
        threading.Thread(target=_pump, daemon=True).start()
        return q

    def _read_chunk(self, process, timeout=15.0):
        """Returns audio bytes, ``b""`` for a REAL end-of-stream, or ``None``
        when the producer is STILL ALIVE but quiet (network buffering/stall).

        Callers must only treat ``b""`` as 'this song is done, advance'. A
        silent-but-alive stream is not the end — conflating the two made a
        buffer hiccup skip songs (and could cascade)."""
        q = self._reader_queue(process)
        try:
            return q.get(timeout=timeout)
        except _q.Empty:
            if getattr(q, '_done', False):
                # The queue is finished and drained: a real end-of-stream.
                return b""
            if self._readers.get(process) is q:
                # The pump thread is still alive on this pipe: the song hasn't
                # ended, its stream is just not delivering bytes right now.
                if not getattr(q, '_stall_shown', False):
                    q._stall_shown = True
                    print("Buffering — waiting for stream data...", flush=True)
                return None
            # Replaced / unknown queue: treat as done.
            return b""

    @staticmethod
    def _fmt_hook(song):
        if song.get('play_start') and song.get('play_end'):
            return f"[{song['play_start']} - {song['play_end']}]"
        return ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Algorithmic DJ: mix agent-built sets.")
    parser.add_argument(
        "--resume", dest="resume_unplayed", action="store_true", default=None,
        help="resume unplayed tracks from the last Agent run (default)",
    )
    parser.add_argument(
        "--no-resume", dest="resume_unplayed", action="store_false",
        help="skip saved unplayed tracks and always select fresh agent sets",
    )
    parser.add_argument(
        "--resume-place", dest="resume_place", default=None,
        help="resume only unplayed tracks flagged with a PLACE_GENRES key "
             "(e.g. gym, party, study)",
    )
    args = parser.parse_args()
    dj = DJ(pool_size=40, top_n=15, resume_unplayed=args.resume_unplayed,
            resume_place=args.resume_place)
    print("Looking for the first batch of songs to play...")

    with dj._saved_lock:
        saved_count = len(dj._saved_set)
    if saved_count:
        first_song = dj.get_next_song()
        where = f" for '{args.resume_place}'" if args.resume_place else ""
        print(f"Found {saved_count} unplayed track(s) from the last Agent run{where} — "
              "resuming them before asking the Agent for a new set.")
    elif dj.resume_unplayed:
        first_song = None
        print("No unplayed tracks left from the last Agent run — waiting for "
              "the Agent to build a fresh set...")
    else:
        first_song = None
        print("Resume disabled — waiting for the Agent to build a fresh set...")

    dj.start(first_song, dj.get_next_song)
    try:
        while dj.player_thread.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        dj.stop()
    finally:
        dj.stop()