"""SQLite storage for transcription history.

Each dictation is stored in the language it was pasted in (`lang`). When
that is a translation, what was actually said is kept beside it
(`source_text`, `source_lang`) — switching the language afterwards then
hands back the original instead of a translation of a translation.
Translations made later, from the history window or a language switch,
are cached per entry in `translations`.
"""

import sqlite3
from pathlib import Path

from config import Config

_config = Config()
_db_path = _config.db_path

# Added after the table already existed on people's machines — init_db()
# adds whichever of these an older database is missing.
_LATER_COLUMNS = ("lang", "source_text", "source_lang")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create database directory and tables, upgrading an older layout."""
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
        have = {row[1] for row in conn.execute("PRAGMA table_info(transcriptions)")}
        for column in _LATER_COLUMNS:
            if column not in have:
                conn.execute(f"ALTER TABLE transcriptions ADD COLUMN {column} TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                entry_id INTEGER NOT NULL,
                lang TEXT NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY (entry_id, lang)
            )
        """)


def save(text: str, duration: float | None = None,
         rms: float | None = None, peak: float | None = None, *,
         lang: str | None = None, source_text: str | None = None,
         source_lang: str | None = None) -> int:
    """Save a transcription and return its id.

    `source_text` is only worth keeping when it differs from `text` — a
    dictation pasted in the language it was spoken in has no separate
    original.
    """
    if source_text == text:
        source_text = source_lang = None
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO transcriptions (text, duration_sec, audio_rms, audio_peak, "
            "lang, source_text, source_lang) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (text, duration, rms, peak, lang, source_text, source_lang),
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
    """Search transcriptions by text or original (case-insensitive LIKE)."""
    pattern = f"%{query}%"
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transcriptions WHERE text LIKE ? OR source_text LIKE ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_entry(row_id: int) -> dict | None:
    """One transcription by id."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM transcriptions WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None


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


def update_entry(row_id: int, text: str) -> None:
    """Update a transcription's text. Its cached translations go stale."""
    with _connect() as conn:
        conn.execute("UPDATE transcriptions SET text=? WHERE id=?", (text, row_id))
        conn.execute("DELETE FROM translations WHERE entry_id=?", (row_id,))


def delete(row_id: int) -> None:
    """Delete a transcription by id, with its translations."""
    with _connect() as conn:
        conn.execute("DELETE FROM transcriptions WHERE id = ?", (row_id,))
        conn.execute("DELETE FROM translations WHERE entry_id = ?", (row_id,))


def get_translation(entry_id: int, lang: str) -> str | None:
    """A cached translation of an entry, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT text FROM translations WHERE entry_id = ? AND lang = ?",
            (entry_id, lang)).fetchone()
        return row[0] if row else None


def put_translation(entry_id: int, lang: str, text: str) -> None:
    """Cache a translation of an entry, replacing an older one."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO translations (entry_id, lang, text) "
            "VALUES (?, ?, ?)", (entry_id, lang, text))
