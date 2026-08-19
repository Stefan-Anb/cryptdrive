"""Lokaler Zustand: Datei-Cache (SQLite) und Statusdatei fuer das Tray-Icon."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS filecache (
    rel       TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    hash      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class LocalIndex:
    """Cache, damit unveraenderte Dateien nicht erneut gelesen werden muessen."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- Datei-Cache ----
    def lookup(self, rel: str, size: int, mtime_ns: int) -> str | None:
        row = self.db.execute(
            "SELECT hash FROM filecache WHERE rel=? AND size=? AND mtime_ns=?",
            (rel, size, mtime_ns),
        ).fetchone()
        return row[0] if row else None

    def remember(self, rel: str, size: int, mtime_ns: int, digest: str) -> None:
        self.db.execute(
            "INSERT INTO filecache(rel,size,mtime_ns,hash) VALUES(?,?,?,?) "
            "ON CONFLICT(rel) DO UPDATE SET size=excluded.size, "
            "mtime_ns=excluded.mtime_ns, hash=excluded.hash",
            (rel, size, mtime_ns, digest),
        )

    def prune(self, keep: set[str]) -> int:
        known = {r[0] for r in self.db.execute("SELECT rel FROM filecache")}
        gone = known - keep
        self.db.executemany("DELETE FROM filecache WHERE rel=?", ((r,) for r in gone))
        return len(gone)

    def commit(self) -> None:
        self.db.commit()

    # ---- Metadaten ----
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    @property
    def last_sync(self) -> datetime | None:
        raw = self.get("last_sync")
        return datetime.fromisoformat(raw) if raw else None

    @last_sync.setter
    def last_sync(self, when: datetime) -> None:
        self.set("last_sync", when.astimezone(timezone.utc).isoformat())


def write_status(path: Path, data: dict) -> None:
    """Status atomar schreiben, damit das Tray nie halbe Dateien liest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".status", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_status(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
