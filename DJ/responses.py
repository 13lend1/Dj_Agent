import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
_MUSIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Music")
if _MUSIC_DIR not in sys.path:
    sys.path.insert(0, _MUSIC_DIR)

import json
import sqlite3
import time


def create_agent_table():
    """Create (once) the Agent table that stores everything build_playlist()
    returns: the ordered song list, the ordered transitions between songs,
    every hook window, and the raw payload. Existing runs are never lost."""
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
                raw_json TEXT
            )
            """
        )


def save_response(response):
    """Persist one full agent response (the playlist from
    Agent.build_playlist) into the Agent table.

    `response` is the list of enriched song dicts the agent returned, in play
    order. Each song already carries play_start_sec/play_end_sec, the M:SS hook
    strings, clip_length, and transition_out. Everything is kept three ways:
        songs_json        the ordered song list, exactly as returned (hook
                          windows and transition_out included per song)
        transitions_json  every transition_out, in play order, labelled with
                          which pair it joins
        hooks_json        each song's hook window + clip length, in play order
        raw_json          the unmodified original response

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
        songs.append(item)

        hooks.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "artist": item.get("artist"),
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

    with DB_LOCK:
        conn = _get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO Agent
                (id, name, created_at, songs_json, transitions_json, hooks_json, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                name,
                time.strftime("%Y-%m-%d %H:%M:%S"),
                json.dumps(songs, default=str),
                json.dumps(transitions, default=str),
                json.dumps(hooks, default=str),
                json.dumps(playlist, default=str),
            ),
        )
        return run_id


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