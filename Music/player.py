import yt_dlp
import subprocess
import sounddevice as sd
import threading
import queue
import sys
import termios
import tty
import select
import time
from songs import get_random_song,save
from audio_metrics import AudioFeatureExtractor


class Player:

    def __init__(self):
        self.song_queue = queue.Queue(maxsize=2)
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
        extractor = AudioFeatureExtractor()   # loaded ONCE, ever
        while True:
            song = self.save_queue.get()
            if song is None:
                break
            try:
                save(song, extractor)
            except Exception as e:
                print("Save error:", e)

    def skip(self):
        self.skip_event.set()

    def prepare_song(self, url):

        options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'noplaylist': True,
            # 'js_runtimes': {'deno': None} 
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
            process = self.prepare_song(song_info['url'])
            self.song_queue.put({
                **song_info,
                'process': process,
            })
            print(f"Preloaded: {song_info['title']}")
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

        current = self.prepare_song(first_song['url'])
        with self.lock:
            self.current_process = current
            self.current_song = first_song
            self.current_url = first_song['url']
            self.current_title = first_song['title']
            self.current_length = first_song['length']
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
                            self.current_url = current_song['url']
                            self.current_song = current_song
                            self.current_title = current_song['title']
                            self.current_length = current_song['length']
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
                            self.current_url = current_song['url']
                            self.current_song = current_song
                            self.current_title = current_song['title']
                            self.current_length = current_song['length']
                            self.current_start_time = time.time()
                            self.current_rating = None

                        print(f"\nNow playing: {current_song['title']}")
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


    def start(self, first_url, get_next_song):
        
        while(first_url==None):
            first_url=get_random_song()
            
        self.player_thread = threading.Thread(
            target=self.play,
            args=(first_url, get_next_song),
            daemon=False
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

        while not self.song_queue.empty():
            try:
                song_info = self.song_queue.get_nowait()
                song_info['process'].kill()
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

        if not song.get('length'):
            return

        listen_fraction = min(seconds_listened / (song['length'] / 1000), 1.0)

        if score is not None:
            likeability = round(score / 9,1)
        else:
            likeability = listen_fraction

        print(f"'{song['title']}' likeability: {round(likeability, 2)}")
        song['score'] = likeability
        self.save_queue.put(song)
    def keyboard_control(self):

        fd = sys.stdin.fileno()

        old_settings = termios.tcgetattr(fd)

        try:
            tty.setcbreak(fd)
            
            print("n = next | s = stop | 0-9 = rate song (0.0-1.0)")

            while not self.stop_event.is_set():

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

                elif key.isdigit():
                    score=int(key)
                    self.rate_current(score)
                
                elif key == '\x03':  # Ctrl+C
                    print("\nStopping...")
                    self.stop()

        finally:
            # ALWAYS restore normal terminal behaviour
            termios.tcsetattr(
                fd,
                termios.TCSADRAIN,
                old_settings
            )
            
    


dj = Player()

first_song = get_random_song()

dj.start(first_song, get_random_song)
try:
    dj.player_thread.join()

except KeyboardInterrupt:
    dj.stop()
    dj.player_thread.join()