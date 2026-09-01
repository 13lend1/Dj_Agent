def get_video_state(page):

    return page.evaluate(
        """
        () => {

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            if (!v) {

                return {
                    exists: false,
                    time: null,
                    paused: true,
                    ended: true,
                    volume: 0
                };
            }

            return {
                exists: true,
                time: v.currentTime,
                paused: v.paused,
                ended: v.ended,
                volume: v.volume
            };
        }
        """
    )


def get_current_time(page):

    return page.evaluate(
        """
        () => {

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            return v
                ? v.currentTime
                : null;
        }
        """
    )


# ============================================================
# VOLUME
# ============================================================


# ============================================================
# PLAY / PAUSE
# ============================================================

def pause_video(page):

    page.evaluate(
        """
        () => {

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            if (v) {
                v.pause();
            }
        }
        """
    )


def play_video(page):

    page.evaluate(
        """
        () => {

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            if (v) {

                v.play().catch(
                    () => {}
                );
            }
        }
        """
    )


# ============================================================
# SEEK
# ============================================================

def seek_video(
    page,
    seconds
):

    page.evaluate(
        """
        (seconds) => {

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            if (v) {
                v.currentTime = seconds;
            }
        }
        """,
        seconds
    )


# ============================================================
# AUTOPLAY LOCK
# ============================================================

def lock_video_paused(page):

    """
    Prevent YouTube from starting the preloaded video.
    """

    page.evaluate(
        """
        () => {

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            if (!v) {
                return;
            }

            window.__DJ_PRELOADED = true;

            if (
                window.__DJ_PLAY_HANDLER
            ) {

                v.removeEventListener(
                    'play',
                    window.__DJ_PLAY_HANDLER
                );
            }

            window.__DJ_PLAY_HANDLER = () => {

                if (
                    window.__DJ_PRELOADED
                ) {

                    v.pause();
                }
            };

            v.addEventListener(
                'play',
                window.__DJ_PLAY_HANDLER
            );

            v.pause();
        }
        """
    )


def unlock_video(page):

    page.evaluate(
        """
        () => {

            window.__DJ_PRELOADED = false;

            const v =
                document.querySelector(
                    'video.html5-main-video'
                );

            if (v) {

                if (
                    window.__DJ_PLAY_HANDLER
                ) {

                    v.removeEventListener(
                        'play',
                        window.__DJ_PLAY_HANDLER
                    );

                    window.__DJ_PLAY_HANDLER = null;
                }
            }
        }
        """
    )

