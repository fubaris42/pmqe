"""Everything a run leaves in its output folder, besides the extracted pages.

``extraction_history.db`` holds the run's settings, queries, every review
item with its decision, and failures, so a run can be reopened later. It
keeps the original ``summary_metrics`` and ``candidates_log`` tables (and
their columns) so existing tooling keeps working; ``pre_approved`` holds the
current approval. ``report.csv`` and ``issues.csv`` are refreshed
after the run and after every review decision.
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .models import OUTPUT_NAME_RE, ReviewItem
from .text import Query, safe_folder_name
from .util import long_path

log = logging.getLogger(__name__)

DB_NAME = "extraction_history.db"
REPORT_NAME = "report.csv"
ISSUES_NAME = "issues.csv"
LOG_NAME = "extractor.log"
MERGED_PDF_NAME = "merged_all_queries.pdf"
STORE_VERSION = 2

_SCHEMA = """
CREATE TABLE run_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE queries (
    idx INTEGER PRIMARY KEY, query TEXT NOT NULL, folder TEXT NOT NULL, tokens TEXT NOT NULL
);
CREATE TABLE summary_metrics (query TEXT PRIMARY KEY, hits INTEGER NOT NULL);
CREATE TABLE candidates_log (
    id            TEXT PRIMARY KEY,
    query         TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    page_index    INTEGER NOT NULL,
    pre_approved  INTEGER NOT NULL,
    query_idx     INTEGER NOT NULL,
    file_hash     TEXT NOT NULL,
    page_count    INTEGER NOT NULL,
    auto_approved INTEGER NOT NULL,
    missing_words TEXT NOT NULL,
    decided_at    REAL
);
CREATE INDEX candidates_by_query ON candidates_log (query_idx);
CREATE TABLE failures (file_path TEXT NOT NULL, error TEXT NOT NULL);
"""


def _plain_folder(folder: str, query: str) -> str:
    """Folder names come from a file the user may have been given: never let
    one point outside the output folder."""
    if (
        folder
        and folder not in (".", "..")
        and os.path.basename(folder) == folder
        and "/" not in folder
    ):
        return folder
    return safe_folder_name(query)


class NoRunFound(Exception):
    """The folder holds no run written by this version of pmqe."""


@dataclass
class RunInfo:
    input_dir: str
    output_dir: str
    queries: list[Query]
    whole_words: bool = True
    engine: str = ""
    status: str = "running"  # running | complete | cancelled
    created: float = field(default_factory=time.time)
    finished: float | None = None
    version: str = __version__
    stats: dict = field(default_factory=dict)


class RunStore:
    def __init__(self, output_dir: str, conn: sqlite3.Connection) -> None:
        self.output_dir = output_dir
        self._conn = conn

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def create(cls, info: RunInfo) -> RunStore:
        """Start a fresh run record, replacing any previous one."""
        os.makedirs(long_path(info.output_dir), exist_ok=True)
        path = Path(info.output_dir) / DB_NAME
        for suffix in ("", "-wal", "-shm", "-journal"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(long_path(f"{path}{suffix}"))
        conn = sqlite3.connect(long_path(path), timeout=30, check_same_thread=False)
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
        store = cls(info.output_dir, conn)
        with conn:
            conn.executemany(
                "INSERT INTO queries VALUES (?,?,?,?)",
                [(i, q.text, q.folder, json.dumps(q.tokens)) for i, q in enumerate(info.queries)],
            )
            conn.executemany(
                "INSERT INTO summary_metrics VALUES (?, 0)", [(q.text,) for q in info.queries]
            )
            store._write_meta(info)
        return store

    @classmethod
    def open(cls, output_dir: str) -> RunStore:
        path = Path(output_dir) / DB_NAME
        if not os.path.exists(long_path(path)):
            raise NoRunFound(f"No pmqe results in {output_dir}")
        conn = sqlite3.connect(long_path(path), timeout=30, check_same_thread=False)
        if conn.execute("PRAGMA user_version").fetchone()[0] != STORE_VERSION:
            conn.close()
            raise NoRunFound(f"{output_dir} holds results from an older pmqe version")
        return cls(output_dir, conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing -------------------------------------------------------------

    def _write_meta(self, info: RunInfo) -> None:
        meta = {
            "input_dir": info.input_dir,
            "whole_words": json.dumps(info.whole_words),
            "engine": info.engine,
            "status": info.status,
            "created": repr(info.created),
            "finished": repr(info.finished) if info.finished else "",
            "version": info.version,
            "stats": json.dumps(info.stats),
        }
        self._conn.executemany("INSERT OR REPLACE INTO run_meta VALUES (?,?)", meta.items())

    def finish(
        self,
        info: RunInfo,
        items: list[ReviewItem],
        failures: list[tuple[str, str]],
    ) -> None:
        """Record the run's results and write the CSV reports."""
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO candidates_log VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        it.id,
                        it.query,
                        it.file_path,
                        it.page_idx,
                        int(bool(it.approved)),
                        it.query_idx,
                        it.file_hash,
                        it.page_count,
                        int(it.auto_approved),
                        json.dumps(it.missing_words),
                        time.time() if it.decided else None,
                    )
                    for it in items
                ],
            )
            self._conn.executemany("INSERT INTO failures VALUES (?,?)", failures)
            self._refresh_summary()
            self._write_meta(info)
        self.write_reports()

    def record_decisions(self, items: Iterable[ReviewItem]) -> None:
        """Persist manual approve/reject decisions (one UPDATE per item)."""
        now = time.time()
        with self._conn:
            self._conn.executemany(
                "UPDATE candidates_log SET pre_approved=?, decided_at=? WHERE id=?",
                [(int(bool(it.approved)), now if it.decided else None, it.id) for it in items],
            )
            self._refresh_summary()
        self.write_reports()

    def _query_totals(self) -> list[tuple[str, int, int]]:
        """(query, approved pages, awaiting review) in query order, in one scan."""
        totals = {
            qi: (approved or 0, pending or 0)
            for qi, approved, pending in self._conn.execute(
                "SELECT query_idx, SUM(pre_approved),"
                " SUM(auto_approved = 0 AND decided_at IS NULL)"
                " FROM candidates_log GROUP BY query_idx"
            )
        }
        return [
            (q, *totals.get(qi, (0, 0)))
            for qi, q in self._conn.execute("SELECT idx, query FROM queries ORDER BY idx")
        ]

    def _refresh_summary(self) -> None:
        self._conn.executemany(
            "UPDATE summary_metrics SET hits=? WHERE query=?",
            [(hits, q) for q, hits, _ in self._query_totals()],
        )

    def write_reports(self) -> None:
        out = Path(self.output_dir)
        try:
            rows = self._query_totals()
            with open(long_path(out / REPORT_NAME), "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Query", "Total Approved Hits", "Awaiting Review"])
                w.writerows(rows)
            failures = self.failures()
            failures_csv = long_path(out / ISSUES_NAME)
            if failures:
                with open(failures_csv, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(["File", "Issue"])
                    w.writerows(failures)
            elif os.path.exists(failures_csv):
                os.remove(failures_csv)
        except OSError:
            log.exception("Could not write reports to %s", out)

    # -- reading ---------------------------------------------------------------

    def info(self) -> RunInfo:
        meta = dict(self._conn.execute("SELECT key, value FROM run_meta"))
        queries = [
            Query(q, tuple(json.loads(t)), _plain_folder(folder, q))
            for q, folder, t in self._conn.execute(
                "SELECT query, folder, tokens FROM queries ORDER BY idx"
            )
        ]
        return RunInfo(
            input_dir=meta.get("input_dir", ""),
            output_dir=self.output_dir,
            queries=queries,
            whole_words=json.loads(meta.get("whole_words", "true")),
            engine=meta.get("engine", ""),
            status=meta.get("status", "running"),
            created=float(meta.get("created") or 0),
            finished=float(meta["finished"]) if meta.get("finished") else None,
            version=meta.get("version", ""),
            stats=json.loads(meta.get("stats") or "{}"),
        )

    def items(self) -> list[ReviewItem]:
        folders = {
            idx: _plain_folder(folder, q)
            for idx, folder, q in self._conn.execute("SELECT idx, folder, query FROM queries")
        }
        rows = self._conn.execute(
            "SELECT id, query, query_idx, file_path, file_hash, page_index, page_count,"
            " auto_approved, missing_words, pre_approved, decided_at FROM candidates_log"
            " ORDER BY rowid"
        )
        return [
            ReviewItem(
                id=r[0],
                query=r[1],
                query_idx=r[2],
                folder=folders.get(r[2], ""),
                file_path=r[3],
                file_hash=r[4],
                page_idx=r[5],
                page_count=r[6],
                auto_approved=bool(r[7]),
                missing_words=json.loads(r[8]),
                approved=bool(r[9]),
                decided=r[10] is not None,
            )
            for r in rows
        ]

    def failures(self) -> list[tuple[str, str]]:
        return list(self._conn.execute("SELECT file_path, error FROM failures ORDER BY rowid"))


# ---------------------------------------------------------------------------
# Previous runs in an output folder
# ---------------------------------------------------------------------------


@dataclass
class PreviousRun:
    output_files: int
    queries: int | None = None
    created: float | None = None
    status: str | None = None


_RUN_FILES = (REPORT_NAME, ISSUES_NAME, MERGED_PDF_NAME, LOG_NAME)


_TMP_SUFFIX = re.compile(r"\.\d+-\d+\.tmp$")  # added by util.atomic_write


def _legacy_folder(query: str) -> str:
    """The folder name pmqe 1.x used for a query."""
    return "".join(c for c in query if c.isalnum() or c in " _-").strip() or "untitled_query"


def _query_folders(output_dir: str) -> list[Path]:
    """Folders a previous run wrote to, as listed in its run record.

    Without a record nothing in the folder is assumed to be ours, so a
    folder the user happens to pick is never cleaned up by mistake.
    """
    root = Path(output_dir)
    try:
        with RunStore.open(output_dir) as store:
            return [root / q.folder for q in store.info().queries]
    except NoRunFound:
        pass
    except sqlite3.Error:
        return []
    # pmqe 1.x recorded its queries (not their folders) in the same file.
    db = root / DB_NAME
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(long_path(db))
        try:
            queries = [r[0] for r in con.execute("SELECT query FROM summary_metrics")]
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [root / f for f in dict.fromkeys(_legacy_folder(str(q)) for q in queries)]


def _is_ours(name: str) -> bool:
    """A page output (or an unfinished write of one) by pmqe's naming."""
    return bool(OUTPUT_NAME_RE.match(_TMP_SUFFIX.sub("", name)))


def _our_files(folder: Path) -> list[Path]:
    try:
        return [f for f in folder.iterdir() if f.is_file() and _is_ours(f.name)]
    except OSError:
        return []


def find_previous_run(output_dir: str) -> PreviousRun | None:
    """Describe the pmqe run recorded in ``output_dir``, if there is one."""
    root = Path(output_dir)
    if not (root / DB_NAME).is_file():
        return None
    folders = _query_folders(output_dir)
    prev = PreviousRun(output_files=sum(len(_our_files(f)) for f in folders), queries=len(folders))
    try:
        with RunStore.open(output_dir) as store:
            info = store.info()
            prev.created, prev.status = info.created, info.status
    except (NoRunFound, sqlite3.Error):
        pass  # a 1.x run: no timestamp to show
    return prev


def clear_previous_run(output_dir: str) -> int:
    """Delete a previous run's outputs and records from ``output_dir``.

    Only files pmqe creates are removed: page outputs that match its naming
    scheme (with their 32-character hash), its reports, database, merged PDF
    and logs. Anything else in the folder is left alone. Returns the number
    of page outputs removed.
    """
    root = Path(output_dir)
    removed = 0
    for folder in _query_folders(output_dir):
        for f in _our_files(folder):
            try:
                os.remove(long_path(f))
                removed += 1
            except OSError:
                log.warning("Could not remove %s", f, exc_info=True)
        with contextlib.suppress(OSError):
            folder.rmdir()  # only succeeds when empty
    names = [*_RUN_FILES, *(f"{DB_NAME}{s}" for s in ("", "-wal", "-shm", "-journal"))]
    for name in names:
        with contextlib.suppress(FileNotFoundError):
            os.remove(long_path(root / name))
    # Rotated and per-worker logs from older versions
    for f in root.glob("extractor*.log*"):
        with contextlib.suppress(OSError):
            f.unlink()
    return removed
