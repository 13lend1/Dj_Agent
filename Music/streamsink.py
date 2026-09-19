"""Browser audio output for the DJ.

The DJ's playback loop normally writes int16 stereo 44.1kHz PCM into a
sounddevice RawOutputStream (the machine's speakers). StreamSink is a drop-in
replacement that accepts the same bytes on write(), paces writes to the wall
clock (so the DJ's crossfade/advance timing is preserved), encodes the PCM to
MP3 with ffmpeg, and fans the encoded chunks out to any number of HTTP
consumers — i.e. the <audio> element in the web UI. Nothing is played on the
machine.

Consumers never block the encoder: if a subscriber lags behind they just get
the live edge (oldest buffered chunk is dropped), so a paused or re-buffering
browser can never stall the DJ's audio loop.
"""

import os
import queue
import subprocess
import threading
import time


class StreamSink:
    RATE = 44100
    CHANNELS = 2
    FRAME_BYTES = RATE * CHANNELS * 2  # bytes per second of int16 stereo

    def __init__(self, bitrate="128k"):
        self.bitrate = bitrate
        self._pcm = 0
        self._clock = None
        self._enc = None
        self._reader = None
        self._subs = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._dead = False

    # ---- output-stream interface used by the DJ's play loop ----

    def __enter__(self):
        self._start_encoder()
        return self

    def __exit__(self, *exc):
        self.close()

    def write(self, data):
        """Receive int16 stereo PCM bytes, paced to the wall clock."""
        self._pcm += len(data)
        seconds = self._pcm / self.FRAME_BYTES
        if self._clock is None:
            self._clock = time.time()
        else:
            remaining = seconds - (time.time() - self._clock)
            if remaining > 0.001:
                time.sleep(remaining)
        enc = self._enc
        if enc is not None:
            try:
                enc.stdin.write(data)
            except (BrokenPipeError, ValueError, OSError):
                pass

    def close(self):
        if self._dead:
            return
        self._dead = True
        enc = self._enc
        self._enc = None
        if enc is not None:
            try:
                enc.stdin.close()
            except Exception:
                pass
            try:
                enc.wait(timeout=5)
            except Exception:
                pass
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for q in subs:
            q.put(None)

    # ---- encoder ----

    def _start_encoder(self):
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-fflags", "+nobuffer", "-flush_packets", "1",
            "-f", "s16le", "-ar", str(self.RATE), "-ac", str(self.CHANNELS),
            "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", self.bitrate,
            "-f", "mp3", "pipe:1",
        ]
        try:
            self._enc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print("StreamSink encoder failed:", e)
            return
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        fd = self._enc.stdout.fileno()
        while self._enc is not None:
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                if self._dead:
                    return
                for q in list(self._subs.values()):
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:
                        try:
                            q.get_nowait()
                            q.put_nowait(chunk)
                        except Exception:
                            pass
        self.close()

    # ---- consumer API for the HTTP endpoint ----

    def subscribe(self):
        q = queue.Queue(maxsize=64)
        with self._lock:
            self._seq += 1
            sid = self._seq
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid):
        with self._lock:
            self._subs.pop(sid, None)

    @property
    def alive(self):
        return not self._dead and self._enc is not None