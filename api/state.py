"""Shared runtime state across the API layer.

The running DJ instance lives here so the route modules (control, models)
don't need a global singleton of their own and tests can swap in a stub."""
dj = None
sink = None