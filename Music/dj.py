import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import queue
import time
import threading
import subprocess
import argparse
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

    _CHUNK = 4096 * 2 * 2  # 4096 stereo int16 frames
    MIN_CROSSFADE = 2.0     # anything shorter sounds like a hard cut
    POOL_DRY_TIMEOUT = 120.0  # how long to keep the current song playing while the
                             # preload/batch pipeline catches up before giving up
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
        candidates = take_preprocessed_batch(n=self.pool_size, place=getattr(self, 'place', None))
        if not candidates:
            return

        try:
            from duplicates import played_ids
            played = played_ids(c.get('id') for c in candidates)
        except Exception as e:
            print("Played-check failed, treating batch as new:", e)
            played = set()
        if played:
            candidates = [c for c in candidates if c.get('id') not in played]
        if not candidates:
            print("Batch contained only already-played songs; skipping.")
            return

        before = len(candidates)
        candidates = [c for c in candidates
                      if not _title_matches_genre(c.get('name') or c.get('title'), c.get('genre'))]
        if len(candidates) < before:
            print(f"Filtered {before - len(candidates)} genre-named song(s) from batch.")
        if not candidates:
            print("All candidates were genre-named songs; skipping batch.")
            return

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

    def _mark_played(self, song):
        """Flag a track as played (start-to-end) in the newest Agent run so the
        next DJ start resumes from where the set actually left off."""
        song_id = song.get("id") if isinstance(song, dict) else None
        try:
            mark_song_played(song_id)
        except Exception as e:
            print("Could not flag song as played:", e)

    # ---- audio: seek to the hook start -----------------------------------

    def prepare_song(self, song, seek_to=None):
        """Seek-aware prepare: accepts a song dict (seeks to play_start_sec, or
        to `seek_to` seconds into the full track when given) or a plain URL.
        If the fast 'seek-before-input' fails, ffmpeg is retried without the
        seek so playback never starts with dead air."""
        if isinstance(song, dict):
            url = song['link']
            seek = seek_to if seek_to is not None else song.get('play_start_sec')
        else:
            url = song
            seek = seek_to

        options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'noplaylist': True,
            'socket_timeout': 30,
            'retries': 3,
            'impersonate': _IMPERSONATE,
            'force_ipv4': True,
            'js_runtimes': _DENO,
            'remote_components': ['ejs:github'],
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/126.0 Safari/537.36',
                'Accept': '*/*',
            },
        }

        last_exc = None
        for use_seek in ([seek, None] if seek else [None]):
            if self.stop_event.is_set():
                raise RuntimeError(f"Stopped while preparing {url}")
            try:
                _info_holder = {}

                def _run_extract():
                    try:
                        with yt_dlp.YoutubeDL(options) as ydl:
                            _info_holder['info'] = ydl.extract_info(url, download=False)
                    except BaseException as exc:
                        _info_holder['exc'] = exc

                _extract_thread = threading.Thread(target=_run_extract, daemon=True)
                _extract_thread.start()
                _extract_thread.join(timeout=60)
                if _extract_thread.is_alive():
                    raise TimeoutError(f"yt-dlp extract timed out for {url}")
                if 'exc' in _info_holder:
                    raise _info_holder['exc']
                stream_url = _info_holder['info'].get('url')
                if not stream_url:
                    raise RuntimeError(f"No stream URL found for {url}")

                process = subprocess.Popen(
                    ['ffmpeg'] +
                    (['-ss', str(use_seek)] if use_seek else []) +
                    ['-i', stream_url,
                     '-f', 's16le',
                     '-acodec', 'pcm_s16le',
                     '-ar', '44100',
                     '-ac', '2',
                     '-'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0
                )

                if use_seek:
                    # give ffmpeg a moment: if input seeking is unsupported the
                    # process exits almost immediately -> retry without the seek
                    time.sleep(0.5)
                    if process.poll() is not None:
                        raise RuntimeError("ffmpeg exited during seek-based input")

                return process
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                last_exc = e
                if not use_seek:
                    raise
                # extraction likely failed, not the seek: retry without seek
                print("prepare_song retrying without seek:", e)

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
        audience input a 0-9 rating — then rating/9 wins."""
        with self.lock:
            song = dict(self.current_song)
            score = self.current_rating

        self._record_genre(song)

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
        # would fight the pump thread for the same pipe.
        try:
            probe = self._read_chunk(out_process, timeout=15.0)
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
        first_iter = True

        while not self.stop_event.is_set():
            t = 1.0 if fade_bytes <= 0 else min(written / fade_bytes, 1.0)

            if first_iter:
                a = probe
                first_iter = False
            else:
                try:
                   a = self._read_chunk(out_process, timeout=15.0)
                   if a is None:
                       a = b""
                except (OSError, ValueError):
                    a = b""
            try:
               b = self._read_chunk(in_process, timeout=15.0)
               if b is None:
                   b = b""
            except (OSError, ValueError):
                b = b""

            if not a and not b:
                break
            a = a[:len(a) // 4 * 4]
            b = b[:len(b) // 4 * 4]
            nframes = max(len(a), len(b)) // 4
            n_samples = nframes * 2
            ba = np.zeros(n_samples, dtype=np.int32)
            bb = np.zeros(n_samples, dtype=np.int32)
            if a:
                ba[:len(a) // 2] = np.frombuffer(a, dtype=np.int16).astype(np.int32)
            if b:
                bb[:len(b) // 2] = np.frombuffer(b, dtype=np.int16).astype(np.int32)

            if len(a):
                rms_a = float(np.sqrt(
                    np.mean(ba[:len(a) // 2].astype(np.float64) ** 2)
                )) + 1e-8
                out_level = rms_a if out_level is None else ema * rms_a + (1 - ema) * out_level
            if len(b):
                rms_b = float(np.sqrt(
                    np.mean(bb[:len(b) // 2].astype(np.float64) ** 2)
                )) + 1e-8
                in_level = rms_b if in_level is None else ema * rms_b + (1 - ema) * in_level
            if in_level is not None:
                reference = out_level if out_level is not None else ref_level
                target = min(max(reference / in_level, min_gain), max_gain)
                gain_in = gain_in + 0.3 * (target - gain_in)

            if dead_out:
                bt = min(1.0, t / fast_frac)
                out_gain = 0.0
                in_gain = np.sin(bt * half_pi)
            else:
                out_gain = np.cos(t * half_pi)
                in_gain = np.sin(t * half_pi)

            mix = ba * out_gain + bb * (in_gain * gain_in)
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
            written += len(chunk)

            if t >= 1.0:
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

                    self._score_current_song()

                    if not self.replay_event.is_set():
                        if transitions.get('type') != 'cut':
                            print(f"Crossfading out: {transitions.get('note', '')}")

                    next_song = self._advance(current, stream, crossfade_sec, effect=effect, wait=True)

                    if next_song is None:
                        print("No preloaded song available.")
                        self.stop()
                        break

                    current = next_song['process']
                    self._advance_fail_count = 0
                    self._dry_started = None
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
                        with self.lock:
                            self.current_elapsed = elapsed
                        song_for_played = dict(self.current_song) if self.current_song is not None else None
                        self._score_current_song()
                        if not self.replay_event.is_set():
                            if transitions.get('type') != 'cut':
                                print(f"\nCrossfading to next: {transitions.get('note', '')}")
                        if song_for_played:
                            self._mark_played(song_for_played)

                    next_song = self._advance(current, stream, crossfade_sec, effect=effect)

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
                        if time.time() - self._dry_started >= self.POOL_DRY_TIMEOUT:
                            print("No more songs available — the pool is dry.", flush=True)
                            self.stop()
                            break
                        self._keep_preloaded(get_next_song)
                    else:
                        self._dry_started = None
                        self._advance_fail_count = 0
                        current = next_song['process']
                        print(f"\nNow playing: {self.current_title} {self._fmt_hook(next_song)}")
                        self._keep_preloaded(get_next_song)
                        continue

                try:
                    data = self._read_chunk(current, timeout=15.0)
                except (OSError, ValueError):
                    data = b""

                if data is None:
                    # Producer is alive but quiet (buffering / network stall).
                    # This is NOT the end of the song: keep this song on deck
                    # and wait rather than advancing. Giving up here is what
                    # made slow streams get skipped a few seconds in.
                    if self.stop_event.is_set():
                        break
                    self._keep_preloaded(get_next_song)
                    time.sleep(0.25)
                    continue

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
                    if early:
                        print(f"Stream ended early ({early_elapsed:.1f}s of a "
                              f"{clip}s window) — treating as a failed link, "
                              "it can be refetched later.", flush=True)
                    prev_last = self.last_song

                    if not self._transition_scored:
                        self._transition_scored = True
                        with self.lock:
                            self.current_elapsed = time.time() - self.current_start_time
                        self._score_current_song()

                    fade = crossfade_sec if (crossfade_sec and crossfade_sec > 0) else self.DEFAULT_TRANSITION['crossfade_sec']
                    next_song = self._advance(current, stream, fade, effect=effect)

                    if next_song is None:
                        # The song has truly ended but nothing is preloaded yet.
                        # Keep waiting for the pipeline for a grace period instead
                        # of dying the instant the queue happens to be empty.
                        if self._dry_started is None:
                            self._dry_started = time.time()
                            print("Song ended — waiting for the next song to "
                                  "preload...", flush=True)
                        if time.time() - self._dry_started >= self.POOL_DRY_TIMEOUT:
                            print("No more songs available — the pool is dry.", flush=True)
                            self.stop()
                            break
                        self._keep_preloaded(get_next_song)
                        time.sleep(2.0)
                        continue
                    if song_for_played and not early:
                        self._mark_played(song_for_played)
                    if early:
                        self.last_song = prev_last

                    self._dry_started = None
                    self._advance_fail_count = 0
                    current = next_song['process']
                    print(f"\nNow playing: {self.current_title} {self._fmt_hook(next_song)}")
                    self._keep_preloaded(get_next_song)
                    continue

                if data:
                    if self._play_gain != 1.0:
                        raw = np.frombuffer(data, dtype=np.int16).astype(np.float64)
                        data = self._soft_limit(raw * self._play_gain).tobytes()
                    stream.write(data)
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


    def _reader_queue(self, process, maxsize=8):
        """Lazily spawn a background thread that keeps pulling process.stdout
        into a queue, so callers never block directly on a stalled pipe.

        The queue is keyed by the process OBJECT — never ``id(process)``:
        id() values get recycled as soon as a dead Popen is garbage-collected
        (CPython reuses the memory address), so an id()-keyed cache collides
        once a NEW process lands on an old address. The player then starts
        reading the PREVIOUS song's dead queue and "hears" an EOF (or the
        wrong audio) mid-song — which surfaces as _read_chunk firing after a
        few song cycles. Keying by the object ties each queue to its pipe for
        life, and the pump prunes its own entry as soon as the stream ends."""
        if not hasattr(self, '_readers'):
            self._readers = {}
        q = self._readers.get(process)
        if q is not None:
            return q
        q = _q.Queue(maxsize=maxsize)
        def _pump():
            # Backpressure matters here: ffmpeg can decode faster than realtime,
            # so the pump MUST block on a full queue (which in turn fills
            # ffmpeg's stdout pipe and paces it to the consumer). Dropping
            # chunks on a full queue silently skips audio and makes songs end
            # seconds after they start. We only bail out when the queue stays
            # full for a full second — meaning the consumer has abandoned this
            # stream — so no stuck daemon thread / pipe is leaked per song.
            try:
                while True:
                    chunk = process.stdout.read(self._CHUNK)
                    if not chunk:
                        break
                    q.put(chunk, timeout=1.0)  # backpressure (blocks, paces ffmpeg)
            except _q.Full:
                pass  # abandoned: nobody drained us for >1s, stop pumping
            except Exception:
                pass
            finally:
                try:
                    q.put(b"", timeout=1.0)  # EOF marker; wait for a slot like data
                except _q.Full:
                    pass  # genuinely abandoned: nobody will read the marker either
                self._readers.pop(process, None)
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
            if self._readers.get(process) is q:
                # The pump thread is still alive on this pipe: the song hasn't
                # ended, its stream is just not delivering bytes right now.
                if not getattr(q, '_stall_shown', False):
                    q._stall_shown = True
                    print("Buffering — waiting for stream data...", flush=True)
                return None
            # The pump already exited: it queued a b"" EOF marker (real end)
            # before its own cleanup, or gave up on an abandoned stream.
            # Either way this stream is done.
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