"""Offline regression tests for DJ.agent hook resolution.

Covers the full cascade: YouTube replay heatmap -> separate hook-only Gemini
request -> deterministic middle-section fallback.

Import-safe: defining the module, or even importing it, performs no test run
and touches no network or database. Execution only happens when this file is
run directly (``python test_hook_repair.py``)."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from DJ.agent import Agent
import Music.heatmap as heatmap_mod
import Music.gemini_hooks as gemini_hooks_mod


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
IDS = [s["id"] for s in songs]

# The middle-section fallback for a 240s song at clip_length=33 is 103.5..136.5.
FALLBACK = (103.5, 136.5)

# Build an Agent that does NOT hit the network: only the Gemini ordering call
# and the two hook resolvers are faked; everything else (resolve/clip/finalize)
# runs for real.
a = Agent.__new__(Agent)
a._model = "fake-model"
a._client = None
a._last = None
a._last_effect_name = None

scenario = {"mode": None, "seen_hooks": None, "gemini_asked_for": None}
seen_prompts = []


def heatmap_hook(song_id, start, end, score=0.87):
    return {
        "hook_start_sec": start,
        "hook_end_sec": end,
        "hook_source": "yt_heatmap",
        "replay_peak_sec": round((start + end) / 2.0, 1),
        "replay_score": score,
        "replay_markers": 100,
    }


def gemini_hook(start, end):
    return {
        "hook_start_sec": start,
        "hook_end_sec": end,
        "hook_source": "gemini",
    }


def fake_heatmap(songs=None, clip_length=33.0, **kwargs):
    """Stand-in for Music.heatmap.resolve_hooks, patched in below so the real
    Agent._resolve_heatmap_hooks wrapper (and its error handling) still runs."""
    scenario["seen_hooks"] = clip_length
    mode = scenario["mode"]
    if mode == "heatmap_explodes":
        raise RuntimeError("yt-dlp blew up")
    if mode in ("gemini_covers_all", "all"):
        if mode == "all":
            return {s["id"]: heatmap_hook(s["id"], 60.0, 100.0) for s in songs}
        return {}  # no heatmap anywhere: Gemini must supply every song
    if mode == "partial":
        # Songs 3 and 5 publish no heatmap.
        return {
            s["id"]: heatmap_hook(s["id"], 60.0, 100.0)
            for s in songs if s["id"] not in ("id-3", "id-5")
        }
    if mode == "gemini_malformed":
        good = heatmap_hook("x", 60.0, 100.0)
        return {s["id"]: dict(good) for s in songs if s["id"] != "id-2"} | {
            "id-2": {"hook_start_sec": None, "hook_end_sec": None}
        }
    if mode in ("gemini_partial", "gemini_silent", "gemini_raises"):
        # Only id-4 has no heatmap, so it is the only song Gemini is asked about.
        return {
            s["id"]: heatmap_hook(s["id"], 60.0, 100.0)
            for s in songs if s["id"] != "id-4"
        }
    return {s["id"]: heatmap_hook(s["id"], 60.0, 100.0) for s in songs}


def fake_gemini(song_list):
    """Stand-in for Music.gemini_hooks.request_hooks."""
    scenario["gemini_asked_for"] = [s["id"] for s in song_list]
    mode = scenario["mode"]
    if mode in ("gemini_explodes", "gemini_raises"):
        raise RuntimeError("Gemini is down")
    if mode == "gemini_partial":
        # Asked about id-4, answered with nothing usable at all.
        return {}
    if mode == "gemini_silent":
        # Asked about id-4, but only answers with a window for some of them.
        return {s["id"]: gemini_hook(120.0, 200.0) for s in song_list
                if s["id"] == "id-4"}
    return {s["id"]: gemini_hook(20.0, 80.0) for s in song_list}


def fake_query(songs=None, previous=None, hooks=None):
    # The ordering call must receive the already-decided windows, and must NOT
    # be asked to return hook fields.
    assert hooks is not None, "Gemini must be handed the resolved hooks"
    scenario["seen_hooks_for_gemini"] = hooks
    order = [s["id"] for s in songs]
    if scenario["mode"] == "omits":
        order = order[:1]
    return [
        {"id": song_id,
         "transition_type": "beatmatched_crossfade", "crossfade_bars": 2,
         "transition_note": "drums", "effect": "none"}
        for song_id in order
    ]


a._query_gemini = fake_query
a._save_response = lambda playlist, place=None: None
heatmap_mod.resolve_hooks = fake_heatmap
gemini_hooks_mod.request_hooks = fake_gemini


def run_case(label, mode):
    scenario["mode"] = mode
    # Reset so "never asked" reads as an empty list rather than stale data.
    scenario["gemini_asked_for"] = []
    print(f"\n== {label} ==")
    pl = a.build_playlist(list(songs), clip_length=33)
    print(f"   ({len(pl)} songs)  gemini asked for: {scenario['gemini_asked_for']}")
    for s in pl:
        print(f"   {s['name']:8} {s['play_start']}..{s['play_end']} "
              f"len={s['clip_length']} src={s.get('hook_source')} "
              f"out={(s.get('transition_out') or {}).get('type')}")
    return pl


def main():
    # 1. Heatmap for every song: the measured window is used exactly, with the
    #    heatmap's own length, and Gemini is never asked for a hook.
    pl1 = run_case("heatmap for every song", "all")
    assert len(pl1) == 6
    for s in pl1:
        assert s["play_start_sec"] == 60.0, s
        assert s["play_end_sec"] == 100.0, s
        assert s["clip_length"] == 40.0, s
        assert s["hook_source"] == "yt_heatmap", s
        assert s["replay_peak_sec"] == 80.0, s
        assert s["replay_score"] == 0.87, s
    assert scenario["gemini_asked_for"] == [], scenario["gemini_asked_for"]
    assert all(s["transition_out"] is not None for s in pl1[:5])
    assert pl1[-1]["transition_out"] is None
    assert scenario["seen_hooks"] == 33
    sent = scenario["seen_hooks_for_gemini"]
    assert set(sent) == set(IDS), sent
    assert sent["id-0"]["hook_start_sec"] == 60.0

    # 2. Partial heatmap: only the uncovered songs are sent to Gemini, and their
    #    Gemini window wins over the fallback. Heatmap songs are untouched.
    pl2 = run_case("heatmap for most, Gemini for the rest", "partial")
    by_id = {s["id"]: s for s in pl2}
    assert len(pl2) == 6
    assert scenario["gemini_asked_for"] == ["id-3", "id-5"], scenario["gemini_asked_for"]
    for covered in ("id-0", "id-1", "id-2", "id-4"):
        assert by_id[covered]["hook_source"] == "yt_heatmap", by_id[covered]
        assert by_id[covered]["clip_length"] == 40.0, by_id[covered]
    for asked in ("id-3", "id-5"):
        s = by_id[asked]
        assert s["hook_source"] == "gemini", s
        assert s["play_start_sec"] == 20.0, s
        assert s["play_end_sec"] == 80.0, s
        assert s["clip_length"] == 60.0, s
        # A Gemini window is an estimate: it must not claim replay measurements.
        assert s.get("replay_score") is None, s
        assert s.get("replay_peak_sec") is None, s

    # 3. No heatmap at all: Gemini supplies every hook and the fallback is unused.
    pl3 = run_case("no heatmap anywhere, Gemini covers all", "gemini_covers_all")
    assert scenario["gemini_asked_for"] == IDS, scenario["gemini_asked_for"]
    assert all(s["hook_source"] == "gemini" for s in pl3), pl3
    assert all(s["clip_length"] == 60.0 for s in pl3), pl3

    # 4. Gemini is asked but returns nothing usable: the fallback takes over.
    pl4 = run_case("Gemini returns nothing for the song it was asked about",
                   "gemini_partial")
    by_id = {s["id"]: s for s in pl4}
    assert scenario["gemini_asked_for"] == ["id-4"], scenario["gemini_asked_for"]
    assert by_id["id-0"]["hook_source"] == "yt_heatmap", by_id["id-0"]
    assert by_id["id-4"]["hook_source"] == "middle_section", by_id["id-4"]
    assert by_id["id-4"]["play_start_sec"] == FALLBACK[0], by_id["id-4"]
    assert by_id["id-4"]["play_end_sec"] == FALLBACK[1], by_id["id-4"]
    assert by_id["id-4"]["clip_length"] == 33.0, by_id["id-4"]

    # 5. Gemini answers with a window for the uncovered song.
    pl5 = run_case("Gemini answers for the uncovered song", "gemini_silent")
    by_id = {s["id"]: s for s in pl5}
    assert scenario["gemini_asked_for"] == ["id-4"], scenario["gemini_asked_for"]
    assert by_id["id-4"]["hook_source"] == "gemini", by_id["id-4"]
    assert by_id["id-4"]["play_start_sec"] == 120.0, by_id["id-4"]
    assert by_id["id-0"]["hook_source"] == "yt_heatmap", by_id["id-0"]

    # 6. A window that is unusable must not be trusted, at either tier.
    pl6 = run_case("unusable window from the heatmap", "gemini_malformed")
    bad = {s["id"]: s for s in pl6}["id-2"]
    assert bad["hook_source"] == "middle_section", bad
    assert bad["play_start_sec"] == FALLBACK[0], bad
    assert {s["id"]: s for s in pl6}["id-0"]["hook_source"] == "yt_heatmap"

    # 7. Both remote layers failing must still produce a playlist.
    pl7 = run_case("heatmap and Gemini both raise", "heatmap_explodes")
    # (heatmap_explodes only fails the heatmap; Gemini then covers everything)
    assert scenario["gemini_asked_for"] == IDS, scenario["gemini_asked_for"]
    assert all(s["hook_source"] == "gemini" for s in pl7), pl7
    assert pl7[-1]["transition_out"] is None

    # 8. Gemini raising on top of a good heatmap pass: heatmap still wins and
    #    only the uncovered song falls back.
    pl8 = run_case("Gemini layer raises", "gemini_raises")
    by_id = {s["id"]: s for s in pl8}
    assert len(pl8) == 6
    assert scenario["gemini_asked_for"] == ["id-4"], scenario["gemini_asked_for"]
    assert by_id["id-0"]["hook_source"] == "yt_heatmap", by_id["id-0"]
    assert by_id["id-4"]["hook_source"] == "middle_section", by_id["id-4"]
    assert by_id["id-4"]["clip_length"] == 33.0, by_id["id-4"]

    # 9. Gemini omitting songs from the playlist must not lose their hooks.
    pl9 = run_case("Gemini omits 5 of 6 songs", "omits")
    assert len(pl9) == 6, pl9
    assert pl9[0]["id"] == "id-0", [s["id"] for s in pl9]
    assert [s["id"] for s in pl9[1:]] == [f"id-{i}" for i in range(1, 6)]
    for s in pl9:
        assert s["hook_source"] == "yt_heatmap", s
        assert s["clip_length"] == 40.0, s
    assert pl9[-1]["transition_out"] is None
    assert all(s["transition_out"] is not None for s in pl9[:5]), pl9

    print("\nALL HOOK-CASCADE TESTS PASSED")


if __name__ == "__main__":
    main()
