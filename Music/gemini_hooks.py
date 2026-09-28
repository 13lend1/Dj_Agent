"""Second-choice hook detection: ask Gemini for the window a video can't tell us.

The YouTube replay heatmap in `heatmap.py` is the primary signal — it is measured
from real listener behaviour. But YouTube publishes no heatmap for plenty of
videos (anything long-tail or archival), and for those a language model's
knowledge of the song is a genuinely better guess than "play the middle".

This module is deliberately standalone: one Gemini call, no playlist, no
transitions, no dependency on the DJ agent. `DJ/agent.py` runs it only for the
songs that came back without a heatmap, and falls back to its own deterministic
window for whatever Gemini still can't answer.

Every failure path returns an empty mapping instead of raising, so a missing API
key, a quota error or a malformed reply degrades to the caller's fallback rather
than breaking playlist generation.
"""
import json
import os
import sys
import threading
import time

from dotenv import load_dotenv

load_dotenv()

HOOK_SOURCE = "gemini"
# Hook detection is a knowledge question, not a sequencing one, so it gets its
# own model knob. It defaults to whatever the DJ agent uses.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_HOOK_MODEL = os.environ.get("GEMINI_HOOK_MODEL", GEMINI_MODEL)
GEMINI_MIN_INTERVAL = 10.0
GEMINI_REQUEST_TIMEOUT = 45.0
REQUEST_ATTEMPTS = 3

try:
    from Music.heatmap import duration_seconds
except ImportError:  # running as a plain script from inside Music/
    from heatmap import duration_seconds

_LOCK = threading.Lock()
_LAST_CALL = 0.0
_CLIENT = None


class GeminiEmptyResponse(RuntimeError):
    """Gemini returned None / empty text — a definitive response failure."""


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


def _client():
    """A lazily created Gemini client, shared per process."""
    global _CLIENT
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set.")
    with _LOCK:
        if _CLIENT is None:
            from google import genai
            _CLIENT = genai.Client(api_key=api_key)
        return _CLIENT


def _throttle():
    global _LAST_CALL
    with _LOCK:
        wait = GEMINI_MIN_INTERVAL - (time.time() - _LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL = time.time()


def _request(fn, timeout=GEMINI_REQUEST_TIMEOUT):
    """Run a Gemini call on a throwaway thread so a hang cannot stall the DJ."""
    out = {}

    def _run():
        try:
            out["res"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            out["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise RuntimeError(f"Gemini hook request timed out after {timeout:.0f}s")
    if "exc" in out:
        raise out["exc"]
    return out.get("res")


def _fmt(seconds):
    seconds = max(0, int(round(float(seconds or 0))))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _describe(song):
    name = song.get("name") or "Unknown Song"
    artist = song.get("artist") or "Unknown Artist"
    parts = [f"{name} by {artist}"]
    for key, label in (("genre", "genre"), ("bpm", "BPM"), ("year", "year")):
        value = song.get(key)
        if value in (None, ""):
            continue
        try:
            parts.append(f"{label} {float(value):.0f}" if key == "bpm"
                         else f"{label} {value}")
        except (TypeError, ValueError):
            parts.append(f"{label} {value}")
    return " | ".join(parts)


def build_prompt(songs):
    """The hook-only prompt. No ordering, no transitions, no commentary."""
    lines = [
        "You are a musicologist choosing the single most replayable passage of "
        "each song below.",
        "",
        "Return hook_start_sec/hook_end_sec for EVERY song listed.",
        "",
        "These songs ALREADY belong together in one DJ set. Do not order them, "
        "do not add transitions, and do not explain. ONLY give each song its "
        "strongest playable window.",
        "",
        "Rules:",
        "- Both values must be numbers INSIDE the song's duration, and "
        "hook_end_sec must be strictly greater than hook_start_sec.",
        "- Never return null, negative, zero-length or out-of-range windows.",
        "- The window must be a real musical passage: the hook, chorus, drop, "
        "build, climax, solo or main theme.",
        "- Do NOT default to any typical length. Let the window length come from "
        "the music: a pop chorus is often 20-40s, but a classical movement or a "
        "long instrumental build may need 60-120s or more.",
        "- Avoid the intro and the outro unless they genuinely are the iconic "
        "part of the recording.",
        "- If you are unsure of the structure, still give your best in-range "
        "guess based on the song's known form. Never fall back to a window "
        "computed from the track length.",
        "",
    ]
    for index, song in enumerate(songs, start=1):
        duration = duration_seconds(song.get("duration"))
        runtime = f"{_fmt(duration)} ({int(duration)}s)" if duration > 0 else "unknown"
        lines.append(
            f"{index}. [id: {song.get('id')}] {_describe(song)} | duration {runtime}"
        )
    lines.extend([
        "",
        "Return the exact bracketed [id: ...] shown above for every song, each "
        "with a valid hook_start_sec/hook_end_sec inside that song's duration. "
        "Every listed song must appear exactly once.",
    ])
    return "\n".join(lines)


def _usable(entry, song):
    """A window is usable only if it is numeric, positive and non-empty.

    Range clamping against the real duration is deliberately left to the caller,
    which owns the fallback and the single set of validation rules.
    """
    song_id = str(entry.get("id") or "").strip()
    if not song_id:
        return None
    try:
        start = float(entry.get("hook_start_sec"))
        end = float(entry.get("hook_end_sec"))
    except (TypeError, ValueError):
        return None
    if start != start or end != end:  # NaN
        return None
    if start < 0 or end <= start:
        return None
    duration = duration_seconds(song.get("duration"))
    if duration > 0 and start >= duration:
        return None
    return song_id, start, end


def request_hooks(songs):
    """Ask Gemini for one hook window per song.

    Returns {id: {"hook_start_sec", "hook_end_sec", "hook_source"}}; songs Gemini
    did not answer with a usable window are simply absent, and the caller falls
    back for them. Returns {} if the request cannot be made at all.
    """
    songs = [song for song in (songs or []) if song and song.get("id")]
    if not songs:
        return {}
    try:
        from google.genai import types
    except ImportError as exc:
        print(f"[hooks] Gemini hook lookup unavailable ({exc}); using fallback.")
        return {}

    by_id = {str(song.get("id")).strip(): song for song in songs}
    prompt = build_prompt(songs)
    last_exc = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            _throttle()
            chat = _client().chats.create(
                model=GEMINI_HOOK_MODEL,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_HOOK_SCHEMA,
                    temperature=0.2,
                ),
            )
            response = _request(lambda: chat.send_message(prompt))
            if response is None or not response.text:
                raise GeminiEmptyResponse("Gemini returned no hook data.")
            data = json.loads(response.text)
            entries = data.get("hooks") or []
            hooks = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                result = _usable(entry, by_id.get(
                    str(entry.get("id") or "").strip(), {}
                ))
                if not result:
                    continue
                song_id, start, end = result
                hooks[song_id] = {
                    "hook_start_sec": round(start, 1),
                    "hook_end_sec": round(end, 1),
                    "hook_source": HOOK_SOURCE,
                }
            return hooks
        except Exception as exc:  # noqa: BLE001 - best effort by design
            last_exc = exc
            lower = str(exc).lower()
            if "quota" in lower or "resource_exhausted" in lower:
                print(f"[hooks] Gemini quota exhausted ({exc}); using fallback.")
                return {}
            if attempt < REQUEST_ATTEMPTS - 1:
                delay = 15.0
                print(f"[hooks] Gemini hook request failed ({exc}); "
                      f"retrying in {delay:.0f}s "
                      f"(attempt {attempt + 2}/{REQUEST_ATTEMPTS})...")
                time.sleep(delay)
    print(f"[hooks] Gemini hook request gave up ({last_exc}); using fallback.")
    return {}


def main():
    """Ad-hoc check: `python Music/gemini_hooks.py "Artist - Title" ...`"""
    if len(sys.argv) < 2:
        print(__doc__)
        print("usage: python Music/gemini_hooks.py \"Artist - Title\" [...]")
        return
    songs = []
    for arg in sys.argv[1:]:
        if " - " in arg:
            artist, name = arg.split(" - ", 1)
        else:
            artist, name = "Unknown Artist", arg
        songs.append({"id": arg, "name": name, "artist": artist, "duration": 0})
    hooks = request_hooks(songs)
    if not hooks:
        print("no hooks returned")
        return
    for song_id, hook in hooks.items():
        print(f"{song_id}: {hook['hook_start_sec']}..{hook['hook_end_sec']} "
              f"({hook['hook_source']})")


if __name__ == "__main__":
    main()
