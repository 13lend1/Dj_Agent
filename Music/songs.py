import musicbrainzngs
import random
import yt_dlp
import sqlite3
from audio_metrics import AudioFeatureExtractor,download_audio_to_tempfile,process_track




def get_new_songs(songs):
    conn = sqlite3.connect("music.db")
    cursor = conn.cursor()

    song_ids = [song["id"] for song in songs]

    if not song_ids:
        conn.close()
        return []

    placeholders = ",".join("?" * len(song_ids))

    cursor.execute(
        f"SELECT id FROM Classical WHERE id IN ({placeholders})",
        song_ids
    )

    existing_ids = {row[0] for row in cursor.fetchall()}

    conn.close()

    return [song for song in songs if song["id"] not in existing_ids]

def save(song,genre):
    conn=sqlite3.connect()
    cursor=conn.cursor()
    with yt_dlp.YoutubeDL(options) as ydl:
            result=ydl.extract_info(f"ytsearch:{title}",
                                download=False)
    video=result['entries'][0]
    cursor.execute(f"INSERT INTO {genre}(id,name,artist,album,genre,year,link,duration,bpm,energy,danceability,valence,acousticness,instrumentalness,likeability)VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(song['id'],song['title'],song['artist-credit-phrase'],None,genre,None,video['url'],song['length'],))

    
    
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
exclude={'artist-credit','ext:score','isrc-list','release-list'}
songs={k:v for k,v in song.items() if k not in exclude}
print(songs)
title=str(song['title'])

with yt_dlp.YoutubeDL(options) as ydl:
    result=ydl.extract_info(f"ytsearch:{title}",
                            download=False)
    
video=result['entries'][0]
print(video["title"])
print(video["url"])


extractor = AudioFeatureExtractor()
features = process_track("https://www.youtube.com/watch?v=btPJPFnesV4", extractor)
print(features)
