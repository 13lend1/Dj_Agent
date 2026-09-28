# Set the default place here (must be a key in PLACE_GENRES).
# Override with --resume-place <key> on the command line or
# DJ_RESUME_PLACE=<key> env variable.
DEFAULT_PLACE = "car"

PLACE_GENRES = {
    "car": [
        "rock", "classic rock", "metal", "hard rock", "pop", "hip-hop",
        "electronic", "punk", "alternative rock", "pop rock",
    ],
    "home": [
        "pop", "house", "classical", "lo-fi", "indie rock", "acoustic rock",
        "singer-songwriter", "indie-pop", "folk",
    ],
    "restaurant": [
        "jazz", "classical", "bossa nova", "soul", "lounge",
        "smooth jazz", "instrumental", "latin jazz", "swing",
    ],
    "gym": [
        "hip-hop", "electronic", "metal", "dubstep", "trap", "rock",
        "drumandbass", "hardstyle", "phonk",
    ],
    "party": [
        "house", "pop", "hip-hop", "reggaeton", "edm", "dancehall",
        "afrobeat", "pop latino", "disco", "funk", "trap",
    ],
    "study": [
        "classical", "lo-fi", "ambient", "instrumental", "acoustic rock",
        "lo-fi hip-hop", "post-rock", "minimal techno",
    ],
    "sleep": [
        "ambient", "classical", "acoustic rock", "lo-fi", "drone",
    ],
    "office": [
        "lo-fi", "jazz", "classical", "instrumental", "ambient",
        "lo-fi hip-hop", "downtempo", "soft rock",
    ],
    "beach": [
        "reggae", "reggaeton", "pop", "latin", "afrobeat", "house",
        "tropical house", "dancehall", "soca",
    ],
    "walk": [
        "indie pop", "pop", "acoustic rock", "hip-hop", "folk",
        "singer-songwriter", "alternative rock",
    ],
    "rave": [
        "techno", "house", "edm", "hardstyle", "drumandbass",
        "trance", "dubstep", "hard techno", "acid house",
    ],
    "date_night": [
        "contemporary r&b", "soul", "jazz", "bossa nova", "neo soul",
        "lounge", "smooth jazz",
    ],
    "road_trip": [
        "rock", "classic rock", "pop", "country", "indie rock", "folk",
        "alternative rock", "singer-songwriter",
    ],
    "cooking": [
        "jazz", "funk", "soul", "latin", "afrobeat", "disco", "bossa nova",
    ],
    "focus_coding": [
        "lo-fi", "ambient", "instrumental", "post-rock", "minimal techno",
        "lo-fi hip-hop", "synthwave",
    ],
    "gaming": [
        "synthwave", "electronic", "drum and bass", "chiptune", "phonk",
        "rock",
    ],
}

# ---- custom places (created from the web UI, persisted) -------------------
#
# A place is just a name -> [genres] mapping. Users can create their own from
# the UI; they are merged into PLACE_GENRES at import (so song fetching and the
# per-place model pick them up) and saved to Database/places.json so they
# survive a restart. The same file remembers which place was last active.

import json as _json
import os as _os_places
import re as _re_places

_PLACES_FILE = _os_places.path.join(
    _os_places.path.dirname(_os_places.path.dirname(_os_places.path.abspath(__file__))),
    "Database", "places.json",
)
_CUSTOM_PLACES = {}


def normalize_place(name):
    """Canonical place key: lowercase, non-alphanumerics collapsed to '_'."""
    name = _re_places.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower())
    return name.strip("_")


def _read_places_file():
    try:
        with open(_PLACES_FILE, encoding="utf-8") as fh:
            data = _json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_places_file(data):
    try:
        _os_places.makedirs(_os_places.path.dirname(_PLACES_FILE), exist_ok=True)
        with open(_PLACES_FILE, "w", encoding="utf-8") as fh:
            _json.dump(data, fh, indent=2)
    except OSError as exc:
        print("Could not save places file:", exc)
    return data


def load_custom_places():
    """Merge user-created places from Database/places.json into PLACE_GENRES."""
    data = _read_places_file()
    for raw_key, genres in (data.get("places") or {}).items():
        key = normalize_place(raw_key)
        if not key or not isinstance(genres, list):
            continue
        clean = [str(g).strip().lower() for g in genres if str(g).strip()]
        if clean:
            _CUSTOM_PLACES[key] = clean
            PLACE_GENRES[key] = clean
    return dict(_CUSTOM_PLACES)


def custom_places():
    """The user-created places only (not the built-in PLACE_GENRES keys)."""
    return dict(_CUSTOM_PLACES)


def add_place(name, genres):
    """Create (or replace) a user-defined place and persist it. Returns the
    normalized key. When the name is reused (redefining the place), the stale
    tracked-pool rows for it are purged too so a place that changes its genres
    never keeps playing the OLD place's tracks (e.g. 'test' was hip-hop, gets
    re-added as pop — the leftover hip-hop catalog must not still play).
    Raises ValueError when the name or genres are unusable."""
    key = normalize_place(name)
    if not key:
        raise ValueError("a place name is required")
    clean = []
    for genre in genres or []:
        genre = str(genre).strip().lower()
        if genre and genre not in clean:
            clean.append(genre)
    if not clean:
        raise ValueError("pick at least one genre for the new place")

    _CUSTOM_PLACES[key] = clean
    PLACE_GENRES[key] = clean
    data = _read_places_file()
    data["places"] = dict(_CUSTOM_PLACES)
    _write_places_file(data)
    # Always purge (no-op when nothing is stale). This MUST NOT be gated on the
    # key being in memory right now: the stale Preprocessed rows tagged with
    # this place name survive in the DB even after places.json is deleted and
    # the key drops out of PLACE_GENRES. Re-adding "test" as pop after it was
    # hip-hop must never keep serving the leftover hip-hop rows just because
    # the key wasn't in PLACE_GENRES at add time — so purge unconditionally.
    _purge_stale_tracked(key, clean)
    return key


def _purge_stale_tracked(place, genres):
    """Delete the tracked-pool rows of `place` whose genre is no longer part of
    `genres` — the rows that would otherwise play sounds for a genre the place
    no longer wants. Runs OUTSIDE the dict update so a failed purge can never
    roll back a valid place change."""
    try:
        from Music.songs import purge_stale_preprocessed
        removed = purge_stale_preprocessed(place, genres)
        if removed:
            print(f"Purged {removed} stale pool row(s) for '{place}' that no "
                  f"longer match its current genres.", flush=True)
    except Exception as e:
        print("Could not purge stale pool rows for",
              f"'{place}':", e)


def delete_place(name):
    """Delete a user-created place: its definition, its songs and its model.

    Returns a summary dict of what was removed. Raises ValueError when the
    place cannot be deleted — unknown, or a built-in. Built-ins are the
    PLACE_GENRES keys that are not in _CUSTOM_PLACES; they ship with the app and
    deleting one would strand the pool rows already tagged with it.

    The active pointer is cleared when it pointed at the deleted place, because
    get_active_place() does not validate against PLACE_GENRES: leaving it
    dangling would make the next DJ start treat an unknown place as "no genre
    filter" and quietly play every genre.
    """
    key = normalize_place(name)
    if not key:
        raise ValueError("a place name is required")
    if key not in _CUSTOM_PLACES:
        if key in PLACE_GENRES:
            raise ValueError(f"'{key}' is a built-in place and cannot be deleted")
        raise ValueError(f"unknown place '{name}'")

    genres = list(_CUSTOM_PLACES.get(key) or [])

    # Drop the definition first so a failure below can never leave a place that
    # the UI still lists as deletable with nothing behind it.
    _CUSTOM_PLACES.pop(key, None)
    PLACE_GENRES.pop(key, None)

    data = _read_places_file()
    data["places"] = dict(_CUSTOM_PLACES)
    cleared_active = normalize_place(data.get("active")) == key
    if cleared_active:
        data["active"] = None
    _write_places_file(data)

    # Songs/pool/history and the model are keyed by the place string, so they are
    # purged even for a name that has already dropped out of the registry.
    removed = {"songs": 0, "preprocessed": 0, "agent": 0, "model": False}
    try:
        from Music.songs import delete_place_songs
        removed.update(delete_place_songs(key))
    except Exception as e:
        print(f"Could not delete songs for '{key}':", e)
    try:
        from Model.linear_regression import delete_place_model
        removed["model"] = bool(delete_place_model(key))
    except Exception as e:
        print(f"Could not delete model for '{key}':", e)

    print(f"Deleted place '{key}' ({', '.join(genres) or 'no genres'}): "
          f"{removed['songs']} song(s), {removed['preprocessed']} pool row(s), "
          f"{removed['agent']} run(s), model "
          f"{'removed' if removed['model'] else 'not present'}.",
          flush=True)
    return {"place": key, "genres": genres, "cleared_active": cleared_active,
            **removed}


def get_active_place():
    """The place the user last selected. Returns None when they never chose one;
    returns "" when they explicitly chose 'any place'."""
    data = _read_places_file()
    if "active" not in data:
        return None
    return normalize_place(data.get("active"))


def set_active_place(name):
    """Persist the active place so the DJ resumes it on the next start. An
    empty/None name means 'any place' (play every genre)."""
    key = normalize_place(name)
    if key and key not in PLACE_GENRES:
        raise ValueError(f"unknown place '{name}'")
    data = _read_places_file()
    data["active"] = key or None
    _write_places_file(data)
    return key or None


load_custom_places()

GENRE_EFFECTS = {
    "classical": [
        "soft_air",
        "gentle_sweep",
        "reverse_reverb",
    ],

    "jazz": [
        "soft_air",
        "vinyl_noise",
        "gentle_sweep",
        "reverse_reverb",
    ],

    "rock": [
        "short_whoosh",
        "vinyl_noise",
        "tape_stop",
        "short_impact",
    ],

    "classic rock": [
        "vinyl_noise",
        "record_stop",
        "tape_stop",
        "soft_whoosh",
    ],

    "metal": [
        "hard_whoosh",
        "short_impact",
        "reverse_sweep",
        "sub_drop",
    ],

    "pop": [
        "soft_whoosh",
        "reverse_sweep",
        "short_impact",
        "vinyl_noise",
    ],

    "hip-hop": [
        "vinyl_scratch",
        "rewind",
        "tape_stop",
        "vinyl_noise",
        "short_impact",
    ],

    "trap": [
        "tape_stop",
        "vinyl_scratch",
        "sub_drop",
        "glitch",
        "reverse_sweep",
    ],

    "house": [
        "riser",
        "white_noise_sweep",
        "reverse_sweep",
        "short_impact",
    ],

    "techno": [
        "riser",
        "white_noise_sweep",
        "sub_drop",
        "glitch",
        "pitch_sweep",
    ],

    "hard techno": [
        "riser",
        "industrial_sweep",
        "impact",
        "glitch",
        "sub_drop",
    ],

    "trance": [
        "riser",
        "reverse_sweep",
        "white_noise_sweep",
        "pitch_sweep",
    ],

    "drumandbass": [
        "riser",
        "glitch",
        "stutter",
        "sub_drop",
    ],

    "dubstep": [
        "glitch",
        "stutter",
        "sub_drop",
        "riser",
        "impact",
    ],

    "hardstyle": [
        "riser",
        "impact",
        "sub_drop",
        "glitch",
    ],

    "edm": [
        "riser",
        "white_noise_sweep",
        "impact",
        "sub_drop",
        "reverse_sweep",
    ],

    "lo-fi": [
        "vinyl_noise",
        "tape_warmth",
        "soft_air",
        "cassette_stop",
    ],

    "ambient": [
        "soft_air",
        "ambient_swell",
        "reverse_reverb",
        "gentle_sweep",
    ],

    "acoustic rock": [
        "soft_air",
        "vinyl_noise",
        "gentle_sweep",
        "reverse_reverb",
    ],

    "indie rock": [
        "vinyl_noise",
        "soft_whoosh",
        "tape_stop",
        "reverse_sweep",
    ],

    "indie pop": [
        "soft_whoosh",
        "vinyl_noise",
        "gentle_sweep",
        "reverse_sweep",
    ],

    "soul": [
        "vinyl_noise",
        "tape_warmth",
        "soft_whoosh",
        "gentle_sweep",
    ],

    "funk": [
        "vinyl_scratch",
        "vinyl_noise",
        "tape_stop",
        "short_whoosh",
    ],

    "disco": [
        "vinyl_noise",
        "record_stop",
        "short_whoosh",
        "reverse_sweep",
    ],

    "reggaeton": [
        "short_whoosh",
        "vinyl_scratch",
        "reverse_sweep",
        "short_impact",
    ],

    "dancehall": [
        "vinyl_scratch",
        "short_whoosh",
        "tape_stop",
    ],

    "afrobeat": [
        "soft_whoosh",
        "vinyl_noise",
        "short_percussion",
    ],

    "bossa nova": [
        "soft_air",
        "vinyl_noise",
        "gentle_sweep",
    ],

    "folk": [
        "soft_air",
        "gentle_sweep",
        "reverse_reverb",
    ],

    "singer-songwriter": [
        "soft_air",
        "gentle_sweep",
        "reverse_reverb",
    ],

    "post-rock": [
        "ambient_swell",
        "reverse_reverb",
        "gentle_sweep",
        "soft_whoosh",
    ],

    "minimal techno": [
        "soft_riser",
        "white_noise_sweep",
        "gentle_sweep",
        "sub_drop",
    ],

    "synthwave": [
        "synth_sweep",
        "riser",
        "reverse_sweep",
        "glitch",
    ],

    "phonk": [
        "vinyl_scratch",
        "tape_stop",
        "rewind",
        "sub_drop",
        "glitch",
    ],

    "electronic": [
        "synth_sweep",
        "riser",
        "pitch_sweep",
        "glitch",
        "laser_riser",
    ],

    "hard rock": [
        "hard_whoosh",
        "short_impact",
        "reverse_sweep",
        "sub_drop",
    ],

    "punk": [
        "hard_whoosh",
        "short_whoosh",
        "short_percussion",
        "vinyl_noise",
    ],

    "alternative rock": [
        "soft_whoosh",
        "vinyl_noise",
        "reverse_sweep",
        "tape_stop",
    ],

    "pop rock": [
        "soft_whoosh",
        "short_whoosh",
        "short_impact",
        "synth_sweep",
    ],

    "pop latino": [
        "soft_whoosh",
        "short_percussion",
        "reverse_sweep",
        "short_impact",
    ],

    "indie-pop": [
        "soft_whoosh",
        "vinyl_noise",
        "gentle_sweep",
        "reverse_sweep",
    ],

    "lounge": [
        "soft_air",
        "vinyl_noise",
        "gentle_sweep",
        "synth_sweep",
    ],

    "smooth jazz": [
        "soft_air",
        "vinyl_noise",
        "gentle_sweep",
        "reverse_reverb",
    ],

    "latin jazz": [
        "gentle_sweep",
        "short_percussion",
        "vinyl_noise",
        "horn_stab",
    ],

    "swing": [
        "vinyl_noise",
        "vinyl_scratch",
        "short_percussion",
        "horn_stab",
    ],

    "instrumental": [
        "soft_air",
        "gentle_sweep",
        "ambient_swell",
        "reverse_reverb",
    ],

    "contemporary r&b": [
        "vinyl_noise",
        "tape_warmth",
        "soft_whoosh",
        "reverse_sweep",
    ],

    "neo soul": [
        "vinyl_noise",
        "tape_warmth",
        "soft_air",
        "ambient_swell",
    ],

    "reggae": [
        "vinyl_noise",
        "short_percussion",
        "steel_drum",
        "soft_whoosh",
    ],

    "latin": [
        "short_percussion",
        "gentle_sweep",
        "soft_whoosh",
        "steel_drum",
    ],

    "tropical house": [
        "soft_riser",
        "soft_whoosh",
        "gentle_sweep",
        "ocean_waves",
    ],

    "soca": [
        "short_percussion",
        "short_whoosh",
        "steel_drum",
        "short_impact",
    ],

    "drum and bass": [
        "riser",
        "glitch",
        "stutter",
        "sub_drop",
    ],

    "drone": [
        "deep_drone",
        "ambient_swell",
        "reverse_reverb",
        "soft_air",
    ],

    "downtempo": [
        "ambient_swell",
        "soft_air",
        "soft_riser",
        "gentle_sweep",
    ],

    "soft rock": [
        "soft_air",
        "gentle_sweep",
        "soft_whoosh",
        "tape_warmth",
    ],

    "lo-fi hip-hop": [
        "vinyl_noise",
        "tape_warmth",
        "cassette_stop",
        "soft_whoosh",
    ],

    "country": [
        "soft_air",
        "gentle_sweep",
        "tape_warmth",
        "reverse_reverb",
    ],

    "chiptune": [
        "arcade_blip",
        "glitch",
        "stutter",
        "short_impact",
    ],

    "acid house": [
        "riser",
        "glitch",
        "sub_drop",
        "laser_riser",
        "white_noise_sweep",
    ],
}

# Where the agent looks for effect audio files. Each effect is one mp3 whose
# base name is the effect's id, e.g. effects\vinyl_noise.mp3.
import os as _os
EFFECTS_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "effects")


def available_effect_names():
    """Set of effect ids (mp3 base names) that actually exist in the effects
    folder, so the agent only ever offers sounds the playback engine can load
    and only genre-matched sounds are put in the prompt."""
    path = _os.path.join(EFFECTS_DIR, ".")
    if not _os.path.isdir(EFFECTS_DIR):
        return set()
    return {
        _os.path.splitext(f)[0]
        for f in _os.listdir(path)
        if f.lower().endswith(".mp3")
    }


def genre_effects(genre):
    """Curated effects for a genre (GENRE_EFFECTS) filtered down to the ones
    that are actually present in the effects folder, in their curated order."""
    avail = available_effect_names()
    return [e for e in GENRE_EFFECTS.get(genre, []) if e in avail]


def missing_genre_effects():
    """Every effect name that GENRE_EFFECTS demands but no audio file exists
    for yet. These are the ones effects.py should fetch (`--fill`)."""
    avail = available_effect_names()
    wanted = set()
    for effects in GENRE_EFFECTS.values():
        wanted.update(effects)
    return sorted(wanted - avail)