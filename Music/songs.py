import musicbrainzngs
import random
import re
import sqlite3
import sys
import os
import threading
import time
import json
import collections
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from Music.audio_specs import get_features_cached
from Music.preference import PLACE_GENRES
import pprint

# Make DJ_YTDLP_COOKIES_FILE (and the other keys) available even when this
# module is run directly (e.g. `python Music/songs.py --prefill`), not only
# through dj.py's import chain.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_yt = None


def _get_yt():
    """Shared, reusable YTMusic client (auth-free). Creating it once keeps the
    internal session warm — searches are then fast JSON API calls."""
    global _yt
    if _yt is None:
        from ytmusicapi import YTMusic
        _yt = YTMusic("Music/headers_auth.json")
    return _yt


def _reset_yt():
    """Drop the shared YTMusic client so the next call builds a fresh one.
    YouTube's anonymous innerTube sessions go stale / get bot-checked and
    start returning empty bodies ('Expecting value: line 1 column 1') — a
    new client usually clears it."""
    global _yt
    _yt = None


_yt_error_logged_at = [0.0]


def _log_ytscrape_error(prefix, exc):
    """Print scrape errors at most once per 30s so a dead API doesn't spam
    the console once per song."""
    now = time.time()
    if now - _yt_error_logged_at[0] > 30:
        _yt_error_logged_at[0] = now
        print(f"{prefix}: {exc}")

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# GENRES is no longer hard-coded: it is the union of every genre list in
# preference.py's PLACE_GENRES (place -> [genres]). Each fetched song is tagged
# with the PLACE_GENRES key it was picked for, so a track knows which place it
# was chosen to fit and can be flagged per-key in the Agent table.
GENRES = sorted({genre for genres in PLACE_GENRES.values() for genre in genres})


def places_for(genre):
    """PLACE_GENRES keys whose genre list includes `genre` (dict order)."""
    if not genre:
        return []
    return [place for place, genres in PLACE_GENRES.items() if genre in genres]


def place_for_genre(genre, default=None):
    """The first PLACE_GENRES key containing `genre`, else `default`."""
    places = places_for(genre)
    return places[0] if places else default


def _random_place_genre():
    """Pick a random (place key, genre) pair straight from PLACE_GENRES."""
    place = random.choice(list(PLACE_GENRES))
    return place, random.choice(PLACE_GENRES[place])
ARTISTS = {
    "Drake": "hip-hop", "Kendrick Lamar": "hip-hop", "Kanye West": "hip-hop",
    "Jay-Z": "hip-hop", "50 Cent": "hip-hop", "Snoop Dogg": "hip-hop",
    "Dr. Dre": "hip-hop", "Eminem": "hip-hop", "Post Malone": "hip-hop",
    "Travis Scott": "hip-hop", "Mac Miller": "hip-hop", "Kid Cudi": "hip-hop",
    "Cardi B": "hip-hop", "Megan Thee Stallion": "hip-hop",

    "Taylor Swift": "pop", "Ariana Grande": "pop", "Dua Lipa": "pop",
    "Ed Sheeran": "pop", "Billie Eilish": "pop", "Justin Bieber": "pop",
    "Adele": "pop", "Sam Smith": "pop", "Lady Gaga": "pop", "Bruno Mars": "pop",

    "The Weeknd": "r&b", "Rihanna": "r&b", "Frank Ocean": "r&b",
    "SZA": "r&b", "Doja Cat": "r&b",

    "Calvin Harris": "edm", "David Guetta": "edm", "Tiësto": "edm",
    "Avicii": "edm", "Deadmau5": "edm", "Skrillex": "edm", "Marshmello": "edm",
    "Daft Punk": "edm",

    "Metallica": "metal", "Nirvana": "rock", "Foo Fighters": "rock",
    "Red Hot Chili Peppers": "rock", "Radiohead": "rock", "The Beatles": "rock",
    "Queen": "rock", "Led Zeppelin": "rock", "Pink Floyd": "rock", "AC/DC": "rock",
    "Arctic Monkeys": "indie", "The Strokes": "indie", "Tame Impala": "indie",

    "Miles Davis": "jazz", "John Coltrane": "jazz", "Louis Armstrong": "jazz",
    "Ella Fitzgerald": "jazz", "Nina Simone": "jazz",

    "Bob Marley": "reggae", "Fela Kuti": "funk", "Toots and the Maytals": "reggae",
    "Stevie Wonder": "soul", "Marvin Gaye": "soul", "Aretha Franklin": "soul",
    "James Brown": "funk", "Earth, Wind & Fire": "funk",

    "Daddy Yankee": "latin", "Shakira": "latin", "J Balvin": "latin",
    "Karol G": "latin", "Rosalía": "latin",

    "Frédéric Chopin": "classical", "Ludwig van Beethoven": "classical",
    "Wolfgang Amadeus Mozart": "classical", "Johann Sebastian Bach": "classical",
}


# canonical database lives in the project's Database/ folder on all OSes
DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Database")
DB_PATH = os.path.join(DB_DIR, "music.db")
os.makedirs(DB_DIR, exist_ok=True)
# Single shared connection + one lock: serializes every DB access so concurrent
# refill/batch/save threads never trip over each other (avoids "database is
# locked" / "disk I/O error" under WAL on Windows).
DB_LOCK = threading.Lock()
_db_conn = None

musicbrainzngs.set_useragent("DjAgent", "1.0.0", "https://github.com/13lend1")

def _played_already(song_id):
    """True if the song was already played (recorded in a genre table)."""
    try:
        from duplicates import is_played
        return is_played(song_id)
    except Exception as e:
        print("Played-check failed, treating song as new:", e)
        return False


def get_random_song(max_attempts=5, place=None):
    for attempt in range(1, max_attempts + 1):
        song = _fetch_random_song(place=place)
        if song and not _played_already(song.get('id')):
            return song
        print(f"Attempt {attempt}/{max_attempts} returned an already-played song, "
              "refetching a new one...")

    print("Could not find a playable, unplayed song after several attempts.")
    return None


def _mb_search(query, attempts=3):
    """MusicBrainz recording search with retry/backoff. `query` is a full
    Lucene query string (e.g. 'tag:house' or 'artist:"Drake"'). Returns a
    result dict or None if the API is unreachable."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return musicbrainzngs.search_recordings(query=query, limit=100)
        except Exception as e:
            last = e
            print(f"MusicBrainz error (attempt {attempt}/{attempts}): {e}")
            time.sleep(1 + attempt * 2)
    print(f"MusicBrainz unreachable after {attempts} attempts, using YTMusic fallback.")
    return last


# MusicBrainz enforces strict rate limits and drops connections when hammered,
# so all MB queries are serialized through one lock, and up to 100 recordings
# are pulled per query then shared from a queue instead of 1 query per song.
MB_LOCK = threading.Lock()
_MB_CANDIDATES = collections.deque()

def _refill_queue(used_artists=(), used_genres=(), place=None):
    # Always run an artist query (plus a tag query) so named artists from the
    # ARTISTS dict get real airtime instead of being drowned by tag results.
    specs = _random_query_specs(2, used_artists=used_artists, used_genres=used_genres, place=place)
    got_any = False
    for kind, value, genre, place in specs:
        result = _mb_search(_mb_query_string(kind, value))
        if not (result and isinstance(result, dict) and result.get("recording-list")):
            continue

        recordings = result["recording-list"]
        if kind == "artist":
            # an artist query returns ~100 tracks by the SAME artist — keep a
            # few so the artist actually surfaces, while the per-artist cap in
            # get_random_songs still stops the batch from flooding
            recordings = random.sample(recordings, k=min(3, len(recordings)))

        for rec in recordings:
            title = rec.get('title')
            artist = rec.get('artist-credit-phrase')
            if not title or not artist:
                continue
            if _title_matches_genre(title, genre):
                continue
            length = rec.get('length')
            album = None
            year = None
            release_list = rec.get('release-list', [])
            if release_list:
                album = release_list[0].get('release-group', {}).get('title')
                date = release_list[0].get('date')
                if date:
                    year = date[:4]
            _MB_CANDIDATES.append({
                'id': rec.get('id', ''),
                'title': title,
                'artist': artist,
                'album': album,
                'year': year,
                'duration': int(length) if length and length.isdigit() else None,
                'genre': genre,
                'place': place,
                'source': 'artist' if kind == "artist" else 'tag',
            })
            got_any = True
    if got_any:
        random.shuffle(_MB_CANDIDATES)
        return True
    return False

def _genre_specs(place, used_genres):
    """(kind, value, genre, place) tuples for genre/tag queries. With a place,
    only that place's genres are candidates, so a place-capped run plays (and
    tags) exclusively its own genres."""
    if place:
        return [("genre", g, g, place)
                for g in PLACE_GENRES.get(place, []) if g not in used_genres]
    return [("genre", g, g, p)
            for p, genres in PLACE_GENRES.items()
            for g in genres if g not in used_genres]


def _artist_specs(place, used_artists):
    """Artist-based specs. With a place, only artists whose genre belongs to
    that place's genre list are candidates (keeps named artists on-place)."""
    if place:
        place_genres = PLACE_GENRES.get(place, [])
        return [("artist", a, g, place) for a, g in ARTISTS.items()
                if a not in used_artists and g in place_genres]
    return [("artist", a, g, place_for_genre(g, random.choice(list(PLACE_GENRES))))
            for a, g in ARTISTS.items() if a not in used_artists]


def _random_query_specs(k=3, used_artists=(), used_genres=(), place=None):
    """Samples k (kind, value, genre, place) tuples so a refill batch mixes
    tag- and artist-based discovery while preferring genres/artists that aren't
    already saturated. Every genre carries the PLACE_GENRES key it came from, so
    the produced tracks can be flagged with that place. When place is set, only
    that place's genres (and its artists) are ever sampled."""
    g_pool = _genre_specs(place, used_genres)
    a_pool = _artist_specs(place, used_artists)
    if len(g_pool) < k // 2:
        g_pool = _genre_specs(place, set())
    if len(a_pool) < k - k // 2:
        a_pool = _artist_specs(place, set())
    if k == 1:
        # single query: flip a coin between a random place-genre and a random artist
        if not g_pool and not a_pool:
            if place and PLACE_GENRES.get(place):
                genre = random.choice(PLACE_GENRES[place])
                return [("genre", genre, genre, place)]
            place, genre = _random_place_genre()
            return [("genre", genre, genre, place)]
        if random.random() < 0.5:
            return [random.choice(g_pool)] if g_pool else [random.choice(a_pool)]
        return [random.choice(a_pool)] if a_pool else [random.choice(g_pool)]
    n_g = min(len(g_pool), k - k // 2)
    n_a = min(len(a_pool), k - n_g)
    n_g = min(n_g, k - n_a)
    specs = random.sample(g_pool, k=n_g) + random.sample(a_pool, k=n_a)
    random.shuffle(specs)
    if specs:
        return specs
    if place and PLACE_GENRES.get(place):
        genre = random.choice(PLACE_GENRES[place])
        return [("genre", genre, genre, place)]
    place, genre = _random_place_genre()
    return [("genre", genre, genre, place)]


def _mb_query_string(kind, value):
    if kind == "artist":
        return f'artist:"{value}"'
    return f"tag:{value}"

def _title_matches_genre(title, genre):
    """True when the song title contains any base word of its genre as a word,
    e.g. 'Rock Rock' (genre 'rock' or 'indie rock') and 'Pop Pop' ('indie pop').
    Each genre is split into base words on spaces AND hyphens, so 'indie pop'
    rejects 'Pop Pop', and 'hip-hop' rejects 'Hip Hop Anthems'. Whole-word
    matching keeps 'pop' from rejecting genuine tracks like 'Popular'."""
    if not title or not genre:
        return False
    words = [w for w in re.split(r'[\s-]+', genre) if w]
    if not words:
        return False
    pattern = r'\b(?:' + '|'.join(re.escape(w) for w in words) + r')\b'
    return bool(re.search(pattern, title, re.IGNORECASE))


def _next_candidate(used_artists=(), used_genres=(), place=None):
    with MB_LOCK:
        while True:
            if _MB_CANDIDATES:
                cand = _MB_CANDIDATES.popleft()
                # Named artists are only capped per-artist; the genre cap must
                # not wash them out once their genre (pop, hip-hop, ...) is
                # saturated by tag-based tracks. Tag-sourced tracks still
                # respect the genre cap.
                if cand['artist'] in used_artists:
                    continue
                if cand.get('source') != 'artist' and cand['genre'] in used_genres:
                    continue
                if place and cand.get('place') and cand['place'] != place:
                    continue
                if _title_matches_genre(cand.get('title') or cand.get('name'), cand.get('genre')):
                    continue
                return cand

            if _refill_queue(used_artists=used_artists, used_genres=used_genres, place=place):
                continue  # loop pops the freshly queued candidate

            kind, value, genre, place = random.choice(_random_query_specs(
                k=1, used_artists=used_artists, used_genres=used_genres, place=place))
            song = _yt_fallback(value, genre, place=place)
            if song is not None:
                if _title_matches_genre(song.get('name'), song.get('genre')):
                    return None
                song['source'] = 'artist' if kind == "artist" else 'tag'
            return song
    
def _parse_duration(dur):
    """Converts YTMusic's 'm:ss' or 'h:mm:ss' strings into milliseconds."""
    if not dur:
        return None
    try:
        parts = dur.split(':')
        if len(parts) == 2:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        if len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
    except ValueError:
        pass
    return None


def _best_match(results, title, artist=None):
    """Picks the search result that best matches the expected song. Scores on
    both title and artist word overlap (case/punctuation-insensitive), so a
    cover or a same-named song by a different artist loses to the real one.
    Returns None if nothing matches well enough, so a wrong link is never
    returned."""
    wanted_title = set(re.sub(r'[^a-z0-9 ]', ' ', title.lower()).split())
    wanted_artist = set(re.sub(r'[^a-z0-9 ]', ' ', (artist or '').lower()).split())
    best, best_score = None, 0.0
    best_title_score = 0.0
    for v in results:
        v_title = set(re.sub(r'[^a-z0-9 ]', ' ', (v.get('title') or '').lower()).split())
        v_artist = set()
        for a in (v.get('artists') or []):
            v_artist |= set(re.sub(r'[^a-z0-9 ]', ' ', (a.get('name') or '').lower()).split())
        if not wanted_title or not v_title:
            continue
        title_score = len(wanted_title.intersection(v_title)) / len(wanted_title)
        artist_score = (len(wanted_artist.intersection(v_artist)) / len(wanted_artist)
                        if wanted_artist and v_artist else 0.0)
        score = title_score + 0.6 * artist_score
        if score > best_score:
            best_score, best, best_title_score = score, v, title_score
    # Must match the title well and, when we know the artist, agree on it too.
    if best is None or best_score < 0.7 or (artist and best_title_score <= 0.5):
        return None
    return best


def _yt_search_link(title, artist, max_results=10):
    """Find the song's link via YTMusic (primary). The search is retried once
    with a fresh client for transient/blank innertube responses; yt-dlp is
    only a last-resort backup when the retry also fails or returns no match."""
    time.sleep(0.1)
    for attempt in range(2):
        try:
            results = _get_yt().search(f"{title} {artist}", filter="songs", limit=max_results)
            video = _best_match(results, title, artist)
            if video and video.get('videoId'):
                return f"https://music.youtube.com/watch?v={video['videoId']}"
            break  # definitive miss or no good match -> no retry needed
        except Exception as e:
            _log_ytscrape_error("YTMusic search error", e)
            _reset_yt()
        if attempt == 0:
            continue
    return _ytdl_search_link(title, artist)


def _is_yt_blank_response(exc):
    """True when ytmusicapi choked on an empty/invalid body ('Expecting value')
    rather than a genuine network error."""
    return isinstance(exc, json.JSONDecodeError) or "Expecting value" in str(exc)

_ytdl = None

def _get_ytdl():
    global _ytdl
    if _ytdl is None:
        import yt_dlp
        import shutil
        from yt_dlp.networking.impersonate import ImpersonateTarget
        deno_path = shutil.which('deno')
        options = {
            'quiet': True,
            'noplaylist': True,
            'skip_download': True,
            'extract_flat': True,
            'default_search': 'ytsearch',
            'socket_timeout': 20,
            'retries': 3,
            'impersonate': ImpersonateTarget.from_str('chrome'),
            'force_ipv4': True,
            'js_runtimes': {'deno': {'path': deno_path}} if deno_path else {'deno': {}},
            'remote_components': ['ejs:github'],
            'http_headers': {'Accept': '*/*'},
        }
        cookie_file = os.environ.get('DJ_YTDLP_COOKIES_FILE', '').strip()
        if cookie_file and os.path.isfile(cookie_file):
            options['cookiefile'] = cookie_file
        _ytdl = yt_dlp.YoutubeDL(options)
    return _ytdl

def _reset_ytdl():
    """Drop the shared YoutubeDL instance so the next call builds a fresh
    one — mirrors _reset_yt() for the YTMusic client."""
    global _ytdl
    _ytdl = None


def _ytdl_search_link(title, artist, max_results=5):
    """Last-resort link lookup via yt-dlp when YTMusic is not responding."""
    time.sleep(0.1)
    try:
        ydl = _get_ytdl()
    except Exception as e:
        _log_ytscrape_error("yt-dlp init error", e)
        return None

    try:
        _info_holder = {}

        def _run_search():
            try:
                _info_holder['info'] = ydl.extract_info(
                    f"ytsearch{max_results}:{title} {artist}", download=False)
            except BaseException as exc:
                _info_holder['exc'] = exc

        _search_thread = threading.Thread(target=_run_search, daemon=True)
        _search_thread.start()
        _search_thread.join(timeout=100)
        if _search_thread.is_alive():
            _reset_ytdl()
            _log_ytscrape_error("yt-dlp search error", TimeoutError("extract timed out"))
            return None
        if 'exc' in _info_holder:
            raise _info_holder['exc']
        info = _info_holder.get('info')
    except Exception as e:
        _log_ytscrape_error("yt-dlp search error", e)
        _reset_ytdl()  # cheap; a broken/blocked session shouldn't stick around
        return None

    for ent in (info or {}).get('entries') or []:
        if ent and ent.get('id') and not ent.get('is_live'):
            return f"https://www.youtube.com/watch?v={ent['id']}"
    return None

def _yt_fallback(term, genre, place=None):
    try:
        results = _get_yt().search(term, filter="songs", limit=20)
    except Exception as e:
        _log_ytscrape_error("YTMusic fallback search error", e)
        if _is_yt_blank_response(e):
            _reset_yt()
        return None
    if not results:
        return None
    video = random.choice(results)
    title = video.get('title')
    if not title or not video.get('videoId'):
        return None
    artist = video['artists'][0]['name'] if video.get('artists') else "Unknown Artist"
    album = video['album']['name'] if video.get('album') else None
    return {
        'link': f"https://music.youtube.com/watch?v={video['videoId']}",
        'name': title,
        'album': album,
        'artist': artist,
        'genre': genre,
        'place': place or place_for_genre(genre),
        'duration': _parse_duration(video.get('duration')),
        'year': None,
        'id': video['videoId'],
    }
    
def _fetch_random_song(place=None):
    kind, value, genre, place = random.choice(_random_query_specs(k=1, place=place))
    result = _mb_search(_mb_query_string(kind, value))

    recording = None
    if result and isinstance(result, dict) and result.get("recording-list"):
        recordings = list(result["recording-list"])
        random.shuffle(recordings)
        # Skip recordings whose title contains the genre name (e.g. a track
        # literally titled 'House' for the house query).
        for rec in recordings:
            if _title_matches_genre(rec.get('title'), genre):
                continue
            recording = rec
            break

    if recording:
        song_id = recording.get('id')
        title = recording.get('title')
        artist = recording.get('artist-credit-phrase')
        if not title or not artist:
            return None

        length = recording.get('length')
        length_ms = int(length) if length and length.isdigit() else None

        album = None
        year = None
        release_list = recording.get('release-list', [])
        if release_list:
            release = release_list[0]
            album = release.get('release-group', {}).get('title')
            date = release.get('date')
            if date:
                year = date[:4]

        link = _yt_search_link(title, artist)
        if not link:
            return None

        return {'link': link, 'name': title, 'album': album, 'artist': artist,
                'genre': genre, 'place': place, 'duration': length_ms, 'year': year, 'id': song_id}

    song = _yt_fallback(value, genre, place=place)
    if song is None:
        print("Could not find a playable song (MusicBrainz + YTMusic both failed).")
    elif _title_matches_genre(song.get('name'), song.get('genre')):
        return None
    return song

def _build_song(cand, max_attempts=3):
    """Turns a candidate into a playable, feature-complete song dict (or None).
    YTMusic link lookup + cached ReccoBeats features, retried a few times.
    Genre-named tracks ('Rock Rock' for genre rock) are rejected up front."""
    if _title_matches_genre(cand.get('title') or cand.get('name'), cand.get('genre')):
        return None
    for _ in range(max_attempts):
        try:
            if cand.get('link'):
                song = cand.copy()
            else:
                link = _yt_search_link(cand['title'], cand['artist'])
                if not link:
                    continue
                song = {
                    'link': link,
                    'name': cand['title'],
                    'album': cand['album'],
                    'artist': cand['artist'],
                    'genre': cand['genre'],
                    'place': cand.get('place'),
                    'duration': cand['duration'],
                    'year': cand['year'],
                    'id': cand['id'],
                }
            features = get_features_cached(song['name'], song['artist'])
            if features is None:
                return None
            song.update(features)
            return song
        except Exception as e:
            print(f"[fail] {cand.get('title')}: {e}")
            continue
    return None


def get_random_songs(n=20, max_attempts=5, batch_size=5, on_song=None, workers=3, place=None):
    """Fetches n songs with parallel link discovery + feature extraction.
    MusicBrainz queries stay serialized (their rate limit), while YTMusic and
    ReccoBeats calls run across `workers` threads. Features are disk-cached.
    With a place, every fetched song comes from that place's genres."""
    songs = []
    seen = set()
    got = 0
    skips = 0
    max_skips = n * 3  # safety valve: stop refetching if we keep hitting played songs
    state_lock = threading.Lock()

    # Diversity caps: never flood a batch with one artist, and keep any single
    # genre to at most ~1/4 of the batch so the selection stays varied.
    artist_max = 2
    genre_max = max(1, n // 4)
    artist_count = collections.Counter()
    genre_count = collections.Counter()

    def _used_sets():
        return (set(a for a, c in artist_count.items() if c >= artist_max),
                set(g for g, c in genre_count.items() if c >= genre_max))

    def _job(_place):
        used_artists, used_genres = _used_sets()
        cand = _next_candidate(used_artists=used_artists, used_genres=used_genres, place=_place)
        if cand is None:
            return None
        return _build_song(cand, max_attempts=max_attempts)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = set()
        while got < n:
            if skips >= max_skips:
                print(f"Stopped refetching: {skips} fetched songs were already "
                      "played — widen the catalog or clear the genre tables.")
                break
            while len(futures) < workers and got < n:
                futures.add(ex.submit(_job, place))
            done, futures = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
            for f in done:
                song = f.result()
                if song is None:
                    skips += 1  # link/feature failure or a rejected genre-named track
                    continue
                if _played_already(song.get('id')):
                    skips += 1
                    continue  # already played — refetch another song instead
                with state_lock:
                    if song['id'] in seen:
                        continue
                    named = song['artist'] in ARTISTS
                    if artist_count[song['artist']] >= artist_max or \
                       (not named and genre_count[song['genre']] >= genre_max):
                        continue  # over a diversity cap — don't count this one
                    seen.add(song['id'])
                    artist_count[song['artist']] += 1
                    genre_count[song['genre']] += 1
                    got += 1
                    progress = got
                # print(f"[{progress}/{n}] {song['name']} - {song['artist']}")
                if on_song:
                    on_song(song)
                songs.append(song)

    return songs


def fill_preprocessed(target=300, workers=3, place=None):
    """Pre-fills the Preprocessed pool up to `target` songs.

    Run it before starting the DJ so the pool is already stocked, e.g.:
        python fill_preprocessed.py 300
    Existing rows are kept and duplicates are skipped. With a place, only that
    place's genres are fetched (and a place-filtered count is used).
    """
    have = preprocessed_count(place=place)
    if have >= target:
        print(f"Preprocessed already has {have} songs (target {target}) — nothing to do.")
        return 0

    need = target - have
    added = 0
    last_report = 0

    def on_song(song):
        nonlocal added, last_report
        if save_preprocessed(song):
            added += 1
            now = time.time()
            if now - last_report >= 10 or added == need:
                print(f"  progress: {have + added}/{target} added so far")
                last_report = now

    print(f"Filling Preprocessed: {have} -> {target} (need {need} more)...")
    t0 = time.time()
    get_random_songs(n=need, workers=workers, on_song=on_song, place=place)
    elapsed = time.time() - t0
    print(f"Done in {elapsed:.0f}s. Preprocessed: {have + added}/{target} (+{added} new).")
    checkpoint()
    return added


def checkpoint():
    """Flushes any WAL journal into music.db so the rows are always visible
    when you open the .db file directly (DB Browser, sqlite3, etc.)."""
    try:
        conn = _get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        print("WAL checkpointed — rows are visible in Database/music.db.")
    except sqlite3.Error as e:
        print(f"Checkpoint skipped (WAL stays active, data still safe): {e}")
def _get_conn():
    global _db_conn
    if _db_conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)  # guard against missing folder
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA mmap_size=0;")   # disable mmap — this is what usually throws "disk I/O error" on Windows/synced/AV-scanned drives
        try:
            # Prefer DELETE journal mode, but never crash the open if the DB is
            # already in WAL mode / held by another tool: WAL works fine on the
            # shared serialized connection too.
            conn.execute("PRAGMA journal_mode=DELETE;")
        except sqlite3.Error:
            conn.execute("PRAGMA journal_mode=WAL;")
        _db_conn = conn
    return _db_conn


def _db_exec(fn, *args, retries=5):
    """Runs fn(conn, *args) under the write lock, retrying transient
    SQLite lock / disk I/O errors with a short backoff."""
    last = None
    for attempt in range(retries):
        try:
            with DB_LOCK:
                return fn(_get_conn(), *args)
        except sqlite3.Error as e:
            last = e
            time.sleep(0.2 * (attempt + 1))
    raise last


def compute_likeability_and_confidence(end_reason, skip_position_sec, hook_length_sec,
                                       replayed, liked, saved):
    # explicit dislike overrides everything else
    if liked is False:
        return 0.0, 1.0

    score = 500
    if end_reason == 'skipped' and skip_position_sec is not None and hook_length_sec:
        pct_through = skip_position_sec / hook_length_sec
        score -= 500 * (1 - pct_through)
    if end_reason == 'interrupted':
        score = 500
    if replayed:
        score += 350
    if liked is True:
        score += 150
    if saved:
        score += 200
    score = max(0, min(1000, score))
    likeability = score / 1000

    # confidence: max across whichever signals fired, not additive
    confidences = [0.0]
    if end_reason == 'interrupted':
        confidences.append(0.1)
    if end_reason == 'finished' and not (replayed or liked is not None or saved):
        confidences.append(0.3)
    if end_reason == 'skipped':
        pct_through = (skip_position_sec / hook_length_sec) if hook_length_sec else 0
        confidences.append(1.0 if pct_through < 0.3 else 0.6)
    if replayed:
        confidences.append(1.0)
    if liked is True:
        confidences.append(1.0)
    if saved:
        confidences.append(1.0)

    confidence = max(confidences)
    return likeability, confidence


def save(song):
    required = ['bpm', 'energy', 'danceability', 'valence', 'acousticness', 'instrumentalness']
    if not all(song.get(k) is not None for k in required):
        # only true for the get_random_song() cold-start fallback path
        features = get_features_cached(song['name'], song['artist'])
        if features is None:
            print(f"Save skipped (extraction failed): {song['name']}")
            return
        song.update(features)

    def _run(conn):
        liked_db = song.get('liked')  # 0=like, 1=dislike, None=unrated
        if liked_db is None:
            liked = None
        else:
            liked = liked_db == 0
        hook_length = song.get('hook_length')
        if hook_length is None:
            start = song.get('play_start_sec')
            end = song.get('play_end_sec')
            if start is not None and end is not None:
                hook_length = round(max(0.0, end - start), 1)
        likeability, confidence = compute_likeability_and_confidence(
            end_reason=song.get('end_reason'),
            skip_position_sec=song.get('skipp'),
            hook_length_sec=hook_length,
            replayed=bool(song.get('replayed', 0)),
            liked=liked,
            saved=bool(song.get('saved', 0)),
        )
        conn.execute(
            """
            INSERT INTO Songs
                (id, name, artist, album, genre, year, link, duration,
                bpm, energy, danceability, valence, acousticness, instrumentalness,
                likeability, confidence, liked, skipp, end_reason, replayed, saved, hook_length, place)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                likeability = COALESCE(excluded.likeability, Songs.likeability),
                confidence = excluded.confidence,
                liked = COALESCE(excluded.liked, Songs.liked),
                skipp = excluded.skipp,
                end_reason = excluded.end_reason,
                replayed = MAX(COALESCE(Songs.replayed, 0), COALESCE(excluded.replayed, 0)),
                saved = MAX(COALESCE(Songs.saved, 0), COALESCE(excluded.saved, 0)),
                hook_length = COALESCE(excluded.hook_length, Songs.hook_length),
                place = COALESCE(excluded.place, Songs.place)
            """,
            (
                song['id'], song['name'], song['artist'], song['album'],
                song['genre'], song['year'], song['link'], song['duration'],
                song['bpm'], song['energy'], song['danceability'],
                song['valence'], song['acousticness'], song['instrumentalness'],
                likeability, confidence, song.get('liked'), song.get('skipp'),
                song.get('end_reason'), song.get('replayed', 0),
                song.get('saved', 0), hook_length, song.get('place'),
            ),
        )
    _db_exec(_run)


def save_song_metadata(song, likeability=None, reorder=False):
    """Upserts a song's row into Songs. Without likeability it leaves the
    existing value untouched; with a value it writes it into the likeability
    column (used to persist the model's *prediction* for the selected songs).

    When reorder=True (used when the playlist queue is persisted) the row is
    deleted first and re-inserted, so the table keeps rows in the exact order
    they were selected/queued (a plain upsert would leave old rows scattered)."""
    def _run(conn):
        if reorder:
            conn.execute("DELETE FROM Songs WHERE id = ?", (song['id'],))
        conn.execute(
            """
            INSERT INTO Songs
                (id, name, artist, album, genre, year, link, duration,
                bpm, energy, danceability, valence, acousticness, instrumentalness, likeability)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, artist = excluded.artist, album = excluded.album,
                genre = excluded.genre, year = excluded.year, link = excluded.link,
                duration = excluded.duration, bpm = excluded.bpm, energy = excluded.energy,
                danceability = excluded.danceability, valence = excluded.valence,
                acousticness = excluded.acousticness, instrumentalness = excluded.instrumentalness,
                likeability = COALESCE(excluded.likeability, Songs.likeability)
            """,
            (
                song['id'], song['name'], song['artist'], song['album'],
                song['genre'], song['year'], song['link'], song['duration'],
                song['bpm'], song['energy'], song['danceability'],
                song['valence'], song['acousticness'], song['instrumentalness'],
                likeability,
            ),
        )
    _db_exec(_run)


def delete_unscored_songs():
    """Deletes Songs rows with no likeability yet, so the training table only
    holds songs with a meaningful target. Returns the number of rows removed."""
    def _run(conn):
        cur = conn.execute("DELETE FROM Songs WHERE likeability IS NULL")
        return cur.rowcount
    return _db_exec(_run)


def _ensure_preprocessed_place(conn):
    """Add the place column to Preprocessed (once), so the per-track
    PLACE_GENRES key survives the pool and reaches the Agent table."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(Preprocessed)").fetchall()}
    if "place" not in cols:
        conn.execute("ALTER TABLE Preprocessed ADD COLUMN place TEXT")


def save_preprocessed(song):
    def _run(conn):
        _ensure_preprocessed_place(conn)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO Preprocessed
                (id, name, artist, album, genre, place, year, link, duration,
                bpm, energy, danceability, valence, acousticness, instrumentalness)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                song['id'], song['name'], song['artist'], song['album'],
                song['genre'], song.get('place'), song['year'], song['link'], song['duration'],
                song['bpm'], song['energy'], song['danceability'],
                song['valence'], song['acousticness'], song['instrumentalness']
            ))
        return cur.rowcount > 0
    return _db_exec(_run)


def peek_preprocessed(limit=100):
    def _run(conn):
        _ensure_preprocessed_place(conn)
        rows = conn.execute(
            "SELECT * FROM Preprocessed ORDER BY id ASC LIMIT ?",
            (limit,)
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM Preprocessed LIMIT 0").description]

        return [dict(zip(cols, r)) for r in rows]
    return _db_exec(_run)


def take_preprocessed_batch(percent=0.2, min_batch=1, n=None, place=None):
    """Atomically pulls rows out of Preprocessed and removes them. If n is given,
    takes up to n rows; otherwise takes percent% (min min_batch). With a place,
    only rows of that place are taken (other places stay in the pool undriven)
    so a place-capped run never plays another place's tracks."""
    def _run(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_preprocessed_place(conn)
            where = " WHERE place = ?" if place else ""
            params = (place,) if place else ()
            total = conn.execute("SELECT COUNT(*) FROM Preprocessed" + where, params).fetchone()[0]
            if total == 0:
                conn.execute("COMMIT")
                return []

            if n is not None:
                take = min(total, n)
            else:
                take = max(min_batch, int(total * percent))
            rows = conn.execute(
                "SELECT * FROM Preprocessed" + where + " ORDER BY id ASC LIMIT ?",
                params + (take,)
            ).fetchall()
            cols = [d[0] for d in conn.execute("SELECT * FROM Preprocessed LIMIT 0").description]
            songs = [dict(zip(cols, r)) for r in rows]

            conn.executemany("DELETE FROM Preprocessed WHERE id = ?", [(s['id'],) for s in songs])
            conn.execute("COMMIT")
            return songs
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _db_exec(_run)


def remove_from_preprocessed(ids):
    if not ids:
        return

    def _run(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("DELETE FROM Preprocessed WHERE id = ?", [(i,) for i in ids])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    _db_exec(_run)


def purge_stale_preprocessed(place, genres):
    """Deletes the Preprocessed rows of `place` whose genre is no longer part
    of `genres`. Called whenever a place is (re)created so a place whose genres
    change — e.g. 'test' was hip-hop, gets re-added as pop — never keeps playing
    the OLD place's tracks. Returns the number of rows removed (0 is fine)."""
    if not place:
        return 0

    def _run(conn):
        _ensure_preprocessed_place(conn)
        placeholders = ",".join("?" for _ in (genres or []))
        if placeholders:
            sql = ("DELETE FROM Preprocessed WHERE place = ? AND "
                   "(genre IS NULL OR genre NOT IN (%s))" % placeholders)
            params = (place,) + tuple(genres)
        else:
            sql = "DELETE FROM Preprocessed WHERE place = ?"
            params = (place,)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(sql, params)
            removed = cur.rowcount
            conn.execute("COMMIT")
            return removed
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _db_exec(_run)


def preprocessed_count(place=None):
    def _run(conn):
        if place:
            return conn.execute("SELECT COUNT(*) FROM Preprocessed WHERE place = ?",
                                (place,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM Preprocessed").fetchone()[0]
    return _db_exec(_run)


def recycle_played_songs(n=20, place=None):
    """Pull scored songs back into rotation so the DJ keeps playing once the
    fresh/unplayed pool runs low. PLAYED state is the gate — not the place tag:
    places are genre bundles, so a song fetched for one place legitimately fits
    another that shares its genre (a morning-fetched "house" track is a fine
    rave track). Two tiers:

    1) First pass: scored, NEVER-played songs in the place's genres — airtime
       for anything scored but not yet heard (under any place's tag).
    2) Backfill: only if tier 1 can't fill n does it fall back to ALREADY-played
       songs, so replays happen only when the catalog is genuinely exhausted.

    Rows must carry likeability to survive delete_unscored_songs. Empty only
    when nothing has ever been scored in the place's genres yet, in which case
    fresh discovery is still the only source and playback keeps waiting."""
    def _run(conn):
        cols = [d[0] for d in conn.execute("SELECT * FROM Songs LIMIT 0").description]
        if place and PLACE_GENRES.get(place):
            genres = list(PLACE_GENRES[place])
            marks = ",".join("?" for _ in genres)
            where = f"genre IN ({marks})"
            params = list(genres)
        else:
            where = "1 = 1"
            params = []
        rows = conn.execute(
            "SELECT * FROM Songs WHERE likeability IS NOT NULL AND " + where +
            " ORDER BY RANDOM() LIMIT 2000",
            params,
        ).fetchall()
        # ids already played: a song appears in any per-genre played table
        # (mirrors duplicates.is_played's global view, on the same connection).
        from duplicates import NON_GENRE_TABLES
        tables = [t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if t[0] not in NON_GENRE_TABLES]
        played = set()
        for t in tables:
            for (sid,) in conn.execute(f'SELECT id FROM "{t}"'):
                played.add(sid)
        songs = [dict(zip(cols, r)) for r in rows]
        fresh = [s for s in songs if s.get('id') not in played]
        used = [s for s in songs if s.get('id') in played]
        return (fresh + used)[:n]
    try:
        return _db_exec(_run)
    except Exception as e:
        print("Recycle failed:", e)
        return []


if __name__=="__main__":
    import os
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # suppress TF INFO/WARNING/ERROR logs (only show FATAL)
    os.environ['CUDA_VISIBLE_DEVICES'] = '-1'

    args = sys.argv[1:]
    if '--prefill' in args:
        idx = args.index('--prefill')
        n = int(args[idx + 1]) if len(args) > idx + 1 else 300
        fill_preprocessed(target=n)
        sys.exit(0)

    options = {
        'format': 'ba/b',
        'quiet': True,
        'noplaylist': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    }

    musicbrainzngs.set_useragent("DjAgent", "1.0.0", "https://github.com/13lend1")

    result = musicbrainzngs.search_recordings(
        query="tag:hip-hop",
        limit=100
    )
    
    recordings = result["recording-list"]  
        
    genres=[]
    titles=[]
    artist=[]
    for record in recordings:
        titles.append(record['title'])
        artist.append(record['artist-credit-phrase'])
        # genres.append(record['tag-list'][0]['name'] if record.get('tag-list') else None)
    songs= [{'title': t, 'artist': a} for t, a in zip(titles, artist)]
    pprint.pprint(songs)

    