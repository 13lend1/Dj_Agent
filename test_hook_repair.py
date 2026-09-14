"""Offline regression tests for DJ.agent hook repair.

Import-safe: defining the module, or even importing it, performs no test run
and touches no network or database. Execution only happens when this file is
run directly (``python test_hook_repair.py``)."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from DJ.agent import Agent


def make_song(i, duration_s, name=None):
    return {
        "id": f"id-{i}",
        "name": name or f"Song {i}",
        "artist": f"Artist {i}",
        "duration": duration_s * 1000,
        "genre": "house",
        "bpm": 120 + i,
        "energy": 0.5,
        "danceability": 0.6,
        "valence": 0.4,
        "acousticness": 0.1,
        "instrumentalness": 0.2,
        "link": f"https://x.test/{i}",
    }


songs = [make_song(i, 240) for i in range(6)]

# Build an Agent that does NOT hit the network: only the two query methods
# are faked; everything else (resolve/clip/finalize) runs for real.
a = Agent.__new__(Agent)
a._model = "fake-model"
a._client = None
a._last = None


scenario = {"mode": None}

def fake_query(songs, previous):
    if scenario["mode"] == "no_hooks":
        # Gemini returns entries for all songs but with null/zero hooks
        return [
            {"id": s["id"], "hook_start_sec": None, "hook_end_sec": None,
             "transition_type": "beatmatched_crossfade", "crossfade_bars": 2,
             "transition_note": "x", "effect": "none"}
            for s in songs
        ]
    if scenario["mode"] == "omits":
        # Gemini only mentions one song; the rest must be appended + repaired
        return [{"id": songs[0]["id"], "hook_start_sec": 40.0, "hook_end_sec": 100.0,
                 "transition_type": "crossfade", "crossfade_bars": 1,
                 "transition_note": "y", "effect": "none"}]
    # default: valid hooks
    return [{"id": s["id"], "hook_start_sec": 60.0, "hook_end_sec": 100.0,
             "transition_type": "crossfade", "crossfade_bars": 1,
             "transition_note": "y", "effect": "none"} for s in songs]

def fake_repair(songs):
    if scenario["mode"] == "no_hooks":
        # repair succeeds for all but song 3
        out = {}
        for s in songs:
            if s["id"] != "id-3":
                out[s["id"]] = {"hook_start_sec": 90.0, "hook_end_sec": 150.0}
        return out
    if scenario["mode"] == "omits":
        return {s["id"]: {"hook_start_sec": 20.0, "hook_end_sec": 80.0}
                for s in songs if s["id"] != "id-1"}
    return {}

a._query_gemini = fake_query
a._request_hooks = fake_repair
a._save_response = lambda playlist: None


def run_case(label, mode):
    scenario["mode"] = mode
    print(f"\n== {label} ==")
    pl = a.build_playlist(list(songs), clip_length=33)
    print(f"   ({len(pl)} songs)")
    for s in pl:
        print(f"   {s['name']:8} {s['play_start']}..{s['play_end']} "
              f"len={s['clip_length']} t={s.get('_gemini_transition')}")
    return pl


def main():
    pl1 = run_case("valid hooks from Gemini", "valid")
    assert all(abs(s["clip_length"] - 40.0) < 0.01 for s in pl1), "valid case should keep 60..100"
    assert all(s["transition_out"] is not None for s in pl1[:5])
    assert pl1[-1]["transition_out"] is None

    pl2 = run_case("Gemini returns NULL hooks", "no_hooks")
    # repair gave real windows to all but id-3 -> those 90..150 (len 60); id-3 must fall back
    by_id = {s["id"]: s for s in pl2}
    assert by_id["id-1"]["clip_length"] == 60.0, by_id["id-1"]
    assert by_id["id-3"]["clip_length"] == 33.0, by_id["id-3"]
    assert len(pl2) == 6

    pl3 = run_case("Gemini omits 5 of 6 songs", "omits")
    # id-0 had a valid Gemini hook (40..100); id-1 excluded from repair -> fallback;
    # id-2..id-5 repaired to 20..80 (len 60)
    by_id = {s["id"]: s for s in pl3}
    assert by_id["id-0"]["play_start_sec"] == 40.0
    assert by_id["id-0"]["play_end_sec"] == 100.0
    assert by_id["id-1"]["clip_length"] == 33.0, by_id["id-1"]
    assert by_id["id-2"]["clip_length"] == 60.0, by_id["id-2"]

    print("\nALL HOOK-REPAIR TESTS PASSED")


if __name__ == "__main__":
    main()