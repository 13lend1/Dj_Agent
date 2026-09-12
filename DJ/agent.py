import os
import json

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_MODEL = "gemini-3.5-flash"

FEATURE_COLS = [
    "bpm", "energy", "danceability", "valence", "acousticness", "instrumentalness"
]

_QUEUE_SCHEMA = {
    "type": "object",
    "properties": {
        "queue": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "hook_start_sec": {"type": "number"},
                    "hook_end_sec": {"type": "number"},
                    "transition_type": {
                        "type": "string",
                        "enum": ["beatmatched_crossfade", "crossfade"],
                    },
                    "crossfade_bars": {"type": "number"},
                    "transition_note": {"type": "string"},
                },
                "required": ["id", "hook_start_sec", "hook_end_sec"],
            },
        }
    },
    "required": ["queue"],
}


class Agent:
    """
    Takes the candidate songs that came out of the linear model and turns them
    into an ordered play queue. Ordering, hook-window picks (e.g. 1:22-1:55),
    and transition calls between consecutive songs are all delegated to
    Gemini in a single call per batch — Gemini sees each candidate's genre,
    audio specs (bpm, energy, danceability, valence, ...) and the model's
    likeability score, and returns the full sequenced queue.

    _validate/_clip_window/_fmt/_transition below are NOT a second ordering
    engine — they're only a safety net for malformed or partial responses
    (e.g. Gemini drops a song, or returns a hook window outside the song's
    duration), so a bad API response degrades gracefully instead of crashing
    the player.
    """

    def __init__(self, api_key=None, model=GEMINI_MODEL):
        self._last = None  # the song currently / most recently played
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set (checked constructor arg, then env / .env)."
            )
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def track(self, song):
        """Remember the song that is currently playing so the next ranking
        transitions out of it."""
        self._last = song
        return song

    def last(self):
        return self._last

    def rank(self, songs, current=None):
        """Order the given n-best songs best-first for the transition, via
        Gemini. Falls back to the given order if the call fails."""
        previous = current if current is not None else self._last
        songs = list(songs)
        if not songs:
            return []

        by_id = {str(s.get("id")): s for s in songs}
        try:
            queue = self._query_gemini(songs, previous, clip_length=None)
        except Exception as exc:
            print(f"[Agent] Gemini ordering failed, returning original order: {exc}")
            return songs

        ordered, seen = [], set()
        for entry in queue:
            sid = str(entry.get("id"))
            song = by_id.get(sid)
            if song is None or sid in seen:
                continue  # hallucinated or duplicate id, skip it
            seen.add(sid)
            ordered.append(song)
        ordered.extend(s for s in songs if str(s.get("id")) not in seen)
        return ordered

    def build_playlist(self, songs, current=None, clip_length=33):
        """
        Build an ordered playlist via a single Gemini call: Gemini sequences
        ALL candidates so each flows out of `current` (then out of the song
        before it), and returns each song's hook window and its transition
        into the next song:
          - play_start_sec / play_end_sec: the clip in seconds
          - play_start / play_end: same window as "m:ss" (e.g. "1:22"/"1:55")
          - transition_out: how to move into the next song (None for last)
        """
        previous = current if current is not None else self._last
        songs = list(songs)
        if not songs:
            return []

        by_id = {str(s.get("id")): s for s in songs}
        try:
            queue = self._query_gemini(songs, previous, clip_length)
        except Exception as exc:
            print(f"[Agent] Gemini call failed, queue will use fallback ordering/hooks: {exc}")
            queue = []

        playlist, seen_ids = [], set()
        for entry in queue:
            song_id = str(entry.get("id"))
            song = by_id.get(song_id)
            if song is None or song_id in seen_ids:
                continue  # hallucinated or duplicate id, skip it
            seen_ids.add(song_id)

            song = self._validate(dict(song))
            start, end = self._resolve_hook(song, entry, clip_length)
            song["play_start_sec"], song["play_end_sec"] = start, end
            song["play_start"], song["play_end"] = self._fmt(start), self._fmt(end)
            song["_gemini_transition"] = {
                "type": entry.get("transition_type"),
                "crossfade_sec": entry.get("crossfade_sec"),
                "note": entry.get("transition_note"),
            }
            playlist.append(song)

        # Anything Gemini dropped (or the whole call failing) still needs to
        # end up in the queue — append in original order via the safety net,
        # not a second ordering pass.
        missing = [s for s in songs if str(s.get("id")) not in seen_ids]
        if missing:
            if queue:
                print(f"[Agent] Gemini omitted {len(missing)} song(s); appending in original order.")
            for s in missing:
                s = self._validate(dict(s))
                start, end = self._clip_window(s, clip_length)
                s["play_start_sec"], s["play_end_sec"] = start, end
                s["play_start"], s["play_end"] = self._fmt(start), self._fmt(end)
                s["_gemini_transition"] = None
                playlist.append(s)

        self._finalize_transitions(playlist, previous=previous)
        return playlist

    def _query_gemini(self, songs, previous, clip_length):
        """Single Gemini call: returns the parsed `queue` list (each item a
        dict with id / hook_start_sec / hook_end_sec / transition_*)."""
        chat = self._client.chats.create(
            model=self._model,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_QUEUE_SCHEMA,
                temperature=0.4,
            ),
        )
        response = chat.send_message(
            self._build_prompt(songs, previous, clip_length)
        )
        return json.loads(response.text).get("queue", [])

    def _build_prompt(self, songs, previous, clip_length):
        """Single-call prompt: Gemini orders the queue by AUDIO compatibility
        (beatmatch-able BPM, matched energy/danceability contour), picks each
        song's hook = the most playable / mixing-friendly part, and returns a
        crossfade for EVERY consecutive pair. Audio specs are the source of
        truth; genre is at most a weak tiebreaker."""
        lines = []
        lines.append("You are a professional DJ building one smooth set.")
        lines.append("")
        lines.append("Currently playing (this is the song the first pick must")
        lines.append("flow out of):")
        lines.append(f"  > {self._desc(previous)}")
        lines.append("")
        lines.append("Candidate songs (audio specs are the source of truth,")
        lines.append("NOT genre):")
        for s in songs:
            lines.append(f"  - {self._desc(s)}")
        lines.append("")
        lines.append(f"Each song will be played for about {clip_length or 33} "
                     "seconds of its hook window.")
        lines.append("")
        lines.append("ORDERING RULES (in priority order):")
        lines.append("  1. BPM compatibility first: prefer consecutive songs")
        lines.append("     whose BPM is within ~8 of one another so a beatmatch")
        lines.append("     is musically real; keep a gradual, mostly-stable BPM")
        lines.append("     contour and avoid big BPM cliffs unless the energy")
        lines.append("     change makes it an intentional lift.")
        lines.append("  2. Energy + danceability contour: adjacent songs should")
        lines.append("     have close energy and danceability (delta energy of")
        lines.append("     about 0.2 or less blends cleanly; a larger jump is")
        lines.append("     fine only as a deliberate lift, paired with a quick")
        lines.append("     fade).")
        lines.append("  3. Valence / acousticness / instrumentalness are mood")
        lines.append("     glue, not hard rules.")
        lines.append("  4. Genre is a weak tiebreaker only. Do NOT order by")
        lines.append("     genre similarity.")
        lines.append("")
        lines.append("HOOK WINDOWS: for every song, return hook_start_sec and")
        lines.append("hook_end_sec pointing at the most playable, mixing-friendly")
        lines.append("part of the track (the drop, hook, or chorus — the part a")
        lines.append("DJ would actually ride), NOT the intro or outro, and never")
        lines.append("beyond the song's duration.")
        lines.append("")
        lines.append("TRANSITIONS: for EVERY consecutive pair — including the")
        lines.append("currently-playing song into the FIRST candidate — return")
        lines.append("transition_type, crossfade_bars and transition_note:")
        lines.append("  - 'beatmatched_crossfade' when BPMs are close enough to")
        lines.append("    key in and ride both tracks; otherwise 'crossfade'.")
        lines.append("  - crossfade_bars: length of the blend in MUSICAL BARS (4")
        lines.append("    beats), not seconds — we convert to real seconds from the")
        lines.append("    actual BPM. Use 2 for a tight beatmatch you can ride")
        lines.append("    freely, 1 for a solid match, 0.5 for groove-tolerant")
        lines.append("    drift, 0.25 (one beat) for a short fade over a bigger jump.")
        lines.append("  - transition_note: name the SPECIFIC element carrying the")
        lines.append("    blend (bassline, vocal, hi-hats, the drop) based on both")
        lines.append("    songs' energy/valence/instrumentalness — never a generic")
        lines.append("    'smooth blend' line.")
        lines.append("  - The LAST song in the order has no outgoing transition")
        lines.append("    (leave its transition fields out).")
        return "\n".join(lines)

    
    @staticmethod
    def _bar_seconds(bpm):
        bpm = bpm or 120.0
        return (60.0 / bpm) * 4  # 4 beats per bar

    def _crossfade_seconds(self, bars, bpm_a, bpm_b):
        bars = bars if bars and bars > 0 else 1.0
        bpms = [b for b in (bpm_a, bpm_b) if b]
        avg_bpm = sum(bpms) / len(bpms) if bpms else 120.0
        seconds = bars * self._bar_seconds(avg_bpm)
        return round(min(max(seconds, 1.0), 16.0), 1)  # clamp to something audible
    
    @staticmethod
    def _desc(song):
        """One-line audio-spec summary of a song (imported DB rows; duration
        is stored in milliseconds)."""
        song = song or {}
        name = song.get("name") or "?"
        artist = song.get("artist") or "?"
        dur = (song.get("duration") or 0) / 1000.0

        def _num(v, nd=2):
            try:
                return f"{float(v):.{nd}f}"
            except (TypeError, ValueError):
                return "?"

        bpm = song.get("bpm")
        bpm_s = f"{float(bpm):.1f}" if isinstance(bpm, (int, float)) else "?"
        return (
            f"{name} by {artist} — bpm {bpm_s}, energy {_num(song.get('energy'))}, "
            f"danceability {_num(song.get('danceability'))}, "
            f"valence {_num(song.get('valence'))}, "
            f"acousticness {_num(song.get('acousticness'))}, "
            f"instrumentalness {_num(song.get('instrumentalness'))}, "
            f"duration {dur:.0f}s, genre {song.get('genre') or '?'}"
        )

    def _resolve_hook(self, song, entry, clip_length):
        duration_sec = (song.get("duration") or 0) / 1000.0
        start, end = entry.get("hook_start_sec"), entry.get("hook_end_sec")
        valid = (
            start is not None
            and end is not None
            and start >= 0
            and end > start
            and (not duration_sec or end <= duration_sec)
        )
        if not valid:
            start, end = self._clip_window(song, clip_length)
        return round(start, 1), round(end, 1)

    def _finalize_transitions(self, playlist, previous=None):
        """Fill transition_out so each crossfade note prints ON TIME.

        Live Gemini anchors each transition - type, crossfade_sec and the
        human note 'Cross fading from Be the one to Geronimo' - on the
        row the move goes INTO (destination-anchored, move-in semantics).
        Two failure shapes result if you read only the source row:
          * pair (X, Y): reading X only misses the note Gemini put on Y with
            a note that names X and Y together; then
          * at a build boundary where Be the one is the CURRENT song
            (passed as `previous`), its note is anchored on playlist[0]
            (Geronimo) and reading only inner-pair endpoints lets that
            boundary note fall through to pair (Geronimo, Levels), printing
            'Cross fading from Be the one to Geronimo' when GERONIMO ends -
            exactly one song late.

        We therefore pick, per pair, the note (from EITHER endpoint) whose
        text actually names BOTH songs of the pair, and attach it to the
        SOURCE. The boundary pair (previous -> playlist[0]) is handled
        explicitly: its note is written onto `previous` (the caller's
        current_song) so it prints when Be the one's clip ends. Any stale
        copy still anchored on the destination is cleared so it is never
        reused for the wrong pair.
        """
        def _pairs(a, b, gem):
            if not gem or not gem.get("type") or gem.get("crossfade_bars") is None:
                return False
            note = (gem.get("note") or "").lower()
            return (a.get("name") or "").lower() in note and (b.get("name") or "").lower() in note
        if previous is not None and playlist:
            song_a, song_b = previous, playlist[0]
            prev_gem = previous.pop("_gemini_transition", None)
            first_gem = playlist[0].pop("_gemini_transition", None)
            gem = None
            if _pairs(song_a, song_b, prev_gem):
                gem = prev_gem
            elif _pairs(song_a, song_b, first_gem):
                gem = first_gem
            if gem is not None:
                previous["transition_out"] = {
                    "type": gem["type"],
                    "crossfade_sec": float(gem["crossfade_sec"]),
                    "note": gem.get("note") or "",
                    "bpm_a": song_a.get("bpm") or 0,
                    "bpm_b": song_b.get("bpm") or 0,
                    "energy_diff": round(
                        abs((song_a.get("energy") or 0) - (song_b.get("energy") or 0)), 2
                    ),
                    "valence_diff": round(
                        abs((song_a.get("valence") or 0) - (song_b.get("valence") or 0)), 2
                    ),
                }
            else:
                # No well-formed note for the boundary pair (or none at all):
                # still blend — give the current song a real crossfade out.
                previous["transition_out"] = self._transition(song_a, song_b)

        for i in range(len(playlist) - 1):
            song_a, song_b = playlist[i], playlist[i + 1]

            source_gem = playlist[i].pop("_gemini_transition", None)
            dest_gem = playlist[i + 1].pop("_gemini_transition", None)

            gem = None
            if _pairs(song_a, song_b, source_gem):
                gem = source_gem
            elif _pairs(song_a, song_b, dest_gem):
                gem = dest_gem

            if gem is None:
                # no well-formed pair-naming note on either endpoint: fall
                # back to the BPM/energy safety net
                playlist[i]["transition_out"] = self._transition(song_a, song_b)
            else:
                playlist[i]["transition_out"] = {
                    "type": gem["type"],
                    "crossfade_sec": self._crossfade_seconds(gem["crossfade_bars"], song_a.get("bpm"), song_b.get("bpm")),
                    "crossfade_bars": gem["crossfade_bars"],
                    "note": gem.get("note") or "",
                    "bpm_a": song_a.get("bpm") or 0,
                    "bpm_b": song_b.get("bpm") or 0,
                    "energy_diff": round(
                        abs((song_a.get("energy") or 0) - (song_b.get("energy") or 0)), 2
                    ),
                    "valence_diff": round(
                        abs((song_a.get("valence") or 0) - (song_b.get("valence") or 0)), 2
                    ),
                }

        if playlist:
            playlist[-1].pop("_gemini_transition", None)
            playlist[-1]["transition_out"] = None

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
        Safety-net segment pick, used only when Gemini's hook window is
        missing or invalid: skip the intro (~25% in) and take clip_length
        seconds from there (or clip_length=33 if unset, e.g. from rank()).
        """
        clip_length = clip_length or 33
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
        bpm_a = song_a.get("bpm") or 0
        bpm_b = song_b.get("bpm") or 0
        bpm_diff = abs(bpm_a - bpm_b)
        energy_diff = abs((song_a.get("energy") or 0) - (song_b.get("energy") or 0))
        valence_diff = abs((song_a.get("valence") or 0) - (song_b.get("valence") or 0))

        if bpm_diff <= 2:
            transition_type, bars = "beatmatched_crossfade", 2.0
            note = f"Key-locked 2-bar blend: BPMs match (~{bpm_a:.0f}), ride both grooves together."
        elif bpm_diff <= 6:
            transition_type, bars = "beatmatched_crossfade", 1.0
            note = f"Tight 1-bar beatmatch (~{bpm_diff:.0f} BPM apart)."
        elif bpm_diff <= 14 and energy_diff <= 0.4:
            transition_type, bars = "crossfade", 0.5
            note = f"Groove-tolerant half-bar crossfade (BPM drift {bpm_diff:.0f}, energy gap {energy_diff:.2f})."
        elif bpm_diff <= 24:
            transition_type, bars = "crossfade", 0.25
            note = f"Quick one-beat fade — {bpm_diff:.0f} BPM drift, energy gap {energy_diff:.2f}."
        else:
            transition_type, bars = "crossfade", 0.25
            note = (f"Big {bpm_diff:.0f} BPM jump — clipped to a single-beat fade "
                    f"so the set never drops out; valence shift {valence_diff:.2f}.")

        return {
            "type": transition_type,
            "crossfade_sec": self._crossfade_seconds(bars, bpm_a, bpm_b),
            "crossfade_bars": bars,
            "note": note,
            "bpm_a": bpm_a,
            "bpm_b": bpm_b,
            "energy_diff": round(energy_diff, 2),
            "valence_diff": round(valence_diff, 2),
        }

    @staticmethod
    def _fmt(seconds):
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"