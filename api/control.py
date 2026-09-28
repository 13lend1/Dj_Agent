"""Playback + status endpoints. Every control just flips the same thread-safe
event the keyboard handler uses, so the UI never touches the audio loop."""

import json
import logging
import os
import re
import threading
import time

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api import state

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/control", tags=["control"])

_MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
_COVERART_BASE = "https://coverartarchive.org"
_MUSICBRAINZ_UA = "DJAgentWeb/0.1 (dj-agent web UI)"
_COVER_TTL = 6 * 3600
_COVER_CACHE = {}
_COVER_LOCK = threading.Lock()
_COVER_INFLIGHT = {}
_COVER_WARMING = set()

# Collapse duplicate control POSTs that land within the same ~300ms. The
# page's in-page Ctrl+Alt handler and the global hotkey daemon (hotkeys.py)
# BOTH fire when a combo is pressed while the tab is focused, so one keypress
# would otherwise execute the action twice (e.g. skip two songs).
_DEBOUNCE_WINDOW = 0.30
_debounce_at = {}
_debounce_lock = threading.Lock()


def _debounced(key):
    now = time.monotonic()
    with _debounce_lock:
        last = _debounce_at.get(key)
        if last is not None and now - last < _DEBOUNCE_WINDOW:
            return True
        _debounce_at[key] = now
        return False


# Each release-group probe is one HTTP round trip; a MusicBrainz recording can
# list a dozen pressings, so cap how many we hand to the Cover Art Archive or a
# single cover lookup can walk them all and take several seconds.
_COVER_MAX_GIDS = 3
_COVER_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Database", "cover_cache.json",
)


def _load_cover_cache():
    try:
        with open(_COVER_CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    now = time.time()
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        if not isinstance(at, (int, float)) or now - at >= _COVER_TTL:
            continue
        _COVER_CACHE[key] = {"url": entry.get("url"), "at": at}


def _save_cover_cache():
    try:
        os.makedirs(os.path.dirname(_COVER_CACHE_FILE), exist_ok=True)
        tmp = _COVER_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_COVER_CACHE, fh)
        os.replace(tmp, _COVER_CACHE_FILE)
    except OSError:
        pass


_load_cover_cache()


class RateBody(BaseModel):
    rating: int = Field(ge=0, le=1, description="0 = like, 1 = dislike")


def _dj():
    dj = state.dj
    if dj is None:
        raise HTTPException(503, "DJ is not running (started with --no-dj).")
    return dj


def _shape_song(song, rating, elapsed):
    """The now-playing snapshot both /status and the action endpoints return,
    shaped from a raw song dict read under the player lock."""
    if song is None:
        return None
    return {
        "title": song.get("name"),
        "artist": song.get("artist"),
        "genre": song.get("genre"),
        "place": song.get("place"),
        "duration_ms": song.get("duration"),
        "play_start_sec": song.get("play_start_sec"),
        "play_end_sec": song.get("play_end_sec"),
        "likeability": song.get("likeability"),
        "rating": rating,
        "elapsed_sec": round(elapsed, 1) if elapsed is not None else None,
    }


@router.get("/status")
def status():
    """Now-playing snapshot: current song, elapsed, rating, place, and the next
    queued tracks. Never blocks on the audio loop — it reads state under the
    same lock the player uses."""
    dj = state.dj
    if dj is None:
        return {
            "dj_running": False,
            "dj_available": bool(state.allow_dj),
            "playing": False,
            "preparing": False,
            "ready_count": None,
            "ready_target": None,
            "trial": False,
            "paused": False,
            "transitioning": False,
            "stopping": False,
            "place": None,
            "song": None,
            "previous": None,
            "queue": [],
            "resume_set": 0,
        }

    with dj.lock:
        song = dict(dj.current_song) if dj.current_song is not None else None
        previous = dict(dj.last_song) if dj.last_song is not None else None
        # Read the live clock (paused time excluded) rather than the stored
        # current_elapsed: that snapshot belongs to the outgoing song and made
        # the progress bar for a freshly started song jump near its end.
        elapsed = dj._elapsed_locked()
        rating = dj.current_rating
        stopping = dj.stop_event.is_set()
        paused = dj.pause_event.is_set()
        transitioning = bool(getattr(dj, "_transitioning", False))

    with getattr(dj, "batch_lock", _null_lock):
        batch = list(getattr(dj, "batch", []) or [])

    # The preload buffer holds the song(s) already handed out of the batch and
    # decoding right now, so the batch alone reads one song ahead. Put those
    # preloaded tracks at the head of Up Next so it starts with the song that
    # will actually play next.
    queued = []
    song_queue = getattr(dj, "song_queue", None)
    if song_queue is not None:
        try:
            with song_queue.mutex:
                queued = [dict(item) for item in list(song_queue.queue)]
        except Exception:
            queued = []
    queued += batch

    # Prefetch covers off the request thread: the current song and the next
    # couple of tracks get resolved while audio keeps playing, so the UI's
    # /cover request never waits on MusicBrainz from a cold start.
    if song:
        warm_cover(song.get("artist"), song.get("name"))
    for item in queued[:2]:
        if item:
            warm_cover(item.get("artist"), item.get("name") or item.get("title"))

    # A freshly chosen place may have an empty pool: the deck sits silent while
    # discovery fetches and preprocesses the first tracks (often ~1-2 minutes).
    # Report that as "preparing" plus how many playable tracks exist so far, so
    # the UI can show progress instead of a blank STANDBY screen.
    preparing = song is None and not stopping
    ready_count = None
    if preparing:
        try:
            from Music.songs import preprocessed_count
            from Music.preference import PLACE_GENRES
            place = getattr(dj, "place", None)
            genres = PLACE_GENRES.get(place) if place else None
            ready_count = preprocessed_count(genres=genres) + len(queued)
        except Exception:
            ready_count = None

    return {
        "dj_running": True,
        "dj_available": True,
        "playing": bool(song) and not stopping,
        "preparing": preparing,
        "ready_count": ready_count,
        "ready_target": int(getattr(dj, "top_n", 0) or 0) or None,
        "trial": bool(getattr(dj, "_trial", False)),
        "paused": paused,
        "transitioning": transitioning,
        "stopping": stopping,
        "place": getattr(dj, "place", None) or None,
        "song": _shape_song(song, rating, elapsed),
        "previous": {
            "title": previous.get("name"),
            "artist": previous.get("artist"),
        } if previous else None,
        "queue": [
            {"title": (b.get("name") or b.get("title")), "artist": b.get("artist")}
            for b in queued[:12] if b
        ],
        "resume_set": len(getattr(dj, "_saved_set", []) or []),
    }


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_null_lock = _NullLock()


def _mb_query(query: str) -> dict:
    """HTTP GET a MusicBrainz web-service query with the app UA + one retry
    on the 503 rate-limit response MusicBrainz sends when we're too fast."""
    params = {"query": query, "fmt": "json", "limit": 3}
    for _ in range(2):
        resp = requests.get(
            f"{_MUSICBRAINZ_BASE}/recording/",
            params=params,
            headers={"User-Agent": _MUSICBRAINZ_UA},
            timeout=6,
        )
        if resp.status_code == 503:
            time.sleep(1)
            continue
        resp.raise_for_status()
        return resp.json()
    raise HTTPException(503, "MusicBrainz rate-limited (too many lookups).")


def _mb_release_group_ids(title: str, artist: str):
    """All release-group ids returned for this artist+title, best first."""
    title = re.sub(r'[^\w .\-&]', " ", title).strip()
    artist = re.sub(r'[^\w .\-&]', " ", artist).strip()
    query = f'recording:"{title}" AND artist:"{artist}"'
    try:
        payload = _mb_query(query)
    except Exception as exc:
        log.warning("musicbrainz lookup failed for %r: %s", title, exc)
        return []
    seen = set()
    ids = []
    for recording in payload.get("recordings") or []:
        for release in recording.get("releases") or []:
            release_group = release.get("release-group") or {}
            gid = release_group.get("id")
            if gid and gid not in seen:
                seen.add(gid)
                ids.append(gid)
    return ids


def _coverart_url(gid: str):
    """One Cover-Art-Archive thumbnail URL for a release-group, or None."""
    try:
        resp = requests.get(
            f"{_COVERART_BASE}/release-group/{gid}",
            headers={"User-Agent": _MUSICBRAINZ_UA},
            timeout=6,
        )
        if resp.status_code != 200:
            return None
        images = resp.json().get("images") or []
        for image in images:
            if not image:
                continue
            thumbs = image.get("thumbnails") or {}
            url = thumbs.get("500") or thumbs.get("250") or image.get("image")
            if url:
                return url
    except requests.RequestException as exc:
        log.warning("coverart archive lookup failed for %s: %s", gid, exc)
    return None


def _cover_cache_key(artist: str, title: str) -> str:
    return f"{(artist or '').strip().lower()}|{(title or '').strip().lower()}"


def _cover_lookup(artist: str, title: str):
    """The network part: MusicBrainz search -> Cover Art Archive. Only the top
    few release groups are probed so the lookup stays sub-second instead of
    walking every pressing of the track."""
    for gid in _mb_release_group_ids(title, artist)[:_COVER_MAX_GIDS]:
        url = _coverart_url(gid)
        if url:
            return url
    return None


def _store_cover(key: str, url):
    _COVER_CACHE[key] = {"url": url, "at": time.time()}
    if len(_COVER_CACHE) > 512:
        keep = sorted(_COVER_CACHE.items(), key=lambda kv: kv[1].get("at", 0))[-256:]
        _COVER_CACHE.clear()
        _COVER_CACHE.update(dict(keep))
    _save_cover_cache()


def cover_for(artist: str, title: str, wait: float = 8.0):
    """Resolve an album-art URL, de-duplicating concurrent look-ups for the same
    song: the first caller does the network work and the rest wait for it rather
    than firing their own MusicBrainz request. Hits and misses are both cached
    for _COVER_TTL, so a coverless song is searched at most once."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return None

    key = _cover_cache_key(artist, title)
    cached = _COVER_CACHE.get(key)
    if cached and time.time() - cached["at"] < _COVER_TTL:
        return cached["url"]

    with _COVER_LOCK:
        existing = _COVER_INFLIGHT.get(key)
        leader = existing is None
        if leader:
            existing = threading.Event()
            _COVER_INFLIGHT[key] = existing
    if not leader:
        existing.wait(wait)
        cached = _COVER_CACHE.get(key)
        return cached["url"] if cached else None

    try:
        url = _cover_lookup(artist, title)
        _store_cover(key, url)
        return url
    finally:
        with _COVER_LOCK:
            _COVER_INFLIGHT.pop(key, None)
        existing.set()


def warm_cover(artist: str, title: str):
    """Fire-and-forget prefetch so a later /cover hit is already resolved. Used
    for the current song and the next tracks in Up Next, which hides the
    MusicBrainz latency behind the currently playing song."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return
    key = _cover_cache_key(artist, title)
    cached = _COVER_CACHE.get(key)
    if cached and time.time() - cached["at"] < _COVER_TTL:
        return
    with _COVER_LOCK:
        if key in _COVER_INFLIGHT or key in _COVER_WARMING:
            return
        _COVER_WARMING.add(key)

    def _run():
        try:
            cover_for(artist, title)
        finally:
            with _COVER_LOCK:
                _COVER_WARMING.discard(key)

    threading.Thread(target=_run, daemon=True).start()


@router.get("/cover")
def cover(artist: str = "", title: str = ""):
    """Album-art URL for a song, resolved via MusicBrainz -> Cover Art Archive.

    Results are cached (in memory and on disk) per artist|title, so repeat plays
    are instant and a restart is still warm. Hits and misses are cached alike."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    return {"artist": artist, "title": title, "url": cover_for(artist, title)}


@router.get("/places")
def places():
    """Every selectable place (built-in + user-created) and the active one."""
    from Music.preference import PLACE_GENRES, custom_places
    custom = custom_places()
    dj = state.dj
    if dj is not None:
        active = getattr(dj, "place", None)
    else:
        from Music.preference import get_active_place
        active = get_active_place()
    return {
        "active": active,
        "places": [
            {"key": key, "genres": list(genres), "custom": key in custom}
            for key, genres in PLACE_GENRES.items()
        ],
    }


class PlaceBody(BaseModel):
    place: str = ""
    genres: list[str] = Field(default_factory=list)


@router.post("/place")
def set_place(body: PlaceBody):
    """Choose the place and start the DJ with it, or create a new place when
    `genres` is supplied. The deck must be stopped first (Stop -> pick ->
    Start), so a running DJ is rejected rather than retargeted mid-stream."""
    from Music.preference import (
        PLACE_GENRES, add_place, normalize_place, set_active_place,
    )
    from api import session

    if session.is_running():
        raise HTTPException(409, "Stop the DJ before changing place.")

    key = normalize_place(body.place)
    if body.genres:
        try:
            key = add_place(key, body.genres)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    elif key and key not in PLACE_GENRES:
        raise HTTPException(404, f"unknown place '{body.place}'")
    if not key:
        raise HTTPException(400, "choose a place before starting the DJ")
    try:
        set_active_place(key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        session.start(key)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True, "place": key,
            "genres": list(PLACE_GENRES.get(key, []))}


@router.delete("/place")
def remove_place(place: str):
    """Delete a custom place along with all of its data: its catalog and pool
    rows, its playlist history and its model pickle.

    Only custom places can be deleted — the built-ins ship with the app. The deck
    must be stopped first, exactly like changing the place, so nothing is pulled
    out of the pool mid-playback.
    """
    from Music.preference import delete_place
    from api import session

    if session.is_running():
        raise HTTPException(409, "Stop the DJ before deleting a place.")

    try:
        result = delete_place(place)
    except ValueError as exc:
        # Unknown or built-in name. 404 reads right for both from the UI's
        # point of view: there is no deletable place by that name.
        raise HTTPException(404, str(exc))
    return {"ok": True, **result}


@router.post("/skip")
def skip():
    if _debounced("skip"):
        # Duplicate from the in-page handler racing the global hotkey daemon.
        return {"ok": True, "action": "skip", "debounced": True}
    dj = _dj()
    with dj.lock:
        before = dj.current_song
    dj.skip()
    return _skip_result(dj, before)


def _skip_result(dj, before):
    """The audio loop picks the skip up and swaps songs just after this POST
    returns. Wait (bounded) for the swap so the response can carry the NEW
    song measured at the instant it started — the page then renders it the
    moment the POST lands instead of lagging a status poll behind the audio.
    If the queue is empty the skip re-arms and keeps the current song; in
    that case we time out and report whatever is actually playing."""
    changed = None
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        with dj.lock:
            cur = dj.current_song
            if cur is not None and cur is not before:
                changed = (dict(cur), dj._elapsed_locked(), dj.current_rating)
                break
        time.sleep(0.02)
    if changed is None:
        with dj.lock:
            changed = (
                dict(dj.current_song) if dj.current_song is not None else None,
                dj._elapsed_locked(),
                dj.current_rating,
            )
    song, elapsed, rating = changed
    return {"ok": True, "action": "skip", "song": _shape_song(song, rating, elapsed)}


@router.post("/full")
def full():
    """Play the current song to its full length: keep the play position but
    drop the hook end-cap so it runs to the track's real end. Counts as a strong
    like signal (like replay: score +350, confidence 1.0) when scored."""
    if _debounced("full"):
        return {"ok": True, "action": "full", "debounced": True}
    dj = _dj()
    dj.play_whole()
    return {"ok": True, "action": "full"}


@router.post("/previous")
def previous():
    """Step back one song through the place's play history. A replayed song
    counts as a deliberate listen-back (score +350, confidence 1.0) when scored.
    replayed=False means there was nothing earlier to step back to."""
    dj = _dj()
    if not dj.can_play_previous():
        return {"ok": True, "action": "previous", "replayed": False}
    dj.play_previous()
    return {"ok": True, "action": "previous", "replayed": True}


@router.post("/restart")
def restart():
    dj = _dj()
    dj.restart()
    return {"ok": True, "action": "restart"}


@router.post("/seek-forward")
def seek_forward():
    dj = _dj()
    dj.seek_forward()
    return {"ok": True, "action": "seek-forward"}


@router.post("/seek-backward")
def seek_backward():
    dj = _dj()
    dj.seek_backward()
    return {"ok": True, "action": "seek-backward"}


@router.post("/pause")
def pause():
    """Toggle pause. The deck freezes: no audio is written and the elapsed
    clock stands still, so the UI's progress bar holds. Resuming shifts the
    song clock past the paused span and picks up where it left off."""
    dj = _dj()
    if _debounced("pause"):
        # Duplicate from the in-page handler racing the global hotkey daemon.
        return {"ok": True, "action": "pause", "paused": dj.paused,
                "debounced": True}
    dj.toggle_pause()
    return {"ok": True, "action": "pause", "paused": dj.paused}


@router.post("/resume")
def resume():
    dj = _dj()
    dj.resume()
    return {"ok": True, "action": "resume", "paused": dj.paused}


@router.post("/rate")
def rate(body: RateBody):
    if _debounced(f"rate:{body.rating}"):
        return {"ok": True, "rating": body.rating, "debounced": True}
    dj = _dj()
    dj.rate_current(body.rating)
    return {"ok": True, "rating": body.rating}


@router.post("/stop")
def stop():
    """Stop the DJ and return to the place gate. Changing place is Stop -> pick
    -> Start, so this is the only way to retarget the deck."""
    from api import session

    return {"ok": True, "action": "stop", "stopped": session.stop()}


@router.get("/stream")
def stream(fresh: bool = False):
    """Live MP3 of whatever the DJ is playing, for the UI's <audio> element.

    The DJ still mixes hooks/crossfades/effects; only the final output goes to
    the browser instead of the machine's speakers. Reconnecting (e.g. after
    restart/seek) naturally resumes from the live edge. `?fresh=1` subscribes
    WITHOUT the ~0.75s tail seed so a skip lands cleanly on the next song
    instead of replaying the outgoing one's last moments."""
    sink = state.sink
    if sink is None or not sink.alive:
        raise HTTPException(503, "no audio stream available (DJ not running).")
    sid, q = sink.subscribe(fresh=fresh)

    def gen():
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    return
                yield chunk
        except GeneratorExit:
            pass
        finally:
            sink.unsubscribe(sid)

    return StreamingResponse(
        gen(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            # Keep any reverse proxy from buffering the live stream, which would
            # add seconds of delay between the DJ and the listener.
            "X-Accel-Buffering": "no",
        },
    )