"""Persistent OCR cache.

One SQLite database per user (not per input folder), so read-only and
network input folders work, results are shared between folders, and the
database never sits on a network filesystem where SQLite's WAL mode is
unsafe. Only OCR output is cached — native PDF text is cheap to re-read —
and only pages whose OCR succeeded, so failures are retried on the next run.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from pathlib import Path

from .util import user_cache_dir

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
CACHE_FILE_NAME = "ocr_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_pages (
    content_hash TEXT    NOT NULL,
    page_num     INTEGER NOT NULL,
    engine       TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    created      REAL    NOT NULL,
    PRIMARY KEY (content_hash, page_num, engine)
) WITHOUT ROWID
"""


def default_cache_path() -> Path:
    return user_cache_dir() / CACHE_FILE_NAME


class OcrCache:
    """Thin wrapper around the cache database. Errors never propagate: a
    broken cache degrades to "no cache" with a warning in the log."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        conn = self._conn
        if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        conn.execute("BEGIN IMMEDIATE")  # re-check under the write lock
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                # v1 cached failed OCR as empty text and skipped the scanned
                # pages of mixed PDFs, so its rows cannot be trusted.
                for table in ("ocr_cache", "ocr_complete", "ocr_pages"):
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.execute(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def get(
        self, file_hash: str, engine: str | None = None, strict: bool = False
    ) -> dict[int, tuple[str, str]]:
        """Cached ``{page: (text, engine)}`` for a file.

        Per page, ``engine``'s result is preferred, otherwise the most recent
        one is used; with ``strict`` only ``engine``'s results count.
        """
        try:
            rows = self._conn.execute(
                "SELECT page_num, text, engine FROM ocr_pages WHERE content_hash=?"
                " ORDER BY created",
                (file_hash,),
            ).fetchall()
        except sqlite3.Error:
            log.warning("OCR cache read failed", exc_info=True)
            return {}
        out: dict[int, tuple[str, str]] = {}
        for p, t, e in rows:  # oldest first, so later rows win...
            if strict and e != engine:
                continue
            if p in out and out[p][1] == engine and e != engine:
                continue  # ...except over the preferred engine
            out[p] = (t, e)
        return out

    def put(self, file_hash: str, pages: dict[int, tuple[str, str]]) -> None:
        if not pages:
            return
        now = time.time()
        try:
            with self._conn:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO ocr_pages VALUES (?,?,?,?,?)",
                    [(file_hash, p, e, t, now) for p, (t, e) in pages.items()],
                )
        except sqlite3.Error:
            log.warning("OCR cache write failed", exc_info=True)

    def page(
        self, file_hash: str, page_num: int, engine: str | None = None
    ) -> tuple[str, str] | None:
        """One page's text, chosen the same way as :meth:`get`."""
        try:
            row = self._conn.execute(
                "SELECT text, engine FROM ocr_pages WHERE content_hash=? AND page_num=?"
                " ORDER BY engine = ? DESC, created DESC LIMIT 1",
                (file_hash, page_num, engine or ""),
            ).fetchone()
        except sqlite3.Error:
            log.warning("OCR cache read failed", exc_info=True)
            return None
        return (row[0], row[1]) if row else None

    def stats(self) -> tuple[int, int]:
        """(files, pages) currently cached."""
        try:
            files, pages = self._conn.execute(
                "SELECT COUNT(DISTINCT content_hash), COUNT(*) FROM ocr_pages"
            ).fetchone()
            return int(files), int(pages)
        except sqlite3.Error:
            return 0, 0

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()


def open_cache(path: str | os.PathLike[str] | None) -> OcrCache | None:
    """Open the cache, or return ``None`` (with a warning) if it is unusable."""
    if not path:
        return None
    try:
        return OcrCache(path)
    except (sqlite3.Error, OSError):
        log.warning("OCR cache at %s is unavailable; continuing without it", path, exc_info=True)
        return None


def _cache_files(path: str | os.PathLike[str]) -> list[Path]:
    base = Path(path)
    return [base, Path(f"{base}-wal"), Path(f"{base}-shm")]


def cache_size(path: str | os.PathLike[str]) -> int:
    """Bytes used by the cache, including its WAL side files."""
    return sum(f.stat().st_size for f in _cache_files(path) if f.exists())


def delete_cache(path: str | os.PathLike[str]) -> None:
    """Delete the cache database. Raises ``OSError`` if it is in use."""
    for f in _cache_files(path):
        if f.exists():
            f.unlink()
