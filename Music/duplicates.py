import sqlite3
import re

NON_GENRE_TABLES = {"Songs", "Preprocessed", "Agent"}


def _songs_mod():
    """The canonical songs module. Prefers Music.songs (the same module object
    the API and DJ use, so they share one connection and one lock); falls back
    to a bare `songs` import for standalone Music/duplicates.py runs, which is
    how the existing genre_table/save_genre paths already import it."""
    try:
        from Music import songs
        return songs
    except ImportError:
        import songs
        return songs


# How many ids a single genre's played table keeps. These tables are an
# append-only "have I played this?" set, so without a cap they grow forever and
# a genre eventually has every song it will ever find marked played — at which
# point played_ids() filters the fresh batch down to nothing and the genre can
# only be served by recycling what was already played. Capping each genre at a
# recent window keeps the "don't repeat yourself" guarantee meaningful for a
# long time before a song becomes eligible again.
GENRE_HISTORY_CAP = 1000


def _table_name(genre: str) -> str:
    """Safe SQL table identifier derived from a genre name ('hip-hop' -> 'hip_hop')."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", genre or "")
    if not name:
        raise ValueError(f"Invalid genre name: {genre!r}")
    return name


def genre_table(genre: str) -> None:
    """Ensures the per-genre table exists (id TEXT PRIMARY KEY, name TEXT NOT NULL)."""
    table = _table_name(genre)
    s = _songs_mod()
    with s.DB_LOCK:
        conn = s._get_conn()
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, name TEXT NOT NULL);"
        )


def save_genre(song: dict, genre: str) -> bool:
    """Records a played song (id, name) into its genre table. Returns True if it
    was newly recorded, False if it had already been added.

    The table is trimmed to GENRE_HISTORY_CAP on every insert, so a genre can
    never accumulate an unbounded played history."""
    table = _table_name(genre)
    genre_table(genre)
    s = _songs_mod()
    try:
        with s.DB_LOCK:
            conn = s._get_conn()
            conn.execute(
                f"INSERT INTO {table}(id, name) VALUES(?, ?)",
                (song['id'], song['name']),
            )
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        # Trim outside the insert's lock so a duplicate-key bail-out still
        # enforces the cap, and so the DELETE cannot run mid-insert.
        trim_genre_table(genre)


def trim_genre_table(genre: str, cap: int = None) -> int:
    """Trims a genre's played table down to the newest `cap` ids, dropping the
    oldest beyond it. Returns the number of rows removed (0 is normal).

    Recency comes from the implicit rowid: these tables declare only
    `id TEXT PRIMARY KEY` (not WITHOUT ROWID), so rowid is the insertion
    sequence and ascends with each play. Deleting the low rowids therefore
    evicts the least-recently played songs, which is the set that should become
    eligible for fresh play again first.
    """
    table = _table_name(genre)
    limit = GENRE_HISTORY_CAP if cap is None else max(0, int(cap))
    s = _songs_mod()
    try:
        with s.DB_LOCK:
            conn = s._get_conn()
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                return 0
            # Only rows beyond the newest `limit` are candidates for removal.
            cur = conn.execute(
                f"DELETE FROM {table} WHERE id IN ("
                f"  SELECT id FROM {table} ORDER BY rowid DESC LIMIT -1 OFFSET ?"
                f")",
                (limit,),
            )
            return cur.rowcount
    except sqlite3.Error as e:
        print(f"Trim genre table {table} failed:", e)
        return 0


def trim_all_genre_tables(cap: int = None) -> dict:
    """Applies the cap to every per-genre table. Returns {table: rows_removed}.

    Used on startup so an existing database is brought under the cap once,
    instead of waiting for each genre to be played again."""
    trimmed = {}
    for table in genre_tables():
        try:
            removed = trim_genre_table(table, cap)
        except ValueError:
            continue
        if removed:
            trimmed[table] = removed
    return trimmed


def genre_tables() -> list[str]:
    """Names of every per-genre (played-song) table currently in the database."""
    s = _songs_mod()
    with s.DB_LOCK:
        conn = s._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows if r[0] not in NON_GENRE_TABLES]


def count_orphans(genre: str = None) -> int:
    """Counts played-marks with no matching Songs row (read-only, no deletes)."""
    s = _songs_mod()
    tables = [genre] if genre else genre_tables()
    total = 0
    with s.DB_LOCK:
        conn = s._get_conn()
        for t in tables:
            try:
                total += conn.execute(
                    f'SELECT COUNT(*) FROM "{t}" WHERE id NOT IN '
                    f'(SELECT id FROM Songs)'
                ).fetchone()[0]
            except (ValueError, sqlite3.Error):
                continue
    return total


def prune_orphans(genre: str = None) -> int:
    """Deletes played-marks whose Songs row no longer exists. Returns rows removed.

    An orphan is a mark for a song that was played but never scored: its Songs
    row was removed by delete_unscored_songs() while the genre mark survived.
    Such a mark can never affect playback (recycle_played_songs() reads from
    Songs, so the song is not selectable there) — it only wastes a cap slot and
    a UNION branch in every played_ids() call.

    Dropping the mark is safe: if that song is rediscovered later it becomes
    fresh-eligible again, which is the correct outcome for a song whose score
    was never recorded.
    """
    s = _songs_mod()
    tables = [genre] if genre else genre_tables()
    removed = 0
    with s.DB_LOCK:
        conn = s._get_conn()
        for t in tables:
            try:
                cur = conn.execute(
                    f'DELETE FROM "{t}" WHERE id NOT IN (SELECT id FROM Songs)'
                )
                removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except (ValueError, sqlite3.Error) as e:
                print(f"Prune orphans from {t} failed:", e)
    return removed


def is_played(song_id: str) -> bool:
    """True if song_id already appears in any genre table (i.e. already played)."""
    s = _songs_mod()
    tables = genre_tables()
    if not tables:
        return False
    query = " UNION ".join(f'SELECT id FROM "{t}" WHERE id = ?' for t in tables)
    with s.DB_LOCK:
        conn = s._get_conn()
        row = conn.execute(query, (song_id,) * len(tables)).fetchone()
    return row is not None


def played_ids(song_ids) -> set:
    """Returns the subset of song_ids already present in the genre tables."""
    s = _songs_mod()
    ids = list(song_ids)
    tables = genre_tables()
    if not tables or not ids:
        return set()
    query = " UNION ".join(f'SELECT id FROM "{t}"' for t in tables)
    with s.DB_LOCK:
        conn = s._get_conn()
        rows = conn.execute(query).fetchall()
    known = {r[0] for r in rows}
    return known & set(ids)
