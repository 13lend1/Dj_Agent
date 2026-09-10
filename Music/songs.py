import musicbrainzngs
import random
import re
import sqlite3
import sys
import os
import threading
import time
import collections
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from audio_specs import get_features_cached
import pprint

_yt = None


def _get_yt():
    """Shared, reusable YTMusic client (auth-free). Creating it once keeps the
    internal session warm — searches are then fast JSON API calls."""
    global _yt
    if _yt is None:
        from ytmusicapi import YTMusic
        _yt = YTMusic()
    return _yt

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# GENRES = [
#     "house", "techno", "jazz", "rock", "hip-hop","hiphop", "pop","alt-pop", "reggae", "funk",
#     "soul", "disco", "classical", "latin", "edm", "blues", "country",
#     "metal", "punk", "ambient", "r&b", "indie",'rap'
# ]
GENRES=['house','techno','rock','hip-hop','pop','alt-pop','edm','metal','rap']
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

    # "Miles Davis": "jazz", "John Coltrane": "jazz", "Louis Armstrong": "jazz",
    # "Ella Fitzgerald": "jazz", "Nina Simone": "jazz",

    # "Bob Marley": "reggae", "Fela Kuti": "funk", "Toots and the Maytals": "reggae",
    # "Stevie Wonder": "soul", "Marvin Gaye": "soul", "Aretha Franklin": "soul",
    # "James Brown": "funk", "Earth, Wind & Fire": "funk",

    "Daddy Yankee": "latin", "Shakira": "latin", "J Balvin": "latin",
    "Karol G": "latin", "Rosalía": "latin",

    # "Frédéric Chopin": "classical", "Ludwig van Beethoven": "classical",
    # "Wolfgang Amadeus Mozart": "classical", "Johann Sebastian Bach": "classical",
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

def get_random_song(max_attempts=5):
    for attempt in range(1, max_attempts + 1):
        song_url = _fetch_random_song()
        if song_url:
            return song_url
        print(f"Attempt {attempt}/{max_attempts} failed, retrying with a new song...")

    print("Could not find a playable song after several attempts.")
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

def _refill_queue(used_artists=(), used_genres=()):
    # Always run an artist query (plus a tag query) so named artists from the
    # ARTISTS dict get real airtime instead of being drowned by tag results.
    specs = _random_query_specs(2, used_artists=used_artists, used_genres=used_genres)
    got_any = False
    for kind, value, genre in specs:
        result = _mb_search(_mb_query_string(kind, value))
        if not (result and isinstance(result, dict) and result.get("recording-list")):
            continue
        got_any = True

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
                'source': 'artist' if kind == "artist" else 'tag',
            })
    if got_any:
        random.shuffle(_MB_CANDIDATES)
        return True
    return False

def _random_query_specs(k=3, used_artists=(), used_genres=()):
    """Samples k (kind, value, genre) triples so a refill batch mixes tag- and
    artist-based discovery, preferring genres/artists that aren't already
    saturated in the current batch. Fresh genres and artists are preferred so
    the pool doesn't flood with one act or one sound."""
    g_pool = [("genre", g, g) for g in GENRES if g not in used_genres]
    a_pool = [("artist", a, g) for a, g in ARTISTS.items() if a not in used_artists]
    if len(g_pool) < k // 2:
        g_pool = [("genre", g, g) for g in GENRES]
    if len(a_pool) < k - k // 2:
        a_pool = [("artist", a, g) for a, g in ARTISTS.items()]
    if k == 1:
        # single query: flip a coin between a random genre and a random artist
        if random.random() < 0.5:
            return [random.choice(g_pool)] if g_pool else [random.choice(a_pool)]
        return [random.choice(a_pool)] if a_pool else [random.choice(g_pool)]
    n_g = min(len(g_pool), k - k // 2)
    specs = random.sample(g_pool, k=n_g) + random.sample(a_pool, k=k - n_g)
    random.shuffle(specs)
    return specs if specs else [("genre", random.choice(GENRES), random.choice(GENRES))]


def _mb_query_string(kind, value):
    if kind == "artist":
        return f'artist:"{value}"'
    return f"tag:{value}"

def _next_candidate(used_artists=(), used_genres=()):
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
                return cand

            if _refill_queue(used_artists=used_artists, used_genres=used_genres):
                continue  # loop pops the freshly queued candidate

            kind, value, genre = random.choice(_random_query_specs(
                k=1, used_artists=used_artists, used_genres=used_genres))
            song = _yt_fallback(value, genre)
            if song is not None:
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
    """Fast link lookup via YTMusic — returns a playable watch URL or None.
    Much quicker than a full yt-dlp youtube search."""
    try:
        results = _get_yt().search(f"{title} {artist}", filter="songs", limit=max_results)
    except Exception as e:
        print("YTMusic search error:", e)
        return None
    if not results:
        return None
    video = _best_match(results, title, artist)
    if not video or not video.get('videoId'):
        return None
    return f"https://music.youtube.com/watch?v={video['videoId']}"


def _yt_fallback(term, genre):
    try:
        results = _get_yt().search(term, filter="songs", limit=20)
    except Exception as e:
        print("YTMusic fallback search error:", e)
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
        'duration': _parse_duration(video.get('duration')),
        'year': None,
        'id': video['videoId'],
    }
    
def _fetch_random_song():
    kind, value, genre = random.choice(_random_query_specs(k=1))
    result = _mb_search(_mb_query_string(kind, value))

    recording = None
    if result and isinstance(result, dict) and result.get("recording-list"):
        try:
            recording = random.choice(result["recording-list"])
        except (IndexError, KeyError):
            recording = None

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
                'genre': genre, 'duration': length_ms, 'year': year, 'id': song_id}

    song = _yt_fallback(value, genre)
    if song is None:
        print("Could not find a playable song (MusicBrainz + YTMusic both failed).")
    return song

def _build_song(cand, max_attempts=3):
    """Turns a candidate into a playable, feature-complete song dict (or None).
    YTMusic link lookup + cached ReccoBeats features, retried a few times."""
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


def get_random_songs(n=20, max_attempts=5, batch_size=5, on_song=None, workers=3):
    """Fetches n songs with parallel link discovery + feature extraction.
    MusicBrainz queries stay serialized (their rate limit), while YTMusic and
    ReccoBeats calls run across `workers` threads. Features are disk-cached."""
    songs = []
    seen = set()
    got = 0
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

    def _job(_genre):
        used_artists, used_genres = _used_sets()
        cand = _next_candidate(used_artists=used_artists, used_genres=used_genres)
        if cand is None:
            return None
        return _build_song(cand, max_attempts=max_attempts)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = set()
        while got < n:
            while len(futures) < workers and got < n:
                futures.add(ex.submit(_job, random.choice(GENRES)))
            done, futures = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
            for f in done:
                song = f.result()
                if song is None:
                    continue
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
                print(f"[{progress}/{n}] {song['name']} - {song['artist']}")
                if on_song:
                    on_song(song)
                songs.append(song)

    return songs


def fill_preprocessed(target=300, workers=3):
    """Pre-fills the Preprocessed pool up to `target` songs.

    Run it before starting the DJ so the pool is already stocked, e.g.:
        python fill_preprocessed.py 300
    Existing rows are kept and duplicates are skipped.
    """
    have = preprocessed_count()
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
    get_random_songs(n=need, workers=workers, on_song=on_song)
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
        conn.execute(
            """
            INSERT INTO Songs
                (id, name, artist, album, genre, year, link, duration,
                bpm, energy, danceability, valence, acousticness, instrumentalness, likeability)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET likeability = excluded.likeability
            """,
            (
                song['id'], song['name'], song['artist'], song['album'],
                song['genre'], song['year'], song['link'], song['duration'],
                song['bpm'], song['energy'], song['danceability'],
                song['valence'], song['acousticness'], song['instrumentalness'],
                song['score'],
            ),
        )
    _db_exec(_run)


def save_song_metadata(song, likeability=None):
    """Upserts a song's row into Songs. Without likeability it leaves the
    existing value untouched; with a value it writes it into the likeability
    column (used to persist the model's *prediction* for the selected songs)."""
    def _run(conn):
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


def save_preprocessed(song):
    def _run(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO Preprocessed
                (id, name, artist, album, genre, year, link, duration,
                bpm, energy, danceability, valence, acousticness, instrumentalness)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                song['id'], song['name'], song['artist'], song['album'],
                song['genre'], song['year'], song['link'], song['duration'],
                song['bpm'], song['energy'], song['danceability'],
                song['valence'], song['acousticness'], song['instrumentalness']
            ))
        return cur.rowcount > 0
    return _db_exec(_run)


def peek_preprocessed(limit=100):
    """Pulls a candidate pool without removing anything — scoring decides what plays."""
    def _run(conn):
        rows = conn.execute(
            """SELECT id, name, artist, album, genre, year, link, duration,
                      bpm, energy, danceability, valence, acousticness, instrumentalness
               FROM Preprocessed ORDER BY id ASC LIMIT ?""",
            (limit,)
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM Preprocessed LIMIT 0").description]

        return [dict(zip(cols, r)) for r in rows]
    return _db_exec(_run)


def take_preprocessed_batch(percent=0.2, min_batch=1, n=None):
    """Atomically pulls rows out of Preprocessed and removes them. If n is given,
    takes up to n rows; otherwise takes percent% (min min_batch)."""
    def _run(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            total = conn.execute("SELECT COUNT(*) FROM Preprocessed").fetchone()[0]
            if total == 0:
                conn.execute("COMMIT")
                return []

            if n is not None:
                take = min(total, n)
            else:
                take = max(min_batch, int(total * percent))
            rows = conn.execute(
                """SELECT id, name, artist, album, genre, year, link, duration,
                          bpm, energy, danceability, valence, acousticness, instrumentalness
                   FROM Preprocessed ORDER BY id ASC LIMIT ?""",
                (take,)
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


def preprocessed_count():
    def _run(conn):
        return conn.execute("SELECT COUNT(*) FROM Preprocessed").fetchone()[0]
    return _db_exec(_run)


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

    options={
        "quiet":True,
        "extract_flat":True
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

    