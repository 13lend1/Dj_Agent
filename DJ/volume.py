import math 
from DJ.video import unlock_video,play_video,pause_video,get_video_state

CROSSFADE_SECONDS = 3.5
CROSSFADE_STEPS = 20
def set_volume(
    page,
    volume: float
):

    volume = max(
        0.0,
        min(1.0, volume)
    )

    page.evaluate(
        """
        (volume) => {

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            if (v) {
                v.volume = volume;
            }
        }
        """,
        volume
    )
    

def crossfade(
    old_page,
    new_page,
    duration_seconds=CROSSFADE_SECONDS,
    steps=CROSSFADE_STEPS
):
    """
    Smooth equal-power crossfade.

    Old track:

        cos(progress * PI/2)

    New track:

        sin(progress * PI/2)

    This generally sounds smoother than:

        old = 1-progress
        new = progress
    """

    print()
    print(
        "[CROSSFADE] Starting..."
    )

    # --------------------------------------------------------
    # Make absolutely sure new track is silent.
    # --------------------------------------------------------

    set_volume(
        new_page,
        0.0
    )

    # --------------------------------------------------------
    # Unlock next video.
    # --------------------------------------------------------

    unlock_video(
        new_page
    )

    # --------------------------------------------------------
    # Start next video SILENTLY.
    # --------------------------------------------------------

    play_video(
        new_page
    )

    # --------------------------------------------------------
    # Give browser time to actually start it.
    # --------------------------------------------------------

    new_page.wait_for_timeout(
        30
    )

    step_duration = (
        duration_seconds * 1000
        / steps
    )

    # --------------------------------------------------------
    # Crossfade.
    # --------------------------------------------------------

    for i in range(
        steps + 1
    ):

        progress = (
            i / steps
        )

        # Equal-power curves.
        old_volume = math.cos(
            progress * math.pi / 2
        )

        new_volume = math.sin(
            progress * math.pi / 2
        )

        set_volume(
            old_page,
            old_volume
        )

        set_volume(
            new_page,
            new_volume
        )

        new_page.wait_for_timeout(
            int(step_duration)
        )
    set_volume(
        old_page,
        0.0
    )

    set_volume(
        new_page,
        1.0
    )

    # --------------------------------------------------------
    # Now stop the old track.
    # --------------------------------------------------------

    hard_pause(
        old_page
    )

    print(
        "[CROSSFADE] Complete."
    )

def hard_pause(
    page
):
    """
    Make sure the old track is actually paused.
    """

    for _ in range(20):

        pause_video(
            page
        )

        page.wait_for_timeout(
            5
        )

        state = get_video_state(
            page
        )

        if (
            state["paused"]
            or
            state["ended"]
        ):

            return True

    return False