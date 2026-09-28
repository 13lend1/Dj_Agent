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
                "required": ["id"],
            },
        }
    },
    "required": ["queue"],
}
_EFFECT_NAMES = set(
    _QUEUE_SCHEMA["properties"]["queue"]["items"]["properties"]["effect"]["enum"]
)


def _norm_sid(value):
    """Normalize a song id for cross-referencing Gemini entries (the ids are
    UUIDs/YouTube ids, so whitespace-only or None never matches a song)."""
    return str(value or "").strip()
class Agent:
    """
    AI DJ agent.
    Responsibilities
    ----------------
    1. Python finds every song's hook from its YouTube replay heatmap
       (Music/heatmap.py) — real audience data, not a guess.
    2. Songs with no heatmap get a second opinion from a separate, hook-only
       Gemini request (Music/gemini_hooks.py).
    3. Anything still unanswered falls back to the song's middle section.
    4. Gemini orders candidate songs.
    5. Gemini chooses the transition between every consecutive song.
    6. Python validates all timestamps.
    7. Python guarantees a transition exists between every pair.
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
        link
    Recommended:
        genre
        bpm
        energy
        danceability
        valence
        acousticness
        instrumentalness
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
            queue = self._query_gemini(songs=songs, previous=previous, hooks={})
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
        Python decides:
            - every song's hook, from its YouTube replay heatmap, then from a
              separate hook-only Gemini request, then from the middle section
        Gemini decides:
            - song order
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
            Target hook length in seconds. A heatmap window decides its own
            length within a band around this number; only the middle-section
            fallback is exactly this long (default 33).
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
            hook_source ("yt_heatmap", "gemini" or "middle_section")
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
                "hook_source": "yt_heatmap",
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

        hooks = self._resolve_heatmap_hooks(songs, clip_length)
        hooks.update(self._resolve_gemini_hooks(songs, hooks, clip_length))

        by_id = {_norm_sid(song.get("id")): song for song in songs}
        try:
            queue = self._query_gemini(
                songs=songs, previous=previous, hooks=hooks
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
        if missing_songs and queue:
            print(f"[Agent] Gemini omitted {len(missing_songs)} song(s); "
                  "appending them.")

        playlist = []
        counts = {"yt_heatmap": 0, "gemini": 0, "middle_section": 0}
        play_order = ([(by_id[_norm_sid(entry.get("id"))], entry)
                       for entry in ordered_entries]
                      + [(song, None) for song in missing_songs])
        for original_song, entry in play_order:
            song = self._validate(dict(original_song))
            song_id = _norm_sid(song.get("id"))
            start, end, source = self._resolve_hook(
                song=song, hook=hooks.get(song_id), clip_length=clip_length
            )
            self._apply_window(song, start, end, source)
            counts[source] = counts.get(source, 0) + 1
            hook = hooks.get(song_id)
            if source == "yt_heatmap" and hook:
                song["replay_peak_sec"] = hook.get("replay_peak_sec")
                song["replay_score"] = hook.get("replay_score")
            if source == "middle_section":
                self._print_fallback(song, clip_length)
            song["_gemini_transition"] = ({
                "type": entry.get("transition_type"),
                "crossfade_bars": entry.get("crossfade_bars"),
                "note": entry.get("transition_note"),
                "effect": entry.get("effect") or "none",
            } if entry else None)
            playlist.append(song)
        total = len(playlist)
        if counts["middle_section"]:
            print(
                f"[hooks] {counts['yt_heatmap']}/{total} hooks from the YouTube "
                f"replay heatmap, {counts['gemini']}/{total} from Gemini, "
                f"{counts['middle_section']}/{total} fell back to the "
                f"{clip_length:.1f}s middle section."
            )
        self._finalize_transitions(playlist, previous=previous)
        self._save_response(playlist, place=place)
        return playlist

    def _resolve_heatmap_hooks(self, songs, clip_length):
        """Every song's hook, taken from its YouTube replay heatmap.

        This is the real audience signal — the seconds people scrub back to —
        so it replaces Gemini guessing a chorus out of a song title. Songs whose
        video publishes no heatmap (YouTube has none for a lot of long-tail
        videos) are simply missing from the result and go on to the Gemini
        second opinion. A heatmap problem must never break playlist generation,
        so any failure here yields an empty mapping.
        """
        try:
            from Music.heatmap import resolve_hooks
        except ImportError:
            from heatmap import resolve_hooks
        try:
            return resolve_hooks(songs, clip_length=clip_length)
        except Exception as exc:
            print(f"[Agent] Heatmap lookup failed ({exc}); "
                  "asking Gemini for those hooks instead.")
            return {}

    def _resolve_gemini_hooks(self, songs, heatmap_hooks, clip_length):
        """Second opinion for the songs no YouTube heatmap could cover.

        This is a separate, hook-only Gemini request (Music/gemini_hooks.py) with
        no playlist and no transitions in it, so it stays out of the agent's
        ordering call and only runs when measured data is missing. Whatever it
        still cannot answer falls back to the deterministic middle section.
        """
        missing = [song for song in songs
                   if _norm_sid(song.get("id")) not in (heatmap_hooks or {})]
        if not missing:
            return {}
        try:
            from Music.gemini_hooks import request_hooks
        except ImportError:
            try:
                from gemini_hooks import request_hooks
            except ImportError:
                print("[hooks] Gemini hook module unavailable; using fallbacks.")
                return {}
        try:
            found = request_hooks(missing)
        except Exception as exc:
            print(f"[hooks] Gemini hook lookup failed ({exc}); using fallbacks.")
            return {}
        if found:
            print(f"[hooks] No heatmap for {len(missing)} song(s); Gemini "
                  f"supplied {len(found)} hook(s) instead.")
        return found or {}

    def _apply_window(self, song, start, end, source):
        """Copy a resolved hook window onto a song, with its provenance, so it
        reaches the player, the database and the saved Agent run."""
        song["play_start_sec"] = start
        song["play_end_sec"] = end
        song["play_start"] = self._fmt(start)
        song["play_end"] = self._fmt(end)
        song["clip_length"] = round(max(0.0, end - start), 1)
        song["hook_length"] = round(max(0.0, end - start), 1)
        song["hook_source"] = source
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
    def _query_gemini(self, songs, previous, hooks=None):
        """
        Make exactly one Gemini request.
        Gemini returns:
            queue[
                {
                    id,
                    transition_type,
                    crossfade_bars,
                    transition_note,
                    effect
                }
            ]
        `hooks` is the heatmap window already resolved for each song. It is
        handed to Gemini as context for the ordering, not as something to
        return: the hook is measured, not chosen.
        """
        _gemini_throttle()
        prompt = self._build_prompt(
            songs=songs, previous=previous, hooks=hooks or {}
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
    def _build_prompt(self, songs, previous, hooks=None):
        hooks = hooks or {}
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
            "(1) order the songs, (2) pick the transition into the next song. "
            "You do NOT pick hooks — those are already decided, see below."
        )
        lines.append("")
        lines.append(self._hook_brief(songs, hooks))
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
        lines.append("TRANSITIONS:")
        lines.append(
            "Every consecutive pair needs a transition, including CURRENTLY "
            "PLAYING -> first candidate if applicable."
        )
        lines.append(
            "IMPORTANT: the hook window of each song is ALREADY FIXED and is "
            "what will actually play. Plan the transition around the material "
            "inside that window — blend the outgoing song into the first bars "
            "of the incoming hook, and name those bars in transition_note."
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
            "The last song needs no outgoing transition."
        )
        return "\n".join(lines)

    @staticmethod
    def _hook_brief(songs, hooks):
        """The hook windows Python already decided, shown to Gemini.

        Gemini is told what will actually play so it can order and blend around
        the real material, but it cannot move these windows.
        """
        lines = ["ALREADY-DECIDED HOOK WINDOWS (measured, do not change these):"]
        measured = 0
        for index, song in enumerate(songs, start=1):
            hook = hooks.get(_norm_sid(song.get("id")))
            if not hook:
                continue
            measured += 1
            start = float(hook["hook_start_sec"])
            end = float(hook["hook_end_sec"])
            if hook.get("hook_source") == "gemini":
                origin = "estimated from the song's structure"
            else:
                origin = (f"YouTube replay peak, seek score "
                          f"{hook.get('replay_score', 0.0):.2f}")
            lines.append(
                f"  #{index} [id: {song.get('id')}] plays "
                f"{Agent._fmt(start)}-{Agent._fmt(end)} ({end - start:.0f}s) "
                f"— {origin}"
            )
        if not measured:
            return ""
        lines.append(
            "These windows are already fixed: most come from each video's "
            "YouTube replay heatmap (the seconds real listeners scrub back to), "
            "and the rest were estimated from the song's structure. Prefer "
            "ordering so that what plays next lands on a strong hook, and name "
            "the incoming hook's material in transition_note."
        )
        lines.append("")
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
    def _resolve_hook(self, song, hook, clip_length):
        """Returns (start, end, source) for one song.

        Precedence, best evidence first:
            1. "yt_heatmap"       measured from the video's replay heatmap
            2. "gemini"           the separate hook-only Gemini request
            3. "middle_section"   the deterministic fallback

        The bounds check below is a last-resort guard, not a tuning pass: a
        window that already came from real data or from Gemini is used exactly
        as given. Only a malformed or out-of-range window is replaced.
        """
        source = "middle_section"
        if isinstance(hook, dict):
            declared = hook.get("hook_source")
            if declared in ("yt_heatmap", "gemini"):
                source = declared
        try:
            start = float(hook.get("hook_start_sec"))
            end = float(hook.get("hook_end_sec"))
        except (AttributeError, TypeError, ValueError):
            return (*self._clip_window(song, clip_length), "middle_section")

        duration_sec = self._duration_seconds(song)
        if start < 0 or end <= start:
            return (*self._clip_window(song, clip_length), "middle_section")
        if duration_sec > 0:
            if start >= duration_sec:
                return (*self._clip_window(song, clip_length), "middle_section")
            end = min(end, duration_sec)
            if end <= start:
                return (*self._clip_window(song, clip_length), "middle_section")

        return (round(start, 1), round(end, 1), source)

    def _print_fallback(self, song, clip_length):
        name = (song.get("name") or "Unknown Song").strip()
        print(
            f"[hooks] fallback {float(clip_length or 33.0):.1f}s for "
            f"'{name}' - no replay heatmap and no Gemini hook, "
            "using the middle section."
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
        Safety fallback for songs whose video has no usable replay heatmap.
        Priority:
        1. Existing hook_start/hook_end on the song.
        2. Middle section of the song.
        3. Beginning if duration is unknown.

        A song shorter than clip_length is returned whole rather than truncated.
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
                    # A stored hook that lies entirely past the end clamps to a
                    # zero-length window; drop it and use the middle section.
                    if end > start:
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
