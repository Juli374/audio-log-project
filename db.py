"""SQLite storage for transcription history."""

import sqlite3
from pathlib import Path

from config import Config

_config = Config()
_db_path = _config.db_path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create database directory and table if they don't exist."""
    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now','localtime')),
                text TEXT NOT NULL,
                duration_sec REAL,
                audio_rms REAL,
                audio_peak REAL
            )
        """)


def save(text: str, duration: float | None = None,
         rms: float | None = None, peak: float | None = None) -> int:
    """Save a transcription and return its id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO transcriptions (text, duration_sec, audio_rms, audio_peak) "
            "VALUES (?, ?, ?, ?)",
            (text, duration, rms, peak),
        )
        return cur.lastrowid


def get_recent(limit: int = 50, offset: int = 0) -> list[dict]:
    """Get recent transcriptions, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transcriptions ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def search(query: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Search transcriptions by text (case-insensitive LIKE)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transcriptions WHERE text LIKE ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (f"%{query}%", limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    """Return summary statistics."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE
                    WHEN date(timestamp) = date('now','localtime') THEN 1
                    ELSE 0 END), 0) AS today,
                COALESCE(SUM(duration_sec), 0) AS total_duration
            FROM transcriptions
        """).fetchone()
        return dict(row)


def delete(row_id: int) -> None:
    """Delete a transcription by id."""
    with _connect() as conn:
        conn.execute("DELETE FROM transcriptions WHERE id = ?", (row_id,))
