import yt_dlp
import subprocess
import sounddevice as sd
import threading
import queue
import sys
import termios
import tty
import select


class Player:

    def __init__(self):
        self.song_queue = queue.Queue(maxsize=2)

        self.stop_event = threading.Event()
        self.skip_event = threading.Event()

        self.current_process = None
        self.lock = threading.Lock()

    # -------------------------
    # CONTROLS
    # -------------------------

    def skip(self):
        self.skip_event.set()
    # -------------------------
    # PREPARE SONG
    # -------------------------

    def prepare_song(self, url):

        options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'noplaylist': True
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

    def preload(self, url):

        if url is None:
            print("No song available to preload.")
            url=get_random_song() 

        try:
            process = self.prepare_song(url)
            self.song_queue.put(process)
            print("Next song preloaded!")

        except Exception as e:
            print("Preload error:", e)

    # -------------------------
    # START PRELOAD
    # -------------------------

    def start_preload(self, get_next_song):

        next_url = get_next_song()

        threading.Thread(
            target=self.preload,
            args=(next_url,),
            daemon=True
        ).start()

    # -------------------------
    # GET PRELOADED SONG
    # -------------------------

    def get_preloaded_song(self):

        while not self.stop_event.is_set():

            try:
                return self.song_queue.get(timeout=0.1)

            except queue.Empty:
                continue

        return None

    # -------------------------
    # PLAYBACK
    # -------------------------

    def play(self, first_url, get_next_song):

        current = self.prepare_song(first_url)

        with self.lock:
            self.current_process = current

        # Preload next song
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

                    # Skip
                    if self.skip_event.is_set():

                        self.skip_event.clear()

                        print("\nSkipping...")

                        current.kill()

                        current = self.get_preloaded_song()

                        if current is None:
                            break

                        with self.lock:
                            self.current_process = current

                        print("Starting next song...")

                        self.start_preload(get_next_song)

                        continue

                    # Read audio
                    data = current.stdout.read(
                        4096 * 2 * 2
                    )

                    # Song ended
                    if not data:

                        current.kill()

                        current = self.get_preloaded_song()

                        if current is None:
                            break

                        with self.lock:
                            self.current_process = current

                        print("\nStarting next song...")

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

    # -------------------------
    # RUN PLAYER IN BACKGROUND
    # -------------------------

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

        # Kill preloaded FFmpeg processes
        while not self.song_queue.empty():
            try:
                process = self.song_queue.get_nowait()
                process.kill()
            except queue.Empty:
                break
    def keyboard_control(self):

        fd = sys.stdin.fileno()

        old_settings = termios.tcgetattr(fd)

        try:
            tty.setcbreak(fd)

            print("\nControls: [n] next | [s] stop | [Ctrl+C] quit\n")

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
    
from songs import get_random_song

dj = Player()

dj.start(
    get_random_song(),
    get_random_song
)

try:
    dj.player_thread.join()

except KeyboardInterrupt:
    dj.stop()
    dj.player_thread.join()