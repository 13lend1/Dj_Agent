"""
download_effects.py

Downloads a curated set of free, CC0/Attribution-licensed DJ transition SFX
from Freesound.org into a local effects/ folder, one file per effect name,
so the Agent can pick from them by name at build_playlist time.

Requires a free Freesound API key: https://freesound.org/apiv2/apply/
Set it as FREESOUND_API_KEY in your environment or .env file.

Note: samples under "Attribution" license (as opposed to CC0) legally
require crediting the author if you ship/publish the set. This script
writes an ATTRIBUTIONS.txt alongside the audio so you have that covered.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("FREESOUND_API_KEY")
if not API_KEY:
    raise RuntimeError("FREESOUND_API_KEY not set (get one free at freesound.org/apiv2/apply)")

OUT_DIR = "effects"
os.makedirs(OUT_DIR, exist_ok=True)

# effect_name -> (list of search queries to try in order, (min_sec, max_sec))
EFFECTS = {
    "riser_white_noise":     (["white noise riser sweep up", "riser sweep up", "build up riser"], (0.2, 12.0)),
    "riser_synth":           (["synth riser build up", "riser build up synth"], (0.2, 12.0)),
    "downlifter":            (["downlifter reverse riser", "downlifter sweep down", "riser down effect"], (0.2, 12.0)),
    "impact_boom":           (["cinematic impact boom hit", "impact hit boom"], (0.2, 6.0)),
    "impact_sub_drop":       (["sub bass drop hit", "bass drop impact"], (0.2, 6.0)),
    "sweep_up":              (["sweep up whoosh", "riser sweep up whoosh"], (0.2, 6.0)),
    "sweep_down":            (["sweep down whoosh", "downlifter whoosh"], (0.2, 6.0)),
    "whoosh":                (["whoosh transition", "whoosh swoosh"], (0.2, 6.0)),
    "filter_sweep_lowpass":  (["low pass filter sweep", "lowpass sweep"], (0.2, 8.0)),
    "filter_sweep_highpass": (["high pass filter sweep", "highpass sweep"], (0.2, 8.0)),
    "vinyl_stop":            (["vinyl brake stop", "record stop scratch", "vinyl brake"], (0.2, 6.0)),
    "tape_stop":             (["tape stop effect", "tape stop"], (0.2, 6.0)),
    "reverse_cymbal":        (["reverse cymbal swell", "reverse cymbal", "cymbal swell reverse"], (0.2, 10.0)),
    "cymbal_crash":          (["crash cymbal hit", "cymbal crash"], (0.2, 6.0)),
    "snare_roll":            (["snare drum roll build", "snare roll buildup"], (0.2, 10.0)),
    "drum_fill":             (["drum fill transition", "drum fill", "percussion fill"], (0.2, 10.0)),
    "echo_throw":            (["echo delay throw vocal", "vocal echo throw", "delay throw fx"], (0.2, 8.0)),
    "air_horn":              (["dj air horn", "air horn"], (0.2, 6.0)),
    "laser_zap":             (["laser zap sound effect", "laser zap"], (0.2, 6.0)),
    "siren":                 (["siren rising", "siren alarm"], (0.2, 8.0)),
    "glitch_stutter":        (["glitch stutter edit", "glitch stutter"], (0.2, 6.0)),
    "scratch":               (["vinyl scratch dj", "dj scratch"], (0.2, 6.0)),
    "white_noise_sweep":     (["white noise sweep transition", "white noise sweep"], (0.2, 8.0)),
    "kick_roll":             (["kick drum roll buildup", "kick roll", "bass drum roll"], (0.2, 10.0)),
    "crowd_cheer":           (["crowd cheer applause", "crowd cheering"], (0.2, 8.0)),
    "vocal_tag":             (["dj vocal tag drop", "dj tag drop", "vocal tag"], (0.2, 6.0)),
}

SEARCH_URL = "https://freesound.org/apiv2/search/text/"


def _license_ok(license_url):
    """Freesound returns license as a URL, e.g.
    'http://creativecommons.org/publicdomain/zero/1.0/' (CC0) or
    'http://creativecommons.org/licenses/by/3.0/' (Attribution).
    Accept CC0 and plain Attribution; reject NC/ND/sampling variants."""
    if not license_url:
        return False
    l = license_url.lower()
    if "publicdomain/zero" in l:
        return True
    if "/licenses/by/" in l:  # plain "by", not by-nc / by-nd / by-sa
        return True
    return False


def _is_attribution_required(license_url):
    return "publicdomain/zero" not in (license_url or "").lower()


def find_and_download(name, queries, duration_range, attributions):
    lo, hi = duration_range
    for query in queries:
        params = {
            "query": query,
            "token": API_KEY,
            "filter": f"duration:[{lo} TO {hi}]",
            "sort": "rating_desc",
            "fields": "id,name,username,license,previews,duration,url",
            "page_size": 15,
        }
        resp = requests.get(SEARCH_URL, params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])

        for r in results:
            license_ = r.get("license", "")
            if not _license_ok(license_):
                continue
            preview_url = r["previews"].get("preview-hq-mp3") or r["previews"].get("preview-lq-mp3")
            if not preview_url:
                continue

            audio = requests.get(preview_url, timeout=15)
            audio.raise_for_status()
            out_path = os.path.join(OUT_DIR, f"{name}.mp3")
            with open(out_path, "wb") as f:
                f.write(audio.content)

            print(f"[ok]   {name:<24} <- '{r['name']}' ({r['duration']:.1f}s, {license_}) [query: '{query}']")
            if _is_attribution_required(license_):
                attributions.append(
                    f"{name}.mp3 — \"{r['name']}\" by {r.get('username', '?')} "
                    f"({license_}) — {r.get('url', '')}"
                )
            return True
        time.sleep(0.3)  # between fallback attempts too

    print(f"[skip] {name:<24} no CC0/Attribution match for any of {queries}")
    return False


def main():
    ok, failed, attributions = 0, [], []
    for name, (queries, duration_range) in EFFECTS.items():
        out_path = os.path.join(OUT_DIR, f"{name}.mp3")
        if os.path.exists(out_path):
            print(f"[have] {name:<24} already downloaded, skipping")
            ok += 1
            continue
        try:
            if find_and_download(name, queries, duration_range, attributions):
                ok += 1
            else:
                failed.append(name)
        except requests.RequestException as exc:
            print(f"[err]  {name:<24} {exc}")
            failed.append(name)
        time.sleep(0.5)  # be polite to the API

    if attributions:
        with open(os.path.join(OUT_DIR, "ATTRIBUTIONS.txt"), "w") as f:
            f.write("\n".join(attributions) + "\n")
        print(f"\nWrote {len(attributions)} required attribution(s) to {OUT_DIR}/ATTRIBUTIONS.txt")

    print(f"\nDone: {ok}/{len(EFFECTS)} effects downloaded into ./{OUT_DIR}/")
    if failed:
        print(f"Missing (search manually or adjust query): {', '.join(failed)}")


if __name__ == "__main__":
    main()