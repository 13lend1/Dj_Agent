import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import queue
import threading
import time
import sounddevice as sd
import traceback
from songs import (
    get_random_song,
    get_random_songs,
    save_preprocessed,
    save_song_metadata,
    delete_unscored_songs,
    preprocessed_count,
    take_preprocessed_batch,
)
from player import Player


class PreloadedPlayer(Player):

    def __init__(self, pool_size=20, top_n=8):
        super().__init__()
        self.song_queue = queue.Queue(maxsize=2)
        self.batch = []
        self.batch_lock = threading.Lock()
        self.pool_size = pool_size
        self.top_n = top_n
        self.model = None  # lazy import to avoid circular dependency at module load

        threading.Thread(target=self._refill_worker, daemon=True).start()
        threading.Thread(target=self._batch_worker, daemon=True).start()

    def _load_model(self):
        if self.model is None:
            from Model.linear_regression import LinearRegressionModel
            self.model = LinearRegressionModel()

    def _refill_worker(self, low_water=None, refill_n=None, check_interval=5):
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
        while not self.stop_event.is_set():
            try:
                with self.batch_lock:
                    queued = len(self.batch)
                if queued >= self.top_n - 5:
                    time.sleep(check_interval)
                    continue

                wait_start = time.time()
                last_print = 0

                # Don't stall playback waiting for a full pool: the first batch
                # can start as soon as enough candidates to fill top_n exist,
                # and a half-full pool after a short wait is better than silence.
                min_needed = max(self.top_n, 3)

                while not self.stop_event.is_set():
                    count = preprocessed_count()
                    now = time.time()
                    if count == 0 and now - last_print >= 15:
                        print("Waiting for the first candidates... (pool is empty)")
                        last_print = now
                    if count >= min_needed:
                        break
                    if now - wait_start > 45 and count >= 3:
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

        for song in records:
            if 'likeability' in song and song['likeability'] is not None:
                try:
                    save_song_metadata(song, likeability=song['likeability'])
                except Exception as e:
                    print("Save predicted likeability failed:", e)

        with self.batch_lock:
            self.batch.extend(records)
        print(f"Queued batch of {len(records)} songs — predicted likeability saved (pool {self.pool_size}, best {self.top_n}).")

    def get_next_song(self):
        while not self.stop_event.is_set():
            with self.batch_lock:
                if self.batch:
                    return self.batch.pop(0)
            time.sleep(0.5)
        return None

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
            self.current_rating = None
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
                    data = current.stdout.read(
                        4096 * 2 * 2
                    )

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

    def stop(self):
        super().stop()

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


if __name__ == "__main__":
    dj = PreloadedPlayer(pool_size=20, top_n=8)
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
