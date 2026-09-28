import sys
import os
import json
import pickle
import time

import pandas as pd
from sklearn.linear_model import LinearRegression
from category_encoders import TargetEncoder
from sklearn.utils.validation import check_is_fitted

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


MIN_SAMPLES = 10
RETRAIN_AFTER = 15
TARGET = 'likeability'
WEIGHT = 'confidence'
# Explicit predictors — only these columns are used for training/prediction.
FEATURES = [
    'artist', 'album', 'genre', 'place', 'year', 'duration',
    'bpm', 'energy', 'danceability', 'valence',
    'acousticness', 'instrumentalness',
]

# One pickle per PLACE_GENRES key ("a model for each key on preference.py"),
# e.g. models/car.pkl, models/home.pkl, models/gym.pkl.
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
os.makedirs(MODEL_DIR, exist_ok=True)

# Records how many scored Songs rows each place's pickle was trained on, so a
# model is only retrained once RETRAIN_AFTER new records have accumulated.
_META_PATH = os.path.join(MODEL_DIR, "_place_meter.json")


class NotEnoughSamplesError(Exception):
    """Raised when a place's Songs rows are too few to train a model.
    Callers fall back to manual/random selection while samples accumulate."""


def place_model_path(place):
    """Pickle file for a place's model, e.g. models/home.pkl."""
    key = (place or "default").strip().lower().replace(" ", "_") or "default"
    return os.path.join(MODEL_DIR, f"{key}.pkl")


def _load_meta():
    try:
        with open(_META_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_meta(meta):
    with open(_META_PATH, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)


def scored_count(place):
    """Rows in Songs that actually train a place's model: they carry both a
    real likeability (listening signal) and a confidence weight."""
    from Music.songs import DB_LOCK, _get_conn
    with DB_LOCK:
        conn = _get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM Songs WHERE place = ? "
            "AND likeability IS NOT NULL AND confidence IS NOT NULL",
            (place,),
        ).fetchone()
        return int(row[0] or 0)


def train_place_model(place):
    """Trains a fresh model on the place's scored Songs rows, writes its pickle
    (overwriting any previous one) and records how many records it saw, so the
    next retrain waits until RETRAIN_AFTER more arrive. Raises
    NotEnoughSamplesError below MIN_SAMPLES.*"""
    model = LinearRegressionModel()
    model.fit(place=place)
    path = model.save(place)
    meta = _load_meta()
    meta[place] = {
        "trained_on_count": scored_count(place),
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_meta(meta)
    print(f"[model] Trained '{place}' ({meta[place]['trained_on_count']} scored "
          f"records) -> {path}")
    return model


def load_place_model(place):
    """Loads a place's fitted model pickle, or None when it has not been
    trained yet."""
    return LinearRegressionModel.load(place)


def delete_place_model(place):
    """Removes a place's model: the pickle and its _place_meter.json entry.

    Both halves matter. A stale pickle would be happily reloaded by
    ensure_place_model() if the same place name is ever created again, and a
    leftover meter entry would make a future model look "already trained on N
    records" and suppress the retrain. Returns True when something was deleted.
    """
    if not place:
        return False
    key = (place or "").strip().lower()
    removed = False

    path = place_model_path(place)
    try:
        os.remove(path)
        removed = True
        print(f"[model] Deleted model {path}", flush=True)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[model] Could not delete {path}: {exc}", flush=True)

    meta = _load_meta()
    if key in meta:
        meta.pop(key, None)
        _save_meta(meta)
        removed = True
    return removed


def ensure_place_model(place):
    """Returns the best currently-available fitted model for a place, or None
    when no model exists yet (fewer than MIN_SAMPLES scored records — callers
    fall back to random selection).

      * place is None        -> train the legacy global model over all Songs.
      * < MIN_SAMPLES         -> no model yet, wait for more records.
      * pickle saved and
        fewer than RETRAIN_AFTER (15) new records since it was trained
                             -> load the pickle (fast, no re-fit).
      * otherwise            -> retrain on the place's current rows and
                                overwrite the pickle."""
    if not place:
        model = LinearRegressionModel()
        model.fit()
        return model

    current = scored_count(place)
    if current < MIN_SAMPLES:
        return None

    trained_on = _load_meta().get(place, {}).get("trained_on_count") or 0
    if trained_on and (current - trained_on) < RETRAIN_AFTER:
        saved = load_place_model(place)
        if saved is not None:
            return saved
    return train_place_model(place)


def train_all_place_models():
    """Ensures a model exists for every PLACE_GENRES key that has at least
    MIN_SAMPLES scored records — training fresh where missing/stale, loading
    the pickle otherwise. Returns a {place: status} summary."""
    from Music.preference import PLACE_GENRES

    summary = {}
    for place in PLACE_GENRES:
        current = scored_count(place)
        if current < MIN_SAMPLES:
            summary[place] = f"skipped ({current}/{MIN_SAMPLES} scored records)"
            continue
        before = _load_meta().get(place, {}).get("trained_on_count")
        ensure_place_model(place)
        after = _load_meta().get(place, {}).get("trained_on_count")
        summary[place] = "trained" if after and after != before else "loaded"
    return summary


class LinearRegressionModel():
    def __init__(self):
        self.data = pd.DataFrame()
        self.model = LinearRegression()
        self.encoder = TargetEncoder()
        self.target = TARGET
        self.cat_cols = None  # categorical feature columns at fit time
        self.num_cols = None  # numeric feature columns at fit time
        self.feature_cols = None  # full column order used for fit
        self.place = None  # place this model was fit for (None -> global)

    def __getstate__(self):
        # Keep pickles small: the raw Songs snapshot is only needed at fit
        # time, never for prediction.
        state = self.__dict__.copy()
        state['data'] = pd.DataFrame()
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        if not hasattr(self, 'place'):
            self.place = None

    def _read_table(self, table, place=None):
        """Read a (place-filtered) table through songs.py's shared locked connection."""
        from Music.songs import DB_LOCK, _get_conn
        with DB_LOCK:
            conn = _get_conn()
            if place:
                return pd.read_sql_query(
                    f"SELECT * FROM {table} WHERE place = ?", conn, params=(place,))
            return pd.read_sql_query(f"SELECT * FROM {table}", conn)

    def fit(self, place=None):
        self.place = place
        self.data = self._read_table("Songs", place=place)
        if self.data is None or self.data.empty:
            raise NotEnoughSamplesError(
                "No 'Songs' rows for %s — at least %d samples are needed."
                % (place if place else "training", MIN_SAMPLES)
            )

        n_samples = len(self.data)
        if n_samples < MIN_SAMPLES:
            raise NotEnoughSamplesError(
                "Only %d song sample(s) for '%s' — need at least %d; "
                "the model will train once enough are collected."
                % (n_samples, place if place else "training", MIN_SAMPLES)
            )

        # Keep exactly the explicit predictors + target + confidence weight.
        cols = [c for c in FEATURES if c in self.data.columns]
        if place and 'place' in cols:
            cols.remove('place')  # constant for a per-place model — useless as a feature
        missing = [c for c in FEATURES if c not in self.data.columns]
        if missing:
            print(f"[model] Missing predictors (dropped from training): {missing}")
        if not cols:
            raise ValueError("No usable predictor columns in the Songs table.")

        df = self.data[[c for c in cols if c in self.data.columns]].copy()
        df[self.target] = self.data[self.target]

        # Artist/album/genre may be unknown for singles/solo tracks — fill them
        # with "unknown" so those rows still train and get a sensible encoded
        # value instead of being silently dropped.
        for col in cols:
            if df[col].dtype == object:
                df[col] = df[col].fillna("unknown")

        # Confidence is the sample weight: rows without a signal (NULL) carry
        # no weight and are excluded from training.
        if WEIGHT not in self.data.columns:
            raise ValueError("Songs table has no 'confidence' column to weight by.")
        df[WEIGHT] = pd.to_numeric(self.data[WEIGHT], errors="coerce")
        df.dropna(inplace=True)
        if len(df) < MIN_SAMPLES:
            print(f"[model] Only {len(df)} weighted sample(s) after cleanup — "
                  f"using what is available.")
        if df.empty:
            raise ValueError("No training rows left after cleaning Songs.")

        y = df[self.target]
        X = df[cols]
        weights = df[WEIGHT]

        self.cat_cols = X.select_dtypes(include=['object']).columns.tolist()
        self.num_cols = X.select_dtypes(include=['number']).columns.tolist()

        encoded = self.encoder.fit_transform(X[self.cat_cols], y)
        X_final = pd.concat([encoded, X[self.num_cols]], axis=1)
        self.feature_cols = X_final.columns.tolist()

        self.model.fit(X_final, y, sample_weight=weights.values)
        return self

    def save(self, place=None):
        """Pickles the fitted model to models/<place>.pkl, overwriting any
        existing model for that place. Returns the path written."""
        path = place_model_path(place or self.place)
        with open(path, "wb") as fh:
            pickle.dump(self, fh)
        return path

    @classmethod
    def load(cls, place):
        """Loads a place's fitted model pickle, or None when nothing is saved
        for it yet."""
        path = place_model_path(place)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            model = pickle.load(fh)
        model.place = place
        return model

    def _align(self, frame):
        """Builds a prediction frame from any song table/dict list using the
        exact fit-time column order, coercing numerics to float (rows that come
        from SQLite may have None -> object dtypes otherwise)."""
        X = frame.reindex(columns=self.feature_cols)
        for col in self.num_cols:
            if col in X.columns:
                X[col] = pd.to_numeric(X[col], errors='coerce')
        # mirror fit(): missing categorical values become "unknown" so unseen
        # artists/albums encode like they did during training
        for col in self.cat_cols:
            if col in X.columns:
                X[col] = X[col].fillna("unknown")
        encoded = self.encoder.transform(X[self.cat_cols])
        X_final = pd.concat([encoded, X[self.num_cols]], axis=1)
        return X_final

    def predict(self, top_pct=0.2):
        """Scores the Preprocessed table, returns the top top_pct% (default 20%)
        ranked by the HIGHEST predicted likeability.

        Predictions are used only for ranking here — they are never persisted
        into the Songs table as likeability. That column is written exclusively
        from real listening signals via songs.save()."""
        check_is_fitted(self.model)  # raises NotFittedError if not fitted

        songs = self._read_table("Preprocessed")
        if songs.empty:
            return songs

        X_pred = self._align(songs)
        valid = X_pred.dropna().index
        if len(valid) == 0:
            # nothing scoreable (missing features) -> return empty
            return songs.iloc[0:0]

        preds = pd.Series(self.model.predict(X_pred.loc[valid]), index=valid)

        result = songs.loc[valid].copy()
        result[self.target] = preds

        # top top_pct (default 20%) by the HIGHEST predicted likeability
        n_top = max(1, int(len(result) * top_pct))
        top = result.sort_values(self.target, ascending=False).head(n_top)
        return top

    def select_best(self, candidates, n=20):
        """Scores an in-memory list of song dicts (e.g. taken out of Preprocessed)
        and returns the top-n as a DataFrame with predicted likeability.

        Predictions are used only for ranking here — they are never persisted
        into the Songs table as likeability. That column is written exclusively
        from real listening signals via songs.save(), which computes
        likeability/confidence from end_reason, skipp, liked, replayed, saved."""
        check_is_fitted(self.model)  # raises NotFittedError if not fitted

        songs = pd.DataFrame(candidates)
        if songs.empty:
            return songs

        X_pred = self._align(songs)
        valid = X_pred.dropna().index
        if len(valid) == 0:
            # nothing scoreable (missing features) -> return empty so caller falls back
            return songs.iloc[0:0]

        preds = self.model.predict(X_pred.loc[valid])

        result = songs.loc[valid].copy()
        result[self.target] = preds
        return result.sort_values(self.target, ascending=False).head(n)


if __name__ == "__main__":
    summary = train_all_place_models()
    for place, status in summary.items():
        print(f"  {place}: {status}")
    print("Per-place models saved in", MODEL_DIR)