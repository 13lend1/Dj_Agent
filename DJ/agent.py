FEATURE_COLS = [
    "bpm", "energy", "danceability", "valence", "acousticness", "instrumentalness"
]

# Genres that tend to flow well into each other (checked both directions).
GENRE_NEIGHBORS = {
    "edm": ["house", "techno", "dance", "pop"],
    "house": ["edm", "techno", "dance"],
    "techno": ["edm", "house"],
    "dance": ["edm", "pop", "house"],
    "pop": ["edm", "dance", "r&b", "indie"],
    "hip-hop": ["r&b", "rap", "pop"],
    "hiphop": ["r&b", "rap", "pop"],
    "rap": ["hip-hop", "r&b"],
    "r&b": ["hip-hop", "soul", "pop"],
    "soul": ["r&b", "funk"],
    "funk": ["soul", "disco"],
    "disco": ["funk", "dance"],
    "reggae": ["latin", "funk"],
    "latin": ["reggae", "pop"],
    "rock": ["indie", "punk", "metal"],
    "indie": ["rock", "pop"],
    "punk": ["rock", "metal"],
    "metal": ["rock", "punk"],
    "jazz": ["blues", "soul"],
    "blues": ["jazz", "soul"],
    "classical": ["ambient"],
    "ambient": ["classical", "jazz"],
    "country": ["rock", "folk"],
}


class Agent:
    """
    Takes the candidate songs that came out of the linear model and turns them
    into an ordered play queue. Each song is picked greedily so that it flows
    AFTER the song that played just before it — the first pick flows out of the
    currently playing song, the second out of the first, and so on — matching
    genre and audio specs (bpm, energy, danceability, valence, ...) while
    keeping the model's likeability in the mix. Every queued song also
    receives the hook window (e.g. 1:22 - 1:55) that should actually be
    played.
    """

    def __init__(self):
        self._last = None  # the song currently / most recently played

    def track(self, song):
        """Remember the song that is currently playing so the next ranking
        transitions out of it."""
        self._last = song
        return song

    def last(self):
        return self._last

    @staticmethod
    def _genre_fit(candidate, current):
        cg = (candidate.get("genre") or "").strip().lower()
        pg = (current.get("genre") or "").strip().lower()
        if cg and cg == pg:
            return 1.0
        if cg and pg:
            if pg in GENRE_NEIGHBORS.get(cg, []) or cg in GENRE_NEIGHBORS.get(pg, []):
                return 0.6
        return 0.0

    @staticmethod
    def _spec_fit(candidate, current):
        weights = {
            "bpm": 0.30,
            "energy": 0.25,
            "danceability": 0.20,
            "valence": 0.15,
            "acousticness": 0.05,
            "instrumentalness": 0.05,
        }
        total = 0.0
        wsum = 0.0
        for col, w in weights.items():
            a = candidate.get(col)
            b = current.get(col)
            if a is None or b is None:
                continue
            span = max(abs(b), 1.0)
            closeness = max(0.0, 1.0 - abs(a - b) / span)
            total += w * closeness
            wsum += w
        return total / wsum if wsum else 0.5

    def fit_score(self, candidate, current):
        """Higher = flows better after `current`. With no current song yet it
        falls back to the model's likeability (trust the ranking)."""
        likeability = candidate.get("likeability") or 0.0
        if current is None:
            return float(likeability)
        return (
            0.40 * self._genre_fit(candidate, current)
            + 0.35 * self._spec_fit(candidate, current)
            + 0.25 * likeability
        )

    def rank(self, songs, current=None):
        """Order the given n-best songs best-first for the transition."""
        if current is None:
            current = self._last
        ordered = sorted(
            songs,
            key=lambda s: self.fit_score(s, current),
            reverse=True,
        )
        return ordered

    def build_playlist(self, songs, current=None, clip_length=33):
        """
        Build an ordered playlist by greedy sequential choice: the first song
        is the candidate that flows best after `current` (the song playing),
        then each following song is the remaining candidate that flows best
        after the song just chosen — so song #3 matches song #2, not the
        song that was playing when the batch was built. Each song also gets
        the hook window to actually play:
          - play_start_sec / play_end_sec: the clip in seconds
          - play_start / play_end: same window as "m:ss" (e.g. "1:22"/"1:55")
          - transition_out: how to move into the next song (None for last)
        """
        remaining_songs = list(songs)
        previous = current if current is not None else self._last

        playlist = []
        while remaining_songs:
            best = max(
                remaining_songs,
                key=lambda s: self.fit_score(s, previous),
            )
            remaining_songs.remove(best)

            best = self._validate(best)
            start, end = self._clip_window(best, clip_length)
            best["play_start_sec"] = start
            best["play_end_sec"] = end
            best["play_start"] = self._fmt(start)
            best["play_end"] = self._fmt(end)
            playlist.append(best)

            # the next song must flow out of THIS one, not out of the seed
            previous = best

        for i in range(len(playlist) - 1):
            playlist[i]["transition_out"] = self._transition(playlist[i], playlist[i + 1])
        if playlist:
            playlist[-1]["transition_out"] = None

        return playlist

    def _validate(self, song):
        """Return a copy of song with obvious data issues fixed/flagged."""
        song = dict(song)
        issues = []

        artist = (song.get("artist") or "").strip()
        if not artist:
            issues.append("missing_artist")
            artist = "Unknown Artist"
        song["artist"] = artist

        link = (song.get("link") or "").strip()
        if not link.startswith(("http://", "https://")):
            issues.append("bad_link")
        song["link"] = link

        if not song.get("duration"):
            issues.append("missing_duration")

        song["issues"] = issues
        return song

    def _clip_window(self, song, clip_length):
        """
        Pick the segment of the song to actually play. Uses hook_start/hook_end
        if the heatmap/analysis pipeline already set them on the row; otherwise
        falls back to skipping the intro (~25% in) and taking clip_length
        seconds from there.
        """
        duration_ms = song.get("duration") or 0
        duration = duration_ms / 1000.0  # duration is stored in milliseconds
        if song.get("hook_start") is not None and song.get("hook_end") is not None:
            start, end = song["hook_start"], song["hook_end"]
        else:
            start = duration * 0.25 if duration else 0
            end = start + clip_length
            if duration:
                end = min(end, duration)
        return round(start, 1), round(end, 1)

    def _transition(self, song_a, song_b):
        """
        Recommend how the DJ should move from song_a into song_b based on
        BPM (beatmatchability) and energy/valence closeness. Returns a dict
        with the transition type, how long the crossfade should last and a
        short note for the DJ.
        """
        bpm_a = song_a.get("bpm") or 0
        bpm_b = song_b.get("bpm") or 0
        bpm_diff = abs(bpm_a - bpm_b)
        energy_diff = abs((song_a.get("energy") or 0) - (song_b.get("energy") or 0))
        valence_diff = abs((song_a.get("valence") or 0) - (song_b.get("valence") or 0))

        if bpm_diff <= 2:
            entry = {
                "type": "beatmatched_crossfade",
                "crossfade_sec": 8.0,
                "note": "Key-locked blend: BPMs match, ride both sections together.",
            }
        elif bpm_diff <= 6:
            entry = {
                "type": "beatmatched_crossfade",
                "crossfade_sec": 4.0,
                "note": f"Tight beatmatch (~{bpm_diff} BPM apart), blend over 4s.",
            }
        elif bpm_diff <= 14 and energy_diff <= 0.4:
            entry = {
                "type": "crossfade",
                "crossfade_sec": 3.0,
                "note": "BPM drift within groove tolerance, quick 3s crossfade.",
            }
        elif bpm_diff <= 24:
            entry = {
                "type": "crossfade",
                "crossfade_sec": 2.0,
                "note": f"BPM drift ~{bpm_diff} BPM, energy gap {energy_diff:.2f} — short 2s fade.",
            }
        else:
            entry = {
                "type": "cut",
                "crossfade_sec": 0.0,
                "note": f"Hard cut on the downbeat — big BPM jump ({bpm_diff} BPM).",
            }

        entry["bpm_a"] = bpm_a
        entry["bpm_b"] = bpm_b
        entry["energy_diff"] = round(energy_diff, 2)
        entry["valence_diff"] = round(valence_diff, 2)
        return entry

    @staticmethod
    def _fmt(seconds):
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"