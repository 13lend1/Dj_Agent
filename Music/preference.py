# Set the default place here (must be a key in PLACE_GENRES).
# Override with --resume-place <key> on the command line or
# DJ_RESUME_PLACE=<key> env variable.
DEFAULT_PLACE = "rave"

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