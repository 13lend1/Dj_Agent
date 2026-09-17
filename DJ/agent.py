import os
import json
import threading
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types
load_dotenv()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_MIN_INTERVAL = 10.0
GEMINI_REQUEST_TIMEOUT = 45.0
_GEMINI_LOCK = threading.Lock()
_GEMINI_LAST_CALL = 0.0

# Build the allowed effect list from actual files on disk (repo/effects/*.mp3).
# Every curated genre effect in preference.GENRE_EFFECTS that has been
# downloaded (via effects.py or effects.py --fill) automatically appears here
# on the next import, so the enum never lists names the playback engine
# can't actually load.
def _load_effect_enum():
    from Music.preference import available_effect_names
    names = available_effect_names() | {"none"}
    return sorted(names)

_EFFECT_ENUM = _load_effect_enum()

# Available genre→effects map, filtered to files actually on disk.
# Used inside _build_prompt to inject per-song effect hints.
from collections import defaultdict as _defaultdict

def _available_genre_effects():
    from Music.preference import available_effect_names, GENRE_EFFECTS
    avail = available_effect_names()
    return {
        genre: [e for e in effects if e in avail]
        for genre, effects in GENRE_EFFECTS.items()
    }

_AVAILABLE_GENRE_EFFECTS = _available_genre_effects()


def _gemini_throttle():
    global _GEMINI_LAST_CALL
    with _GEMINI_LOCK:
        wait = GEMINI_MIN_INTERVAL - (time.time() - _GEMINI_LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _GEMINI_LAST_CALL = time.time()


def _gemini_request(fn, timeout=GEMINI_REQUEST_TIMEOUT):
    out = {}

    def _run():
        try:
            out["res"] = fn()
        except BaseException as exc:
            out["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise RuntimeError(f"Gemini request timed out after {timeout:.0f}s")
    if "exc" in out:
        raise out["exc"]
    return out.get("res")


class GeminiEmptyResponse(RuntimeError):
    """Gemini returned None / empty text — a definitive response failure."""
    pass
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
                    # "clip_length_sec": {"type": "number"},
                    "transition_type": {
                        "type": "string",
                        "enum": ["beatmatched_crossfade", "crossfade"],
                    },
                    "crossfade_bars": {"type": "number"},
                    "transition_note": {"type": "string"},
                    "effect": {
                        "type": "string",
                        "enum": _EFFECT_ENUM,
                    },
                },
                "required": ["id", "hook_start_sec", "hook_end_sec"],
            },
        }
    },
    "required": ["queue"],
}
_EFFECT_NAMES = set(
    _QUEUE_SCHEMA["properties"]["queue"]["items"]["properties"]["effect"]["enum"]
)
_HOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "hook_start_sec": {"type": "number"},
                    "hook_end_sec": {"type": "number"},
                },
                "required": ["id", "hook_start_sec", "hook_end_sec"],
            },
        }
    },
    "required": ["hooks"],
}


def _norm_sid(value):
    """Normalize a song id for cross-referencing Gemini entries (the ids are
    UUIDs/YouTube ids, so whitespace-only or None never matches a song)."""
    return str(value or "").strip()
class Agent:
    """
    AI DJ agent.
    Responsibilities
    ----------------
    1. Gemini orders candidate songs.
    2. Gemini chooses the best hook/window for every song.
    3. Gemini chooses the transition between every consecutive song.
    4. Python validates all timestamps.
    5. Python calculates clip_length.
    6. Python guarantees a transition exists between every pair.
    The class is safe to import from another module:
        from agent import Agent
        agent = Agent()
        playlist = agent.build_playlist(
            songs,
            current=current_song,
            clip_length=33
        )
    Expected song dictionary fields
    --------------------------------
    Required:
        id
        name
        artist
        duration
    Recommended:
        genre
        bpm
        energy
        danceability
        valence
        acousticness
        instrumentalness
        link
    Duration is expected to be milliseconds, matching the database structure
    used by the DJ project.
    """
    def __init__(self, api_key=None, model=GEMINI_MODEL):
        self._last = None
        self._last_effect_name = None
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. " "Set it in your environment or .env file."
            )
        self._client = genai.Client(api_key=api_key)
        self._model = model
    def track(self, song):
        """
        Remember the currently playing song.
        Useful from player.py:
            agent.track(song)
        The next playlist will then be built as:
            current song -> first candidate -> second candidate -> ...
        """
        self._last = song
        return song
    def last(self):
        """Return the last tracked/current song."""
        return self._last
    def rank(self, songs, current=None):
        """
        Return songs ordered by Gemini.
        This method only returns the ordered song dictionaries.
        For full DJ information including hooks and transitions,
        use build_playlist().
        """
        previous = current if current is not None else self._last
        songs = list(songs)
        if not songs:
            return []
        by_id = {str(song.get("id")): song for song in songs}
        try:
            queue = self._query_gemini(songs=songs, previous=previous)
        except Exception as exc:
            print(
                f"[Agent] Gemini ordering failed, " f"returning original order: {exc}"
            )
            return songs
        ordered = []
        seen = set()
        for entry in queue:
            song_id = str(entry.get("id"))
            song = by_id.get(song_id)
            if song is None:
                continue
            if song_id in seen:
                continue
            seen.add(song_id)
            ordered.append(song)
        for song in songs:
            song_id = str(song.get("id"))
            if song_id not in seen:
                ordered.append(song)
        return ordered
    def build_playlist(self, songs, current=None, clip_length=33, place=None):
        """
        Build the complete AI-DJ playlist.
        Gemini decides:
            - song order
            - hook/best section
            - transition between songs
        Python guarantees:
            - valid timestamps
            - clip_length
            - transition_out on every non-final song
            - no transition on final song
        Parameters
        ----------
        songs:
            Iterable of song dictionaries.
        current:
            Currently playing song. If omitted, self._last is used.
        clip_length:
            Fallback clip length in seconds, used only when Gemini does
            not return a clip_length_sec for a song or returns no valid
            hook (default 33).
        place:
            Active place key (e.g. "home"). When given it is stored as the
            run's single place key in the Agent table; otherwise the place is
            derived from the songs (stored only when every song shares one).
        Returns
        -------
        list[dict]
        Each song receives:
            play_start_sec
            play_end_sec
            play_start
            play_end
            clip_length
            transition_out
        Example:
            {
                "id": "123",
                "name": "Song",
                "artist": "Artist",
                "play_start_sec": 82.0,
                "play_end_sec": 115.0,
                "play_start": "1:22",
                "play_end": "1:55",
                "clip_length": 33.0,
                "transition_out": {
                    "type": "beatmatched_crossfade",
                    "crossfade_sec": 4.0,
                    "crossfade_bars": 2.0,
                    "note": "...",
                    "effect": "none",
                    ...
                }
            }
        """
        previous = current if current is not None else self._last
        songs = list(songs)
        if not songs:
            return []
        try:
            clip_length = float(clip_length)
        except (TypeError, ValueError):
            clip_length = 33.0
        clip_length = max(1.0, clip_length)


        by_id = {_norm_sid(song.get("id")): song for song in songs}
        try:
            queue = self._query_gemini(
                songs=songs, previous=previous
            )
        except Exception as exc:
            print(
                "[Agent] Gemini call failed, "
                "queue will use fallback ordering/hooks: "
                f"{exc}"
            )
            queue = []
        # Deduplicate Gemini's queue into play order; the songs Gemini decided
        # to leave out are appended afterwards (order still follows Gemini's
        # set, those songs just join in input order at the tail).
        ordered_entries = []
        used_ids = set()
        for entry in queue:
            song_id = _norm_sid(entry.get("id"))
            if song_id not in by_id or song_id in used_ids:
                continue
            used_ids.add(song_id)
            ordered_entries.append(entry)
        missing_songs = [song for song in songs
                         if _norm_sid(song.get("id")) not in used_ids]

        # Gemini sometimes skips the hook fields (null / zero-length / out of
        # range) even though the schema asks for them. Collect every song that
        # ended up without a usable hook and ask Gemini once more, ONLY for
        # windows. Whatever it is still unsure about falls back to the middle
        # section, exactly as before.
        repair_needed = []
        for entry in ordered_entries:
            song = self._validate(dict(by_id[_norm_sid(entry.get("id"))]))
            _, _, fallback = self._resolve_hook(
                song=song, entry=entry, clip_length=clip_length
            )
            if fallback:
                repair_needed.append(song)
        for original_song in missing_songs:
            repair_needed.append(self._validate(dict(original_song)))
        repairs = self._request_hooks(repair_needed) if repair_needed else {}
        if len(repairs):
            print(
                f"[Agent] Gemini hook repair recovered real hooks for "
                f"{len(repairs)} song(s)."
            )

        def _fused_entry(entry, song_id):
            """Prefer the repaired window over the original Gemini window."""
            fused = dict(entry)
            window = repairs.get(song_id)
            if window:
                fused["hook_start_sec"] = window["hook_start_sec"]
                fused["hook_end_sec"] = window["hook_end_sec"]
            return fused

        playlist = []
        fallback_count = 0
        for entry in ordered_entries:
            song_id = _norm_sid(entry.get("id"))
            song = self._validate(dict(by_id[song_id]))
            fused = _fused_entry(entry, song_id)
            start, end, fallback = self._resolve_hook(
                song=song, entry=fused, clip_length=clip_length
            )
            if fallback:
                fallback_count += 1
                self._print_fallback(song, clip_length)
            song["play_start_sec"] = start
            song["play_end_sec"] = end
            song["play_start"] = self._fmt(start)
            song["play_end"] = self._fmt(end)
            song["clip_length"] = round(max(0.0, end - start), 1)
            song["_gemini_transition"] = {
                "type": entry.get("transition_type"),
                "crossfade_bars": entry.get("crossfade_bars"),
                "note": entry.get("transition_note"),
                "effect": entry.get("effect") or "none",
            }
            playlist.append(song)
        if missing_songs:
            if queue:
                print(
                    f"[Agent] Gemini omitted "
                    f"{len(missing_songs)} song(s); "
                    "appending them."
                )
            for original_song in missing_songs:
                song = self._validate(dict(original_song))
                fused = repairs.get(_norm_sid(song.get("id")))
                if fused:
                    start, end, fallback = self._resolve_hook(
                        song=song, entry=fused, clip_length=clip_length
                    )
                else:
                    start, end = self._clip_window(song, clip_length)
                    fallback = True
                if fallback:
                    fallback_count += 1
                    self._print_fallback(song, clip_length)
                song["play_start_sec"] = start
                song["play_end_sec"] = end
                song["play_start"] = self._fmt(start)
                song["play_end"] = self._fmt(end)
                song["clip_length"] = round(max(0.0, end - start), 1)
                song["_gemini_transition"] = None
                playlist.append(song)
        if fallback_count:
            print(
                f"[hooks] {len(playlist) - fallback_count}/{len(playlist)} used "
                f"Gemini hooks; {fallback_count}/{len(playlist)} fell back to the "
                f"{clip_length:.1f}s fallback."
            )
        self._finalize_transitions(playlist, previous=previous)
        self._save_response(playlist, place=place)
        return playlist
    def _save_response(self, playlist, place=None):
        """Persist every completed agent response to the Agent table.

        Stores the ordered songs, the transitions between them, the hook
        windows, the raw payload, and the run's single place key, exactly as
        returned. Saving must never break playlist generation, so a DB error
        is logged and swallowed.
        """
        try:
            from DJ.responses import save_response
        except ImportError:
            from responses import save_response
        try:
            run_id = save_response(playlist, place=place)
            if run_id:
                print(f"[Agent] Saved response {run_id} ({len(playlist)} songs).")
        except Exception as exc:
            print(f"[Agent] Could not save response: {exc}")
    def _query_gemini(self, songs, previous):
        """
        Make exactly one Gemini request.
        Gemini returns:
            queue[
                {
                    id,
                    hook_start_sec,
                    hook_end_sec,
                    transition_type,
                    crossfade_bars,
                    transition_note,
                    effect
                }
            ]
        """
        _gemini_throttle()
        prompt = self._build_prompt(
            songs=songs, previous=previous
        )
        last_exc = None
        for attempt in range(5):
            try:
                chat = self._client.chats.create(
                    model=self._model,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=_QUEUE_SCHEMA,
                        temperature=0.4,
                    ),
                )
                response = _gemini_request(
                    lambda: chat.send_message(prompt), GEMINI_REQUEST_TIMEOUT
                )
                if response is None:
                    raise GeminiEmptyResponse("Gemini returned no response.")
                if not response.text:
                    raise GeminiEmptyResponse("Gemini returned an empty response.")
                data = json.loads(response.text)
                queue = data.get("queue", [])
                if not isinstance(queue, list):
                    raise RuntimeError("Gemini response contained an invalid queue.")
                return queue
            except GeminiEmptyResponse as exc:
                raise exc
            except Exception as exc:
                last_exc = exc
                lower = str(exc).lower()
                if "quota" in lower or "resource_exhausted" in lower:
                    print(f"[Gemini] Quota exhausted ({exc}); giving up on this set.")
                    raise exc
                if "503" in str(exc) or "unavailable" in lower \
                        or "overloaded" in lower or "busy" in lower:
                    delay = 20.0  # model overloaded — needs the pause to recover
                elif "429" in lower or "too many" in lower or "rate limit" in lower:
                    delay = 20.0 * (attempt + 1)
                else:
                    delay = 5.0 * (attempt + 1)
                if attempt < 4:
                    print(f"[Gemini] Request failed ({exc}); retrying in {delay:.0f}s "
                          f"(attempt {attempt + 2}/5)...")
                    time.sleep(delay)
        raise last_exc
    def _request_hooks(self, songs):
        """Targeted follow-up request for songs whose first-pass hook was
        missing or unusable.

        The main request orders songs AND picks hooks AND picks transitions in
        one shot; structured output sometimes satisfies 'required' by emitting
        null/zero windows instead of a real section. This second request asks
        for ONLY the hook window of EVERY listed song (no ordering, no
        transitions), which is far easier for the model to answer completely.

        Returns {id: {"hook_start_sec": float, "hook_end_sec": float}} for the
        songs Gemini answered with a valid in-range window, else {}.
        """
        if not songs:
            return {}
        _gemini_throttle()
        prompt = self._build_hook_prompt(songs)
        last_exc = None
        for attempt in range(4):
            try:
                chat = self._client.chats.create(
                    model=self._model,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=_HOOK_SCHEMA,
                        temperature=0.2,
                    ),
                )
                response = _gemini_request(
                    lambda: chat.send_message(prompt), GEMINI_REQUEST_TIMEOUT
                )
                if response is None or not response.text:
                    last_exc = GeminiEmptyResponse(
                        "Gemini returned no data for the hook repair."
                    )
                    if attempt == 0:
                        print(
                            "[Gemini] Hook repair returned empty; retrying in 15s..."
                        )
                        time.sleep(15.0)
                    continue
                data = json.loads(response.text)
                hooks = data.get("hooks") or []
                windows = {}
                for entry in hooks:
                    song_id = _norm_sid(entry.get("id"))
                    if not song_id:
                        continue
                    try:
                        start = float(entry.get("hook_start_sec"))
                        end = float(entry.get("hook_end_sec"))
                    except (TypeError, ValueError):
                        continue
                    if start < 0 or end <= start:
                        continue
                    windows[song_id] = {
                        "hook_start_sec": start,
                        "hook_end_sec": end,
                    }
                return windows
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    print(
                        f"[Gemini] Hook repair failed ({exc}); retrying in 15s..."
                    )
                    time.sleep(15.0)
        # The repair is best-effort: never let it kill playlist generation.
        print(f"[Gemini] Hook repair gave up ({last_exc}); using fallbacks.")
        return {}
    def _build_hook_prompt(self, songs):
        lines = []
        lines.append("Return hook_start_sec/hook_end_sec for EVERY song below.")
        lines.append("")
        lines.append(
            "These songs ALREADY belong together in one set — do not order "
            "them, do not add transitions, ONLY give each one its strongest "
            "playable window."
        )
        lines.append("")
        lines.append(
            "Rules: hook_start_sec/hook_end_sec must both be numbers INSIDE "
            "the song's duration, hook_end_sec strictly greater than "
            "hook_start_sec, never 0/0, never null, never negative. Windows "
            "should be real musical passages (chorus, drop, build, climax, "
            "solo, peak) and may vary in length per song."
        )
        lines.append("")
        for index, song in enumerate(songs, start=1):
            lines.append(
                f"{index}. [id: {song.get('id')}] {self._desc(song)}"
            )
        lines.append("")
        lines.append(
            "Return the exact bracketed [id: ...] that appears above for each "
            "song, with a valid hook_start_sec/hook_end_sec for that song. "
            "Every song in the list must appear exactly once."
        )
        return "\n".join(lines)
    def _build_prompt(self, songs, previous):
        lines = []
        lines.append("You are an expert DJ building one continuous set.")
        lines.append("")
        if previous:
            lines.append(f"CURRENTLY PLAYING: {self._desc(previous)}")
            lines.append("The first candidate must flow naturally out of this.")
        else:
            lines.append("No song is currently playing.")
        lines.append("")
        lines.append("CANDIDATE SONGS:")
        lines.append(
            "(BPM/energy/danceability/valence/acousticness/instrumentalness "
            "below are averaged over the WHOLE song, not just the hook — the "
            "hook itself may feel more/less intense than these numbers suggest. "
            "Use your own knowledge of the song to judge the hook's actual "
            "character when ordering and matching transitions.)"
        )
        for index, song in enumerate(songs, start=1):
            lines.append(f"{index}. [id: {song.get('id')}] {self._desc(song)}")
        lines.append("")
        lines.append(
            "Return the EXACT bracketed [id: ...] for each song. You must: "
            "(1) order the songs, (2) pick each song's hook window, "
            "(3) pick the transition into the next song."
        )
        lines.append("")
        lines.append("SET ARC:")
        lines.append(
            "First state a one-line energy trajectory for the whole set "
            "(e.g. steady build, warm-up->peak->cooldown, peak-early->sustain) "
            "in `set_arc`. Order songs to follow it — don't just optimize "
            "each adjacent pair in isolation."
        )
        lines.append("")
        lines.append("ORDERING:")
        lines.append(
            "Don't preserve input order. Sequence for BPM compatibility, "
            "energy progression, danceability, valence/mood, and genre fit."
        )
        lines.append(
            "Group songs into clusters of similar genre/mood/BPM range where "
            "possible, and move between clusters at natural arc transition "
            "points (e.g. build->peak, peak->cooldown) rather than bouncing "
            "back and forth between styles multiple times in one set."
        )
        lines.append(
            "Avoid unnecessary BPM cliffs; an intentional jump is fine if it "
            "serves the music. ~0-8 BPM diff mixes easily, 8-15 needs a "
            "shorter/creative transition, larger diffs should use a short cut."
        )
        lines.append(
            "Avoid jagged energy (up-down-up-down); each phase of the arc "
            "should move mostly in one direction, with at most one deliberate "
            "contrast moment per phase if it serves the music."
        )
        lines.append(
            "A candidate does NOT have to be used. If a song doesn't fit "
            "anywhere in the set without hurting flow (BPM/key/genre clash, "
            "breaks the arc, no clean transition in or out), leave it out "
            "rather than forcing it in. Prefer a slightly shorter but "
            "consistently smooth set over including every candidate."
        )
        lines.append("")
        lines.append("HOOK SELECTION:")
        lines.append(
            "Return hook_start_sec/hook_end_sec: the actual most compelling "
            "section, using your knowledge of the song. Do NOT default to "
            "any typical length (e.g. ~30s) out of habit — let the window "
            "length come from the music itself, and vary it song to song."
        )
        lines.append(
            "NEVER return null, negative, zero-length, or out-of-range hook "
            "values, and never omit or empty hook_start_sec/hook_end_sec for "
            "a song you include. There are exactly two mandatory fields per "
            "song besides its id: hook_start_sec and hook_end_sec — every "
            "included song MUST have both, filled with real numbers. If you "
            "are not sure about a song's structure, still give "
            "a best-guess in-range window — e.g. the strongest passage you "
            "expect (a drop/build for club tracks, a crescendo or the middle "
            "movement for classical/orchestral), not timed to the track length "
            "by formula."
        )
        lines.append(
            "Not every song has a pop-style hook/chorus/drop. For music "
            "without one — classical, orchestral, jazz, ambient, long-form "
            "instrumental — pick the strongest passage or movement on its "
            "own terms (a theme, a climax, a solo), and let the window run "
            "as long as that passage actually needs, even 60-120s+. Don't "
            "force this kind of music into a short pop-length clip."
        )
        lines.append(
            "Avoid intro/outro unless it's genuinely the iconic part."
        )
        lines.append("TRANSITIONS:")
        lines.append(
            "Every consecutive pair needs a transition, including CURRENTLY "
            "PLAYING -> first candidate if applicable."
        )
        lines.append(
            "IMPORTANT: attach the transition to the song being ENTERED, not "
            "the song ending. That is, song X's transition_type/crossfade_bars/"
            "transition_note/effect describe how the set moves INTO song X "
            "from whatever plays immediately before it — not how X transitions "
            "into whatever comes after it. The first candidate's fields "
            "describe the transition from CURRENTLY PLAYING into it."
        )
        lines.append(
            "Use beatmatched_crossfade when BPMs are compatible, else crossfade. "
            "crossfade_bars: 2=smooth, 1=tight, 0.5=short, 0.25=very quick. "
            "transition_note names the carrying element (drums, bassline, "
            "hi-hats, vocal phrase, chorus, drop, percussion)."
        )
        lines.append("")
        lines.append("EFFECTS:")
        lines.append(
            "Default 'none'. The transition INTO each song may use an effect "
            "from its genre list below — all are files the playback engine "
            "can actually load. Choose an effect only when it genuinely "
            "improves the transition's motion or energy shift; 'none' is "
            "always a valid choice."
        )
        lines.append("")
        # Group songs by genre for the per-genre effect hint.
        genre_to_songs = {}
        for i, song in enumerate(songs, start=1):
            g = song.get("genre")
            if g:
                genre_to_songs.setdefault(g, []).append(i)
        lines.append("GENRE-SPECIFIC EFFECTS:")
        for genre, indices in sorted(genre_to_songs.items()):
            effects = _AVAILABLE_GENRE_EFFECTS.get(genre, [])
            if not effects:
                continue
            song_nums = ", ".join(f"#{i}" for i in indices)
            lines.append(f"  {genre} ({song_nums}): {', '.join(effects)}")
        lines.append("")
        lines.append(
            "Only return IDs from the candidate list, no invented IDs, and "
            "no duplicates. It is fine to omit a candidate if it doesn't fit "
            "the set — do not force in a song just to include every candidate. "
            "hook_start_sec/hook_end_sec must be valid timestamps inside "
            "the song. The last song needs no outgoing transition."
        )
        return "\n".join(lines)
    @staticmethod
    def _desc(song):
        """
        Create the information Gemini sees for a song.
        """
        song = song or {}
        name = song.get("name") or "Unknown Song"
        artist = song.get("artist") or "Unknown Artist"
        duration_ms = song.get("duration") or 0
        try:
            duration_sec = float(duration_ms) / 1000.0
        except (TypeError, ValueError):
            duration_sec = 0.0
        def num(value, decimals=2):
            try:
                return f"{float(value):.{decimals}f}"
            except (TypeError, ValueError):
                return "?"
        bpm = song.get("bpm")
        try:
            bpm_text = f"{float(bpm):.1f}"
        except (TypeError, ValueError):
            bpm_text = "?"
        return (
            f"{name} by {artist} | "
            f"BPM {bpm_text} | "
            f"energy {num(song.get('energy'))} | "
            f"danceability {num(song.get('danceability'))} | "
            f"valence {num(song.get('valence'))} | "
            f"acousticness {num(song.get('acousticness'))} | "
            f"instrumentalness {num(song.get('instrumentalness'))} | "
            f"duration {duration_sec:.0f}s | "
            f"genre {song.get('genre') or '?'}"
        )
    def _resolve_hook(self, song, entry, clip_length):
        """Returns (start, end, from_fallback). from_fallback is True when the
        Gemini-provided hook was missing or invalid, so the set summary can tell
        real hooks apart from the 33s fallback."""
        duration_sec = self._duration_seconds(song)
        try:
            start = float(entry.get("hook_start_sec"))
            end = float(entry.get("hook_end_sec"))
        except (TypeError, ValueError):
            return (*self._clip_window(song, clip_length), True)
        if start < 0 or end <= start:
            return (*self._clip_window(song, clip_length), True)
        if duration_sec > 0:
            start = min(start, duration_sec)
            end = min(end, duration_sec)
            if end <= start:
                return (*self._clip_window(song, clip_length), True)

        min_len = 8.0
        max_len = 120.0
        span = end - start
        if span < min_len:
            if duration_sec > 0 and start + min_len <= duration_sec:
                end = start + min_len
            else:
                start = max(0.0, end - min_len)
                end = start + min_len
        elif span > max_len:
            end = start + max_len
            if duration_sec > 0 and end > duration_sec:
                end = duration_sec
                start = max(0.0, end - max_len)
        return (round(start, 1), round(end, 1), False)

    def _print_fallback(self, song, clip_length):
        name = (song.get("name") or "Unknown Song").strip()
        print(
            f"[hooks] fallback {float(clip_length or 33.0):.1f}s for "
            f"'{name}' — Gemini gave no usable hook, using the middle section."
        )
    # def _entry_clip_length(self, entry):
    #     try:
    #         length = float(entry.get("clip_length_sec"))
    #     except (TypeError, ValueError):
    #         return None
    #     if length < 1.0:
    #         return None
    #     return max(8.0, min(length, 90.0))
    def _clip_window(self, song, clip_length):
        """
        Safety fallback when Gemini does not provide a valid hook.
        Priority:
        1. Existing hook_start/hook_end from the database.
        2. Middle section of the song.
        3. Beginning if duration is unknown.
        """
        desired_length = float(clip_length or 33.0)
        desired_length = max(1.0, desired_length)
        duration = self._duration_seconds(song)
        existing_start = song.get("hook_start")
        existing_end = song.get("hook_end")
        if existing_start is not None and existing_end is not None:
            try:
                start = float(existing_start)
                end = float(existing_end)
                if end > start:
                    if duration > 0:
                        start = max(0.0, min(start, duration))
                        end = max(start, min(end, duration))
                    return (round(start, 1), round(end, 1))
            except (TypeError, ValueError):
                pass
        if duration <= 0:
            return (0.0, round(desired_length, 1))
        if duration <= desired_length:
            return (0.0, round(duration, 1))
        start = max(0.0, (duration - desired_length) / 2.0)
        end = start + desired_length
        return (round(start, 1), round(end, 1))
    @staticmethod
    def _duration_seconds(song):
        """
        Convert the database duration from milliseconds to seconds.
        """
        duration = song.get("duration") or 0
        try:
            return max(0.0, float(duration) / 1000.0)
        except (TypeError, ValueError):
            return 0.0
    def _finalize_transitions(self, playlist, previous=None):
        """
        Convert Gemini's transition decisions into the final
        transition_out structure.
        IMPORTANT:
        A transition belongs to the SONG THAT IS ENDING.
        Therefore:
            A.transition_out = transition A -> B
        not:
            B.transition_out = transition A -> B
        This prevents the transition from happening one song late.
        """
        if not playlist:
            return
        if previous is not None:
            song_a = previous
            song_b = playlist[0]
            gem = song_b.pop("_gemini_transition", None)
            transition = self._make_transition(song_a, song_b, gem)
            previous["transition_out"] = transition
        for i in range(len(playlist) - 1):
            song_a = playlist[i]
            song_b = playlist[i + 1]
            gem_a = song_a.pop("_gemini_transition", None)
            gem_b = song_b.pop("_gemini_transition", None)
            gem = self._select_transition(song_a, song_b, gem_a, gem_b)
            song_a["transition_out"] = self._make_transition(song_a, song_b, gem)
        playlist[-1].pop("_gemini_transition", None)
        playlist[-1]["transition_out"] = None
    def _select_transition(self, song_a, song_b, gem_a, gem_b):
        """
        Select Gemini's transition data.
        Gemini may attach it to either endpoint.
        We therefore support both.
        """
        if self._transition_is_valid(gem_b):
            return gem_b
        if self._transition_is_valid(gem_a):
            return gem_a
        return None
    @staticmethod
    def _transition_is_valid(gem):
        """
        Check whether Gemini supplied enough transition information.
        """
        if not isinstance(gem, dict):
            return False
        transition_type = gem.get("type")
        bars = gem.get("crossfade_bars")
        if transition_type not in {"beatmatched_crossfade", "crossfade"}:
            return False
        try:
            bars = float(bars)
        except (TypeError, ValueError):
            return False
        return bars > 0
    def _make_transition(self, song_a, song_b, gem):
        """
        Create a complete transition.
        Gemini's decision is preferred.
        If Gemini failed to provide a usable transition,
        Python creates a musically reasonable fallback.
        Therefore EVERY non-final song gets transition_out.
        """
        if self._transition_is_valid(gem):
            transition_type = gem.get("type")
            try:
                bars = float(gem.get("crossfade_bars"))
            except (TypeError, ValueError):
                bars = 1.0
            bars = max(0.25, min(bars, 4.0))
            bpm_a = self._number(song_a.get("bpm"), 0.0)
            bpm_b = self._number(song_b.get("bpm"), 0.0)
            crossfade_sec = self._crossfade_seconds(bars, bpm_a, bpm_b)
            energy_diff = abs(
                self._number(song_a.get("energy"), 0.0)
                - self._number(song_b.get("energy"), 0.0)
            )
            valence_diff = abs(
                self._number(song_a.get("valence"), 0.0)
                - self._number(song_b.get("valence"), 0.0)
            )
            return {
                "type": transition_type,
                "crossfade_sec": crossfade_sec,
                "crossfade_bars": bars,
                "note": (
                    gem.get("note")
                    or f"Transition from "
                    f"{song_a.get('name', '?')} "
                    f"into "
                    f"{song_b.get('name', '?')}."
                ),
                "bpm_a": bpm_a,
                "bpm_b": bpm_b,
                "energy_diff": round(energy_diff, 2),
                "valence_diff": round(valence_diff, 2),
                "effect": self._normalize_effect(gem.get("effect")),
            }
        return self._transition(song_a, song_b)
    def _transition(self, song_a, song_b):
        """
        Deterministic transition fallback.
        This is NOT an ordering engine.
        It exists so that a malformed Gemini response can never
        result in a missing transition.
        """
        bpm_a = self._number(song_a.get("bpm"), 0.0)
        bpm_b = self._number(song_b.get("bpm"), 0.0)
        bpm_diff = abs(bpm_a - bpm_b)
        energy_a = self._number(song_a.get("energy"), 0.0)
        energy_b = self._number(song_b.get("energy"), 0.0)
        energy_diff = abs(energy_a - energy_b)
        valence_a = self._number(song_a.get("valence"), 0.0)
        valence_b = self._number(song_b.get("valence"), 0.0)
        valence_diff = abs(valence_a - valence_b)
        if bpm_diff <= 2:
            transition_type = "beatmatched_crossfade"
            bars = 2.0
            note = (
                f"2-bar beatmatched blend from "
                f"{song_a.get('name', '?')} "
                f"into "
                f"{song_b.get('name', '?')}."
            )
        elif bpm_diff <= 6:
            transition_type = "beatmatched_crossfade"
            bars = 1.0
            note = (
                f"Tight 1-bar beatmatched blend; "
                f"BPM difference is approximately "
                f"{bpm_diff:.0f}."
            )
        elif bpm_diff <= 14 and energy_diff <= 0.4:
            transition_type = "crossfade"
            bars = 0.5
            note = (
                f"Short groove-tolerant blend; "
                f"BPM difference is approximately "
                f"{bpm_diff:.0f}."
            )
        elif bpm_diff <= 24:
            transition_type = "crossfade"
            bars = 0.25
            note = f"Quick transition across a " f"{bpm_diff:.0f} BPM difference."
        else:
            transition_type = "crossfade"
            bars = 0.25
            note = f"Very short transition across a " f"{bpm_diff:.0f} BPM difference."
        crossfade_sec = self._crossfade_seconds(bars, bpm_a, bpm_b)
        effect = self._pick_fallback_effect(transition_type, bpm_diff, energy_diff)
        return {
            "type": transition_type,
            "crossfade_sec": crossfade_sec,
            "crossfade_bars": bars,
            "note": note,
            "bpm_a": bpm_a,
            "bpm_b": bpm_b,
            "energy_diff": round(energy_diff, 2),
            "valence_diff": round(valence_diff, 2),
            "effect": effect,
        }
    @staticmethod
    def _bar_seconds(bpm):
        """
        One 4/4 musical bar in seconds.
        """
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            bpm = 120.0
        if bpm <= 0:
            bpm = 120.0
        return (60.0 / bpm) * 4.0
    def _crossfade_seconds(self, bars, bpm_a, bpm_b):
        """
        Convert musical bars to real seconds.
        Uses the average BPM of the two songs.
        """
        try:
            bars = float(bars)
        except (TypeError, ValueError):
            bars = 1.0
        bars = max(0.25, bars)
        bpms = []
        for bpm in (bpm_a, bpm_b):
            try:
                bpm = float(bpm)
                if bpm > 0:
                    bpms.append(bpm)
            except (TypeError, ValueError):
                pass
        if bpms:
            avg_bpm = sum(bpms) / len(bpms)
        else:
            avg_bpm = 120.0
        seconds = bars * self._bar_seconds(avg_bpm)
        seconds = max(0.15, min(seconds, 16.0))  # was max(1.0, ...) — was erasing all quick transitions
        return round(seconds, 1)
    _EFFECT_FALLBACKS = ["sweep_up", "filter_sweep_lowpass",
                         "impact_sub_drop", "tape_stop",
                         "downlifter", "whoosh", "synth_sweep"]

    def _pick_fallback_effect(self, transition_type, bpm_diff, energy_diff):
        """
        Pick an effect only when Gemini didn't provide one. Consecutive
        transitions rotate away from the effect just used, so the same sound
        never repeats back-to-back and the set doesn't sound like one effect
        firing over and over.
        """
        if transition_type == "beatmatched_crossfade":
            if bpm_diff <= 2:
                effect = "sweep_up"
            else:
                effect = "filter_sweep_lowpass"
        elif energy_diff > 0.4:
            effect = "impact_sub_drop"
        elif bpm_diff > 24:
            effect = "tape_stop"
        else:
            return "none"
        if effect == self._last_effect_name and len(self._EFFECT_FALLBACKS) > 1:
            effect = self._EFFECT_FALLBACKS[
                (self._EFFECT_FALLBACKS.index(effect) + 1) % len(self._EFFECT_FALLBACKS)
            ]
        self._last_effect_name = effect
        return effect
    @staticmethod
    def _normalize_effect(name):
        """
        Keep only effect names the playback engine can actually use.
        """
        if not name or name not in _EFFECT_NAMES or name == "none":
            return "none"
        return name
    @staticmethod
    def _validate(song):
        """
        Validate obvious database problems without changing
        the actual musical information.
        """
        song = dict(song)
        issues = []
        artist = (song.get("artist") or "").strip()
        if not artist:
            issues.append("missing_artist")
            artist = "Unknown Artist"
        song["artist"] = artist
        name = (song.get("name") or "").strip()
        if not name:
            issues.append("missing_name")
            name = "Unknown Song"
        song["name"] = name
        link = (song.get("link") or "").strip()
        if link and not link.startswith(("http://", "https://")):
            issues.append("bad_link")
        song["link"] = link
        if not song.get("duration"):
            issues.append("missing_duration")
        song["issues"] = issues
        return song
    @staticmethod
    def _number(value, default=0.0):
        """
        Safely convert a value to float.
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    @staticmethod
    def _fmt(seconds):
        """
        Format seconds as M:SS.
        """
        try:
            seconds = max(0, int(seconds))
        except (TypeError, ValueError):
            seconds = 0
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d}"
if __name__ == "__main__":
    print("Agent module loaded successfully.")
    print("Import it with:")
    print("    from agent import Agent")
