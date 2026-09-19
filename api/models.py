"""Per-place model status and training endpoints.

Each PLACE_GENRES key gets its own model (models/<place>.pkl): it trains once
it has >= MIN_SAMPLES scored Songs rows and retrains every RETRAIN_AFTER new
records. These endpoints surface that state and let the UI trigger a retrain."""

import os

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
def list_models():
    from Music.preference import PLACE_GENRES
    from Model.linear_regression import (
        MIN_SAMPLES,
        RETRAIN_AFTER,
        scored_count,
        place_model_path,
        _load_meta,
    )

    meta = _load_meta()
    models = []
    for place, genres in PLACE_GENRES.items():
        records = scored_count(place)
        trained_on = (meta.get(place) or {}).get("trained_on_count")
        ready = records >= MIN_SAMPLES
        has_model = os.path.isfile(place_model_path(place))
        new_records = (records - trained_on) if trained_on is not None else None
        needs_retrain = ready and (new_records is None or new_records >= RETRAIN_AFTER)
        models.append({
            "place": place,
            "genres": genres,
            "records": records,
            "min_required": MIN_SAMPLES,
            "ready": ready,
            "has_model": has_model,
            "trained_on_count": trained_on,
            "new_records_since_train": new_records,
            "retrain_after": RETRAIN_AFTER,
            "needs_retrain": needs_retrain,
        })
    return {"models": models}


@router.post("/train/{place}")
def train_place(place: str):
    from Music.preference import PLACE_GENRES
    from Model.linear_regression import train_place_model

    if place not in PLACE_GENRES:
        raise HTTPException(404, f"Unknown place '{place}' — must be a PLACE_GENRES key.")
    try:
        train_place_model(place)
    except Exception as e:
        raise HTTPException(400, f"Training '{place}' failed: {e}")
    return {"ok": True, "place": place, "trained": True}


@router.post("/train-all")
def train_all():
    from Model.linear_regression import train_all_place_models

    return train_all_place_models()