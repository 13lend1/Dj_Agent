"""Live test: ask the real Gemini API for hooks on a sample of candidates.

Inert by default — safe to keep in the repo. Importing this module has no
side effects, and running it directly does nothing unless you opt in with:

    set DJ_TESTS_LIVE=1   (Windows:  $env:DJ_TESTS_LIVE=1)

Without the flag it prints a skip notice and exits 0. When enabled it reads a
few Preprocessed rows READ-ONLY from Database/music.db (path derived from this
file, never created) and makes one real Gemini request. It writes nothing."""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import sqlite3
from DJ.agent import Agent


def _sample_songs(limit=15):
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Database", "music.db")
    if not os.path.isfile(db_path):
        raise OSError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT * FROM Preprocessed LIMIT ?", (limit,)).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM Preprocessed LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def main():
    if os.environ.get("DJ_TESTS_LIVE") != "1":
        print("SKIPPED: set DJ_TESTS_LIVE=1 to run the live Gemini hook test.")
        return
    if not os.environ.get("GEMINI_API_KEY"):
        print("SKIPPED: GEMINI_API_KEY not set.")
        return

    songs = _sample_songs()

    agent = Agent()
    t0 = time.time()
    queue = agent._query_gemini(songs=songs, previous=None)
    print(f'[{time.time()-t0:.1f}s] raw queue entries:', len(queue))
    hooks_ok = 0
    null_hooks = 0
    for e in queue:
        hs = e.get('hook_start_sec')
        he = e.get('hook_end_sec')
        ok = isinstance(hs, (int, float)) and isinstance(he, (int, float)) and hs >= 0 and he > hs
        hook_ok = ok
        if hook_ok:
            hooks_ok += 1
        if hs is None or he is None:
            null_hooks += 1
        print(f"  id={e.get('id')} hook={hs!r}..{he!r} bars={e.get('crossfade_bars')!r} ok={hook_ok}")
    print(f'\nhook_ok: {hooks_ok}/{len(queue)}  null_hooks: {null_hooks}')

    # Now validate through _resolve_hook like build_playlist does
    by_id = {str(s.get('id')): s for s in songs}
    playlist = []
    fallbacks = 0
    for entry in queue:
        sid = str(entry.get('id') or '').strip()
        song = by_id.get(sid)
        if song is None:
            continue
        start, end, fb = agent._resolve_hook(song=dict(song), entry=entry, clip_length=33)
        if fb:
            fallbacks += 1
        print(f"  resolved {song['name']}: {start}..{end} fallback={fb}")
    print('fallbacks after validation:', fallbacks)


if __name__ == "__main__":
    main()