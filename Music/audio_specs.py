import http.client
import urllib.parse
import json
import os
import re
import threading
import time


_FEATURE_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feature_cache.json")
_CACHE_LOCK = threading.Lock()
_FEATURE_CACHE = None

_RATE_LOCK = threading.Lock()
_LAST_API_CALL = 0.0
MIN_CALL_INTERVAL = 0.8  # seconds enforced between ReccoBeats calls (pool-wide)
_MAX_429_MSG = 0.0


class RateLimited(Exception):
    """Raised when ReccoBeats keeps returning 429/5xx after backoff retries."""


def _throttle():
    global _LAST_API_CALL
    with _RATE_LOCK:
        wait = MIN_CALL_INTERVAL - (time.time() - _LAST_API_CALL)
        if wait > 0:
            time.sleep(wait)
        _LAST_API_CALL = time.time()


def _rate_limit_notice(reset=True):
    global _MAX_429_MSG
    now = time.time()
    if now - _MAX_429_MSG > 30:  # print at most once per 30s
        print("ReccoBeats rate limit hit — backing off and retrying...")
        _MAX_429_MSG = now


def _close_response(res):
    if res is not None:
        try:
            res.close()
        except Exception:
            pass


def _get_json(path):
    """Throttled GET helper with exponential backoff on 429/5xx/timeouts.
    Returns parsed JSON, None on a definitive miss, or raises RateLimited."""
    _throttle()
    backoff = 2
    for attempt in range(4):  # up to 3 retries
        conn = http.client.HTTPSConnection("api.reccobeats.com", timeout=10)
        headers = {'Accept': 'application/json'}
        res = None
        try:
            conn.request("GET", path, '', headers)
            res = conn.getresponse()
            data = res.read().decode("utf-8", errors="replace")
            # Close the response BEFORE the connection: closing the socket
            # underneath an open HTTPResponse makes its GC finalizer flush a
            # closed file ("Exception ignored while finalizing ..."). The
            # object itself keeps its attributes, so status checks still work.
            _close_response(res)
        except (TimeoutError, OSError) as e:
            print(f"ReccoBeats network error: {e}")
            _close_response(res)
            if attempt < 3:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None
        finally:
            _close_response(res)
            try:
                conn.close()
            except Exception:
                pass

        if res.status == 200:
            return json.loads(data)
        if res.status == 429 or res.status >= 500:
            _rate_limit_notice()
            if attempt < 3:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RateLimited(f"ReccoBeats rate-limited (HTTP {res.status})")
        return None  # 4xx other than 429 = song not found
    return None


def _cache_norm(query):
    return re.sub(r'\s+', ' ', query.strip().lower())


def _load_cache():
    global _FEATURE_CACHE
    with _CACHE_LOCK:
        if _FEATURE_CACHE is None:
            try:
                with open(_FEATURE_CACHE_PATH, 'r', encoding='utf-8') as f:
                    _FEATURE_CACHE = json.load(f)
            except (OSError, ValueError):
                _FEATURE_CACHE = {}


def _persist_cache():
    tmp = _FEATURE_CACHE_PATH + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_FEATURE_CACHE, f)
        os.replace(tmp, _FEATURE_CACHE_PATH)
    except OSError as e:
        print(f"Feature cache save error: {e}")


def _lookup(song_name, with_artist, artist_name=None):
    key = _cache_norm(f"{song_name}|{artist_name}" if with_artist else song_name)
    _load_cache()
    hit = _FEATURE_CACHE.get(key)
    if hit is not None:
        if hit.get('_miss'):
            return None
        return dict(hit)

    try:
        feats = get_features_by_name(song_name, artist_name if with_artist else None)
    except RateLimited:
        # A rate limit is NOT a "song not found" — do NOT bake it into the cache
        return None

    with _CACHE_LOCK:
        if feats is not None:
            _FEATURE_CACHE[key] = feats
        else:
            _FEATURE_CACHE[key] = {"_miss": True}
        _persist_cache()
    return feats


def get_features_cached(song_name, artist_name=None):
    """get_features_by_name() wrapped with an on-disk cache, so songs already
    analyzed (in this or a previous run) skip the slow ReccoBeats round-trip.
    A negative result is cached too, so a query known to fail (e.g. artist
    string that breaks the API) never triggers a redundant network call."""
    feats = _lookup(song_name, True, artist_name)
    if feats is None and artist_name:
        feats = _lookup(song_name, False)
    return feats


def search_reccobeats(song_name, artist_name=None):
    query_text = f"{song_name} {artist_name}" if artist_name else song_name
    encoded = urllib.parse.urlencode({"searchText": query_text})
    return _get_json(f"/v1/track/search?{encoded}")

def get_audio_features(track_id):
    return _get_json(f"/v1/track/{track_id}/audio-features")


def get_features_by_name(song_name, artist_name=None):
    """
    Returns a dict with bpm, energy, danceability, valence,
    acousticness, instrumentalness (all floats), or None if not found.
    """
    results = search_reccobeats(song_name, artist_name)
    if not results or not results.get("content"):
        return None

    track_id = results["content"][0]["id"]
    features = get_audio_features(track_id)
    if not features:
        return None

    return {
        "bpm": float(features.get("tempo", 0.0)),
        "energy": float(features.get("energy", 0.0)),
        "danceability": float(features.get("danceability", 0.0)),
        "valence": float(features.get("valence", 0.0)),
        "acousticness": float(features.get("acousticness", 0.0)),
        "instrumentalness": float(features.get("instrumentalness", 0.0)),
    }


# Usage
if __name__=="__main__":
    data = get_features_by_name("Blinding Lights")
    print(data)
# {'bpm': 171.0, 'energy': 0.73, 'danceability': 0.51, 'valence': 0.33,
#  'acousticness': 0.0011, 'instrumentalness': 0.0, ...}
