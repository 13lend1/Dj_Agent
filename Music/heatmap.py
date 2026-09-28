"""Most-replayed passage detection from YouTube's own heatmap.

yt-dlp returns a `heatmap` entry for every YouTube video: ~100 markers that tile
the whole runtime, each carrying a normalized replay intensity (0..1). That is
real audience behaviour — the seconds people scrub back to — so it beats asking a
language model to guess a song's chorus from its title.

`resolve_hooks()` turns that raw data into one playback window per song and hands
it to the DJ agent, which then only has to order the songs. Every function here
degrades to None instead of raising: YouTube publishes no heatmap for plenty of
videos, and the caller keeps its own remaining sources in that case (a separate
hook-only Gemini request, then a deterministic fallback).
"""
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "heatmap_cache.json")
_CACHE_LOCK = threading.Lock()
_CACHE = None
_CACHE_DIRTY = False

# The extract is a full page fetch, so it is slow and occasionally hangs on a
# dead YouTube session. Every fetch runs in a throwaway thread that is abandoned
# after this budget, which keeps one bad video from stalling a whole batch.
FETCH_TIMEOUT = 45.0
# Wall-clock budget for a whole batch. The DJ is waiting on this, so a pool of
# never-before-seen videos is not allowed to hold the deck hostage: whatever has
# not come back in time is simply left for the caller's next source and gets
# picked up on the next batch, when the cache makes it free.
BUDGET_SEC = 120.0

# Window shape. CORE_SEC is how long a passage must stay hot to count as the
# hook (a one-marker spike is a curiosity, not a hook); EDGE_RATIO is how much of
# that peak intensity the window's own edges must hold; GAP_SEC is a dip the
# window is allowed to bridge so a chorus split by a quiet bar stays whole.
GRID_SEC = 1.0
CORE_SEC = 15.0
EDGE_RATIO = 0.65
GAP_SEC = 3.0
MIN_HOOK_SEC = 8.0
MAX_HOOK_SEC = 120.0
# How far in to look for the end of the opening decay, how far the density has
# to climb back out of its early low to count as done opening, and how far into
# the window the peak is placed (a third in: the hook lands early enough that
# the clip still has somewhere to go after it).
OPENING_SEC = 45.0
RISING_MARGIN = 1.05
PEAK_LEAD = 1.0 / 3.0
# How clearly the opening marker must stand out from the rest before it is
# treated as YouTube's every-video "1.0" anchor instead of a real peak.
ANCHOR_SPIKE_RATIO = 1.1
# How far the data may stretch the requested clip length.
MIN_LENGTH_RATIO = 0.75
MAX_LENGTH_RATIO = 2.0

HOOK_SOURCE = "yt_heatmap"

_VIDEO_ID_RE = re.compile(
    r"(?:[?&]v=|/shorts/|/embed/|/live/|youtu\.be/|/v/)([A-Za-z0-9_-]{11})"
)
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_ytdl_local = threading.local()


def video_key(link):
    """The 11-character YouTube id behind any watch/shorts/embed URL, or None."""
    if not link:
        return None
    text = str(link).strip()
    if _BARE_ID_RE.match(text):
        return text
    match = _VIDEO_ID_RE.search(text)
    return match.group(1) if match else None


def duration_seconds(value):
    """Song durations live in the database as milliseconds; accept either unit."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number <= 0:
        return 0.0
    return number / 1000.0 if number > 10000 else number


def _load_cache():
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            try:
                with open(_CACHE_PATH, "r", encoding="utf-8") as handle:
                    _CACHE = json.load(handle)
            except (OSError, ValueError):
                _CACHE = {}
            if not isinstance(_CACHE, dict):
                _CACHE = {}


def _store_cache(key, payload):
    """Remember a heatmap (or the fact that there is none) for next time. A
    miss is cached too, so a video YouTube has no data for is only ever asked
    about once."""
    global _CACHE_DIRTY
    _load_cache()
    with _CACHE_LOCK:
        _CACHE[key] = payload
        _CACHE_DIRTY = True


def _flush_cache():
    global _CACHE_DIRTY
    _load_cache()
    with _CACHE_LOCK:
        if not _CACHE_DIRTY:
            return
        snapshot = dict(_CACHE)
        _CACHE_DIRTY = False
    tmp = _CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        os.replace(tmp, _CACHE_PATH)
    except OSError as exc:
        print(f"Heatmap cache save error: {exc}")


def _ytdl():
    """A thread-local yt-dlp handle. A full extract is needed (not extract_flat)
    because the heatmap only exists in the watch page's initial data. The
    options mirror Music/dj.py so both use the same impersonated, cookie-bearing
    session that is known to reach YouTube from this machine."""
    handle = getattr(_ytdl_local, "ydl", None)
    if handle is None:
        import shutil

        import yt_dlp

        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            impersonate = ImpersonateTarget.from_str("chrome")
        except Exception:
            impersonate = None

        options = {
            "quiet": True,
            "noprogress": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 20,
            "retries": 2,
            "force_ipv4": True,
            "http_headers": {"Accept": "*/*"},
        }
        if impersonate is not None:
            options["impersonate"] = impersonate
        deno = shutil.which("deno")
        if deno:
            options["js_runtimes"] = {"deno": {"path": deno}}
            options["remote_components"] = ["ejs:github"]

        cookie_file = os.environ.get("DJ_YTDLP_COOKIES_FILE", "").strip()
        if cookie_file and os.path.isfile(cookie_file):
            options["cookiefile"] = cookie_file
        handle = yt_dlp.YoutubeDL(options)
        _ytdl_local.ydl = handle
    return handle


def fetch_markers(link, timeout=None):
    """The raw `[{start_time, end_time, value}, ...]` heatmap for a video URL,
    or None when the video is gone, has no heatmap, or the fetch failed.

    Results are cached per video id, so a track keeps the same hook across runs.
    """
    key = video_key(link)
    if not key:
        return None
    known, markers = _cached(key)
    if known:
        return markers

    holder = {}

    def _run():
        try:
            holder["info"] = _ytdl().extract_info(str(link).strip(), download=False)
        except BaseException as exc:  # a failed extract must not kill the batch
            holder["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(FETCH_TIMEOUT if timeout is None else max(1.0, float(timeout)))
    if worker.is_alive():
        print(f"[heatmap] gave up on {link} (no answer in time)")
        return None
    if "exc" in holder:
        print(f"[heatmap] {link}: {holder['exc']}")
        _store_cache(key, {"_miss": True})
        return None

    info = holder.get("info") or {}
    markers = _normalize_markers(info.get("heatmap"))
    if not markers:
        _store_cache(key, {"_miss": True})
        return None
    _store_cache(key, {
        "markers": [[m["start_time"], m["end_time"], m["value"]] for m in markers],
        "duration": duration_seconds(info.get("duration")),
    })
    return markers


def _cached(key):
    """(already_looked_up, markers) for a video id.

    The two are separate because "YouTube has no heatmap for this video" is
    itself an answer: it is cached like a hit, so a video that will never have
    data is only ever asked about once instead of on every single batch.
    """
    _load_cache()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if not isinstance(entry, dict):
        return False, None
    if entry.get("_miss"):
        return True, None
    rows = entry.get("markers")
    if not isinstance(rows, list) or not rows:
        return True, None
    try:
        return True, [{"start_time": float(r[0]), "end_time": float(r[1]),
                       "value": float(r[2])} for r in rows]
    except (TypeError, ValueError, IndexError):
        return True, None


def _normalize_markers(raw):
    """yt-dlp's marker dicts -> a clean, sorted, positive-intensity list."""
    if not raw:
        return []
    cleaned = []
    for marker in raw:
        if not isinstance(marker, dict):
            continue
        try:
            start = float(marker["start_time"])
            end = float(marker["end_time"])
            value = float(marker["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or value <= 0.0:
            continue
        cleaned.append({"start_time": max(0.0, start),
                        "end_time": end,
                        "value": value})
    cleaned.sort(key=lambda m: m["start_time"])
    return cleaned


def _without_intro_anchor(markers):
    """Drop YouTube's leading anchor marker.

    Every video's heatmap is normalized so its single hottest marker is 1.0, and
    on a music video that marker is always the opening second: everybody plays
    the track once from the top, which is not a replay. Left in, it anchors the
    peak search on the intro and every hook collapses to 0:00, so it is removed
    whenever it really is that anchor (a spike at t=0 standing well clear of
    every other marker). The spike ratio keeps the strip idempotent, so running
    it twice can never eat a genuine peak.
    """
    if len(markers) < 4 or markers[0]["start_time"] > 1.0:
        return markers
    first = markers[0]
    rest = max(m["value"] for m in markers[1:])
    if first["value"] >= rest * ANCHOR_SPIKE_RATIO:
        return markers[1:]
    return markers


def _density_grid(markers, duration):
    """A 1-second replay-density curve over the video, plus the time of each
    grid point. The markers tile the runtime, so a lookup per point is enough.

    The curve starts at the first real marker and stops at the last one, so
    nothing is invented outside the range YouTube actually measured.
    """
    points = []
    density = []
    index = 0
    origin = markers[0]["start_time"]
    limit = min(duration, markers[-1]["end_time"])
    step = int(origin / GRID_SEC) + 1
    while True:
        moment = step * GRID_SEC
        if moment > limit:
            break
        while index + 1 < len(markers) and markers[index + 1]["start_time"] <= moment:
            index += 1
        points.append(moment)
        density.append(markers[index]["value"])
        step += 1
    return points, density


def _smooth(values, width):
    """Centred moving average, so the peak search only rewards passages that
    stay hot for a while instead of one lucky marker."""
    if width <= 1 or len(values) < 2:
        return list(values)
    half = width // 2
    out = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _grow(density, index, step, threshold, first, last):
    """Walk away from `index` while intensity stays above `threshold`, bridging
    dips of at most GAP_SEC so a chorus is not cut in two by a single quiet
    marker. Returns the last index still inside the passage."""
    gap = max(0, int(GAP_SEC / GRID_SEC))
    i = index
    last_good = index
    below = 0
    while first <= i + step <= last:
        nxt = i + step
        if density[nxt] >= threshold:
            last_good = nxt
            below = 0
        else:
            below += 1
            if below > gap:
                break
        i = nxt
    return last_good


def _after_opening_decay(density):
    """Index where the track stops opening and starts climbing.

    The heatmap's first marker is YouTube's everybody-pressed-play anchor, but
    on plenty of videos the anchor is not one spike — the whole opening decays
    away from it as the initial play-through bleeds off. A monotone slide is not
    a peak, so the peak search starts where the curve climbs back out of its own
    early low. A video that builds from the first bar reaches that point almost
    immediately, leaving its hook exactly where it was.
    """
    limit = min(len(density), int(OPENING_SEC / GRID_SEC) + 1)
    low = density[0]
    for i in range(1, limit):
        low = min(low, density[i])
        if density[i] >= low * RISING_MARGIN:
            return i
    # The opening window never climbs back out: it is one long decay, so start
    # after its lowest point instead of at the top of the slide.
    return min(range(limit), key=lambda i: density[i])


def best_window(markers, duration, clip_length=33.0):
    """The most-replayed window of a video as (start_sec, end_sec), or None.

    The peak is found on a CORE_SEC moving average (so the hook is a sustained
    hot passage, not one lucky marker), skipping the opening decay. The window
    then grows outward from that peak while the local intensity holds EDGE_RATIO
    of it. A clearly sized hot passage sets the window's length; anything much
    bigger or smaller than the requested clip falls back to that clip. The peak
    is placed about a third of the way in, so the hook lands early enough for the
    rest of the clip to go somewhere.
    """
    if not markers or not duration or duration <= 0:
        return None
    markers = _without_intro_anchor(_normalize_markers(markers))
    if len(markers) < 2:
        return None
    points, density = _density_grid(markers, duration)
    if len(points) < 2:
        return None

    first = _after_opening_decay(density)
    last = len(points) - 1
    width = max(1, int(CORE_SEC / GRID_SEC))
    smoothed = _smooth(density, width)
    peak = max(range(first, last + 1), key=lambda i: smoothed[i])
    if smoothed[peak] <= 0.0:
        return None

    threshold = smoothed[peak] * EDGE_RATIO
    left = _grow(density, peak, -1, threshold, first, last)
    right = _grow(density, peak, +1, threshold, first, last)
    natural = points[right] - points[left]

    wanted = min(MAX_HOOK_SEC, max(MIN_HOOK_SEC, float(clip_length or 33.0)))
    lo = wanted * MIN_LENGTH_RATIO
    hi = wanted * MAX_LENGTH_RATIO
    length = natural if lo <= natural <= hi else wanted
    length = min(length, MAX_HOOK_SEC, duration)

    # Peak about a third of the way in, kept inside the hot passage when the
    # passage is long enough to contain the whole window.
    start = points[peak] - length * PEAK_LEAD
    if natural >= length:
        start = max(points[left], min(start, points[right] - length))
    start = max(0.0, min(start, duration - length))
    end = min(duration, start + length)
    return (round(start, 1), round(end, 1))


def hook_for_song(song, clip_length=33.0, timeout=None):
    """The heatmap hook for one song dict, or None when YouTube has no data.

    The window is returned with the raw heatmap numbers that produced it, so the
    agent can see not just where the hook is but how strongly listeners seek to
    it — which is real information for deciding what to play next.
    """
    song = song or {}
    link = song.get("link")
    markers = fetch_markers(link, timeout=timeout)
    if not markers:
        return None

    duration = duration_seconds(song.get("duration"))
    if duration <= 0:
        duration = _video_duration(link)
    window = best_window(markers, duration, clip_length)
    if not window:
        return None

    start, end = window
    values = [m["value"] for m in _without_intro_anchor(markers)]
    return {
        "hook_start_sec": start,
        "hook_end_sec": end,
        "hook_source": HOOK_SOURCE,
        "replay_peak_sec": _peak_second(markers, duration),
        "replay_score": round(max(values), 4) if values else 0.0,
        "replay_markers": len(markers),
    }


def _video_duration(link):
    """Runtime as yt-dlp reports it, for songs whose database duration is
    missing. It rides along in the heatmap cache, so this costs nothing extra."""
    key = video_key(link)
    if not key:
        return 0.0
    _load_cache()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if not isinstance(entry, dict):
        return 0.0
    return duration_seconds(entry.get("duration"))

def _peak_second(markers, duration):
    """Where in the track the replay peak sits — where the hook is anchored.

    Reads exactly the range best_window() searches, so the number reported to
    the agent is the moment the chosen window was built around.
    """
    markers = _without_intro_anchor(_normalize_markers(markers))
    points, density = _density_grid(markers, duration or 0.0)
    if not points:
        return 0.0
    smoothed = _smooth(density, max(1, int(CORE_SEC / GRID_SEC)))
    first = _after_opening_decay(density)
    peak = max(range(first, len(points)), key=lambda i: smoothed[i])
    return round(points[peak], 1)


def resolve_hooks(songs, clip_length=33.0, workers=4, budget=BUDGET_SEC):
    """Heatmap hooks for a batch of songs, as {song id: hook}.

    Fetches run in parallel (each is an independent YouTube page request) and
    the disk cache makes repeat batches free. The dict is keyed by the song id
    string, matching the ids the agent and the database use. Songs whose video
    has no heatmap — YouTube publishes none for plenty of long-tail videos —
    are simply absent, which is the caller's cue to try its next source.
    `budget` caps the whole batch's wall clock; anything still unfinished then
    waits for the next batch instead of holding up the deck.
    """
    songs = list(songs or [])
    if not songs:
        return {}

    deadline = None if budget is None else time.monotonic() + float(budget)

    def _job(song):
        song_id = str(song.get("id") or "").strip()
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return song_id, None
        try:
            return song_id, hook_for_song(song, clip_length, timeout=remaining)
        except Exception as exc:
            name = (song.get("name") or "?").strip()
            print(f"[heatmap] '{name}' failed: {exc}")
            return song_id, None

    hooks = {}
    workers = max(1, min(int(workers or 1), len(songs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for song_id, hook in pool.map(_job, songs):
            if song_id and hook:
                hooks[song_id] = hook
    _flush_cache()

    if hooks:
        print(f"[heatmap] {len(hooks)}/{len(songs)} song(s) have a YouTube replay "
              f"heatmap; the rest need another source.")
    elif songs:
        print(f"[heatmap] no YouTube replay heatmap for any of {len(songs)} "
              "song(s).")
    return hooks


def _fmt(seconds):
    try:
        seconds = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        seconds = 0
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}"


if __name__ == "__main__":
    # python Music/heatmap.py "https://www.youtube.com/watch?v=..."
    # python Music/heatmap.py "Artist - Song"
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(0)

    target = args[0]
    if not target.startswith(("http://", "https://")):
        from Music.songs import _yt_search_link
        target = _yt_search_link(*(args[0].split(" - ", 1) + [""])[:2]) or target
    markers = fetch_markers(target)
    if not markers:
        print(f"No heatmap available for {target}")
        raise SystemExit(1)
    song = {"id": target, "link": target, "duration": _video_duration(target) or
            (markers[-1]["end_time"] * 1000)}
    hook = hook_for_song(song, clip_length=float(args[1]) if len(args) > 1 else 33.0)
    print(f"{len(markers)} markers, duration {duration_seconds(song['duration']):.0f}s")
    print(f"hook {_fmt(hook['hook_start_sec'])}..{_fmt(hook['hook_end_sec'])} "
          f"({hook['hook_end_sec'] - hook['hook_start_sec']:.1f}s) "
          f"peak {hook['replay_peak_sec']:.0f}s score {hook['replay_score']}")
