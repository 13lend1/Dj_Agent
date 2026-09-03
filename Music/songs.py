import musicbrainzngs
import random
import yt_dlp
import sqlite3
from audio_metrics import process_track

# extractor = AudioFeatureExtractor()
# features = process_track(video['url'], extractor)
# print(features)


GENRES = ["house", "techno", "jazz", "rock", "hip hop", "pop", "reggae", "funk", "soul", "disco","classic","Latin","edm","jazz"]

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
                return {'url': entries[0]['url'], 'title': title,'album':album, 'artist': artist, 'genre': genre, 'length': length_ms, 'year': year,'id':id}
    return None


def save(song,extractor):
    conn = sqlite3.connect("Database/music.db") 
    cursor = conn.cursor()
    features = process_track(song['url'], extractor)

    cursor.execute(
        """
        INSERT INTO Songs
            (id, name, artist, album, genre, year, link, duration,
            bpm, energy, danceability, valence, acousticness, instrumentalness, likeability)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET likeability = excluded.likeability
        """,
        (
            song['id'], song['title'], song['artist'], song['album'],
            song['genre'], song['year'], song['url'], song['length'],
            features['bpm'], features['energy'], features['danceability'],
            features['valence'], features['acousticness'], features['instrumentalness'],
            song['score'],
        ),
    )
    conn.commit()
    conn.close()


if __name__=="__main__":
        
    options={
        "quiet":True,
        "extract_flat":True
    }

    musicbrainzngs.set_useragent("DjAgent", "1.0.0", "https://github.com/13lend1")

    result = musicbrainzngs.search_recordings(
        query="tag:rock",
        limit=100
    )

    recordings = result["recording-list"]  

    song = random.choice(recordings)

    print(f"Song: {song['title']}")
    print(f"ID: {song['id']}")
    print(song.keys())
    exclude={'artist-credit','ext:score','isrc-list'}
    songs={k:v for k,v in song.items() if k not in exclude}

    if song['release-list'][0]['date'] != None:
        print(song['release-list'][0]['date'])
        year=song['release-list'][0]['date']
    else:
        print("No year ")
    album=song['release-list'][0]['release-group']['title']
    print(album)
    title=str(song['title'])
    print(f"Title:{title} by {song['artist-credit-phrase']}")
    query=f"{title} by {song['artist-credit-phrase']}-{album}"
    print(query)
    with yt_dlp.YoutubeDL(options) as ydl:
        result=ydl.extract_info(f"ytsearch:{query}",
                                download=False)
        
    video=result['entries'][0]
    print(video["title"])
    print(video["url"])

    get_random_song()