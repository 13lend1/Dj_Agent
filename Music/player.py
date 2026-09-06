import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import queue
import threading
import time
import yt_dlp
import subprocess
import sounddevice as sd
import traceback
from songs import (
    get_random_song,
    get_random_songs,
    save,
    save_preprocessed,
    preprocessed_count,
    take_preprocessed_batch,
)
from Model.linear_regression import LinearRegressionModel


class Player:

    def __init__(self, pool_size=100, top_n=20):
        self.song_queue = queue.Queue(maxsize=2)
        self.save_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.skip_event = threading.Event()
        self.batch = []
        self.batch_lock = threading.Lock()
        self.model = LinearRegressionModel()
        self.pool_size = pool_size
        self.top_n = top_n
        self.current_length = None
        self.current_elapsed = None
        self.current_process = None
        self.current_url = None
        self.current_rating = None
        self.lock = threading.Lock()

        threading.Thread(target=self._save_worker, daemon=True).start()
        threading.Thread(target=self._refill_worker, daemon=True).start()
        threading.Thread(target=self._batch_worker, daemon=True).start()

    def _refill_worker(self, low_water=None, refill_n=None, check_interval=5):
        # keeps the Preprocessed table topped up so there is always a fresh
        # ~pool_size candidate pool waiting for the next scored batch
        if low_water is None:
            low_water = self.pool_size
        if refill_n is None:
            refill_n = self.pool_size
        while not self.stop_event.is_set():
            try:
                if preprocessed_count() < low_water:
                    get_random_songs(n=refill_n, on_song=save_preprocessed)
            except Exception as e:
                print("Refill error:")
                traceback.print_exc()
            time.sleep(check_interval)

    def _batch_worker(self, check_interval=2):
        # keeps the play queue fed: takes a full Preprocessed batch (~pool_size),
        # scores it with the linear regression model, keeps the best top_n,
        # saves those with predicted likeability, and chains into the next batch
        while not self.stop_event.is_set():
            try:
                with self.batch_lock:
                    queued = len(self.batch)
                if queued >= self.top_n - 5:
                    time.sleep(check_interval)
                    continue

                wait_start = time.time()
                last_print = 0

                while preprocessed_count() < self.pool_size and not self.stop_event.is_set():
                    count = preprocessed_count()
                    now = time.time()
                    if now - last_print >= 15:
                        print(f"Refilling candidate pool... ({count}/{self.pool_size} in Preprocessed)")
                        last_print = now
                    # don't wait forever for a full pool: start a smaller
                    # batch once it has enough songs to score meaningfully
                    
                    if now - wait_start > 180 and count >= 10:
                        break
                    time.sleep(check_interval)

                if self.stop_event.is_set():
                    return

                self._fetch_batch()
            except Exception as e:
                print("Batch error:")
                traceback.print_exc()
                time.sleep(check_interval)

    def _fetch_batch(self):
        # delete the whole pool (~pool_size) out of Preprocessed, then pick the best
        candidates = take_preprocessed_batch(n=self.pool_size)
        if not candidates:
            return

        try:
            self.model.fit()
            top = self.model.select_best(candidates, n=self.top_n)
            records = top.to_dict('records')
        except Exception as e:
            print("Scoring unavailable, selecting randomly instead:", e)
            records = random.sample(candidates, min(self.top_n, len(candidates)))

        new_batch = []
        for song in records:
            predicted = song.get('likeability')
            if predicted is not None:
                song['score'] = round(float(predicted), 4)
                try:
                    save(song)  # land in Songs with the predicted likeability
                except Exception as e:
                    print("Save predicted likeability failed:", e)
            new_batch.append(song)

        with self.batch_lock:
            self.batch.extend(new_batch)
        print(f"Queued batch of {len(new_batch)} songs (predicted likeability).")

    def get_next_song(self):
        while not self.stop_event.is_set():
            with self.batch_lock:
                if self.batch:
                    return self.batch.pop(0)
            time.sleep(0.5)
        return None

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

    def prepare_song(self, url):

        options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'noplaylist': True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info['url']

        process = subprocess.Popen(
            [
                'ffmpeg',
                '-i', stream_url,
                '-f', 's16le',
                '-acodec', 'pcm_s16le',
                '-ar', '44100',
                '-ac', '2',
                '-'
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0
        )

        return process

    def preload(self, song_info):
        if song_info is None:
            print("No song available to preload, fetching a random one.")
            song_info = get_random_song()

        if song_info is None:
            print("Could not find a song to preload.")
            return

        try:
            process = self.prepare_song(song_info['link'])
            self.song_queue.put({
                **song_info,
                'process': process,
            })
            print(f"Preloaded: {song_info['name']}")
        except Exception as e:
            print("Preload error:", e)

    def start_preload(self, get_next_song):

        if self.stop_event.is_set():
            return

        next_song = get_next_song()

        if self.stop_event.is_set():
            return

        threading.Thread(
            target=self.preload,
            args=(next_song,),
            daemon=True
        ).start()

    def get_preloaded_song(self, timeout=10):
        try:
            return self.song_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def play(self, first_song, get_next_song):

        current = self.prepare_song(first_song['link'])
        with self.lock:
            self.current_process = current
            self.current_song = first_song
            self.current_url = first_song['link']
            self.current_title = first_song['name']
            self.current_length = first_song['duration']
            self.current_start_time = time.time()
        # Preload next song
        print(f"Now playing: {self.current_title}")
        self.start_preload(get_next_song)

        print("\nDJ started!")
        print("n = next")
        print("s = stop")
        print("Ctrl+C = stop\n")

        try:

            with sd.RawOutputStream(
                samplerate=44100,
                channels=2,
                dtype='int16',
                blocksize=4096
            ) as stream:

                while not self.stop_event.is_set():

                    if self.skip_event.is_set():

                        self.skip_event.clear()

                        with self.lock:
                            self.current_elapsed = time.time() - self.current_start_time

                        self._score_current_song()

                        current.kill()

                        current_song = self.get_preloaded_song()

                        if current_song is None:
                            print("No preloaded song available.")
                            self.stop()
                            break

                        current = current_song['process']

                        with self.lock:
                            self.current_process = current
                            self.current_url = current_song['link']
                            self.current_song = current_song
                            self.current_title = current_song['name']
                            self.current_length = current_song['duration']
                            self.current_start_time = time.time()
                            self.current_rating = None

                        print("Starting next song...")
                        self.start_preload(get_next_song)
                        continue
                    # Read audio
                    data = current.stdout.read(
                        4096 * 2 * 2
                    )

                    # Song ended

                    if not data:

                        with self.lock:
                            self.current_elapsed = time.time() - self.current_start_time

                        self._score_current_song()

                        current.kill()

                        current_song = self.get_preloaded_song()

                        if current_song is None:
                            break

                        current = current_song['process']

                        with self.lock:
                            self.current_process = current
                            self.current_url = current_song['link']
                            self.current_song = current_song
                            self.current_title = current_song['name']
                            self.current_length = current_song['duration']
                            self.current_start_time = time.time()
                            self.current_rating = None

                        print(f"\nNow playing: {current_song['name']}")
                        self.start_preload(get_next_song)
                        continue

                    stream.write(data)

        finally:
            with self.lock:
                self.current_process = None

            try:
                current.kill()
            except:
                pass

    def start(self, first_song, get_next_song):

        while first_song is None:
            if self.stop_event.is_set():
                return None
            first_song = get_next_song()

        if self.stop_event.is_set():
            return None

        self.player_thread = threading.Thread(
            target=self.play,
            args=(first_song, get_next_song),
            daemon=True
        )

        self.player_thread.start()

        self.keyboard_thread = threading.Thread(
            target=self.keyboard_control,
            daemon=True
        )

        self.keyboard_thread.start()

        return self.player_thread

    def stop(self):
        print("\nStopping DJ...")

        self.stop_event.set()

        # Kill the current FFmpeg process.
        # This also releases stdout.read() if it is blocked.
        with self.lock:
            if self.current_process:
                try:
                    self.current_process.kill()
                except:
                    pass
                try:
                    self.current_process.stdout.close()
                except:
                    pass

        while not self.song_queue.empty():
            try:
                song_info = self.song_queue.get_nowait()
                try:
                    song_info['process'].kill()
                except:
                    pass
                try:
                    song_info['process'].stdout.close()
                except:
                    pass
            except queue.Empty:
                break

    def rate_current(self, score):
        with self.lock:
            if not self.current_url:
                print("No song currently playing to rate.")
                return
            self.current_rating = score
            title = self.current_title

        print(f"Rating queued for '{title}': {score}")

    def _score_current_song(self):
        with self.lock:
            song = dict(self.current_song)
            seconds_listened = self.current_elapsed
            score = self.current_rating

        if not song.get('duration'):
            return

        listen_fraction = min(seconds_listened / (song['duration'] / 1000), 1.0)

        if score is not None:
            likeability = round(score / 9, 1)
        else:
            likeability = listen_fraction

        print(f"'{song['name']}' likeability: {round(likeability, 2)}")
        song['score'] = likeability
        self.save_queue.put(song)

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

        print("n = next | s = stop | 0-9 = rate song (0.0-1.0)")

        try:
            if not is_windows:
                tty.setcbreak(fd)

            while not self.stop_event.is_set():

                if is_windows:
                    if msvcrt.kbhit():
                        key = msvcrt.getwch().lower()
                    else:
                        time.sleep(0.05)
                        continue
                else:
                    # Check whether a key has been pressed
                    ready, _, _ = select.select([sys.stdin], [], [], 0.1)

                    if not ready:
                        continue

                    key = sys.stdin.read(1).lower()

                if key == 'n':
                    print("\nSkipping...")
                    self.skip()

                elif key == 's':
                    print("\nStopping...")
                    self.stop()

                elif key == '\x03':  # Ctrl+C
                    print("\nStopping...")
                    self.stop()

                elif key.isdigit():
                    score = int(key)
                    self.rate_current(score)

        finally:
            # ALWAYS restore normal terminal behaviour (Unix only)
            if not is_windows:
                termios.tcsetattr(
                    fd,
                    termios.TCSADRAIN,
                    old_settings
                )


if __name__ == "__main__":
    dj = Player(pool_size=100, top_n=20)
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