"""DJ lifecycle for the web UI.

The deck is deliberately NOT started at server boot: the UI must choose a place
first (the startup gate). Changing place is always Stop -> pick -> Start, so a
stopped deck can never keep streaming the old place. A stopped DJ cannot be
restarted (its stop_event is latched and its worker threads have exited), so
every start builds a fresh DJ and StreamSink.
"""

import threading

from api import state

_start_lock = threading.Lock()

# A place with nothing preprocessed yet must reach its first track fast: the
# batch worker's first-batch threshold is max(top_n, 3), so a cold place with
# the default top_n=15 waits for 15 freshly-discovered songs. Cap that at
# _FAST_START_TOP_N so a songless place starts as soon as ~8 are ready.
# Places that already have a pool keep the configured profile (default 40/15).
_FAST_START_TOP_N = 8


def _start_profile(place):
    """Pick (pool_size, top_n) for a fresh DJ based on whether `place` already
    has preprocessed songs. A cold place gets the fast-start top_n; any place
    with a pool continues with the old configured values."""
    try:
        from Music.songs import preprocessed_count
        empty = preprocessed_count(place=place) == 0
    except Exception:
        empty = False
    if empty:
        return state.pool_size, min(state.top_n, _FAST_START_TOP_N)
    return state.pool_size, state.top_n


def _is_cold(place):
    """The place has nothing preprocessed yet (i.e. the next start is a trial
    fast-start that plays the first fetched songs raw, no model/agent)."""
    try:
        from Music.songs import preprocessed_count
        return preprocessed_count(place=place) == 0
    except Exception:
        return False


def _boot(dj, sink):
    """Mirror Music/dj.py __main__: resume saved unplayed tracks when present,
    otherwise hand the deck to the batch pipeline. Blocks until a first song
    is available, hence the background thread."""
    try:
        with dj._saved_lock:
            saved_count = len(dj._saved_set)
        first_song = dj.get_next_song() if saved_count else None
        # keyboard=False: all control happens through the web UI, not the
        # terminal — no msvcrt/termios listener is started. sink != None sends
        # the audio to the browser instead of the machine speaker.
        dj.start(first_song, dj.get_next_song, keyboard=False, sink=sink)
    except Exception as e:
        print("DJ boot failed:", e)


def is_running():
    return state.dj is not None


def start(place=None):
    """Create and start a fresh DJ for `place`. Raises RuntimeError when a DJ is
    already running or the server has no DJ capability (--no-dj)."""
    if not state.allow_dj:
        raise RuntimeError("DJ playback is unavailable (server started with --no-dj).")
    with _start_lock:
        if state.dj is not None:
            raise RuntimeError("the DJ is already running — press Stop before switching place")
        try:
            from Music.dj import DJ

            if state.speaker:
                sink = None
            else:
                from Music.streamsink import StreamSink

                sink = StreamSink()
            pool_size, top_n = _start_profile(place)
            dj = DJ(pool_size=pool_size, top_n=top_n, resume_place=place,
                    trial=_is_cold(place))
        except Exception as exc:
            raise RuntimeError(f"could not start the DJ: {exc}")
        state.sink = sink
        state.dj = dj
        threading.Thread(target=_boot, args=(dj, sink), daemon=True).start()
        return dj


def stop():
    """Stop the DJ and return to the 'choose a place' state. Returns True when a
    running DJ was stopped, False when there was nothing to stop."""
    with _start_lock:
        dj = state.dj
        if dj is None:
            return False
        try:
            dj.stop()
        except Exception as e:
            print("DJ stop failed:", e)
        thread = getattr(dj, "player_thread", None)
        if thread is not None:
            thread.join(timeout=10)
        state.dj = None
        return True
