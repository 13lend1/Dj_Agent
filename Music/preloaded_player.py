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
        self._preload_procs_lock = threading.Lock()
        self._preload_processes = set()  # in-flight decode processes, killed on stop

        threading.Thread(target=self._refill_worker, daemon=True).start()
        threading.Thread(target=self._batch_worker, daemon=True).start()

    def _load_model(self):
        if self.model is None:
            from Model.linear_regression import LinearRegressionModel
            self.model = LinearRegressionModel()

    def _refill_worker(self, low_water=None, check_interval=5):
        """Keep the Preprocessed pool topped up nonstop while the DJ is active.

        Instead of waiting until the pool runs dry (one fetch, then idle), it
        rolls toward a high-water mark: finishing one fetch chunk then starting
        the next immediately as long as the pool is below target. Candidates are
        also inserted progressively inside each chunk, so the batch worker and
        playback always find pool ready.
        """
        if low_water is None:
            low_water = self.pool_size
        top_n = getattr(self, 'top_n', None) or low_water
        high_water = getattr(self, 'prefill_high_water', None) or max(2 * low_water, low_water + top_n)
        max_chunk = 50  # one fetch round is still snappy; the loop chains them
        while not self.stop_event.is_set():
            try:
                count = preprocessed_count(place=getattr(self, 'place', None))
                need = high_water - count
                if need >= 5:
                    get_random_songs(
                        n=min(need, max_chunk),
                        on_song=save_preprocessed,
                        place=getattr(self, 'place', None),
                    )
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
                max_wait = 90.0  # hard cap: never wait forever for a tiny pool

                # Don't stall playback waiting for a full pool: the first batch
                # can start as soon as enough candidates to fill top_n exist,
                # and a half-full pool after a short wait is better than silence.
                min_needed = max(self.top_n, 3)

                while not self.stop_event.is_set():
                    count = preprocessed_count(place=getattr(self, 'place', None))
                    now = time.time()
                    if count == 0 and now - last_print >= 15:
                        print("Waiting for the first candidates... (pool is empty)")
                        last_print = now
                    if count >= min_needed:
                        break
                    if now - wait_start > 45 and count >= 3:
                        break
                    if now - wait_start > max_wait:
                        print("Pool stayed too small — building a smaller batch "
                              "from what exists.")
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

    def get_next_song(self, timeout=None):
        start = time.time()
        while not self.stop_event.is_set():
            with self.batch_lock:
                if self.batch:
                    song = self.batch.pop(0)
                    break
            if timeout is not None and time.time() - start > timeout:
                return None
            time.sleep(0.5)
        else:
            return None
        self._record_genre(song)
        return song

    def preload(self, song_info):
        if self.stop_event.is_set():
            return

        if song_info is None:
            print("No song available to preload, fetching a random one.")
            if self.stop_event.is_set():
                return
            song_info = get_random_song()

        if song_info is None:
            print("Could not find a song to preload.")
            return

        if self.stop_event.is_set():
            return

        try:
            process = self.prepare_song(song_info['link'])
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
                    print(f"Preloaded: {song_info['name']}")
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
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_event.is_set():
                return None
            try:
                return self.song_queue.get(timeout=min(0.5, end - time.time()))
            except queue.Empty:
                continue
        return None

    def _settle_next(self, current, get_next_song):
        """Settle the outgoing song and hand back (song, process) to play next,
        tracking the outgoing song as the previous one. Returns (None, None)
        when there is nothing left to play."""
        with self.lock:
            self.current_elapsed = time.time() - self.current_start_time

        try:
            current.kill()
        except Exception:
            pass

        self._score_current_song()

        song = self.get_preloaded_song()
        if song is None:
            return None, None
        process = song['process']

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
                            if length and target >= length / 1000:
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

                        current_song, current = self._settle_next(current, get_next_song)

                        if current_song is None:
                            print("No preloaded song available.")
                            self.stop()
                            break

                        print("Starting next song...")
                        self.start_preload(get_next_song)
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

                        current_song, current = self._settle_next(current, get_next_song)

                        if current_song is None:
                            break

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

        # kill any in-flight decode workers so preloading halts with the DJ
        with self._preload_procs_lock:
            procs = list(self._preload_processes)
            self._preload_processes.clear()
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
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
