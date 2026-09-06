import http.client
import urllib.parse
import json


def search_reccobeats(song_name, artist_name=None):
    query_text = f"{song_name} {artist_name}" if artist_name else song_name
    conn = http.client.HTTPSConnection("api.reccobeats.com", timeout=10)
    headers = {'Accept': 'application/json'}
    try:
        encoded = urllib.parse.urlencode({"searchText": query_text})
        conn.request("GET", f"/v1/track/search?{encoded}", '', headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8", errors="replace")
    except (TimeoutError, OSError) as e:
        print(f"ReccoBeats search timed out: {e}")
        return None
    finally:
        conn.close()

    if res.status != 200:
        print(f"Search error {res.status}")
        return None
    return json.loads(data)

def get_audio_features(track_id):
    conn = http.client.HTTPSConnection("api.reccobeats.com", timeout=10)
    headers = {'Accept': 'application/json'}
    try:
        conn.request("GET", f"/v1/track/{track_id}/audio-features", '', headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8", errors="replace")
    except (TimeoutError, OSError) as e:
        print(f"ReccoBeats features timed out: {e}")
        return None
    finally:
        conn.close()

    if res.status != 200:
        print(f"Audio features error {res.status}")
        return None
    return json.loads(data)


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
