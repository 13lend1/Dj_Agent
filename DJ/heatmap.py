import yt_dlp
MIN_HOOK_SECONDS = 30


# Maximum hook length.
MAX_HOOK_SECONDS = 45


# Natural heatmap threshold.
THRESHOLD_RATIO = 0.70


# Don't select peaks right at the beginning/end.
IGNORE_START_SECONDS = 5
IGNORE_END_SECONDS = 5
def get_heatmap(video_url: str):
    """
    Fetch YouTube's Most Replayed heatmap.

    Metadata only.
    No audio/video is downloaded.
    """

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,

        "socket_timeout": 15,

        "extractor_retries": 2,

        "extractor_args": {
            "youtube": {
                "skip": [
                    "dash",
                    "hls"
                ]
            }
        }
    }

    try:

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(
                video_url,
                download=False
            )

        return info.get("heatmap")

    except Exception as e:
        print(f"[HEATMAP ERROR] {video_url}")
        print(f"                {e}")
        return None
    

def get_hook_range(
    heatmap,
    threshold_ratio=THRESHOLD_RATIO,
    min_hook_seconds=MIN_HOOK_SECONDS,
    max_hook_seconds=MAX_HOOK_SECONDS,
    ignore_start_seconds=IGNORE_START_SECONDS,
    ignore_end_seconds=IGNORE_END_SECONDS,
):
    """
    Find a useful musical section around the Most Replayed peak.

    Returns:

        (start, end, peak)

    The algorithm first finds the natural heatmap region.

    If that region is too short, it expands around the peak.

    Example:

        Natural:
            2:41 -> 2:43

        Final:
            ~2:26 -> ~2:56

    This avoids tiny heatmap ranges.
    """

    if not heatmap:

        return None

    # --------------------------------------------------------
    # Sort heatmap.
    # --------------------------------------------------------

    heatmap = sorted(
        heatmap,
        key=lambda x: x["start_time"]
    )

    total_duration = (
        heatmap[-1]["end_time"]
    )

    if total_duration <= 0:

        return None

    # --------------------------------------------------------
    # Valid peak candidates.
    # --------------------------------------------------------

    candidate_indices = [

        i

        for i, seg in enumerate(heatmap)

        if (
            seg["start_time"]
            >= ignore_start_seconds
            and
            seg["end_time"]
            <=
            total_duration
            - ignore_end_seconds
        )
    ]

    if not candidate_indices:

        candidate_indices = list(
            range(len(heatmap))
        )

    # --------------------------------------------------------
    # Find peak.
    # --------------------------------------------------------

    peak_idx = max(
        candidate_indices,
        key=lambda i:
            heatmap[i]["value"]
    )

    peak_value = (
        heatmap[peak_idx]["value"]
    )

    peak_time = (
        heatmap[peak_idx]["start_time"]
    )

    threshold = (
        peak_value
        * threshold_ratio
    )

    # --------------------------------------------------------
    # Expand naturally around peak.
    # --------------------------------------------------------

    start_idx = peak_idx

    while (
        start_idx > 0
        and
        heatmap[start_idx - 1]["value"]
        >= threshold
    ):

        start_idx -= 1

    end_idx = peak_idx

    while (
        end_idx
        <
        len(heatmap) - 1
        and
        heatmap[end_idx + 1]["value"]
        >= threshold
    ):

        end_idx += 1

    natural_start = (
        heatmap[start_idx]["start_time"]
    )

    natural_end = (
        heatmap[end_idx]["end_time"]
    )

    natural_length = (
        natural_end
        - natural_start
    )

    # ========================================================
    # NATURAL RANGE IS GOOD
    # ========================================================

    if natural_length >= min_hook_seconds:

        # ----------------------------------------------------
        # Natural range within desired maximum.
        # ----------------------------------------------------

        if natural_length <= max_hook_seconds:

            return (
                natural_start,
                natural_end,
                peak_time
            )

        # ----------------------------------------------------
        # Natural range is too long.
        # Center maximum range around peak.
        # ----------------------------------------------------

        half = (
            max_hook_seconds / 2
        )

        final_start = (
            peak_time - half
        )

        final_end = (
            peak_time + half
        )

        final_start = max(
            ignore_start_seconds,
            final_start
        )

        final_end = min(
            total_duration
            - ignore_end_seconds,
            final_end
        )

        return (
            final_start,
            final_end,
            peak_time
        )

    # ========================================================
    # NATURAL RANGE TOO SHORT
    # ========================================================

    half = (
        min_hook_seconds / 2
    )

    final_start = (
        peak_time - half
    )

    final_end = (
        peak_time + half
    )

    # --------------------------------------------------------
    # Keep inside video.
    # --------------------------------------------------------

    minimum_start = (
        ignore_start_seconds
    )

    maximum_end = (
        total_duration
        - ignore_end_seconds
    )

    # Shift right if necessary.
    if final_start < minimum_start:

        shift = (
            minimum_start
            - final_start
        )

        final_start += shift
        final_end += shift

    # Shift left if necessary.
    if final_end > maximum_end:

        shift = (
            final_end
            - maximum_end
        )

        final_start -= shift
        final_end -= shift

    # Final clamp.
    final_start = max(
        minimum_start,
        final_start
    )

    final_end = min(
        maximum_end,
        final_end
    )

    # --------------------------------------------------------
    # Safety.
    # --------------------------------------------------------

    if final_end <= final_start:

        return (
            natural_start,
            natural_end,
            peak_time
        )

    return (
        final_start,
        final_end,
        peak_time
    )
