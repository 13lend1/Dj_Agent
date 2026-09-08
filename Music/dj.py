import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import time
import threading
import subprocess
import numpy as np
import yt_dlp
import sounddevice as sd

from preloaded_player import PreloadedPlayer
from DJ.agent import Agent
from songs import (
    get_random_song,
    take_preprocessed_batch,
    delete_unscored_songs,
    save_song_metadata,
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

    def __init__(self, pool_size=40, top_n=15, clip_length=33):
        super().__init__(pool_size=pool_size, top_n=top_n)
        self.agent = Agent()
        self.clip_length = clip_length
        self.current_clip_duration = None
        self._advance_fail_count = 0
        self._preload_lock = threading.Lock()
        self._preloading = False

    _CHUNK = 4096 * 2 * 2  # 4096 stereo int16 frames
    DEFAULT_TRANSITION = {"type": "crossfade", "crossfade_sec": 2.0, "note": "Default crossfade."}

    # ---- selection: linear model -> Agent (rank + hook windows) ----------

    def _fetch_batch(self):
        candidates = take_preprocessed_batch(n=self.pool_size)
        if not candidates:
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
        current = getattr(self, 'current_song', None)
        if not current:
            current = self.agent.last()
        playlist = self.agent.build_playlist(
            records, current=current, clip_length=self.clip_length
        ) if records else []

        # Agent-approved songs are persisted to the Songs table (the linear
        # model's prediction); a user rating overwrites it when played.
        for song in playlist:
            likeability = song.get('likeability')
            if likeability is not None:
                try:
                    save_song_metadata(song, likeability=likeability)
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

    # ---- audio: seek to the hook start -----------------------------------

    def prepare_song(self, song):
        """Seek-aware prepare: accepts a song dict (seeks to play_start_sec)
        or a plain URL. If the fast 'seek-before-input' fails, ffmpeg is retried
        without the seek so playback never starts with dead air."""
        if isinstance(song, dict):
            url = song['link']
            seek = song.get('play_start_sec')
        else:
            url = song
            seek = None

        options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'noplaylist': True,
        }

        for use_seek in ([seek] if seek else [None]):
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                    stream_url = info['url']

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
                        continue

                return process
            except Exception as e:
                if not use_seek:
                    raise
                # extraction likely failed, not the seek: retry without seek
                print("prepare_song retrying without seek:", e)

        raise RuntimeError(f"Could not prepare audio for {url}")

    def preload(self, song_info):
        if song_info is None:
            print("No song available to preload, fetching a random one.")
            song_info = get_random_song()

        if song_info is None:
            print("Could not find a song to preload.")
            return

        try:
            process = self.prepare_song(song_info)
            self.song_queue.put({
                **song_info,
                'process': process,
            })
            print(f"Preloaded: {song_info['name']} {self._fmt_hook(song_info)}")
        except Exception as e:
            print("Preload error:", e)

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
            self.preload(get_next_song())
        finally:
            with self._preload_lock:
                self._preloading = False

    # ---- playback ---------------------------------------------------------

    def _begin_song(self, song, current):
        start = song.get('play_start_sec')
        end = song.get('play_end_sec')
        with self.lock:
            self.current_process = current
            self.current_url = song['link']
            self.current_song = song
            self.current_title = song['name']
            self.current_length = song['duration']
            self.current_start_time = time.time()
            self.current_rating = None
            self.current_clip_duration = (
                (end - start) if (start is not None and end is not None) else None
            )
        self.agent.track(song)

    @staticmethod
    def _transition_secs(crossfade_sec):
        try:
            return float(crossfade_sec)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _cap_crossfade(clip, crossfade_sec):
        """Never let the fade swallow the whole hook: keep at least ~5s (or half
        the clip, whichever is smaller) of solo audio before the blend starts."""
        if not clip or clip <= 0:
            return crossfade_sec
        solo = min(5.0, clip / 2.0)
        return min(crossfade_sec, max(0.0, clip - solo))

    def _score_current_song(self):
        """Likeability stays the model's prediction for the song, unless the
        audience input a 0-9 rating — then rating/9 wins."""
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

    def _crossfade(self, out_process, in_process, stream, secs):
        """Equal-power crossfade from out_process into in_process over `secs`.

        Progress is measured by bytes actually written (== audio time consumed,
        since stream.write blocks on real playback), so the ramp stays correct
        regardless of how fast the pipes drain."""
        if not secs or secs <= 0:
            return
        bytes_per_sec = 44100 * 2 * 2
        fade_bytes = int(secs * bytes_per_sec)
        written = 0
        half_pi = np.pi / 2

        while not self.stop_event.is_set():
            t = 1.0 if fade_bytes <= 0 else min(written / fade_bytes, 1.0)

            a = out_process.stdout.read(self._CHUNK)
            b = in_process.stdout.read(self._CHUNK)

            if not a and not b:
                break
            if not a:
                stream.write(b)
                written += len(b) if isinstance(b, bytes) else 0
            elif not b:
                stream.write(a)
                written += len(a) if isinstance(a, bytes) else 0
            else:
                a = a[:len(a) // 4 * 4]
                b = b[:len(b) // 4 * 4]
                n = min(len(a), len(b))
                ba = np.frombuffer(a[:n], dtype=np.int16).astype(np.int32)
                bb = np.frombuffer(b[:n], dtype=np.int16).astype(np.int32)
                out_gain = np.cos(t * half_pi)
                in_gain = np.sin(t * half_pi)
                mix = np.clip(ba * out_gain + bb * in_gain, -32768, 32767).astype(np.int16)
                chunk = mix.tobytes()
                stream.write(chunk)
                written += len(chunk)

            if t >= 1.0:
                return

    def _advance(self, current, stream, crossfade_sec):
        """Move from the current (outgoing) process into the next: crossfade if
        requested, then kill the old process and hand the deck to the new song."""
        next_song = self.get_preloaded_song(timeout=20)
        if next_song is None:
            return None
        next_process = next_song['process']
        if crossfade_sec and crossfade_sec > 0:
            self._crossfade(current, next_process, stream, crossfade_sec)
        try:
            current.kill()
        except Exception:
            pass
        self._begin_song(next_song, next_process)
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
            print("Ctrl+C = stop\n")

            while not self.stop_event.is_set():

                transitions = (
                    (getattr(self, 'current_song', None) or {}).get('transition_out')
                    or self.DEFAULT_TRANSITION
                )
                crossfade_sec = self._transition_secs(transitions.get('crossfade_sec'))
                clip = self.current_clip_duration
                crossfade_sec = self._cap_crossfade(clip, crossfade_sec)

                if self.skip_event.is_set():

                    self.skip_event.clear()

                    with self.lock:
                        self.current_elapsed = time.time() - self.current_start_time

                    self._score_current_song()

                    if transitions.get('type') != 'cut':
                        print(f"Crossfading out: {transitions.get('note', '')}")

                    next_song = self._advance(current, stream, crossfade_sec)

                    if next_song is None:
                        print("No preloaded song available.")
                        self.stop()
                        break

                    current = next_song['process']
                    self._advance_fail_count = 0
                    print(f"Starting next song: {self.current_title} {self._fmt_hook(next_song)}")
                    self._keep_preloaded(get_next_song)
                    continue

                # As the hook window runs out, begin the suggested crossfade
                elapsed = time.time() - self.current_start_time
                if clip is not None and elapsed >= max(0.0, clip - crossfade_sec - 0.1):

                    with self.lock:
                        self.current_elapsed = elapsed

                    self._score_current_song()

                    if transitions.get('type') != 'cut':
                        print(f"\nCrossfading to next: {transitions.get('note', '')}")

                    next_song = self._advance(current, stream, crossfade_sec)

                    if next_song is None:
                        # preload is still warming up / pool refilling: keep the
                        # current song going and retry instead of killing the set
                        self._advance_fail_count += 1
                        if self._advance_fail_count >= 5:
                            print("No more songs available — the pool is dry.", flush=True)
                            self.stop()
                            break
                        print("Waiting for the next song to preload...", flush=True)
                        continue

                    current = next_song['process']
                    self._advance_fail_count = 0
                    print(f"\nNow playing: {self.current_title} {self._fmt_hook(next_song)}")
                    self._keep_preloaded(get_next_song)
                    continue

                data = current.stdout.read(self._CHUNK)

                if not data:

                    with self.lock:
                        self.current_elapsed = time.time() - self.current_start_time

                    self._score_current_song()

                    next_song = self._advance(current, stream, 0.0)

                    if next_song is None:
                        break

                    current = next_song['process']
                    print(f"\nNow playing: {self.current_title} {self._fmt_hook(next_song)}")
                    self._keep_preloaded(get_next_song)
                    continue

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

    @staticmethod
    def _fmt_hook(song):
        if song.get('play_start') and song.get('play_end'):
            return f"[{song['play_start']} - {song['play_end']}]"
        return ""


if __name__ == "__main__":
    dj = DJ(pool_size=40, top_n=15)
    print("Looking for the first batch of songs to play...")
    first_song = dj.get_next_song()
    dj.start(first_song, dj.get_next_song)
    try:
        while dj.player_thread.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        dj.stop()
    finally:
        dj.stop()