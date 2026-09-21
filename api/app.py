"""FastAPI app for the DJ web UI.

    GET  /api/control/status   now-playing snapshot
    POST /api/control/{skip,previous,restart,seek-forward,seek-backward,stop}
    POST /api/control/{pause,resume}
    POST /api/control/rate     {"rating": 0|1}
    GET  /api/control/places   selectable places + the active one
    POST /api/control/place    choose a place (starts the DJ) or create one
                               {"place": name, "genres": [...]}  (genres = create)
                               returns 409 while the DJ runs — Stop first
    GET  /api/models            per-place model train/retrain state
    POST /api/models/train/{place}
    POST /api/models/train-all

The UI lives in ui/ (HTML/CSS/JS) and is served from "/".
"""

import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from api import control, models

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title="DJ Agent", version="0.1.0")
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