import sqlite3
from contextlib import contextmanager
from pathlib import Path
from app import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, source BLOB NOT NULL, book BLOB NOT NULL,
  result TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  run_id TEXT NOT NULL REFERENCES runs(id), version INTEGER NOT NULL,
  decision TEXT NOT NULL, note TEXT NOT NULL, changed TEXT NOT NULL,
  PRIMARY KEY(run_id,version)
);
CREATE TABLE IF NOT EXISTS sheets (
  run_id TEXT NOT NULL REFERENCES runs(id), district TEXT NOT NULL,
  shortage INTEGER NOT NULL, requested TEXT NOT NULL, allocated TEXT NOT NULL,
  proof TEXT NOT NULL, unmet INTEGER NOT NULL, status TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', changed TEXT NOT NULL,
  PRIMARY KEY(run_id,district)
);
CREATE TABLE IF NOT EXISTS segments (
  run_id TEXT NOT NULL, segment TEXT NOT NULL,
  capacity INTEGER NOT NULL, committed INTEGER NOT NULL,
  PRIMARY KEY(run_id,segment)
);
"""


@contextmanager
def connection():
    path = Path(settings.DB)
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path,timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys = ON')
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()
