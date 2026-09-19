"""Playback + status endpoints. Every control just flips the same thread-safe
event the keyboard handler uses, so the UI never touches the audio loop."""

import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api import state

router = APIRouter(prefix="/api/control", tags=["control"])


class RateBody(BaseModel):
    rating: int = Field(ge=0, le=1, description="0 = like, 1 = dislike")


def _dj():
    dj = state.dj
    if dj is None:
        raise HTTPException(503, "DJ is not running (started with --no-dj).")
    return dj


@router.get("/status")
def status():
    """Now-playing snapshot: current song, elapsed, rating, place, and the next
    queued tracks. Never blocks on the audio loop — it reads state under the
    same lock the player uses."""
    dj = state.dj
    if dj is None:
        return {
            "dj_running": False,
            "playing": False,
            "place": None,
            "song": None,
            "previous": None,
            "queue": [],
            "resume_set": 0,
        }

    with dj.lock:
        song = dict(dj.current_song) if dj.current_song is not None else None
        previous = dict(dj.last_song) if dj.last_song is not None else None
        elapsed = dj.current_elapsed
        if elapsed is None and getattr(dj, "current_start_time", None) is not None:
            elapsed = time.time() - dj.current_start_time
        rating = dj.current_rating
        stopping = dj.stop_event.is_set()

    with getattr(dj, "batch_lock", _null_lock):
        batch = list(getattr(dj, "batch", []) or [])

    return {
        "dj_running": True,
        "playing": bool(song) and not stopping,
        "stopping": stopping,
        "place": getattr(dj, "place", None) or None,
        "song": {
            "title": song.get("name"),
            "artist": song.get("artist"),
            "genre": song.get("genre"),
            "place": song.get("place"),
            "duration_ms": song.get("duration"),
            "play_start_sec": song.get("play_start_sec"),
            "play_end_sec": song.get("play_end_sec"),
            "likeability": song.get("likeability"),
            "rating": rating,
            "elapsed_sec": round(elapsed, 1) if elapsed is not None else None,
        } if song else None,
        "previous": {
            "title": previous.get("name"),
            "artist": previous.get("artist"),
        } if previous else None,
        "queue": [
            {"title": (b.get("name") or b.get("title")), "artist": b.get("artist")}
            for b in batch[:10] if b
        ],
        "resume_set": len(getattr(dj, "_saved_set", []) or []),
    }


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_null_lock = _NullLock()


@router.post("/skip")
def skip():
    dj = _dj()
    dj.skip()
    return {"ok": True, "action": "skip"}


@router.post("/previous")
def previous():
    """Listen to the previous song again. A deliberate listen-back counts as a
    replay (strong like signal) in the playback loop."""
    dj = _dj()
    with dj.lock:
        has_previous = dj.last_song is not None
    if not has_previous:
        return {"ok": True, "action": "previous", "replayed": False}
    dj.replay()
    return {"ok": True, "action": "previous", "replayed": True}


@router.post("/restart")
def restart():
    dj = _dj()
    dj.restart()
    return {"ok": True, "action": "restart"}


@router.post("/seek-forward")
def seek_forward():
    dj = _dj()
    dj.seek_forward()
    return {"ok": True, "action": "seek-forward"}


@router.post("/seek-backward")
def seek_backward():
    dj = _dj()
    dj.seek_backward()
    return {"ok": True, "action": "seek-backward"}


@router.post("/rate")
def rate(body: RateBody):
    dj = _dj()
    dj.rate_current(body.rating)
    return {"ok": True, "rating": body.rating}


@router.post("/stop")
def stop():
    dj = _dj()
    dj.stop()
    return {"ok": True, "action": "stop"}


@router.get("/stream")
def stream():
    """Live MP3 of whatever the DJ is playing, for the UI's <audio> element.

    The DJ still mixes hooks/crossfades/effects; only the final output goes to
    the browser instead of the machine's speakers. Reconnecting (e.g. after
    restart/seek) naturally resumes from the live edge."""
    sink = state.sink
    if sink is None or not sink.alive:
        raise HTTPException(503, "no audio stream available (DJ not running).")
    sid, q = sink.subscribe()

    def gen():
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    return
                yield chunk
        except GeneratorExit:
            pass
        finally:
            sink.unsubscribe(sid)

    return StreamingResponse(
        gen(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )