import os
import json
import threading
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types
load_dotenv()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
GEMINI_MIN_INTERVAL = 20.0
GEMINI_REQUEST_TIMEOUT = 45.0
_GEMINI_LOCK = threading.Lock()
_GEMINI_LAST_CALL = 0.0


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
                    "transition_type": {
                        "type": "string",
                        "enum": ["beatmatched_crossfade", "crossfade"],
                    },
                    "crossfade_bars": {"type": "number"},
                    "transition_note": {"type": "string"},
                    "effect": {
                        "type": "string",
                        "enum": [
                            "none",
                            "riser_white_noise",
                            "riser_synth",
                            "downlifter",
                            "impact_boom",
                            "impact_sub_drop",
                            "sweep_up",
                            "sweep_down",
                            "whoosh",
                            "filter_sweep_lowpass",
                            "filter_sweep_highpass",
                            "vinyl_stop",
                            "tape_stop",
                            "reverse_cymbal",
                            "cymbal_crash",
                            "snare_roll",
                            "drum_fill",
                            "echo_throw",
                            "air_horn",
                            "laser_zap",
                            "siren",
                            "glitch_stutter",
                            "scratch",
                            "white_noise_sweep",
                            "kick_roll",
                            "crowd_cheer",
                            "vocal_tag",
                        ],
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
            queue = self._query_gemini(songs=songs, previous=previous, clip_length=None)
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
    def build_playlist(self, songs, current=None, clip_length=33):
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
            Desired clip length in seconds.
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
        except TypeError, ValueError:
            clip_length = 33.0
        clip_length = max(1.0, clip_length)

        def _norm_id(value):
            return str(value or "").strip()

        by_id = {_norm_id(song.get("id")): song for song in songs}
        try:
            queue = self._query_gemini(
                songs=songs, previous=previous, clip_length=clip_length
            )
        except Exception as exc:
            print(
                "[Agent] Gemini call failed, "
                "queue will use fallback ordering/hooks: "
                f"{exc}"
            )
            queue = []
        playlist = []
        seen_ids = set()
        for entry in queue:
            song_id = _norm_id(entry.get("id"))
            song = by_id.get(song_id)
            if song is None:
                continue
            if song_id in seen_ids:
                continue
            seen_ids.add(song_id)
            song = self._validate(dict(song))
            start, end = self._resolve_hook(
                song=song, entry=entry, clip_length=clip_length
            )
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
        missing = [song for song in songs if _norm_id(song.get("id")) not in seen_ids]
        if missing:
            if queue:
                print(
                    f"[Agent] Gemini omitted "
                    f"{len(missing)} song(s); "
                    "appending them."
                )
            for original_song in missing:
                song = self._validate(dict(original_song))
                start, end = self._clip_window(song, clip_length)
                song["play_start_sec"] = start
                song["play_end_sec"] = end
                song["play_start"] = self._fmt(start)
                song["play_end"] = self._fmt(end)
                song["clip_length"] = round(max(0.0, end - start), 1)
                song["_gemini_transition"] = None
                playlist.append(song)
        self._finalize_transitions(playlist, previous=previous)
        return playlist
    def _query_gemini(self, songs, previous, clip_length):
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
            songs=songs, previous=previous, clip_length=clip_length
        )
        last_exc = None
        for attempt in range(3):
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
                    print(
                        "[Agent] Gemini free-tier quota is exhausted "
                        f"for today ({exc}); falling back to "
                        "deterministic ordering."
                    )
                    raise exc
                if attempt < 2:
                    lower = str(exc).lower()
                    if "429" in lower or "too many" in lower or "rate limit" in lower:
                        delay = 20.0 * (attempt + 1)
                    else:
                        delay = 5.0 * (attempt + 1)
                    print(
                        f"[Agent] Gemini call failed ({exc}); "
                        f"retrying in {delay:.0f}s..."
                    )
                    time.sleep(delay)
        raise last_exc
    def _build_prompt(self, songs, previous, clip_length):
        """
        Build the DJ prompt.
        Gemini is explicitly told that it is responsible for:
            ORDER
            HOOK
            TRANSITIONS
        The song name and artist are included because Gemini can use
        its knowledge of the actual songs to identify likely choruses,
        drops, hooks, etc.
        """
        lines = []
        lines.append("You are an expert professional DJ and music selector.")
        lines.append("You are building one continuous DJ set.")
        lines.append("")
        if previous:
            lines.append("CURRENTLY PLAYING:")
            lines.append(f"  {self._desc(previous)}")
            lines.append("The first candidate must flow naturally " "out of this song.")
        else:
            lines.append("There is no currently playing song.")
        lines.append("")
        lines.append("CANDIDATE SONGS:")
        for index, song in enumerate(songs, start=1):
            lines.append(f"{index}. [id: {song.get('id')}] {self._desc(song)}")
        lines.append("")
        lines.append("Return the EXACT bracketed [id: ...] of each song "
                     "you include in the queue.")
        lines.append("Your job is to create the best possible DJ sequence.")
        lines.append("You must make THREE decisions:")
        lines.append("1. ORDER the songs.")
        lines.append("2. SELECT the best hook/playable section " "of every song.")
        lines.append("3. SELECT the transition from every song " "into the next song.")
        lines.append("")
        lines.append("SONG ORDER:")
        lines.append("- Do not simply preserve the input order.")
        lines.append("- Choose the order that creates the best " "continuous DJ flow.")
        lines.append("- Consider BPM compatibility.")
        lines.append("- Consider energy progression.")
        lines.append("- Consider danceability.")
        lines.append("- Consider valence and overall mood.")
        lines.append("- Consider genre/style compatibility.")
        lines.append("- Avoid unnecessary BPM cliffs.")
        lines.append(
            "- An intentional energy jump is allowed " "when it sounds musically good."
        )
        lines.append("")
        lines.append("BPM / MIXING:")
        lines.append("- Prefer consecutive songs with compatible BPM.")
        lines.append(
            "- A difference of around 0-8 BPM is generally " "easy to beatmatch."
        )
        lines.append(
            "- 8-15 BPM can work with a shorter crossfade " "or creative transition."
        )
        lines.append(
            "- Larger BPM differences should normally use " "a short transition."
        )
        lines.append("")
        lines.append("HOOK SELECTION:")
        lines.append(
            f"Each song should have approximately "
            f"{clip_length:.0f} seconds of playable material."
        )
        lines.append("Return hook_start_sec and hook_end_sec.")
        lines.append(
            "The hook should be the most exciting and " "recognizable part of the song."
        )
        lines.append(
            "Prefer a chorus, drop, main hook, memorable "
            "vocal section, or strongest musical section."
        )
        lines.append(
            "Do NOT choose the intro unless the intro itself "
            "is clearly the iconic/strongest section."
        )
        lines.append(
            "Do NOT choose the outro unless it is musically "
            "important and useful for mixing."
        )
        lines.append(
            "Use your knowledge of the actual song from "
            "its title and artist when deciding where the "
            "chorus/drop/hook occurs."
        )
        lines.append("The hook must remain inside the song duration.")
        lines.append(
            "Return the ACTUAL strongest musical window - "
            "typically 12 to 30 seconds of one full phrase. "
            "Do NOT force a fixed length; the clip length is "
            "simply hook_end_sec minus hook_start_sec and "
            "should match whichever section carries the hook."
        )
        lines.append("")
        lines.append("TRANSITIONS:")
        lines.append("You MUST choose a transition for EVERY " "consecutive pair.")
        if previous and songs:
            lines.append(
                "This includes the transition from the "
                "CURRENTLY PLAYING song into the first "
                "candidate."
            )
        lines.append("For example:")
        lines.append("Song A -> Song B")
        lines.append("Song B -> Song C")
        lines.append("Song C -> Song D")
        lines.append("Every one of these must have a transition.")
        lines.append("")
        lines.append("Use transition_type:")
        lines.append(
            "- beatmatched_crossfade when BPMs are "
            "compatible and both songs can be blended."
        )
        lines.append("- crossfade when beatmatching is not appropriate.")
        lines.append("")
        lines.append("crossfade_bars is the number of musical bars.")
        lines.append("Use approximately:")
        lines.append("- 2 bars for a smooth beatmatched transition.")
        lines.append("- 1 bar for a tighter transition.")
        lines.append("- 0.5 bars for a short blend.")
        lines.append("- 0.25 bars for a very quick transition.")
        lines.append("")
        lines.append("transition_note:")
        lines.append("Describe WHAT musical element should carry " "the transition.")
        lines.append("Examples include:")
        lines.append("- drums")
        lines.append("- bassline")
        lines.append("- hi-hats")
        lines.append("- vocal phrase")
        lines.append("- chorus")
        lines.append("- drop")
        lines.append("- percussion")
        lines.append("")
        lines.append("EFFECTS:")
        lines.append("Use effects sparingly.")
        lines.append("Most normal transitions should use 'none'.")
        lines.append("Use risers for builds.")
        lines.append("Use impacts for drops or large energy changes.")
        lines.append("Use sweeps for smooth transitions.")
        lines.append(
            "Use vinyl_stop/tape_stop/scratch only for " "intentional dramatic changes."
        )
        lines.append("")
        lines.append("OUTPUT RULES:")
        lines.append("Return every candidate exactly once.")
        lines.append("Do not invent IDs.")
        lines.append("The queue order IS the DJ order.")
        lines.append(
            "hook_start_sec and hook_end_sec must be valid "
            "timestamps inside each song."
        )
        lines.append(
            "Return a transition for every song that has " "another song after it."
        )
        lines.append("The final song does not need an outgoing transition.")
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
        except TypeError, ValueError:
            duration_sec = 0.0
        def num(value, decimals=2):
            try:
                return f"{float(value):.{decimals}f}"
            except TypeError, ValueError:
                return "?"
        bpm = song.get("bpm")
        try:
            bpm_text = f"{float(bpm):.1f}"
        except TypeError, ValueError:
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
        """
        Validate Gemini's hook WITHOUT rewriting it to a fixed length.
        Gemini's hook_start_sec/hook_end_sec become the clip as-is, so the
        clip length is simply hook_end - hook_start and varies per song.
        Only sanity guards apply: clip must stay inside the song and length
        is clamped to a reasonable [MIN, MAX] band (MAX defaults to the
        requested clip_length so playback never gets a 3-minute hook).
        If Gemini returns no valid hook, fall back to _clip_window.
        """
        duration_sec = self._duration_seconds(song)
        try:
            start = float(entry.get("hook_start_sec"))
            end = float(entry.get("hook_end_sec"))
        except TypeError, ValueError:
            return self._clip_window(song, clip_length)
        if start < 0 or end <= start:
            return self._clip_window(song, clip_length)
        if duration_sec > 0:
            start = min(start, duration_sec)
            end = min(end, duration_sec)
            if end <= start:
                return self._clip_window(song, clip_length)
        min_len = 8.0
        try:
            max_len = max(min_len, float(clip_length))
        except TypeError, ValueError:
            max_len = 30.0
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
        return (round(start, 1), round(end, 1))
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
            except TypeError, ValueError:
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
        except TypeError, ValueError:
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
        except TypeError, ValueError:
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
            except TypeError, ValueError:
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
        except TypeError, ValueError:
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
        except TypeError, ValueError:
            bars = 1.0
        bars = max(0.25, bars)
        bpms = []
        for bpm in (bpm_a, bpm_b):
            try:
                bpm = float(bpm)
                if bpm > 0:
                    bpms.append(bpm)
            except TypeError, ValueError:
                pass
        if bpms:
            avg_bpm = sum(bpms) / len(bpms)
        else:
            avg_bpm = 120.0
        seconds = bars * self._bar_seconds(avg_bpm)
        seconds = max(1.0, min(seconds, 16.0))
        return round(seconds, 1)
    @staticmethod
    def _pick_fallback_effect(transition_type, bpm_diff, energy_diff):
        """
        Pick an effect only when Gemini didn't provide one.
        """
        if transition_type == "beatmatched_crossfade":
            if bpm_diff <= 2:
                return "sweep_up"
            return "filter_sweep_lowpass"
        if energy_diff > 0.4:
            return "impact_sub_drop"
        if bpm_diff > 24:
            return "tape_stop"
        return "none"
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
        except TypeError, ValueError:
            return default
    @staticmethod
    def _fmt(seconds):
        """
        Format seconds as M:SS.
        """
        try:
            seconds = max(0, int(seconds))
        except TypeError, ValueError:
            seconds = 0
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d}"
if __name__ == "__main__":
    print("Agent module loaded successfully.")
    print("Import it with:")
    print("    from agent import Agent")
