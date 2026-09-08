import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import queue
import threading
import time
import yt_dlp
import subprocess
import sounddevice as sd
from songs import (
    save,
)


class Player:

    def __init__(self):
        self.save_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.skip_event = threading.Event()
        self.current_length = None
        self.current_elapsed = None
        self.current_process = None
        self.current_url = None
        self.current_rating = None
        self.lock = threading.Lock()

        threading.Thread(target=self._save_worker, daemon=True).start()

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

    def play(self, first_song, get_next_song):

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

                        next_song = get_next_song()
                        if next_song is None:
                            print("No more songs available.")
                            self.stop()
                            break

                        current_song = next_song
                        current = self.prepare_song(current_song['link'])

                        with self.lock:
                            self.current_process = current
                            self.current_url = current_song['link']
                            self.current_song = current_song
                            self.current_title = current_song['name']
                            self.current_length = current_song['duration']
                            self.current_start_time = time.time()
                            self.current_rating = None

                        print(f"\nNow playing: {self.current_title}")
                        continue

                    data = current.stdout.read(
                        4096 * 2 * 2
                    )

                    if not data:

                        with self.lock:
                            self.current_elapsed = time.time() - self.current_start_time

                        self._score_current_song()

                        current.kill()

                        next_song = get_next_song()
                        if next_song is None:
                            break

                        current_song = next_song
                        current = self.prepare_song(current_song['link'])

                        with self.lock:
                            self.current_process = current
                            self.current_url = current_song['link']
                            self.current_song = current_song
                            self.current_title = current_song['name']
                            self.current_length = current_song['duration']
                            self.current_start_time = time.time()
                            self.current_rating = None

                        print(f"\nNow playing: {current_song['name']}")
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
            likeability = round(score / 9, 2)
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
            if not is_windows:
                termios.tcsetattr(
                    fd,
                    termios.TCSADRAIN,
                    old_settings
                )
