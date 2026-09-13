import os
import json
import threading
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types
load_dotenv()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6flash")
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
                    "clip_length_sec": {"type": "number"},
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
                "required": ["id", "hook_start_sec", "hook_end_sec", "clip_length_sec"],
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
            Fallback clip length in seconds, used only when Gemini does
            not return a clip_length_sec for a song or returns no valid
            hook (default 33).
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


        def _norm_id(value):
            return str(value or "").strip()

        by_id = {_norm_id(song.get("id")): song for song in songs}
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
        self._save_response(playlist)
        return playlist
    def _save_response(self, playlist):
        """Persist every completed agent response to the Agent table.

        Stores the ordered songs, the transitions between them, the hook
        windows and the raw payload, exactly as returned. Saving must never
        break playlist generation, so a DB error is logged and swallowed.
        """
        try:
            from DJ.responses import save_response
        except ImportError:
            from responses import save_response
        try:
            run_id = save_response(playlist)
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
            "Return hook_start_sec/hook_end_sec: the actual most-played/"
            "most-replayed section (chorus, drop, or strongest part), using "
            "your knowledge of the song. No fixed target length — use the "
            "section's real span. Avoid intro/outro unless it's genuinely the iconic part."
        )
        lines.append(
            "Choose boundaries WITH the rest of the sequence in mind, not "
            "each song in isolation: hook_end_sec of a song should land on "
            "material whose energy/intensity is close to the hook_start_sec "
            "of the next song, so the splice point itself is a smooth match, "
            "not just each song's best moment picked independently. If the "
            "single 'best' window would create a jarring jump into the next "
            "song's opening, shift the boundary slightly (earlier/later, "
            "still inside the real hook/decay region) to smooth that specific "
            "handoff."
        )
        lines.append(
            "End a few seconds into the decay/sustain after the peak, not "
            "exactly on it, so the transition has material to fade over — "
            "unless the transition is a hard-cut effect (vinyl_stop/"
            "tape_stop/scratch)."
        )
        lines.append("")
        lines.append("CLIP LENGTH (clip_length_sec):")
        lines.append(
            "Return clip_length_sec per song: how many seconds that song "
            "actually plays on the floor. This is the authoritative play "
            "time — vary it so it is NOT the same for every song. Follow "
            "the set arc: short/quick cuts ~15-25s for warm-up or cooldown "
            "rests, steady 25-45s for mid-set momentum, and 45-90s for "
            "peak/top-momentum songs so the crowd stays on the peak. "
            "It should roughly match hook_end_sec - hook_start_sec, and "
            "must stay inside the song's duration (you know each song's "
            "duration from the candidate list)."
        )
        lines.append("")
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
            "Default 'none'. Riser for builds, impact for drops/big energy "
            "changes, sweep for smooth transitions, vinyl_stop/tape_stop/"
            "scratch only for intentional dramatic breaks."
        )
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
        duration_sec = self._duration_seconds(song)
        try:
            start = float(entry.get("hook_start_sec"))
            end = float(entry.get("hook_end_sec"))
        except (TypeError, ValueError):
            return self._clip_window(song, clip_length)
        if start < 0 or end <= start:
            return self._clip_window(song, clip_length)
        if duration_sec > 0:
            start = min(start, duration_sec)
            end = min(end, duration_sec)
            if end <= start:
                return self._clip_window(song, clip_length)

        min_len = 8.0
        max_len = 90.0  # was `clip_length` (default 33) — too low for peak-arc hooks
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
    def _entry_clip_length(self, entry, fallback):
        """
        Extract Gemini's authoritative clip_length_sec for a song.
        Returns None when Gemini did not provide a usable value so the
        caller can fall back to the requested clip_length (default 33s).
        The value is clamped to a sane DJ band so a bad model value can
        never produce a zero- or multi-minute clip.
        """
        try:
            length = float(entry.get("clip_length_sec"))
        except (TypeError, ValueError):
            return None
        if length < 1.0:
            return None
        return max(8.0, min(length, 90.0))
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
