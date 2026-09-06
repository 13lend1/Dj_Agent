import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from category_encoders import TargetEncoder
from sklearn.utils.validation import check_is_fitted
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Database", "music.db")


def _open_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

class LinearRegressionModel():
    def __init__(self):
        self.data = pd.DataFrame()
        self.model = LinearRegression()
        self.encoder = TargetEncoder()
        self.target = 'likeability'
        self.feature_cols = None  # remember column order used for fit

    def fit(self):
        conn = _open_db()
        self.data = pd.read_sql_query("SELECT * FROM Songs", conn)
        conn.close()
        self.data.dropna(inplace=True)
        y = self.data[self.target]
        X = self.data.drop(self.target, axis=1)

        objects = X.select_dtypes(include=['object'])
        numeric = X.select_dtypes(include=['number'])

        self.encoder.set_params(cols=objects.columns.tolist())
        encoded = self.encoder.fit_transform(objects, y)

        X_final = pd.concat([encoded, numeric], axis=1)
        self.feature_cols = X_final.columns.tolist()

        self.model.fit(X_final, y)
        return self

    def predict(self, top_pct=0.2):
        check_is_fitted(self.model)  # raises NotFittedError if not fitted

        conn = _open_db()
        songs = pd.read_sql_query("SELECT * FROM Preprocessed", conn)
        conn.close()

        objects = songs.select_dtypes(include=['object'])
        numeric = songs.select_dtypes(include=['number'])
        encoded = self.encoder.transform(objects)

        X_pred = pd.concat([encoded, numeric], axis=1)
        X_pred = X_pred[self.feature_cols]  # match fit-time column order

        preds = self.model.predict(X_pred)

        result = songs.copy()
        result[self.target] = preds

        n_top = max(1, int(len(result) * top_pct))
        result = result.sort_values(self.target, ascending=False).head(n_top)

        return result

    def select_best(self, candidates, n=20):
        """Scores an in-memory list of song dicts (e.g. taken out of Preprocessed)
        and returns the top-n as a DataFrame with predicted likeability."""
        check_is_fitted(self.model)  # raises NotFittedError if not fitted

        songs = pd.DataFrame(candidates)
        if songs.empty:
            return songs

        objects = songs.select_dtypes(include=['object'])
        numeric = songs.select_dtypes(include=['number'])

        if not objects.empty:
            encoded = self.encoder.transform(objects)
            X_pred = pd.concat([encoded, numeric], axis=1)
        else:
            X_pred = numeric

        X_pred = X_pred.reindex(columns=self.feature_cols)  # match fit-time columns

        valid = X_pred.dropna().index
        if len(valid) == 0:
            # nothing scoreable (missing features) -> return empty so caller falls back
            return songs.iloc[0:0]

        preds = self.model.predict(X_pred.loc[valid])

        result = songs.loc[valid].copy()
        result[self.target] = preds
        return result.sort_values(self.target, ascending=False).head(n)