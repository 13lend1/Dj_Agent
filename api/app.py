"""FastAPI app for the DJ web UI.

    GET  /api/control/status   now-playing snapshot
    POST /api/control/{skip,full,restart,seek-forward,seek-backward,stop}
    POST /api/control/{pause,resume}
    POST /api/control/rate     {"rating": 0|1}
    GET  /api/control/places   selectable places + the active one
    POST /api/control/place    choose a place (starts the DJ) or create one
                               {"place": name, "genres": [...]}  (genres = create)
                               returns 409 while the DJ runs — Stop first
    DELETE /api/control/place  delete a CUSTOM place, its songs and its model
                               ?place=name — 404 if unknown or built-in
    GET  /api/models            per-place model train/retrain state
    POST /api/models/train/{place}
    POST /api/models/train-all

The UI lives in ui/ (HTML/CSS/JS) and is served from "/".
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from api import control, models

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Clean shutdown: stop the DJ and its StreamSink when the server exits.

    Without this, one Ctrl+C makes uvicorn wait for the browser's open /stream
    connection (which only ends when the sink closes), so the first press hangs
    and you end up mashing Ctrl+C to force-quit. Stopping the DJ here closes the
    sink (-> every /stream subscriber gets a None chunk and the response ends),
    and timeout_graceful_shutdown in api/server.py guarantees exit even if a
    subscriber never drains."""
    try:
        yield
    finally:
        from api import session, state

        try:
            session.stop()
        except Exception as exc:
            print("DJ shutdown cleanup failed:", exc)
        sink = state.sink
        if sink is not None:
            try:
                sink.close()
            except Exception as exc:
                print("StreamSink shutdown cleanup failed:", exc)


app = FastAPI(title="DJ Agent", version="0.1.0", lifespan=lifespan)
app.include_router(control.router)
app.include_router(models.router)


@app.api_route(
    "/api/{paths:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    tags=["api"],
)
async def api_unknown(paths: str, request: Request):
    """Unknown /api/* path — never let it fall through to the static file
    mount (which would 405 on POSTs and hide the real cause)."""
    return JSONResponse(
        status_code=404,
        content={"detail": f"unknown API endpoint: /api/{paths}", "method": request.method},
    )


app.mount("/", StaticFiles(directory=os.path.join(APP_ROOT, "ui"), html=True), name="ui")