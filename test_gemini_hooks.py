"""Live test: real YouTube replay heatmaps + real Gemini hook requests.

Inert by default - safe to keep in the repo. Importing this module has no side
effects, and running it directly does nothing unless you opt in with:

    set DJ_TESTS_LIVE=1   (Windows:  $env:DJ_TESTS_LIVE=1)

Without the flag it prints a skip notice and exits 0. When enabled it reads a
few Preprocessed rows READ-ONLY from Database/music.db (path derived from this
file, never created) and walks the real hook cascade:

    YouTube replay heatmap -> separate hook-only Gemini request -> fallback

It then makes one real Gemini ordering request. It writes nothing except the
heatmap cache."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import sqlite3
from DJ.agent import Agent
from Music.heatmap import resolve_hooks
from Music import gemini_hooks


def _sample_songs(limit=8):
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
        print("SKIPPED: set DJ_TESTS_LIVE=1 to run the live hook-cascade test.")
        return
    if not os.environ.get("GEMINI_API_KEY"):
        print("SKIPPED: GEMINI_API_KEY not set.")
        return

    songs = _sample_songs()
    agent = Agent()

    # 1. Tier 1: the YouTube replay heatmap.
    t0 = time.time()
    hooks = resolve_hooks(songs, clip_length=33)
    print(f"[{time.time()-t0:.1f}s] heatmap hooks: {len(hooks)}/{len(songs)}")
    for sid, hook in hooks.items():
        print(f"  heatmap {sid} {hook['hook_start_sec']}..{hook['hook_end_sec']} "
              f"score={hook['replay_score']}")

    # 2. Tier 2: the separate hook-only Gemini request, for what tier 1 missed.
    missing = [s for s in songs if str(s.get("id")) not in hooks]
    guessed = gemini_hooks.request_hooks(missing) if missing else {}
    print(f"asked Gemini for {len(missing)} uncovered song(s), got {len(guessed)}")
    hooks.update(guessed)

    # 3. Tier 3: whatever is still missing must fall back, never 0/0.
    print("\nresolved windows:")
    fallbacks = 0
    for song in songs:
        sid = str(song.get("id") or "").strip()
        start, end, source = agent._resolve_hook(
            song=dict(song), hook=hooks.get(sid), clip_length=33
        )
        if source == "middle_section":
            fallbacks += 1
        assert end > start, f"{song.get('name')} produced a zero-length window"
        print(f"  {str(song.get('name'))[:32]:32} {start:6.1f}..{end:6.1f} {source}")
    print(f"fallbacks: {fallbacks}/{len(songs)}")
    assert fallbacks + len(hooks) >= 1, "no song resolved a hook at all"

    # 4. The ordering request must not return hook fields any more.
    t0 = time.time()
    queue = agent._query_gemini(songs=songs, previous=None, hooks=hooks)
    print(f"[{time.time()-t0:.1f}s] queue entries: {len(queue)}")
    leaked = [e for e in queue if "hook_start_sec" in e or "hook_end_sec" in e]
    assert not leaked, f"Gemini should not return hooks any more: {leaked[:2]}"
    assert all("transition_type" in e for e in queue), queue[:2]
    print("  no hook fields returned by the ordering call")

    # 5. The full builder must reproduce the same per-song sources.
    agent._save_response = lambda playlist, place=None: None
    playlist = agent.build_playlist(songs, clip_length=33)
    by_id = {str(s.get("id") or "").strip(): s for s in playlist}
    for sid, hook in hooks.items():
        assert by_id[sid]["hook_source"] == hook["hook_source"], (
            sid, by_id[sid]["hook_source"], hook["hook_source"])
    print(f"build_playlist agreed on all {len(hooks)} resolved source(s)")


if __name__ == "__main__":
    main()
