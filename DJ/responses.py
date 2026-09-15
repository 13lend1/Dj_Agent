import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
_MUSIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Music")
if _MUSIC_DIR not in sys.path:
    sys.path.insert(0, _MUSIC_DIR)

import json
import sqlite3
import time
from Music.preference import PLACE_GENRES


def _place_for_genre(genre):
    """Return the first PLACE_GENRES key whose genre list contains `genre`,
    or None if the genre is unknown (old track, unrecognised tag)."""
    if not genre:
        return None
    for place, genres in PLACE_GENRES.items():
        if genre in genres:
            return place
    return None


def create_agent_table():
    """Create (once) the Agent table that stores everything build_playlist()
    returns: the ordered song list, the ordered transitions between songs,
    every hook window, and the raw payload. Existing runs are never lost.

    The ``place`` column holds the run's single PLACE_GENRES key (e.g.
    "home"), so the place of the whole set is queryable straight from the
    table. Runs that mix places (no explicit place) leave it NULL."""
    from Music.songs import DB_LOCK, _get_conn
    with DB_LOCK:
        conn = _get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Agent (
                id TEXT PRIMARY KEY,
                name TEXT,
                created_at TEXT,
                songs_json TEXT,
                transitions_json TEXT,
                hooks_json TEXT,
                raw_json TEXT,
                played TEXT,
                place TEXT
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(Agent)").fetchall()}
        if "played" not in cols:
            conn.execute("ALTER TABLE Agent ADD COLUMN played TEXT")
        if "place" not in cols:
            conn.execute("ALTER TABLE Agent ADD COLUMN place TEXT")


def save_response(response, place=None):
    """Persist one full agent response (the playlist from
    Agent.build_playlist) into the Agent table.

    `response` is the list of enriched song dicts the agent returned, in play
    order. Each song already carries play_start_sec/play_end_sec, the M:SS hook
    strings, clip_length, transition_out, and its own place flag. Everything
    is kept three ways:
        songs_json        the ordered song list, exactly as returned (hook
                          windows and transition_out included per song)
        transitions_json  every transition_out, in play order, labelled with
                          which pair it joins
        hooks_json        each song's hook window + clip length, in play order
        raw_json          the unmodified original response

    The Agent table's ``place`` column stores ONLY the run's single place key
    (no song ids, no genres): the explicit `place` when given, otherwise a key
    derived from the songs only when every song shares the same one.

    Returns the run id of the saved row (None when there was nothing to save).
    """
    from Music.songs import DB_LOCK, _get_conn

    playlist = list(response or [])
    if not playlist:
        return None

    create_agent_table()

    run_id = f"run_{int(time.time() * 1000)}"
    first = playlist[0]
    name = first.get("name") or first.get("id") or "untitled set"

    songs = []
    transitions = []
    hooks = []
    for song in playlist:
        item = dict(song)
        # Tag each track with its PLACE_GENRES key so the flag persists in
        # songs_json / hooks_json / raw_json (the per-track flag).
        if not item.get("place"):
            item["place"] = _place_for_genre(item.get("genre"))
        songs.append(item)

        hooks.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "artist": item.get("artist"),
            "place": item.get("place"),
            "play_start_sec": item.get("play_start_sec"),
            "play_end_sec": item.get("play_end_sec"),
            "play_start": item.get("play_start"),
            "play_end": item.get("play_end"),
            "clip_length": item.get("clip_length"),
        })

    # Each song's transition_out joins it to the NEXT song, so pair every non-
    # final song with its successor to keep the transitions in play order.
    for i in range(len(songs) - 1):
        src = songs[i]
        dst = songs[i + 1]
        transition_out = src.get("transition_out")
        if transition_out is not None:
            transitions.append({
                "from_id": src.get("id"),
                "from_name": src.get("name"),
                "to_id": dst.get("id"),
                "to_name": dst.get("name"),
                "transition": dict(transition_out),
            })

    # Run-level place: only ever a single key (no ids, no genres).
    if place is None:
        places = {item.get("place") for item in songs if item.get("place")}
        place = places.pop() if len(places) == 1 else None

    with DB_LOCK:
        conn = _get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO Agent
                (id, name, created_at, songs_json, transitions_json, hooks_json, raw_json, played, place)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                name,
                time.strftime("%Y-%m-%d %H:%M:%S"),
                json.dumps(songs, default=str),
                json.dumps(transitions, default=str),
                json.dumps(hooks, default=str),
                json.dumps(playlist, default=str),
                "unplayed",
                place,
            ),
        )
        return run_id


def last_run_songs():
    """Return (run_id, songs) for the most recent Agent run, or (None, []) when
    nothing has been saved yet. Songs come back in play order, each carrying a
    ``played`` flag derived from the row's ``played`` column (a JSON list of
    song IDs that have finished playing start-to-end) and a ``place`` flag:
    taken from the song's own stored dict, then the run's ``place`` column
    (either a legacy JSON object of song id -> PLACE_GENRES key or the current
    single place key for the whole run), then backfilled from the genre.

    Runs created before the ``played``/``place`` columns existed are treated as
    all-unplayed and get their place backfilled from the genre, so the DJ still
    resumes the full set.
    """
    from Music.songs import DB_LOCK, _get_conn

    create_agent_table()
    with DB_LOCK:
        conn = _get_conn()
        cols = {c[1] for c in conn.execute("PRAGMA table_info(Agent)").fetchall()}
        place_col = ", place" if "place" in cols else ""
        row = conn.execute(
            f"SELECT id, songs_json, played{place_col} FROM Agent ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None, []

    run_id, text, played_text = row[:3]
    run_place = None
    place_map = {}
    if place_col and len(row) >= 4:
        try:
            parsed = json.loads(row[3]) if row[3] else None
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            # legacy runs stored {song_id: place}
            place_map = parsed
        elif isinstance(parsed, str):
            # current runs store a single place key (e.g. "home")
            run_place = parsed
        else:
            run_place = row[3]
    try:
        songs = json.loads(text)
    except Exception:
        return run_id, []
    if not isinstance(songs, list):
        return run_id, []

    # The played column accepts:
    #   'played'   -> every song in this run counts as played
    #   'unplayed' / NULL -> nothing played yet
    #   JSON list  -> ids of the individual songs already played
    raw = played_text
    if raw is None or str(raw).strip().lower() == "unplayed":
        played_ids = set()
    elif str(raw).strip().lower() == "played":
        played_ids = "ALL"
    else:
        try:
            played_ids = set(json.loads(raw))
        except Exception:
            played_ids = set()
        if not isinstance(played_ids, set):
            played_ids = set()

    for song in songs:
        if isinstance(song, dict):
            song["played"] = (
                True if played_ids == "ALL"
                else (str(song.get("id") or "") in played_ids)
            )
            # Per-track place comes from the stored song dict first, then the
            # run's `place` column (a legacy {song_id: place} map or the single
            # run key), then from the genre so old runs still resume by key.
            if not song.get("place"):
                song["place"] = (
                    place_map.get(str(song.get("id") or ""))
                    or run_place
                    or _place_for_genre(song.get("genre"))
                )

    return run_id, songs


def mark_song_played(song_id):
    """Flag a song as played in EVERY Agent run that contains it.

    A run's ``played`` column is only reliable if marks land in the run the
    song actually belongs to, not just the newest one: as new batches (new
    runs) are created while older sets still play, flags previously drifted to
    the newest row and older runs were left looking unplayed.

    The column can hold:
        'played'   -> every song in the run finished start-to-end
        'unplayed' / NULL -> nothing flagged yet
        JSON list  -> ids flagged so far (partial set)

    When the last unplayed song of a run is flagged, that run flips to
    'played'.  Returns True when any row was updated, False otherwise.  Never
    raises for a bad / missing song id."""
    from Music.songs import DB_LOCK, _get_conn

    song_id = str(song_id or "").strip()
    if not song_id:
        return False

    create_agent_table()
    updated = False
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute("SELECT id, songs_json, played FROM Agent").fetchall()
        for run_id, songs_text, played_text in rows:
            # Fast path: a run flagged as fully played can never contain an
            # unplayed song, so skip it before the (growing) songs_json parse.
            # This keeps mark_song_played cheap as the Agent table accumulates
            # completed runs over long sessions.
            if played_text is not None and str(played_text).strip().lower() == "played":
                continue
            try:
                songs = json.loads(songs_text)
            except Exception:
                continue
            if not isinstance(songs, list):
                continue

            run_ids = [str(s.get("id") or "") for s in songs if isinstance(s, dict)]
            if song_id not in run_ids:
                continue

            raw = played_text
            if raw is None or str(raw).strip().lower() == "unplayed":
                played = []
            else:
                try:
                    played = list(json.loads(raw))
                except Exception:
                    played = []
                if not isinstance(played, list):
                    played = []
            if song_id in played:
                continue

            played.append(song_id)
            updated = True
            if len(played) >= len(run_ids):  # every song in this run has played
                conn.execute("UPDATE Agent SET played = 'played' WHERE id = ?",
                             (run_id,))
            else:
                conn.execute(
                    "UPDATE Agent SET played = ? WHERE id = ?",
                    (json.dumps(played, default=str), run_id),
                )
    return updated


def debug_response(run_id=None):
    """Debug helper: print a saved agent response's raw_json.

    With no run id, lists the most recent saved runs so you can pick one:
        debug_response()
    With a run id, pretty-prints that run's unmodified raw payload:
        debug_response("run_1789337902750")
    """
    from Music.songs import DB_LOCK, _get_conn

    create_agent_table()
    with DB_LOCK:
        conn = _get_conn()
        if run_id is None:
            rows = conn.execute(
                "SELECT id, name, created_at FROM Agent ORDER BY id DESC LIMIT 10"
            ).fetchall()
            if not rows:
                print("No saved responses yet.")
                return None
            print("Recent saved responses:")
            for rid, name, created in rows:
                print(f"  {rid}  {name}  ({created})")
            return None

        row = conn.execute(
            "SELECT raw_json FROM Agent WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            print(f"No saved response with id {run_id!r}.")
            return None
        data = json.loads(row[0])
        print(f"raw_json for {run_id}:")
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return data


if __name__ == "__main__":
    debug_response("run_1789338449180")