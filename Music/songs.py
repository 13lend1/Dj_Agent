import musicbrainzngs
import random
import yt_dlp
import sqlite3
from audio_metrics import AudioFeatureExtractor,process_track,process_track_safe
import pprint

GENRES = [
    "house", "techno", "jazz", "rock", "hip-hop","hiphop", "pop","alt-pop", "reggae", "funk",
    "soul", "disco", "classical", "latin", "edm", "blues", "country",
    "metal", "punk", "ambient", "r&b", "indie",'rap'
]
DB_PATH = "Database/music.db"

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
                print(f"Processing: {song['name']} — {song['artist']}...")
                features = process_track_safe(song['link'])
                if features is None:
                    print(f"❌ Skipped (crash/timeout): {song['name']}")
                    continue
                song.update(features)
                songs.append(song)
                print(f"[{len(songs)}/{n}] {song['name']} — {song['artist']} ✓")
                if on_song:
                    on_song(song)   # lands in Preprocessed the moment it's ready
            except Exception as e:
                print(f"❌ Failed: {song['name']} - {e}")

    return songs
def save(song):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    required = ['bpm', 'energy', 'danceability', 'valence', 'acousticness', 'instrumentalness']
    if not all(song.get(k) is not None for k in required):
        # only true for the get_random_song() cold-start fallback path
        features = process_track_safe(song['link'])
        if features is None:
            print(f"Save skipped (extraction failed): {song['name']}")
            conn.close()
            return
        song.update(features)

    cursor.execute(
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
    conn.commit()
    conn.close()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")  # lets refill writes and batch reads coexist
    return conn

def save_preprocessed(song):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
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
    conn.commit()
    conn.close()

def get_preprocessed_batch(limit=50):
    """Pulls a candidate pool without removing anything — scoring decides what plays."""
    conn = _connect()
    rows = conn.execute(
        """SELECT id, name, artist, album, genre, year, link, duration,
                  bpm, energy, danceability, valence, acousticness, instrumentalness
           FROM Preprocessed ORDER BY id ASC LIMIT ?""",
        (limit,)
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM Preprocessed LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]

def remove_from_preprocessed(ids):
    if not ids:
        return
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany("DELETE FROM Preprocessed WHERE id = ?", [(i,) for i in ids])
    conn.commit()
    conn.close()

def preprocessed_count():
    conn = _connect()
    count = conn.execute("SELECT COUNT(*) FROM Preprocessed").fetchone()[0]
    conn.close()
    return count

def get_preprocessed_batch(percent=0.2, min_batch=1):
    """Atomically pulls percent% of whatever's currently in Preprocessed and removes it."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")  # blocks other writers until this commits — no race with refill
    total = cursor.execute("SELECT COUNT(*) FROM Preprocessed").fetchone()[0]
    if total == 0:
        conn.commit()
        conn.close()
        return []

    take = max(min_batch, int(total * percent))
    rows = cursor.execute(
        """SELECT id, name, artist, album, genre, year, link, duration,
                  bpm, energy, danceability, valence, acousticness, instrumentalness
           FROM Preprocessed ORDER BY id ASC LIMIT ?""",
        (take,)
    ).fetchall()
    cols = [d[0] for d in cursor.description]
    songs = [dict(zip(cols, r)) for r in rows]

    cursor.executemany("DELETE FROM Preprocessed WHERE id = ?", [(s['id'],) for s in songs])
    conn.commit()
    conn.close()
    return songs

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

    