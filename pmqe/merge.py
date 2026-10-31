"""Merge a run's extracted pages into one PDF with a bookmark per query.

Pages are copied from their source documents (each opened once, with shared
fonts copied once) rather than from the thousands of single-page output
files, which is many times faster. A page is included only while its output
file exists, so deleting an output by hand still removes it from the merge.
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageOps

from .extract import Cancelled, content_hash, one_at_a_time
from .models import OUTPUT_NAME_RE
from .store import MERGED_PDF_NAME, NoRunFound, RunStore
from .util import atomic_write, long_path, natural_key

log = logging.getLogger(__name__)


@dataclass
class MergeResult:
    path: str
    pages: int
    files: int
    skipped: list[str] = field(default_factory=list)


@dataclass
class _Entry:
    output: str  # the extracted page file
    title: str
    source: str | None = None  # source PDF to copy the page from, when known
    page: int = 0
    source_hash: str = ""  # its content hash at extraction time


def _plan_from_store(output_dir: str) -> list[tuple[str, list[_Entry]]]:
    """Approved pages from the run record, in query order, then by source
    file (natural order) and page number."""
    with RunStore.open(output_dir) as store:
        queries = store.info().queries
        approved = [it for it in store.items() if it.approved]
    by_query: dict[int, list] = {}
    for it in approved:
        by_query.setdefault(it.query_idx, []).append(it)
    plan = []
    for qi, q in enumerate(queries):
        seen: set[str] = set()
        entries = []
        for it in sorted(
            by_query.get(qi, []), key=lambda i: (natural_key(i.file_path), i.page_idx)
        ):
            out = os.path.join(output_dir, it.folder, it.output_name)
            if out in seen:  # identical copies of one file share an output
                continue
            seen.add(out)
            is_pdf = it.file_path.lower().endswith(".pdf")
            title = (
                f"{it.file_name} — page {it.page_idx + 1}"
                if is_pdf or it.page_count > 1
                else it.file_name
            )
            entries.append(
                _Entry(out, title, it.file_path if is_pdf else None, it.page_idx, it.file_hash)
            )
        plan.append((q.text, entries))
    return plan


def _plan_from_folders(output_dir: str) -> list[tuple[str, list[_Entry]]]:
    """Fallback for output folders written by older versions."""
    plan = []
    root = Path(output_dir)
    for folder in sorted(
        (p for p in root.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)
    ):
        parsed = []
        for f in folder.iterdir():
            m = OUTPUT_NAME_RE.match(f.name)
            if f.is_file() and m:
                page = int(m["page"]) if m["page"] else 0
                parsed.append((natural_key(m["stem"]), m["hash"], page, f, m))
        parsed.sort(key=lambda t: t[:3])
        entries = [
            _Entry(str(f), f"{m['stem']} — page {page}" if page else f"{m['stem']}.{m['ext']}")
            for _, _, page, f, m in parsed
        ]
        plan.append((folder.name, entries))
    return plan


def _image_pdf_bytes(path: str) -> bytes:
    try:
        with fitz.open(long_path(path)) as img_doc:
            return img_doc.convert_to_pdf()
    except Exception:  # e.g. HEIC, which MuPDF cannot decode
        with Image.open(long_path(path)) as img:
            buf = io.BytesIO()
            ImageOps.exif_transpose(img).convert("RGB").save(buf, format="PDF")
            return buf.getvalue()


class _Sources:
    """Keeps the current source PDF open while consecutive pages come from it."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.doc: fitz.Document | None = None
        self.failed: set[str] = set()

    def get(self, path: str, expected_hash: str) -> fitz.Document | None:
        """The source, or None if it is unreadable or has changed since the
        run (then the extracted page file is used instead)."""
        if path == self.path:
            return self.doc
        self.close()
        if path in self.failed:
            return None
        if expected_hash and content_hash(path) != expected_hash:
            self.failed.add(path)
            return None
        try:
            doc = fitz.open(long_path(path))
            if doc.needs_pass and not doc.authenticate(""):
                raise ValueError("password-protected")
        except Exception:
            self.failed.add(path)
            return None
        self.path, self.doc = path, doc
        return doc

    def close(self) -> None:
        if self.doc is not None:
            self.doc.close()
        self.path, self.doc = None, None


def _insert(
    merged: fitz.Document, entry: _Entry, sources: _Sources, last_from_source: bool
) -> None:
    if entry.source:
        src = sources.get(entry.source, entry.source_hash)
        if src is not None and entry.page < src.page_count:
            merged.insert_pdf(src, from_page=entry.page, to_page=entry.page, final=last_from_source)
            return
    # Source moved, not a PDF, or a legacy output: use the extracted file.
    if entry.output.lower().endswith(".pdf"):
        with fitz.open(long_path(entry.output)) as doc:
            merged.insert_pdf(doc)
    else:
        with fitz.open("pdf", _image_pdf_bytes(entry.output)) as doc:
            merged.insert_pdf(doc)


def _save_compact(doc: fitz.Document, path: str) -> None:
    """garbage=4 de-duplicates the fonts every source embeds (about 3x
    smaller here); object streams shave off another 10%."""
    try:
        doc.save(path, garbage=4, deflate=True, use_objstms=True)
    except TypeError:  # PyMuPDF without object-stream support
        doc.save(path, garbage=4, deflate=True)


@one_at_a_time
def merge_outputs_to_pdf(
    output_dir: str,
    progress: Callable[[int, int], None] | None = None,
    cancel: Any = None,
) -> MergeResult:
    """Combine every approved page into ``merged_all_queries.pdf``.

    Level-1 bookmarks are queries, level-2 bookmarks are pages. Returns a
    result with zero files when there is nothing to merge (no file is
    written then).
    """
    try:
        plan = _plan_from_store(output_dir)
    except (NoRunFound, sqlite3.Error):
        plan = _plan_from_folders(output_dir)

    total = sum(len(entries) for _, entries in plan)
    merged = fitz.open()
    sources = _Sources()
    toc: list[list[Any]] = []
    files = done = 0
    skipped: list[str] = []
    try:
        for title, entries in plan:
            children: list[list[Any]] = []
            for n, entry in enumerate(entries):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                done += 1
                if progress:
                    progress(done, total)
                if not os.path.exists(long_path(entry.output)):
                    continue  # removed by hand after the run: leave it out
                nxt = entries[n + 1] if n + 1 < len(entries) else None
                last = nxt is None or nxt.source != entry.source
                start = merged.page_count + 1
                try:
                    _insert(merged, entry, sources, last)
                except Exception as exc:
                    log.warning("Skipping %s during merge: %s", entry.output, exc)
                    skipped.append(entry.output)
                    continue
                if merged.page_count >= start:
                    children.append([2, entry.title, start])
                    files += 1
            if children:
                toc.append([1, title, children[0][2]])
                toc.extend(children)
        sources.close()

        path = os.path.join(output_dir, MERGED_PDF_NAME)
        pages = merged.page_count
        if files:
            merged.set_toc(toc)
            atomic_write(path, lambda tmp: _save_compact(merged, tmp))
        return MergeResult(path, pages, files, skipped)
    finally:
        sources.close()
        merged.close()
