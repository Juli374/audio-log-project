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


def update_entry(row_id: int, text: str) -> None:
    """Update a transcription's text."""
    with _connect() as conn:
        conn.execute("UPDATE transcriptions SET text=? WHERE id=?", (text, row_id))


def delete(row_id: int) -> None:
    """Delete a transcription by id."""
    with _connect() as conn:
        conn.execute("DELETE FROM transcriptions WHERE id = ?", (row_id,))


# ────────────────────────────────────────────────────────────
# Notes / Diary / Tasks
# ────────────────────────────────────────────────────────────

def init_notes_table() -> None:
    """Create notes table if not exists."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT,
                text TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'note',
                is_done INTEGER DEFAULT 0,
                date TEXT
            )
        """)


# --- Заметки ---

def save_note(text: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes (text, type) VALUES (?, 'note')", (text,))
        return cur.lastrowid


def get_notes(limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE type='note' ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def search_notes(query: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE type='note' AND text LIKE ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (f"%{query}%", limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def update_note(row_id: int, text: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE notes SET text=?, updated_at=datetime('now','localtime') WHERE id=?",
            (text, row_id),
        )


def delete_note(row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (row_id,))


# --- Дневник ---

def get_diary(date: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM notes WHERE type='diary' AND date=? LIMIT 1",
            (date,),
        ).fetchone()
        return dict(row) if row else None


def save_diary(date: str, text: str) -> int:
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM notes WHERE type='diary' AND date=? LIMIT 1",
            (date,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE notes SET text=?, updated_at=datetime('now','localtime') WHERE id=?",
                (text, existing["id"]),
            )
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO notes (text, type, date) VALUES (?, 'diary', ?)",
                (text, date),
            )
            return cur.lastrowid


def get_diary_dates() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM notes WHERE type='diary' ORDER BY date DESC"
        ).fetchall()
        return [r["date"] for r in rows]


def get_diary_streak() -> int:
    from datetime import date as dt_date, timedelta
    dates = set(get_diary_dates())
    if not dates:
        return 0
    today = dt_date.today()
    streak = 0
    d = today
    while d.isoformat() in dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


# --- Задачи ---

def save_task(text: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes (text, type) VALUES (?, 'task')", (text,))
        return cur.lastrowid


def get_tasks(filter: str = "all") -> list[dict]:
    with _connect() as conn:
        if filter == "active":
            rows = conn.execute(
                "SELECT * FROM notes WHERE type='task' AND is_done=0 ORDER BY id DESC"
            ).fetchall()
        elif filter == "completed":
            rows = conn.execute(
                "SELECT * FROM notes WHERE type='task' AND is_done=1 ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notes WHERE type='task' ORDER BY is_done ASC, id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def toggle_task(row_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE notes SET is_done = NOT is_done, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (row_id,),
        )


def update_task(row_id: int, text: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE notes SET text=?, updated_at=datetime('now','localtime') WHERE id=?",
            (text, row_id),
        )


def delete_task(row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (row_id,))


# ────────────────────────────────────────────────────────────
# Long sessions (continuous recording + chunked transcription)
# ────────────────────────────────────────────────────────────

# Status lifecycle:
#   recording    → SessionRecorder is actively writing audio to disk
#   transcribing → recording stopped, LongTranscriber is processing chunks
#   done         → transcription finished successfully
#   error        → something failed (details in `error` column)
#   cancelled    → user cancelled via ESC
#
# Crash recovery: rows stuck in `recording` on startup are marked `error`
# or resumed by `sessions_find_stale()`.

def init_sessions_table() -> None:
    """Create sessions table if not exists."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT DEFAULT (datetime('now','localtime')),
                ended_at TEXT,
                duration_sec REAL,
                audio_path TEXT NOT NULL,
                text TEXT,
                status TEXT NOT NULL DEFAULT 'recording',
                error TEXT,
                chunk_count INTEGER DEFAULT 0,
                chunks_done INTEGER DEFAULT 0
            )
        """)


def session_create(audio_path: str) -> int:
    """Start a new session row. Returns session id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (audio_path, status) VALUES (?, 'recording')",
            (audio_path,),
        )
        return cur.lastrowid


def session_finalize(session_id: int, duration_sec: float) -> None:
    """Mark end of recording phase — recording finished, transcription next."""
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at=datetime('now','localtime'), "
            "duration_sec=?, status='transcribing' WHERE id=?",
            (duration_sec, session_id),
        )


def session_set_status(session_id: int, status: str,
                       error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET status=?, error=? WHERE id=?",
            (status, error, session_id),
        )


def session_set_text(session_id: int, text: str) -> None:
    """Save transcribed text and mark status=done."""
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET text=?, status='done' WHERE id=?",
            (text, session_id),
        )


def session_set_progress(session_id: int, done: int, total: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET chunks_done=?, chunk_count=? WHERE id=?",
            (done, total, session_id),
        )


def session_get(session_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        return dict(row) if row else None


def session_list(limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def session_search(query: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE text LIKE ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (f"%{query}%", limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def session_delete(session_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def sessions_find_stale() -> list[dict]:
    """Find sessions stuck in 'recording' status — likely crashed mid-record."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status='recording' ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
