from DJ.video import get_current_time,lock_video_paused,seek_video,pause_video,unlock_video,play_video,get_video_state
from DJ.volume import set_volume
POLL_INTERVAL_MS = 50

def prepare_track(
    page,
    video_url,
    seek_to
):
    """
    Load and prepare the next track.

    IMPORTANT:

        The track remains paused.

        Its volume is set to 0.

        Autoplay is locked.
    """

    print(
        "[TAB] Loading next track..."
    )

    page.goto(
        video_url,
        wait_until="domcontentloaded"
    )

    page.wait_for_selector(
        "video.html5-main-video",
        timeout=15000
    )

    # --------------------------------------------------------
    # Consent
    # --------------------------------------------------------

    consent_btn = page.query_selector(
        "text=Accept all"
    )

    if consent_btn:

        try:

            consent_btn.click()

        except Exception:

            pass

    # --------------------------------------------------------
    # Prevent autoplay.
    # --------------------------------------------------------

    lock_video_paused(
        page
    )

    # --------------------------------------------------------
    # Start silent.
    # --------------------------------------------------------

    set_volume(
        page,
        0.0
    )

    # --------------------------------------------------------
    # Seek.
    # --------------------------------------------------------

    if seek_to is not None:

        seek_video(
            page,
            seek_to
        )

        print(
            f"[TAB] Seek -> "
            f"{seek_to:.2f}s"
        )

    else:

        print(
            "[TAB] No heatmap."
        )

    # --------------------------------------------------------
    # Give YouTube time to settle.
    # --------------------------------------------------------

    page.wait_for_timeout(
        1000
    )

    # --------------------------------------------------------
    # Verify seek.
    # --------------------------------------------------------

    if seek_to is not None:

        current = get_current_time(
            page
        )

        if current is not None:

            if abs(
                current - seek_to
            ) > 5:

                print(
                    "[TAB] Seek reset."
                )

                seek_video(
                    page,
                    seek_to
                )

    # --------------------------------------------------------
    # Force pause.
    # --------------------------------------------------------

    pause_video(
        page
    )

    set_volume(
        page,
        0.0
    )

    print(
        "[TAB] READY + PAUSED + SILENT"
    )


# ============================================================
# START TRACK
# ============================================================

def start_track(
    page,
    video_url,
    seek_to
):

    prepare_track(
        page,
        video_url,
        seek_to
    )

    unlock_video(
        page
    )

    # Full volume for first track.
    set_volume(
        page,
        1.0
    )

    play_video(
        page
    )

    print(
        "[PLAY] Track started."
    )


# ============================================================
# WAIT UNTIL TIME
# ============================================================

def wait_until_time(
    page,
    target_time
):

    while True:

        page.wait_for_timeout(
            POLL_INTERVAL_MS
        )

        state = get_video_state(
            page
        )

        if not state["exists"]:

            return False

        if state["ended"]:

            return False

        current = state["time"]

        if (
            current is not None
            and
            current >= target_time
        ):

            return True
