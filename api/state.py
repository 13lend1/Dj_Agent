"""Shared runtime state across the API layer.

The running DJ instance lives here so the route modules (control, models)
don't need a global singleton of their own and tests can swap in a stub."""

dj = None
sink = None

# True when the server was started with DJ capability (i.e. not --no-dj).
# The deck is no longer started at boot: the UI must pick a place first, so
# the DJ only exists after POST /api/control/place.
allow_dj = False

# Playback tuning, mirrored from the server's CLI flags and handed to every
# DJ the UI starts.
pool_size = 40
top_n = 15

# Play through the machine's speakers instead of streaming to the browser.
speaker = False
