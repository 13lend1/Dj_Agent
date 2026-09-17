import pandas as pd
import sys, os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from Music.songs import DB_LOCK, _get_conn

with DB_LOCK:
    conn = _get_conn()
    df = pd.read_sql_query("SELECT * FROM Songs", conn)

df = df.drop(['id', 'link'], axis=1)

# Remove leading/trailing spaces from column names
df.columns = df.columns.str.strip()
# Remove commas from text fields
text_columns = df.select_dtypes(include=['object']).columns

for col in text_columns:
    df[col] = df[col].str.replace(',', '', regex=False)

df.to_csv("songs.csv", index=False)