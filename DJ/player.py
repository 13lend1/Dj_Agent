
"""
AI DJ Agent — Smooth Crossfade + Background Heatmap Prefetch

Features
--------

1. Fetches YouTube Most Replayed heatmaps.
2. Detects a useful hook around the Most Replayed peak.
3. Prevents extremely short 1-3 second heatmap ranges.
4. Plays each track from its detected hook.
5. Fetches the NEXT track's heatmap while the current track plays.
6. Opens the NEXT YouTube tab 10 seconds before the current hook ends.
7. Loads and seeks the next video while keeping it PAUSED.
8. Current track continues normally until its EXACT hook end.
9. Performs a smooth equal-power crossfade.
10. Closes the old tab after the crossfade.

The 10-second preload does NOT change the music timing.

The crossfade is independent from the heatmap detection.

Setup
-----

    pip install playwright yt-dlp
    playwright install chromium

Launch Chrome with remote debugging:

Windows:

    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
        --remote-debugging-port=9222

Then:

    python dj_agent_phase1.py
"""

import math

import yt_dlp

from playwright.sync_api import sync_playwright

from concurrent.futures import ThreadPoolExecutor

from DJ.heatmap import *
from DJ.volume import *
from DJ.video import *
from DJ.track import *
from DJ.range import *

PRELOAD_SECONDS = 10


def main():

    urls = [

        "https://www.youtube.com/watch?v=9ZEURntrQOg",

        "https://www.youtube.com/watch?v=wCQfkEkePx8",

        "https://www.youtube.com/watch?v=2nGKqH26xlg",

        "https://www.youtube.com/watch?v=yKNxeF4KMsY",

        "https://www.youtube.com/watch?v=d-diB65scQU",

    ]

    if not urls:

        print(
            "No tracks."
        )

        return

    with sync_playwright() as p:

        print(
            "[CHROME] Connecting..."
        )

        browser = p.chromium.connect_over_cdp(
            "http://127.0.0.1:9222"
        )

        context = browser.contexts[0]

        executor = ThreadPoolExecutor(
            max_workers=1
        )
        print()
        print("=" * 65)
        print("TRACK 1")
        print("=" * 65)

        first_future = executor.submit(
            fetch_track_range,
            urls[0]
        )

        first_range = (first_future.result())

        current_page = (
            context.new_page()
        )

        first_seek = (
            first_range[0]
            if first_range
            else None
        )

        start_track(
            current_page,
            urls[0],
            first_seek
        )

        current_range = (
            first_range
        )

        if len(urls) > 1:

            print(
                "[PREFETCH] Fetching Track 2..."
            )

            next_future = (
                executor.submit(
                    fetch_track_range,
                    urls[1]
                )
            )
        else:

            next_future = None
        for i in range(
            len(urls) - 1
        ):

            print()
            print("=" * 65)

            print(
                f"TRANSITION "
                f"{i + 1} -> {i + 2}"
            )

            print("=" * 65)

            next_range = (
                next_future.result()
            )

            if current_range:

                range_start = (
                    current_range[0]
                )

                range_end = (
                    current_range[1]
                )

                preload_time = (
                    range_end
                    - PRELOAD_SECONDS
                )

                preload_time = max(
                    range_start,
                    preload_time
                )

                print()
                print(
                    f"[CURRENT]"
                )

                print(
                    f"  Hook:"
                    f" {range_start:.2f}s"
                    f" -> "
                    f"{range_end:.2f}s"
                )

                print(
                    f"  Next tab opens at:"
                    f" {preload_time:.2f}s"
                )

                wait_until_time(
                    current_page,
                    preload_time
                )

            else:

                input(
                    "Current track has no heatmap. "
                    "Press Enter to prepare next track..."
                )

            print()
            print(
                "[PRELOAD] Opening next tab..."
            )

            next_page = (
                context.new_page()
            )

            next_seek = (
                next_range[0]
                if next_range
                else None
            )

            prepare_track(
                next_page,
                urls[i + 1],
                next_seek
            )

            print()
            print(
                "[PRELOAD] Next track is READY."
            )

            print(
                "[PRELOAD] Current track continues."
            )

            if current_range:

                print()
                print(
                    f"[CURRENT] Playing until "
                    f"{range_end:.2f}s..."
                )

                wait_until_time(
                    current_page,
                    range_end
                )

            else:

                input(
                    "Press Enter when ready to transition..."
                )
            
            print()
            print(
                "[TRANSITION] Beginning crossfade."
            )
            crossfade(
                current_page,
                next_page,
                duration_seconds=
                    CROSSFADE_SECONDS
            )
            print(
                "[TRANSITION] Closing old tab."
            )

            current_page.close()
            current_page = (
                next_page
            )
            current_range = (
                next_range
            )

            following_index = (
                i + 2
            )

            if (
                following_index
                < len(urls)
            ):

                print()
                print(
                    f"[PREFETCH] Fetching Track "
                    f"{following_index + 1}..."
                )

                next_future = (
                    executor.submit(
                        fetch_track_range,
                        urls[following_index]
                    )
                )

            else:

                next_future = None

        print()
        print("=" * 65)
        print("LAST TRACK")
        print("=" * 65)

        if current_range:

            print(
                f"[LAST] Playing until "
                f"{current_range[1]:.2f}s"
            )

            wait_until_time(
                current_page,
                current_range[1]
            )

            hard_pause(
                current_page
            )

        else:

            input(
                "Last track has no heatmap. "
                "Press Enter to finish..."
            )
        executor.shutdown(
            wait=False,
            cancel_futures=False
        )

        print()
        print("=" * 65)
        print("PLAYLIST FINISHED")
        print("=" * 65)


if __name__ == "__main__":
    main()

