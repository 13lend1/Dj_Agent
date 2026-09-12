import sqlite3
import re

NON_GENRE_TABLES = {"Songs", "Preprocessed"}


def _table_name(genre: str) -> str:
    """Safe SQL table identifier derived from a genre name ('hip-hop' -> 'hip_hop')."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", genre or "")
    if not name:
        raise ValueError(f"Invalid genre name: {genre!r}")
    return name


def genre_table(genre: str) -> None:
    """Ensures the per-genre table exists (id TEXT PRIMARY KEY, name TEXT NOT NULL)."""
    table = _table_name(genre)
    from songs import DB_LOCK, _get_conn
    with DB_LOCK:
        conn = _get_conn()
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, name TEXT NOT NULL);"
        )


def save_genre(song: dict, genre: str) -> bool:
    """Records a played song (id, name) into its genre table. Returns True if it
    was newly recorded, False if it had already been added."""
    table = _table_name(genre)
    genre_table(genre)
    from songs import DB_LOCK, _get_conn
    try:
        with DB_LOCK:
            conn = _get_conn()
            conn.execute(
                f"INSERT INTO {table}(id, name) VALUES(?, ?)",
                (song['id'], song['name']),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def genre_tables() -> list[str]:
    """Names of every per-genre (played-song) table currently in the database."""
    from songs import DB_LOCK, _get_conn
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows if r[0] not in NON_GENRE_TABLES]


def is_played(song_id: str) -> bool:
    """True if song_id already appears in any genre table (i.e. already played)."""
    tables = genre_tables()
    if not tables:
        return False
    query = " UNION ".join(f'SELECT id FROM "{t}" WHERE id = ?' for t in tables)
    from songs import DB_LOCK, _get_conn
    with DB_LOCK:
        conn = _get_conn()
        row = conn.execute(query, (song_id,) * len(tables)).fetchone()
    return row is not None


def played_ids(song_ids) -> set:
    """Returns the subset of song_ids already present in the genre tables."""
    ids = list(song_ids)
    tables = genre_tables()
    if not tables or not ids:
        return set()
    query = " UNION ".join(f'SELECT id FROM "{t}"' for t in tables)
    from songs import DB_LOCK, _get_conn
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute(query).fetchall()
    known = {r[0] for r in rows}
    return known & set(ids)