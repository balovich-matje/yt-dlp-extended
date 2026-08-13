"""SQLite persistence for settings, library files and job history.

Everything is small and single-user, so a single connection guarded by a lock is
plenty. Rows are returned as plain dicts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR, DB_PATH, DEFAULT_SETTINGS, ensure_dirs

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    title       TEXT,
    origin      TEXT NOT NULL,          -- download | upload | convert
    source_url  TEXT,
    job_id      TEXT,
    size        INTEGER,
    duration    REAL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- download | convert
    status      TEXT NOT NULL,          -- queued | running | done | error | cancelled
    title       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    progress    REAL DEFAULT 0,
    detail      TEXT,                   -- JSON blob: request, progress fields, result
    error       TEXT,
    file_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_created ON files (created_at DESC);
"""


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            ensure_dirs(DATA_DIR)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def query(sql: str, params: Iterable = ()) -> list[dict]:
    with _lock:
        return _rows(connect().execute(sql, tuple(params)))


def execute(sql: str, params: Iterable = ()) -> None:
    with _lock:
        conn = connect()
        conn.execute(sql, tuple(params))
        conn.commit()


# ---------------------------------------------------------------- settings


def get_settings() -> dict:
    values = dict(DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


def set_settings(updates: dict) -> dict:
    with _lock:
        conn = connect()
        for key, value in updates.items():
            if key not in DEFAULT_SETTINGS:
                continue
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
        conn.commit()
    return get_settings()


# ------------------------------------------------------------------- files


def add_file(
    path: Path,
    origin: str,
    *,
    title: str | None = None,
    source_url: str | None = None,
    job_id: str | None = None,
    duration: float | None = None,
) -> dict:
    """Register a file in the library, replacing any stale row for the same path."""
    path = Path(path)
    size = path.stat().st_size if path.exists() else None
    file_id = uuid.uuid4().hex
    with _lock:
        conn = connect()
        existing = conn.execute("SELECT id FROM files WHERE path = ?", (str(path),)).fetchone()
        if existing:
            file_id = existing["id"]
            conn.execute(
                "UPDATE files SET name=?, title=?, origin=?, source_url=?, job_id=?, "
                "size=?, duration=?, created_at=? WHERE id=?",
                (
                    path.name, title, origin, source_url, job_id,
                    size, duration, time.time(), file_id,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO files (id, path, name, title, origin, source_url, job_id, "
                "size, duration, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    file_id, str(path), path.name, title, origin, source_url,
                    job_id, size, duration, time.time(),
                ),
            )
        conn.commit()
    return get_file(file_id)


def get_file(file_id: str) -> dict | None:
    rows = query("SELECT * FROM files WHERE id = ?", (file_id,))
    return rows[0] if rows else None


def list_files() -> list[dict]:
    """Library listing, annotated with whether the file is still on disk."""
    out = []
    for row in query("SELECT * FROM files ORDER BY created_at DESC"):
        p = Path(row["path"])
        row["exists"] = p.exists()
        if row["exists"]:
            row["size"] = p.stat().st_size
        out.append(row)
    return out


def delete_file_row(file_id: str) -> dict | None:
    row = get_file(file_id)
    if row:
        execute("DELETE FROM files WHERE id = ?", (file_id,))
    return row


# -------------------------------------------------------------------- jobs


def save_job(job: dict) -> None:
    detail = json.dumps(job.get("detail") or {})
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO jobs (id, kind, status, title, created_at, updated_at, "
            "progress, detail, error, file_id) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, title=excluded.title, "
            "updated_at=excluded.updated_at, progress=excluded.progress, "
            "detail=excluded.detail, error=excluded.error, file_id=excluded.file_id",
            (
                job["id"], job["kind"], job["status"], job.get("title"),
                job["created_at"], job["updated_at"], job.get("progress") or 0,
                detail, job.get("error"), job.get("file_id"),
            ),
        )
        conn.commit()


def _decode_job(row: dict) -> dict:
    row = dict(row)
    try:
        row["detail"] = json.loads(row.get("detail") or "{}")
    except json.JSONDecodeError:
        row["detail"] = {}
    return row


def list_jobs(limit: int = 100) -> list[dict]:
    return [_decode_job(r) for r in query(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    )]


def get_job(job_id: str) -> dict | None:
    rows = query("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return _decode_job(rows[0]) if rows else None


def delete_job(job_id: str) -> None:
    execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def mark_orphaned_jobs() -> None:
    """A job still marked running at startup died with the previous process."""
    execute(
        "UPDATE jobs SET status='error', error=COALESCE(error,'') || "
        "'\nInterrupted: the server restarted while this job was running.' "
        "WHERE status IN ('running','queued')"
    )


def cleanup_missing_files() -> int:
    """Drop library rows whose file disappeared from disk outside the app."""
    removed = 0
    for row in query("SELECT id, path FROM files"):
        if not Path(row["path"]).exists():
            execute("DELETE FROM files WHERE id = ?", (row["id"],))
            removed += 1
    return removed
