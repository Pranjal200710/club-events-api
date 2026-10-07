"""SQLite setup and one reusable transaction context manager."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "club.db"


@contextmanager
def connect(path, *, write=False):
    # Each operation gets its own connection. SQLite coordinates the connections.
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        if write:
            # Take the write lock before reading the seat count or tokens.
            db.execute("BEGIN IMMEDIATE")
        yield db
        if write:
            db.commit()
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        # WAL lets readers continue while a writer is working.
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(Path(__file__).with_name("schema.sql").read_text())
