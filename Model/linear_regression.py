import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from category_encoders import TargetEncoder
from sklearn.utils.validation import check_is_fitted


MIN_SAMPLES = 50
TARGET = 'likeability'
WEIGHT = 'confidence'
# Explicit predictors — only these columns are used for training/prediction.
FEATURES = [
    'artist', 'album', 'genre', 'place', 'year', 'duration',
    'bpm', 'energy', 'danceability', 'valence',
    'acousticness', 'instrumentalness',
]


class NotEnoughSamplesError(Exception):
    """Raised when the Songs table has too few rows to train a model.
    Callers fall back to manual/random selection while samples accumulate."""


class LinearRegressionModel():
    def __init__(self):
        self.data = pd.DataFrame()
        self.model = LinearRegression()
        self.encoder = TargetEncoder()
        self.target = TARGET
        self.cat_cols = None  # categorical feature columns at fit time
        self.num_cols = None  # numeric feature columns at fit time
        self.feature_cols = None  # full column order used for fit

    def _read_table(self, table):
        """Read a table through songs.py's shared locked connection."""
        from Music.songs import DB_LOCK, _get_conn
        with DB_LOCK:
            conn = _get_conn()
            return pd.read_sql_query(f"SELECT * FROM {table}", conn)

    def fit(self):
        self.data = self._read_table("Songs")
        if self.data is None or self.data.empty:
            raise NotEnoughSamplesError(
                "Songs table is empty — at least %d samples are needed." % MIN_SAMPLES
            )

        n_samples = len(self.data)
        if n_samples < MIN_SAMPLES:
            raise NotEnoughSamplesError(
                "Only %d song sample(s) so far — need at least %d; "
                "the model will train once enough are collected." % (n_samples, MIN_SAMPLES)
            )

        # Keep exactly the explicit predictors + target + confidence weight.
        cols = [c for c in FEATURES if c in self.data.columns]
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