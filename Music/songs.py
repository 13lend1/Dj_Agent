import musicbrainzngs
import random
import yt_dlp
import sqlite3
import sys
import os
import threading
import time
from audio_specs import get_features_by_name
import pprint

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

GENRES = [
    "house", "techno", "jazz", "rock", "hip-hop","hiphop", "pop","alt-pop", "reggae", "funk",
    "soul", "disco", "classical", "latin", "edm", "blues", "country",
    "metal", "punk", "ambient", "r&b", "indie",'rap'
]

# new — store the db in your Linux home dir instead of on /mnt/c
DB_DIR = os.path.expanduser("~/dj_agent")
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


def _fetch_random_song():
    genre = random.choice(GENRES)
    options = {"quiet": True, "extract_flat": True}

    try:
        result = musicbrainzngs.search_recordings(query=f"tag:{genre}", limit=100)
    except musicbrainzngs.WebServiceError as e:
        print("MusicBrainz error:", e)
        return None

    recordings = result.get("recording-list", [])
    if not recordings:
        return None

    song = random.choice(recordings)
    id=song['id']
    title = song.get('title')
    artist = song.get('artist-credit-phrase')
    length=song.get('length')
    length_ms = int(length) if length and length.isdigit() else None
    if not title or not artist:
        return None

    release_list = song.get('release-list', [])
    album = None
    year=None
    if release_list:
        release = release_list[0]
        album = release.get('release-group', {}).get('title')
        date = release.get('date')  # e.g. "1994-11-01" or just "1994"
        if date:
            year = date[:4]


    queries = [f"{title} {artist} {album}"] if album else []
    queries.append(f"{title} {artist}")

    with yt_dlp.YoutubeDL(options) as ydl:
        for query in queries:
            try:
                result = ydl.extract_info(f"ytsearch:{query}", download=False)
            except Exception as e:
                print("yt-dlp search error:", e)
                continue

            entries = result.get('entries', [])
            if entries:
                return {'link': entries[0]['url'], 'name': title,'album':album, 'artist': artist, 'genre': genre, 'duration': length_ms, 'year': year,'id':id}
    return None

def get_random_songs(n=20, max_attempts=5, batch_size=5, on_song=None):
    """Process songs in batches; calls on_song(song) immediately as each one succeeds."""
    songs = []
    seen_ids = set()

    while len(songs) < n:
        batch_songs = []
        remaining = min(batch_size, n - len(songs))

        for _ in range(remaining):
            song = None
            for attempt in range(max_attempts):
                song = _fetch_random_song()
                if song and song['id'] not in seen_ids:
                    break
            if song:
                seen_ids.add(song['id'])
                batch_songs.append(song)
            else:
                print(f"Failed to fetch song after {max_attempts} attempts")

        for song in batch_songs:
            try:
                features = get_features_by_name(song['name'])
                if features is None:
                    print(f"[skip] no audio features for: {song['name']}")
                    continue
                song.update(features)
                songs.append(song)
                print(f"[{len(songs)}/{n}] {song['name']} - {song['artist']}")
                if on_song:
                    on_song(song)   # lands in Preprocessed the moment it's ready
            except Exception as e:
                print(f"[fail] {song['name']}: {e}")

    return songs
def _get_conn():
    global _db_conn
    if _db_conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)  # guard against missing folder
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA mmap_size=0;")   # disable mmap — this is what usually throws "disk I/O error" on Windows/synced/AV-scanned drives
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
        features = get_features_by_name(song['name'])
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


def save_preprocessed(song):
    def _run(conn):
        conn.execute(
            """
            INSERT INTO Preprocessed
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
    _db_exec(_run)


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

    