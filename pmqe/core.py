"""Processing core for pmqe — everything that does not need Qt.

This module is imported by every OCR worker process (via
``ProcessPoolExecutor``'s spawn pickling), so it must stay free of PySide6
imports to keep worker startup time and memory low.

Covers: logging setup, path utilities, content hashing, Aho-Corasick query
matching, the per-directory SQLite OCR cache, the platform OCR backends
(Windows Media OCR / Apple Vision / RapidOCR), per-file processing, report
persistence, and merging extracted outputs into one bookmarked PDF.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import logging.handlers
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import ahocorasick
import fitz  # PyMuPDF
import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OCR_DPI = 200
OCR_SAMPLE_PAGES = 5
OCR_SAMPLE_THRESH = 200
OCR_BATCH_SIZE = 8  # stream OCR in batches of this many pages
CONTENT_HASH_BYTES = 1_048_576
LOG_MAX_BYTES = 5_242_880
LOG_BACKUP_COUNT = 3
SMALL_FILE_BYTES = 512_000  # files smaller than this get batched together
SMALL_FILE_BATCH = 50

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heif"}
SUPPORTED_EXTENSIONS = {".pdf"} | IMAGE_EXTENSIONS

OCR_CACHE_NAME = ".ocr_cache.db"
MERGED_PDF_NAME = "merged_all_queries.pdf"

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


_log_file_configured: str | None = None


def _setup_logging(log_path: str | None = None) -> None:
    """Idempotent: attaches the stderr handler once, and (re)attaches the file
    handler whenever a new log_path is requested — even if stderr logging was
    already set up earlier in this process."""
    global _log_file_configured
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    if not root.handlers:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if log_path and log_path != _log_file_configured:
        try:
            # Ensure the output directory exists before opening the log file
            log_dir = os.path.dirname(_strip_unc(log_path))
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            for h in list(root.handlers):
                if isinstance(h, logging.handlers.RotatingFileHandler):
                    root.removeHandler(h)
                    h.close()
            root.addHandler(fh)
            _log_file_configured = log_path
        except OSError as exc:
            root.warning(
                "Could not open log file %s: %s — logging to stderr only.",
                log_path,
                exc,
            )


def worker_log_path(log_path: str) -> str:
    """Per-process log file for OCR workers.

    Multiple processes rotating the same RotatingFileHandler file is unsafe
    (rollover raises PermissionError on Windows while another process holds
    the file open), so each worker logs to its own ``.w<pid>`` file.
    """
    base, ext = os.path.splitext(log_path)
    return f"{base}.w{os.getpid()}{ext}"


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------


def _unc(path: str) -> str:
    """Return a \\\\?\\ -prefixed absolute path on Windows to bypass MAX_PATH."""
    if sys.platform != "win32":
        return path
    p = os.path.abspath(path)
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def _strip_unc(path: str) -> str:
    """Inverse of _unc: drop a leading \\\\?\\ prefix if present."""
    return path[4:] if path.startswith("\\\\?\\") else path


def safe_folder_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip()
    return cleaned or "untitled_query"


def output_page_name(stem: str, page_idx: int, file_hash: str) -> str:
    """Canonical filename for one extracted PDF page (individual-page output)."""
    return f"{stem}_p{page_idx + 1}_{file_hash}.pdf"


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def content_hash(path: str) -> str:
    try:
        stat = os.stat(path)
        h = hashlib.blake2b(digest_size=16)
        h.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
        with open(path, "rb") as fh:
            h.update(fh.read(CONTENT_HASH_BYTES))
        return h.hexdigest()
    except OSError:
        return hashlib.blake2b(path.encode(), digest_size=16).hexdigest()


# ---------------------------------------------------------------------------
# Aho-Corasick helpers
# ---------------------------------------------------------------------------


def query_tokens(query: str) -> list[str]:
    """Lowercased words of a query worth matching (single chars are noise)."""
    return [w.lower() for w in query.split() if len(w) > 1]


def build_automaton(
    queries: list[str],
) -> tuple[ahocorasick.Automaton, dict[str, set[int]]]:
    """Return a compiled automaton and a token→query-index map."""
    token_qset: dict[str, set[int]] = defaultdict(set)
    for qi, q in enumerate(queries):
        for part in query_tokens(q):
            token_qset[part].add(qi)

    A = ahocorasick.Automaton()
    for token, qset in token_qset.items():
        A.add_word(token, (token, qset))
    A.make_automaton()
    return A, token_qset


def match_pages(
    page_texts: dict[int, str],
    automaton: ahocorasick.Automaton,
    queries: list[str],
    query_token_lists: list[list[str]],
    file_path: str,
    file_hash: str,
) -> tuple[dict[str, int], list[dict]]:
    """Match every query against every page's text.

    Returns (per-query approved-hit counts, review items). A page is
    pre-approved for a query when every token of the query appears on it; a
    partial hit on a multi-token query becomes a review candidate.
    """
    match_counts: dict[str, int] = {q: 0 for q in queries}
    review_items: list[dict] = []

    # An automaton with no words is still a trie; iterating it raises.
    # Happens when every query lacks usable tokens — then nothing can match.
    scannable = automaton.kind == ahocorasick.AHOCORASICK

    for page_idx, raw_text in page_texts.items():
        clean = raw_text.lower()

        # Scan the automaton once per page, then bucket hits by query index
        found_by_query: dict[int, set[str]] = defaultdict(set)
        if scannable:
            for _, (token, qset) in automaton.iter(clean):
                for qi in qset:
                    found_by_query[qi].add(token)

        for qi, query in enumerate(queries):
            tokens = query_token_lists[qi]
            if not tokens:
                # A query with no usable tokens matches nothing — without this
                # guard it would "approve" every page of every file.
                continue
            found_tokens = found_by_query.get(qi, set())

            missing_tokens = [t for t in tokens if t not in found_tokens]
            is_approved = not missing_tokens

            if is_approved or (found_tokens and len(tokens) > 1):
                if is_approved:
                    match_counts[query] += 1

                # text_content is intentionally excluded from the IPC payload;
                # the review panel fetches it on demand from the OCR cache.
                review_items.append(
                    {
                        "id": f"{file_hash}_{page_idx}_{qi}",
                        "query": query,
                        "file_path": file_path,
                        "file_hash": file_hash,
                        "page_idx": page_idx,
                        "pre_approved": is_approved,
                        "missing_words": missing_tokens,
                    }
                )

    return match_counts, review_items


# ---------------------------------------------------------------------------
# Worker-process globals — automaton initialised once per worker
# ---------------------------------------------------------------------------

_worker_automaton: ahocorasick.Automaton | None = None
_worker_queries: list[str] = []
_worker_query_tokens: list[list[str]] = []


def _worker_init(queries: list[str], log_path: str, use_rapidocr: bool = False) -> None:
    """Runs once when a worker process starts. Builds the automaton globally."""
    global _worker_automaton, _worker_queries, _worker_query_tokens
    _setup_logging(worker_log_path(log_path))
    set_ocr_backend(use_rapidocr)
    _worker_automaton, _ = build_automaton(queries)
    _worker_queries = queries
    _worker_query_tokens = [query_tokens(q) for q in queries]


# ---------------------------------------------------------------------------
# OCR cache (SQLite, per-input-directory)
# ---------------------------------------------------------------------------

_ocr_cache_conn: sqlite3.Connection | None = None
_ocr_cache_path: str = ""

_CREATE_CACHE_TABLE = """
    CREATE TABLE IF NOT EXISTS ocr_cache (
        content_hash TEXT NOT NULL,
        page_num     INTEGER NOT NULL,
        text         TEXT NOT NULL,
        timestamp    REAL NOT NULL,
        PRIMARY KEY (content_hash, page_num)
    )
"""

_CREATE_COMPLETION_TABLE = """
    CREATE TABLE IF NOT EXISTS ocr_complete (
        content_hash TEXT PRIMARY KEY,
        total_pages  INTEGER NOT NULL,
        timestamp    REAL NOT NULL
    )
"""


def _get_cache_conn(db_path: str) -> sqlite3.Connection:
    global _ocr_cache_conn, _ocr_cache_path
    if _ocr_cache_conn is None or _ocr_cache_path != db_path:
        if _ocr_cache_conn is not None:
            try:
                _ocr_cache_conn.close()
            except Exception:
                pass
        _ocr_cache_conn = sqlite3.connect(db_path, check_same_thread=False)
        _ocr_cache_conn.execute("PRAGMA journal_mode=WAL")
        _ocr_cache_conn.execute("PRAGMA synchronous=NORMAL")
        _ocr_cache_conn.execute("PRAGMA busy_timeout=5000")
        _ocr_cache_conn.execute("PRAGMA cache_size=-32768")  # 32 MB
        _ocr_cache_conn.execute("PRAGMA mmap_size=268435456")  # 256 MB
        _ocr_cache_conn.execute(_CREATE_CACHE_TABLE)
        _ocr_cache_conn.execute(_CREATE_COMPLETION_TABLE)
        _ocr_cache_conn.commit()
        _ocr_cache_path = db_path
    return _ocr_cache_conn


def cache_load(db_path: str, file_hash: str, total_pages: int) -> dict[int, str] | None:
    """Only returns cached data when the file was fully OCR'd previously
    (checked via the completion table, so partial caches are skipped)."""
    try:
        con = _get_cache_conn(db_path)
        row = con.execute(
            "SELECT total_pages FROM ocr_complete WHERE content_hash=?",
            (file_hash,),
        ).fetchone()
        if not row or row[0] != total_pages:
            return None
        rows = con.execute(
            "SELECT page_num, text FROM ocr_cache WHERE content_hash=? ORDER BY page_num",
            (file_hash,),
        ).fetchall()
        if len(rows) == total_pages:
            con.execute(
                "UPDATE ocr_complete SET timestamp=? WHERE content_hash=?",
                (time.time(), file_hash),
            )
            con.commit()
            return {r[0]: r[1] for r in rows}
    except Exception:
        log.warning("OCR cache read failed for %s", file_hash, exc_info=True)
    return None


def cache_save(db_path: str, file_hash: str, page_texts: dict[int, str]) -> None:
    """Write pages then stamp the completion record."""
    try:
        con = _get_cache_conn(db_path)
        now = time.time()
        con.executemany(
            "INSERT OR REPLACE INTO ocr_cache (content_hash, page_num, text, timestamp) VALUES (?,?,?,?)",
            [(file_hash, pn, txt, now) for pn, txt in page_texts.items()],
        )
        con.execute(
            "INSERT OR REPLACE INTO ocr_complete (content_hash, total_pages, timestamp) VALUES (?,?,?)",
            (file_hash, len(page_texts), now),
        )
        con.commit()
    except Exception:
        log.warning("OCR cache write failed for %s", file_hash, exc_info=True)


def cache_load_page(db_path: str, file_hash: str, page_num: int) -> str | None:
    """Fetch a single page's text on demand (for the UI review panel)."""
    try:
        con = _get_cache_conn(db_path)
        row = con.execute(
            "SELECT text FROM ocr_cache WHERE content_hash=? AND page_num=?",
            (file_hash, page_num),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        log.debug(
            "OCR cache page read failed for %s p%d", file_hash, page_num, exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# OCR backends  (worker-process scope – lazy globals reset per spawn)
#   win32  -> Windows Media OCR (winrt), RapidOCR as backup
#   darwin -> ocrmac / Apple Vision,     RapidOCR as backup
#   linux  -> RapidOCR
# _use_rapidocr=True forces RapidOCR everywhere (toggle in the dashboard).
# ---------------------------------------------------------------------------

_use_rapidocr = False
_ocr_loop: asyncio.AbstractEventLoop | None = None
_ocr_engine: Any = None
_rapidocr_engine: Any = None


def set_ocr_backend(use_rapidocr: bool) -> None:
    global _use_rapidocr
    _use_rapidocr = use_rapidocr


def ocr_backend_name() -> str:
    if _use_rapidocr or sys.platform.startswith("linux"):
        return "rapidocr"
    if sys.platform == "win32":
        return "winrt"
    if sys.platform == "darwin":
        return "ocrmac"
    return "rapidocr"


def _rapidocr_lazy() -> Any:
    global _rapidocr_engine
    if _rapidocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import]

        _rapidocr_engine = RapidOCR()
    return _rapidocr_engine


def _ocr_rapid(images: list[Image.Image]) -> list[str]:
    import numpy as np

    engine = _rapidocr_lazy()
    out: list[str] = []
    for img in images:
        try:
            result, _ = engine(np.array(img))
            out.append("\n".join(r[1] for r in result) if result else "")
        except Exception:
            log.warning("RapidOCR failed on a page", exc_info=True)
            out.append("")
    return out


def _ocr_mac(images: list[Image.Image]) -> list[str]:
    from ocrmac import ocrmac as _ocrmac  # type: ignore[import]

    out: list[str] = []
    for img in images:
        try:
            annotations = _ocrmac.OCR(img).recognize()
            out.append("\n".join(a[0] for a in annotations))
        except Exception:
            log.warning("ocrmac failed on a page", exc_info=True)
            out.append("")
    return out


def _ocr_engine_lazy() -> Any:
    global _ocr_engine
    if _ocr_engine is None:
        from winrt.windows.media.ocr import OcrEngine  # type: ignore[import]

        _ocr_engine = OcrEngine.try_create_from_user_profile_languages()
    return _ocr_engine


def _ocr_loop_lazy() -> asyncio.AbstractEventLoop:
    global _ocr_loop
    if _ocr_loop is None or _ocr_loop.is_closed():
        _ocr_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_ocr_loop)
    return _ocr_loop


async def _decode_bitmap(pil_img: Image.Image) -> Any:
    from winrt.windows.graphics.imaging import BitmapDecoder  # type: ignore[import]
    from winrt.windows.security.cryptography import (
        CryptographicBuffer,  # type: ignore[import]
    )
    from winrt.windows.storage.streams import (
        InMemoryRandomAccessStream,  # type: ignore[import]
    )

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    winrt_buf = CryptographicBuffer.create_from_byte_array(buf.getvalue())
    stream = InMemoryRandomAccessStream()
    await stream.write_async(winrt_buf)
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    return await decoder.get_software_bitmap_async()


async def _ocr_pipeline_async(images: list[Image.Image]) -> list[str]:
    engine = _ocr_engine_lazy()
    if not engine:
        return [""] * len(images)
    try:
        bitmaps = await asyncio.gather(*(_decode_bitmap(img) for img in images))
    except Exception:
        return [""] * len(images)
    results: list[str] = []
    for bitmap in bitmaps:
        try:
            result = await engine.recognize_async(bitmap)
            results.append(result.text if result else "")
        except Exception:
            results.append("")
    return results


def _ocr_winrt(images: list[Image.Image]) -> list[str] | None:
    """Returns None when the Windows engine is unavailable (triggers backup)."""
    try:
        if not _ocr_engine_lazy():
            log.warning("Windows OCR engine unavailable")
            return None
        return _ocr_loop_lazy().run_until_complete(_ocr_pipeline_async(images))
    except Exception:
        log.warning("Windows OCR pipeline failed", exc_info=True)
        return None


def ocr_all_pages(images: list[Image.Image]) -> list[str]:
    if not images:
        return []
    backend = ocr_backend_name()
    if backend == "winrt":
        results = _ocr_winrt(images)
        if results is not None:
            return results
        log.warning("Falling back to RapidOCR backup engine")
    elif backend == "ocrmac":
        try:
            return _ocr_mac(images)
        except Exception:
            log.warning("ocrmac unavailable — falling back to RapidOCR", exc_info=True)
    try:
        return _ocr_rapid(images)
    except Exception:
        log.error("RapidOCR unavailable — returning empty text", exc_info=True)
        return [""] * len(images)


def render_page(page: fitz.Page, dpi: int = OCR_DPI) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _ocr_pages_streaming(
    doc: fitz.Document,
    dpi: int = OCR_DPI,
    batch_size: int = OCR_BATCH_SIZE,
):
    """Generator: render + OCR `batch_size` pages at a time to cap RAM usage."""
    total = len(doc)
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch_images = [render_page(doc[i], dpi=dpi) for i in range(start, end)]
        texts = ocr_all_pages(batch_images)
        for offset, text in enumerate(texts):
            yield start + offset, text


# ---------------------------------------------------------------------------
# Core per-file processing  (runs in worker processes)
# ---------------------------------------------------------------------------


def process_file(
    file_path: str,
    output_dir: str,
    cache_db: str,
) -> dict[str, Any]:
    """
    Process a single file. Queries and automaton come from the worker-global
    set by _worker_init — not passed as arguments to avoid repeated pickling.
    """
    A = _worker_automaton
    queries = _worker_queries

    if A is None:
        log.error("Worker automaton not initialised for %s", file_path)
        return {"counts": {}, "review_data": []}

    p_obj = Path(file_path)
    file_ext = p_obj.suffix.lower()
    file_hash = content_hash(file_path)
    is_pdf = file_ext == ".pdf"

    page_texts: dict[int, str] = {}

    # ---- extract text (or streaming OCR) ----------------------------------
    if is_pdf:
        try:
            # Open once, keep open for both extraction AND output writing
            doc = fitz.open(_unc(file_path))
        except Exception as exc:
            log.error("Cannot open PDF %s: %s", file_path, exc)
            return {"counts": {}, "review_data": []}

        total_pages = len(doc)
        cached = cache_load(cache_db, file_hash, total_pages)
        if cached is not None:
            page_texts = cached
        else:
            sample_n = min(OCR_SAMPLE_PAGES, total_pages)
            sample_chars = sum(len(doc[i].get_text() or "") for i in range(sample_n))
            if sample_chars >= OCR_SAMPLE_THRESH:
                for i in range(total_pages):
                    page_texts[i] = doc[i].get_text() or ""
                # Save native-text pages to cache so the review panel can fetch them
                cache_save(cache_db, file_hash, page_texts)
            else:
                # Stream — don't hold all rendered pages in RAM
                for page_idx, text in _ocr_pages_streaming(doc):
                    page_texts[page_idx] = text
                cache_save(cache_db, file_hash, page_texts)

    else:
        total_pages = 1
        cached = cache_load(cache_db, file_hash, total_pages)
        if cached is not None:
            page_texts = cached
        else:
            try:
                with Image.open(file_path) as img:
                    img_corrected = ImageOps.exif_transpose(img)
                    results = ocr_all_pages([img_corrected.convert("RGB")])
                    page_texts = {0: results[0] if results else ""}
                    cache_save(cache_db, file_hash, page_texts)
            except Exception as exc:
                log.error("Cannot open image %s: %s", file_path, exc)
                return {"counts": {}, "review_data": []}

    # ---- match queries against extracted text -----------------------------
    query_token_lists = _worker_query_tokens or [query_tokens(q) for q in queries]
    match_counts, review_items = match_pages(
        page_texts, A, queries, query_token_lists, file_path, file_hash
    )

    # ---- write approved pages to output folder (still inside open doc) ----
    approved_by_query: dict[str, list[int]] = defaultdict(list)
    for item in review_items:
        if item["pre_approved"]:
            approved_by_query[item["query"]].append(item["page_idx"])

    if is_pdf:
        # Each approved page becomes its own single-page PDF so the review
        # panel can approve/reject pages independently.
        for q, pages in approved_by_query.items():
            try:
                folder = Path(output_dir) / safe_folder_name(q)
                folder.mkdir(parents=True, exist_ok=True)
                for pn in pages:
                    out_doc = fitz.open()
                    out_doc.insert_pdf(doc, from_page=pn, to_page=pn)
                    out_doc.save(
                        _unc(str(folder / output_page_name(p_obj.stem, pn, file_hash))),
                        garbage=4,
                        deflate=True,
                    )
                    out_doc.close()
            except Exception as exc:
                log.error("Failed to write PDF output for %s: %s", file_path, exc)
        doc.close()
    else:
        for q, pages in approved_by_query.items():
            if pages:
                try:
                    folder = Path(output_dir) / safe_folder_name(q)
                    folder.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(
                        file_path, folder / f"{p_obj.stem}_{file_hash}{file_ext}"
                    )
                except Exception as exc:
                    log.error("Failed to copy image output for %s: %s", file_path, exc)

    return {"counts": match_counts, "review_data": review_items}


def process_file_batch(
    file_paths: list[str],
    output_dir: str,
    cache_db: str,
) -> dict[str, Any]:
    """Process a list of small files sequentially inside one worker call."""
    aggregate_counts: dict[str, int] = {q: 0 for q in _worker_queries}
    all_review: list[dict] = []

    for fp in file_paths:
        result = process_file(fp, output_dir, cache_db)
        for q, c in result["counts"].items():
            aggregate_counts[q] += c
        all_review.extend(result["review_data"])

    return {"counts": aggregate_counts, "review_data": all_review}


# ---------------------------------------------------------------------------
# Report persistence (shared by the batch run and the manual review panel)
# ---------------------------------------------------------------------------


def write_reports(
    output_dir: str, counts: dict[str, int], review_data: list[dict]
) -> None:
    try:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        with open(out_path / "report.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Query", "Total Approved Hits"])
            writer.writerows(counts.items())

        conn = sqlite3.connect(str(out_path / "extraction_history.db"))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS summary_metrics (query TEXT PRIMARY KEY, hits INTEGER)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS candidates_log (
                    id TEXT PRIMARY KEY, query TEXT, file_path TEXT,
                    page_index INTEGER, pre_approved INTEGER
                )
            """)
            cursor.executemany(
                "INSERT OR REPLACE INTO summary_metrics VALUES (?,?)",
                list(counts.items()),
            )
            cursor.executemany(
                "INSERT OR REPLACE INTO candidates_log VALUES (?,?,?,?,?)",
                [
                    (
                        item["id"],
                        item["query"],
                        item["file_path"],
                        item["page_idx"],
                        1 if item["pre_approved"] else 0,
                    )
                    for item in review_data
                ],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.error("Failed to write output reports: %s", exc)


# ---------------------------------------------------------------------------
# Merge all extracted outputs into one bookmarked PDF
# Output structure: output_dir/<query folder>/<stem>_pN_<hash>.pdf (+ images).
# Each query folder becomes a level-1 bookmark; each file a level-2 bookmark.
# ---------------------------------------------------------------------------


def _image_pdf_bytes(path: Path) -> bytes:
    """Convert an image file to single-page PDF bytes (Pillow fallback for HEIF)."""
    try:
        with fitz.open(_unc(str(path))) as img_doc:
            return img_doc.convert_to_pdf()
    except Exception:
        with Image.open(path) as img:
            rgb = ImageOps.exif_transpose(img).convert("RGB")
            buf = io.BytesIO()
            rgb.save(buf, format="PDF")
            return buf.getvalue()


def merge_outputs_to_pdf(output_dir: str) -> tuple[str, int, int]:
    """Merge every extracted output under output_dir into one bookmarked PDF.

    Returns (merged_path, page_count, files_merged)."""
    out_root = Path(output_dir)
    merged = fitz.open()
    toc: list[list] = []
    files_merged = 0

    folders = sorted(
        (p for p in out_root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()
    )
    for folder in folders:
        children: list[list] = []
        files = sorted(
            (
                f
                for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
            ),
            key=lambda p: p.name.lower(),
        )
        for f in files:
            start = merged.page_count + 1
            try:
                if f.suffix.lower() == ".pdf":
                    with fitz.open(_unc(str(f))) as src:
                        merged.insert_pdf(src)
                else:
                    with fitz.open("pdf", _image_pdf_bytes(f)) as src:
                        merged.insert_pdf(src)
            except Exception as exc:
                log.error("Skipping %s during merge: %s", f, exc)
                continue
            if merged.page_count >= start:
                children.append([2, f.stem, start])
                files_merged += 1
        if children:
            toc.append([1, folder.name, children[0][2]])
            toc.extend(children)

    merged_path = out_root / MERGED_PDF_NAME
    if files_merged:
        merged.set_toc(toc)
        merged.save(_unc(str(merged_path)), garbage=4, deflate=True)
    page_count = merged.page_count
    merged.close()
    return str(merged_path), page_count, files_merged
