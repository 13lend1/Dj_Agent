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

The one thing that made the UI show "buffering" was the encoder itself going
dry: MP3 only moves while the DJ writes PCM, so every legit gap (song change,
a skip cut, a track still downloading, a dry-pool hold) produced NO output and
the <audio> element waited. A filler thread now keeps ffmpeg fed with silence
(at roughly realtime) whenever no real PCM has arrived for ~0.3s, so the
browser always has a continuous stream to play — it never sees a stall.
"""

import os
import queue
import subprocess
import threading
import time
from collections import deque


class StreamSink:
    RATE = 44100
    CHANNELS = 2
    FRAME_BYTES = RATE * CHANNELS * 2  # bytes per second of int16 stereo

    # Seed each new consumer with this much most-recent audio. The DJ produces
    # PCM at exactly realtime, so the MP3 can only ever be DELIVERED at realtime
    # too — the browser's buffer would otherwise sit at ~0 and any hiccup (GC,
    # TCP, encoder lag) drains it to zero and fires <audio>'s `waiting`. A
    # generous seed is SUSTAINED headroom: the seed plays out at realtime while
    # live audio arrives at realtime, so the cushion never shrinks in normal
    # operation. ~3s absorbs real jitter without much join lag.
    _PREBUFFER_BYTES = 48000  # ~3 s at 128 kbit/s

    # Silence-filler: once real PCM has been absent this long, stream silence
    # so the encoder never goes dry. Well above the ~93 ms chunk period of a
    # normally playing song (so it never bleeds into real audio). The filler
    # feeds silence FASTER than realtime — so the browser builds buffer in the
    # gap — but only up to this much PCM beyond the last real write, otherwise a
    # long dry spell (empty pool, dead link) would bury the next song under
    # dead air.
    _FILL_AFTER_SECS = 0.28
    _FILL_LEAD_SECS = 2.0
    _FILL_CHUNK_FRAMES = 4096  # 4096 stereo int16 frames ~= 93 ms; same as the DJ's writes
    _FILL_CHUNK_BYTES = _FILL_CHUNK_FRAMES * 2 * 2
    _FILL_FEED_GAP = 0.01  # sleep between filler writes: fast, but not a busy-spin

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
        # Wall-clock of the last real PCM write; the filler uses it to decide
        # when the deck has gone silent on us.
        self._last_write = 0.0
        # PCM seconds of silence fed during the current dry spell; reset on the
        # next real write, and capped so the filler can't bury the stream.
        self._silence_lead = 0.0
        self._paused = False
        # Rolling window of encoded chunks, newest at the right end. Shared by
        # every subscriber for the reconnect prebuffer. 128 * 4 KB plays ~32 s,
        # but only _PREBUFFER_BYTES of it is ever handed to a new consumer.
        self._history = deque(maxlen=128)

    # ---- output-stream interface used by the DJ's play loop ----

    def __enter__(self):
        self._start_encoder()
        return self

    def __exit__(self, *exc):
        self.close()

    # When a stall (skip/seek/restart download) puts the encoder more than this
    # far behind the wall clock, re-anchor instead of catching up. Without this
    # the first post-stall writes burst-flush everything at once, which the
    # browser then plays as a fast-forwarded glitch.
    _MAX_LAG = 0.75

    def write(self, data):
        """Receive int16 stereo PCM bytes, paced to the wall clock."""
        with self._lock:
            self._last_write = time.time()
            self._silence_lead = 0.0
            self._pcm += len(data)
            seconds = self._pcm / self.FRAME_BYTES
            now = time.time()
            if self._clock is None:
                self._clock = now
                remaining = 0.0
            else:
                lag = (now - self._clock) - seconds
                if lag > self._MAX_LAG:
                    # We fell behind by more than the buffer window: play
                    # forward from here rather than racing to catch up.
                    self._clock = now - seconds
                    remaining = 0.0
                else:
                    remaining = seconds - (now - self._clock)
        if remaining > 0.001:
            time.sleep(remaining)
        enc = self._enc
        if enc is not None:
            try:
                enc.stdin.write(data)
            except (BrokenPipeError, ValueError, OSError):
                pass

    def note_paused(self, paused):
        """Called by the player on pause/resume so the filler stops streaming
        silence while the deck is intentionally frozen (nothing should be
        heard, and history shouldn't fill with pause-time silence)."""
        with self._lock:
            self._paused = bool(paused)

    def resync_clock(self):
        """Re-anchor pacing to 'now'. Called on resume so the first write after
        a pause does not treat the paused span as audio lag and burst-flush the
        audio that was buffered while paused."""
        with self._lock:
            self._pcm = 0
            self._clock = time.time()

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
        self._filler = threading.Thread(target=self._fill_loop, daemon=True)
        self._filler.start()

    def _fill_loop(self):
        """Guarantee the encoder NEVER goes dry — and keep the browser's buffer
        topped up. Whenever no real PCM has been written for _FILL_AFTER_SECS
        (song change, skip cut, a track still downloading, dry-pool hold,
        startup), feed ffmpeg silence quickly so the browser gains real headroom
        it can ride through the next stretch of real audio. Stops feeding once
        _FILL_LEAD_SECS of silence has been shipped since the last real write,
        so a long dry spell can't bury the next song under dead air."""
        silence = b"\x00\x00" * (self._FILL_CHUNK_BYTES // 2)
        while not self._dead:
            if self._enc is None:
                return
            with self._lock:
                enc = self._enc
                paused = self._paused
                last = self._last_write
                lead = self._silence_lead
            if (
                enc is None
                or paused
                or (time.time() - last) < self._FILL_AFTER_SECS
                or lead >= self._FILL_LEAD_SECS
            ):
                time.sleep(0.05)
                continue
            try:
                with self._lock:
                    if self._dead:
                        return
                    # _last_write is only ever touched by real PCM writes, so a
                    # chain of silence chunks feeds fast (one per loop) instead
                    # of resetting the dry-detection clock every chunk.
                    self._silence_lead = lead + self._FILL_CHUNK_FRAMES / self.RATE
                    enc.stdin.write(silence)
            except (BrokenPipeError, ValueError, OSError):
                self.close()
                return
            # Fast feed: one 93 ms chunk per iteration instead of waiting a
            # real 93 ms, so a gap actually builds a few seconds of cushion.
            time.sleep(self._FILL_FEED_GAP)

    def _read_loop(self):
        fd = self._enc.stdout.fileno()
        while self._enc is not None:
            try:
                # Small reads = small initial browser buffer: the <audio>
                # element can start and stay closer to the live edge instead of
                # sitting on half-second slabs of MP3.
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                if self._dead:
                    return
                self._history.append(chunk)
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

    def subscribe(self, fresh=False):
        # The queue bounds how far a lagging browser can drift from the live
        # edge (~32s) while still fitting the ~3s seed + ~2s silence lead +
        # live tail. New subscribers start pre-seeded with the most recent audio
        # (instant start, sustained jitter cushion), then ride the live edge.
        # `fresh=True` (used by skip / previous / restart / resume): skip the
        # seed so the browser lands exactly on the live edge — replaying part of
        # the song the DJ just skipped away from is what read as "the next song
        # is buffering". A fresh consumer pays with a few frames of lead-in,
        # which beats stuttering stale audio after a cut.
        q = queue.Queue(maxsize=128)
        with self._lock:
            hist = list(self._history)
            self._seq += 1
            sid = self._seq
            self._subs[sid] = q
        if hist and not fresh:
            seed = []
            total = 0
            for chunk in reversed(hist):
                total += len(chunk)
                seed.append(chunk)
                if total >= self._PREBUFFER_BYTES:
                    break
            for chunk in reversed(seed):
                q.put_nowait(chunk)
        return sid, q

    def unsubscribe(self, sid):
        with self._lock:
            self._subs.pop(sid, None)

    @property
    def alive(self):
        return not self._dead and self._enc is not None