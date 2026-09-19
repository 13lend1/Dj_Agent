"""DJ Agent web UI — run the DJ in the background and serve the control UI.

    python api/server.py                     # DJ + UI on http://127.0.0.1:8000
    python api/server.py --resume-place car  # run only that place's model
    python api/server.py --no-dj             # API + UI only (control returns 503)

Flags mirror Music/dj.py so the playback behaviour is identical to the CLI.
"""

import argparse
import os
import sys
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _boot_dj(dj, sink=None):
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


def main():
    parser = argparse.ArgumentParser(description="DJ Agent web UI")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", default=8000, type=int, help="port (default 8000)")
    parser.add_argument("--pool-size", default=40, type=int)
    parser.add_argument("--top-n", default=15, type=int)
    parser.add_argument(
        "--resume", dest="resume_unplayed", action="store_true", default=None,
        help="resume unplayed tracks from the last Agent run (default)",
    )
    parser.add_argument(
        "--no-resume", dest="resume_unplayed", action="store_false",
        help="skip saved unplayed tracks and always select fresh agent sets",
    )
    parser.add_argument(
        "--resume-place", dest="resume_place", default=None,
        help="resume only unplayed tracks flagged with a PLACE_GENRES key",
    )
    parser.add_argument(
        "--no-dj", action="store_true",
        help="serve the API/UI only (control endpoints return 503)",
    )
    parser.add_argument(
        "--speaker", action="store_true",
        help="play sound through the machine's speakers instead of the web browser",
    )
    args = parser.parse_args()

    from api import state

    if not args.no_dj:
        from Music.dj import DJ
        from Music.streamsink import StreamSink

        dj = DJ(pool_size=args.pool_size, top_n=args.top_n,
                resume_unplayed=args.resume_unplayed,
                resume_place=args.resume_place)
        state.dj = dj
        if args.speaker:
            sink = None
            print("Audio output: machine speakers (--speaker).")
        else:
            sink = StreamSink()
            state.sink = sink
            print("Audio output: web browser stream (use --speaker for machine sound).")
        role = f"place='{args.resume_place}'" if args.resume_place else "all places"
        print(f"Starting DJ in the background ({role})...")
        threading.Thread(target=_boot_dj, args=(dj, sink), daemon=True).start()
    else:
        print("API-only mode (--no-dj) — playback controls return 503.")

    import uvicorn

    print(f"UI: http://{args.host}:{args.port}/")
    uvicorn.run("api.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()