"""Plain data shared by the pipeline, the run store, the merge step and the UI.

Everything here is picklable: :class:`ReviewItem` and :class:`FileResult`
travel back from worker processes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MAX_STEM_CHARS = 80

# Output files are ``<stem>_p<page>_<hash>.pdf`` (document pages) or
# ``<stem>_<hash><ext>`` (single images). The 32-hex hash makes them safe to
# recognise when cleaning up an output folder.
OUTPUT_NAME_RE = re.compile(
    r"^(?P<stem>.+?)_(?:p(?P<page>\d+)_)?(?P<hash>[0-9a-f]{32})"
    r"\.(?P<ext>pdf|jpe?g|png|tiff?|bmp|webp|hei[cf])$",
    re.IGNORECASE,
)


def output_name(file_path: str, page_idx: int, file_hash: str, page_count: int) -> str:
    """Canonical output file name for one matched page.

    PDFs and multi-page images become single-page PDFs named after the page;
    single images are copied as-is.
    """
    base = os.path.basename(file_path)
    stem, ext = os.path.splitext(base)
    stem = stem[:MAX_STEM_CHARS].rstrip(" .") or "file"
    ext = ext.lower()
    if ext == ".pdf" or page_count > 1:
        return f"{stem}_p{page_idx + 1}_{file_hash}.pdf"
    return f"{stem}_{file_hash}{ext}"


@dataclass
class ReviewItem:
    """One (query, page) pair: an automatic match or a partial-match candidate."""

    id: str
    query: str
    query_idx: int
    folder: str
    file_path: str
    file_hash: str
    page_idx: int
    page_count: int
    auto_approved: bool
    missing_words: list[str]
    approved: bool | None = None  # current decision; defaults to auto_approved
    decided: bool = False  # True once a person approved or rejected it

    def __post_init__(self) -> None:
        if self.approved is None:
            self.approved = self.auto_approved

    @property
    def status(self) -> str:
        """``matched`` | ``candidate`` | ``approved`` | ``rejected``."""
        if not self.decided:
            return "matched" if self.auto_approved else "candidate"
        return "approved" if self.approved else "rejected"

    @property
    def output_name(self) -> str:
        return output_name(self.file_path, self.page_idx, self.file_hash, self.page_count)

    @property
    def file_name(self) -> str:
        return os.path.basename(self.file_path)


@dataclass
class FileResult:
    """Outcome of processing one input file in a worker."""

    path: str
    file_hash: str = ""
    pages: int = 0
    ocr_pages: int = 0  # pages that needed OCR
    ocr_cached: int = 0  # ...of which came from the OCR cache
    ocr_failed: int = 0  # ...of which OCR failed (not cached; retried next run)
    counts: dict[str, int] = field(default_factory=dict)
    items: list[ReviewItem] = field(default_factory=list)
    error: str | None = None  # fatal: the file could not be processed
    warnings: list[str] = field(default_factory=list)  # partial problems
    seconds: float = 0.0
