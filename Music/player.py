import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import queue
import threading
import time
import yt_dlp
import subprocess
import sounddevice as sd
from Music.songs import (
    save,
)


class Player:

    def __init__(self):
        self.save_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.skip_event = threading.Event()
        self.replay_event = threading.Event()
        self.restart_event = threading.Event()
        self.seek_forward_event = threading.Event()
        self.seek_backward_event = threading.Event()
        self.pause_event = threading.Event()
        self._pause_clock = None
        self.last_song = None
        self.current_song = None
        self.current_length = None
        self.current_elapsed = None
        self.current_process = None
        self.current_url = None
        self.current_rating = None
        self.lock = threading.Lock()
        self._last_scored_song_id = None
        self._sink = None  # live output stream (StreamSink for the web UI)

        threading.Thread(target=self._save_worker, daemon=True).start()
        self._ensure_genre_tables()

    def _ensure_genre_tables(self):
        """Create the per-genre 'played songs' tables so every genre has one."""
        try:
            from duplicates import genre_table
            from songs import GENRES, ARTISTS
            for genre in set(list(GENRES) + list(ARTISTS.values())):
                try:
                    genre_table(genre)
                except Exception:
                    pass
        except Exception as e:
            print("Failed to ensure genre tables:", e)

    def _record_genre(self, song):
        """Record a played song into its genre table so it won't be played twice."""
        genre = song.get('genre')
        if not genre or not song.get('id'):
            return
        try:
            from duplicates import save_genre
            save_genre(song, genre)
        except Exception as e:
            print("Save genre failed:", e)

    def _save_worker(self):
        while True:
            song = self.save_queue.get()
            if song is None:
                break
            try:
                save(song)
            except Exception as e:
                print("Save error:", e)

    def skip(self):
        self.skip_event.set()

    def replay(self):
        self.replay_event.set()

    def restart(self):
        self.restart_event.set()

    def seek_forward(self):
        """Skip 5s ahead (falls back to next song at the end of a track)."""
        self.seek_forward_event.set()

    def seek_backward(self):
        """Skip 5s back (falls back to the previous song at the start)."""
        self.seek_backward_event.set()

    def pause(self):
        """Freeze the deck: the play loop stops writing audio and the elapsed
        clock stands still. Resume shifts the song's start time forward by the
        paused duration, so every `time.time() - current_start_time` reading
        (progress, hook window, scoring) picks up exactly where it left off."""
        if self.pause_event.is_set():
            return
        with self.lock:
            self._pause_clock = time.time()
        self.pause_event.set()
        sink = getattr(self, '_sink', None)
        if sink is not None and hasattr(sink, 'note_paused'):
            sink.note_paused(True)

    def resume(self):
        if not self.pause_event.is_set():
            return
        with self.lock:
            if self._pause_clock is not None and self.current_start_time is not None:
                self.current_start_time += time.time() - self._pause_clock
            self._pause_clock = None
        # Re-anchor wall-clock pacing BEFORE the loop wakes, otherwise the first
        # post-pause write sees the whole paused span as lag and burst-flushes
        # the audio buffered during the pause.
        sink = getattr(self, '_sink', None)
        if sink is not None and hasattr(sink, 'resync_clock'):
            sink.resync_clock()
        if sink is not None and hasattr(sink, 'note_paused'):
            sink.note_paused(False)
        self.pause_event.clear()

    def toggle_pause(self):
        if self.pause_event.is_set():
            self.resume()
        else:
            self.pause()

    @property
    def paused(self):
        return self.pause_event.is_set()

    def elapsed_now(self):
        """Seconds into the current song, paused time excluded, or None when
        nothing is playing. Never holds the lock while the caller does."""
        with self.lock:
            return self._elapsed_locked()

    def _elapsed_locked(self):
        start = getattr(self, 'current_start_time', None)
        if start is None:
            return None
        total = time.time() - start
        if self.pause_event.is_set() and self._pause_clock is not None:
            total -= time.time() - self._pause_clock
        return max(0.0, total)

    def prepare_song(self, url, seek_to=None):

        options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'noplaylist': True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info['url']

        cmd = ['ffmpeg']
        if seek_to is not None and seek_to > 0:
            cmd += ['-ss', str(seek_to)]
        cmd += [
            '-i', stream_url,
            '-f', 's16le',
            '-acodec', 'pcm_s16le',
            '-ar', '44100',
            '-ac', '2',
            '-'
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0
        )

        return process

    def play(self, first_song, get_next_song, sink=None):

        current_song = first_song
        current = self.prepare_song(current_song['link'])
        with self.lock:
            self.current_process = current
            self.current_song = current_song
            self.current_url = current_song['link']
            self.current_title = current_song['name']
            self.current_length = current_song['duration']
            self.current_start_time = time.time()
            self.current_rating = None

        print(f"Now playing: {self.current_title}")

        print("\nDJ started!")
        print("n = next")
        print("s = stop")
        print("r = replay the song that played before this one")
        print("a = restart current song from the beginning")
        print("<- / -> = seek back / forward 5 seconds")
        print("Ctrl+C = stop\n")

        try:

            with sd.RawOutputStream(
                samplerate=44100,
                channels=2,
                dtype='int16',
                blocksize=4096
            ) as stream:

                while not self.stop_event.is_set():

                    if self.restart_event.is_set():

                        self.restart_event.clear()

                        with self.lock:
                            song = dict(self.current_song)

                        current.kill()

                        current = self.prepare_song(song['link'])

                        with self.lock:
                            self.current_process = current
                            self.current_start_time = time.time()
                            self.current_elapsed = None

                        print(f"\nRestarting from the beginning: {song['name']}")
                        continue

                    if self.replay_event.is_set():

                        self.replay_event.clear()

                        with self.lock:
                            replay_song = dict(self.last_song) if self.last_song else None

                        if replay_song is None:
                            print("\nNo previous song to replay.")
                            continue

                        self._score_replay(replay_song)

                        current.kill()

                        current = self.prepare_song(replay_song['link'])

                        with self.lock:
                            self.last_song = dict(self.current_song) if self.current_song is not None else None
                            self.current_process = current
                            self.current_url = replay_song['link']
                            self.current_song = replay_song
                            self.current_title = replay_song['name']
                            self.current_length = replay_song['duration']
                            self.current_start_time = time.time()
                            self.current_elapsed = None
                            self.current_rating = None

                        print(f"\nNow playing (replayed): {replay_song['name']}")
                        continue

                    if self.seek_forward_event.is_set() or self.seek_backward_event.is_set():

                        seek_fwd = self.seek_forward_event.is_set()
                        self.seek_forward_event.clear()
                        self.seek_backward_event.clear()

                        with self.lock:
                            song = dict(self.current_song)
                            elapsed = time.time() - self.current_start_time

                        offset = 5 if seek_fwd else -5
                        target = elapsed + offset
                        length = song.get('duration')

                        if seek_fwd:
                            # can't seek past the end -> skip to the next song
                            if length and target >= length / 1000:
                                print("\nAt end of song — skipping to next...")
                                self.skip()
                                continue
                            target = max(0.0, target)
                        else:
                            # can't seek back beyond the start -> go to previous song
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

                        current = self.prepare_song(song['link'], seek_to=target)

                        with self.lock:
                            self.current_process = current
                            self.current_start_time = time.time() - target
                            self.current_elapsed = None

                        print(f"\nSeeked to {target:.1f}s: {song['name']}")
                        continue

                    if self.skip_event.is_set():

                        self.skip_event.clear()

                        if self.stop_event.is_set():
                            break

                        current_song, current = self._next_play(current, get_next_song, end_reason='skipped')

                        if current_song is None:
                            print("No more songs available.")
                            self.stop()
                            break

                        print(f"\nNow playing: {self.current_title}")
                        continue

                    try:
                        data = current.stdout.read(
                            4096 * 2 * 2
                        )
                    except (OSError, ValueError):
                        data = b""

                    if not data:

                        if self.stop_event.is_set():
                            break

                        current_song, current = self._next_play(current, get_next_song)

                        if current_song is None:
                            break

                        print(f"\nNow playing: {self.current_title}")
                        continue

                    stream.write(data)

        finally:
            try:
                with self.lock:
                    song = dict(self.current_song) if self.current_song is not None else None
                if song is not None and song.get('id') != self._last_scored_song_id:
                    self._score_current_song(end_reason='interrupted')
            except Exception as e:
                print("Interrupted-song scoring failed:", e)

            with self.lock:
                self.current_process = None

            try:
                current.kill()
            except:
                pass

    def start(self, first_song, get_next_song, keyboard=True, sink=None):

        while first_song is None:
            if self.stop_event.is_set():
                return None
            first_song = get_next_song()

        if self.stop_event.is_set():
            return None

        self.player_thread = threading.Thread(
            target=self.play,
            args=(first_song, get_next_song, sink),
            daemon=True
        )

        self.player_thread.start()

        if keyboard:
            self.keyboard_thread = threading.Thread(
                target=self.keyboard_control,
                daemon=True
            )

            self.keyboard_thread.start()

        return self.player_thread

    def stop(self):
        print("\nStopping DJ...")

        self.stop_event.set()

        with self.lock:
            if self.current_process:
                try:
                    self.current_process.kill()
                except:
                    pass

    def rate_current(self, score):
        with self.lock:
            if not self.current_url:
                print("No song currently playing to rate.")
                return
            self.current_rating = score
            title = self.current_title

        print(f"Rating queued for '{title}': {score}")

    def _score_current_song(self, end_reason='interrupted'):
        with self.lock:
            song = dict(self.current_song)
            seconds_listened = self.current_elapsed
            if seconds_listened is None and self.current_start_time is not None:
                seconds_listened = time.time() - self.current_start_time
            score = self.current_rating

        self._record_genre(song)

        liked = None
        likeability = None
        if score is not None:
            liked = score if score in (0, 1) else None
            likeability = 1.0 if liked == 0 else 0.0
        elif song.get('duration') and seconds_listened is not None:
            likeability = min(seconds_listened / (song['duration'] / 1000), 1.0)

        skipp = None
        if end_reason == 'skipped' and seconds_listened is not None:
            skipp = round(seconds_listened, 2)

        print(f"'{song['name']}' end_reason: {end_reason}, liked: {liked}, skipp: {skipp}")
        song['likeability'] = likeability
        song['liked'] = liked
        song['skipp'] = skipp
        song['end_reason'] = end_reason
        song['replayed'] = 0
        self.save_queue.put(song)
        self._last_scored_song_id = song.get('id')

    def _score_replay(self, song):
        """Update for a replayed song: a deliberate replay is the strongest like signal."""
        with self.lock:
            song = dict(song)

        self._record_genre(song)

        print(f"'{song['name']}' replayed — liked: 0 (like)")
        song['likeability'] = 1.0
        song['liked'] = 0
        song['skipp'] = None
        song['end_reason'] = 'finished'
        song['replayed'] = 1
        self.save_queue.put(song)
        self._last_scored_song_id = song.get('id')

    def _next_play(self, current, get_next_song, end_reason='finished'):
        """Settle the outgoing song and hand back (song, process) to play next,
        tracking the outgoing song as the previous one. Returns (None, None)
        when there is nothing left to play."""
        with self.lock:
            self.current_elapsed = time.time() - self.current_start_time

        try:
            current.kill()
        except Exception:
            pass

        self._score_current_song(end_reason=end_reason)

        song = get_next_song()
        if song is None:
            return None, None

        process = self.prepare_song(song['link'])

        with self.lock:
            self.last_song = dict(self.current_song) if self.current_song is not None else None
            self.current_process = process
            self.current_url = song['link']
            self.current_song = song
            self.current_title = song['name']
            self.current_length = song['duration']
            self.current_start_time = time.time()
            self.current_rating = None

        return song, process

    def keyboard_control(self):
        is_windows = sys.platform.startswith('win')

        if is_windows:
            import msvcrt
        else:
            import termios
            import tty
            import select

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)

        print("n = next | s = stop | r = replay previous song | a = restart song | <- / -> = seek back / forward 5s | 0 = like | 1 = dislike")

        try:
            if not is_windows:
                tty.setcbreak(fd)

            while not self.stop_event.is_set():

                if is_windows:
                    if not msvcrt.kbhit():
                        time.sleep(0.05)
                        continue
                    key = msvcrt.getwch()
                    # arrow keys come as a two-char sequence (prefix + direction)
                    if key in ('\xe0', '\x00'):
                        arrow = msvcrt.getwch()
                        if arrow == 'M':  # right arrow
                            print("\nSeeking forward 5s...")
                            self.seek_forward()
                        elif arrow == 'K':  # left arrow
                            print("\nSeeking backward 5s...")
                            self.seek_backward()
                        continue
                    key = key.lower()
                else:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.1)

                    if not ready:
                        continue

                    key = sys.stdin.read(1)
                    if key == '\x1b':
                        # escape sequence: consume '[' then the direction byte
                        ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                        if ready:
                            seq1 = sys.stdin.read(1)
                        else:
                            continue
                        ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                        if not ready:
                            continue
                        seq2 = sys.stdin.read(1)
                        if seq2 == 'C':  # right arrow
                            print("\nSeeking forward 5s...")
                            self.seek_forward()
                        elif seq2 == 'D':  # left arrow
                            print("\nSeeking backward 5s...")
                            self.seek_backward()
                        continue
                    key = key.lower()

                if key == 'n':
                    print("\nSkipping...")
                    self.skip()

                elif key == 's':
                    print("\nStopping...")
                    self.stop()

                elif key == 'r':
                    print("\nReplaying the song that played before this one...")
                    self.replay()

                elif key == 'a':
                    print("\nRestarting current song from the beginning...")
                    self.restart()

                elif key == '\x03':  # Ctrl+C
                    print("\nStopping...")
                    self.stop()

                elif key in ('0', '1'):
                    score = int(key)
                    self.rate_current(score)

        finally:
            if not is_windows:
                termios.tcsetattr(
                    fd,
                    termios.TCSADRAIN,
                    old_settings
                )
