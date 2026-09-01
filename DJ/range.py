
from DJ.heatmap import get_heatmap,get_hook_range
def fetch_track_range(video_url: str):

    print()
    print(
        f"[PREFETCH] Fetching:"
    )

    print(
        f"           {video_url}"
    )

    heatmap = get_heatmap(
        video_url
    )

    if not heatmap:

        print(
            "[PREFETCH] No heatmap."
        )

        return None

    result = get_hook_range(
        heatmap
    )

    if result:

        start, end, peak = result

        print(
            "[PREFETCH] READY"
        )

        print(
            f"           Range : "
            f"{start:.2f}s -> "
            f"{end:.2f}s"
        )

        print(
            f"           Peak  : "
            f"{peak:.2f}s"
        )

        print(
            f"           Length: "
            f"{end - start:.2f}s"
        )

    else:

        print(
            "[PREFETCH] No usable hook."
        )

    return result
