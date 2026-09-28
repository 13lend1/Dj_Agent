"""DJ Agent web UI — serve the control UI and start the DJ once a place is
chosen in the browser.

    python api/server.py                     # DJ + UI on http://127.0.0.1:8000
    python api/server.py --resume-place car  # auto-start on that place (skip gate)
    python api/server.py --no-dj             # API + UI only (control returns 503)

The DJ does not start at boot: the UI shows a place gate and POST
/api/control/place starts the deck with the chosen place. Pass --resume-place
to skip the gate and start immediately (handy for scripted/CLI use).
"""

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


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
    parser.add_argument(
        "--no-hotkeys", action="store_true",
        help="do not register the global Ctrl+Alt hotkeys with this server",
    )
    args = parser.parse_args()

    from api import state

    state.allow_dj = not args.no_dj
    state.pool_size = args.pool_size
    state.top_n = args.top_n
    state.speaker = args.speaker

    if args.no_dj:
        print("API-only mode (--no-dj) — playback controls return 503.")
    elif args.resume_place:
        # Explicit command-line place: skip the gate and start straight away.
        from api import session

        print(f"Auto-starting DJ on place '{args.resume_place}' (skipping the UI gate)...")
        try:
            session.start(args.resume_place)
        except RuntimeError as exc:
            print("DJ start failed:", exc)
    else:
        print("Waiting for a place to be chosen in the UI before starting the DJ.")

    if not args.no_dj:
        if args.speaker:
            print("Audio output: machine speakers (--speaker).")
        else:
            print("Audio output: web browser stream (use --speaker for machine sound).")

    # Ride the global Ctrl+Alt+N/L/F hotkeys along with this server: they run
    # in a background thread of the SAME process (no extra window/terminal) and
    # die with the server. Disable with --no-hotkeys.
    if not args.no_hotkeys:
        try:
            from hotkeys import start_hotkey_thread
            start_hotkey_thread(host=args.host, port=args.port)
            print("Global hotkeys active: Ctrl+Alt+N skip, Ctrl+Alt+L like, "
                  "Ctrl+Alt+F full, Ctrl+Alt+P previous, Ctrl+Alt+D dislike, "
                  "Ctrl+Alt+Space pause (--no-hotkeys to disable).")
        except Exception as exc:
            print("Hotkeys unavailable:", exc)

    import uvicorn

    print(f"UI: http://{args.host}:{args.port}/")

    uvicorn.run(
        "api.app:app",
        host=args.host,
        port=args.port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()