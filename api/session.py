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

# How many songs the click that picks a place should queue straight away.
#
# The first batch is the only part of startup that skips the slow pipeline
# (pool fill -> model retrain -> hook resolution -> Gemini ordering), so it is
# the buffer that decides whether the deck keeps playing or stalls waiting for
# the second batch. Too small and the user hears two songs and then silence
# while the AI path catches up; too large and a cold place sits waiting for
# candidates to be discovered before it can start at all. Sized to cover a few
# minutes of playback.
FIRST_BATCH_N = 8


def _start_profile(place):
    """Pick (pool_size, top_n, first_batch_n) for a fresh DJ.

    Nothing is pre-warmed: the fill happens on demand, targeted at the place that
    was just clicked. A cold place (no songs of its genres preprocessed anywhere)
    gets a smaller top_n so the normal batches are quick to build, but the same
    first-batch burst, because starting on time matters more there than anywhere
    else. Any place with a pool continues with the configured values.
    """
    try:
        from Music.songs import preprocessed_count
        from Music.preference import PLACE_GENRES
        # The deck now draws from EVERY place's preprocessed pool, but only
        # tracks whose genre is part of this place's list, so "cold" means no
        # songs of the place's genres are ready anywhere.
        genres = PLACE_GENRES.get(place) if place else None
        empty = preprocessed_count(genres=genres) == 0
    except Exception:
        empty = False
    if empty:
        return state.pool_size, min(state.top_n, FIRST_BATCH_N), FIRST_BATCH_N
    return state.pool_size, state.top_n, FIRST_BATCH_N


def _is_cold(place):
    """The place has nothing preprocessed yet (i.e. the next start is a trial
    fast-start that plays the first fetched songs raw, no model/agent)."""
    try:
        from Music.songs import preprocessed_count
        from Music.preference import PLACE_GENRES
        genres = PLACE_GENRES.get(place) if place else None
        return preprocessed_count(genres=genres) == 0
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
            pool_size, top_n, first_batch_n = _start_profile(place)
            dj = DJ(pool_size=pool_size, top_n=top_n, resume_place=place,
                    trial=_is_cold(place), first_batch_n=first_batch_n)
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
