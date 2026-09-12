import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from category_encoders import TargetEncoder
from sklearn.utils.validation import check_is_fitted


class LinearRegressionModel():
    def __init__(self):
        self.data = pd.DataFrame()
        self.model = LinearRegression()
        self.encoder = TargetEncoder()
        self.target = 'likeability'
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
        # Artist/album/genre may be unknown for singles/solo tracks — give them
        # a placeholder so those rows still train (and get a sensible encoded
        # value) instead of being silently dropped.
        for col in self.data.columns:
            if self.data[col].dtype == object and col != self.target:
                self.data[col] = self.data[col].fillna("unknown")
        self.data.dropna(inplace=True)
        y = self.data[self.target]
        # id & link are unique per-row identifiers, NOT predictors: including
        # them makes target-encoding memorize the label (a leak that collapses
        # predictions on unseen songs to the mean, e.g. a constant 0.379).
        # name is ~unique per row as well, so it's dropped too.
        # artist & album are intentionally KEPT: they carry real signal (who
        # made it, what record it came from), and the TargetEncoder's smoothed
        # target encoding is designed for high-cardinality categories like these.
        X = self.data.drop(self.target, axis=1)\
                     .drop(columns=["id", "link", "name"], errors="ignore")

        # any remaining categorical with near-unique values is still a leak:
        # drop columns whose cardinality is more than half the training rows —
        # but never artist/album, which are explicitly requested predictors.
        protected = {"artist", "album"}
        cat_candidates = X.select_dtypes(include=['object']).columns.tolist()
        n = len(X)
        for col in cat_candidates:
            if col in protected:
                continue
            if X[col].nunique() > max(2, n // 2):
                X = X.drop(columns=[col])

        self.cat_cols = X.select_dtypes(include=['object']).columns.tolist()
        self.num_cols = X.select_dtypes(include=['number']).columns.tolist()

        self.encoder.set_params(cols=self.cat_cols)
        encoded = self.encoder.fit_transform(X[self.cat_cols], y)
        X_final = pd.concat([encoded, X[self.num_cols]], axis=1)
        self.feature_cols = X_final.columns.tolist()

        self.model.fit(X_final, y)
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
        ranked by the HIGHEST predicted likeability, and persists their predicted
        likeability into the Songs table."""
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

        # persist the predicted likeability of the selected songs into Songs
        self.save_predictions(top)
        return top

    def save_predictions(self, top):
        """Saves each returned song into the Songs table with its predicted
        likeability as the likeability value (upsert, re-running just updates)."""
        try:
            from songs import save_song_metadata
        except ImportError:
            from Music.songs import save_song_metadata

        for song in top.to_dict('records'):
            likeability = song.get(self.target)
            if likeability is None or likeability != likeability:  # skip NaN
                continue
            try:
                save_song_metadata(song, likeability=float(likeability))
            except Exception as e:
                print("Save predicted likeability failed:", e)

    def select_best(self, candidates, n=20):
        """Scores an in-memory list of song dicts (e.g. taken out of Preprocessed)
        and returns the top-n as a DataFrame with predicted likeability."""
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