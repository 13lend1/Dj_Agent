import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from category_encoders import TargetEncoder
from sklearn.utils.validation import check_is_fitted
import sqlite3

class LinearRegressionModel():
    def __init__(self):
        self.data = pd.DataFrame()
        self.model = LinearRegression()
        self.encoder = TargetEncoder()
        self.target = 'likeability'
        self.feature_cols = None  # remember column order used for fit

    def fit(self):
        conn = sqlite3.connect("Database/music.db")
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

    def predict(self, top_pct=0.2):
        check_is_fitted(self.model)  # raises NotFittedError if not fitted

        conn = sqlite3.connect("Database/music.db")
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