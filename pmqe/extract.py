"""Per-file work: discover inputs, get page text (native or OCR), match the
queries and write the matched pages. Runs inside worker processes, so this
module (and everything it imports) must stay free of Qt.
"""

from __future__ import annotations

import functools
import hashlib
import io
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

import fitz  # PyMuPDF
import pillow_heif
from PIL import Image, ImageOps

from . import ocr
from .cache import OcrCache, open_cache
from .models import FileResult, ReviewItem
from .text import Matcher, Query
from .util import copy_atomic, display_path, ensure_dir, is_within, long_path, write_bytes_atomic

pillow_heif.register_heif_opener()
log = logging.getLogger(__name__)

PDF_EXTENSION = ".pdf"
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".heif"}
)
SUPPORTED_EXTENSIONS = frozenset({PDF_EXTENSION}) | IMAGE_EXTENSIONS
MULTIPAGE_EXTENSIONS = frozenset({".tif", ".tiff"})

OCR_DPI = 200
OCR_BATCH_SIZE = 8  # pages rendered + OCR'd at a time (bounds memory)
CONTENT_HASH_BYTES = 1 << 20
PREVIEW_DPI = 150
PREVIEW_MAX_SIDE = 3200

# Per-page OCR decision for PDFs (counts of non-whitespace characters):
TEXT_LAYER_FULL = 200  # this much native text means a real text layer: no OCR
TEXT_LAYER_EMPTY = 32  # less than this plus any image: treat as a scan
SCAN_COVERAGE = 0.5  # sparse text plus an image covering half the page: OCR

Image.MAX_IMAGE_PIXELS = 300_000_000  # allow large scans, still stop decompression bombs

# PyMuPDF releases the GIL while it works but is not thread-safe. In the app,
# page previews, approve/undo, the text view and merging run on different
# threads, so every entry point that uses it takes this lock. Worker
# processes are single-threaded, so there it is never contended.
MUPDF_LOCK = threading.RLock()
_P = ParamSpec("_P")
_R = TypeVar("_R")


def one_at_a_time(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run ``fn`` holding :data:`MUPDF_LOCK`."""

    @functools.wraps(fn)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with MUPDF_LOCK:
            return fn(*args, **kwargs)

    return locked


class Cancelled(Exception):
    """Raised inside a worker when the run is cancelled."""


class SourceError(Exception):
    """A file that cannot be processed, with a message fit for the user."""


# ---------------------------------------------------------------------------
# Discovery and hashing
# ---------------------------------------------------------------------------


def discover_files(
    input_dir: str, exclude: Sequence[str] = ()
) -> tuple[list[str], list[tuple[str, str]]]:
    """Supported files under ``input_dir``, recursively and in a stable order.

    Folders in ``exclude`` (e.g. an output folder inside the input folder)
    are skipped, as are macOS resource forks and Office lock files.
    Returns ``(files, problems)`` where problems are unreadable folders.
    """
    files: list[str] = []
    problems: list[tuple[str, str]] = []

    def on_error(exc: OSError) -> None:
        problems.append(
            (display_path(exc.filename or input_dir), f"Folder unreadable: {exc.strerror}")
        )

    for dirpath, dirnames, filenames in os.walk(long_path(input_dir), onerror=on_error):
        dirnames[:] = sorted(
            d for d in dirnames if not any(is_within(os.path.join(dirpath, d), x) for x in exclude)
        )
        for name in sorted(filenames):
            if name.startswith(("._", "~$")):
                continue
            if os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS:
                files.append(display_path(os.path.join(dirpath, name)))
    return files, problems


def content_hash(path: str) -> str:
    """Fast identity for a file: size, modification time and the first MiB.

    Used as the OCR cache key and in output file names. Falls back to a hash
    of the path when the file cannot be read.
    """
    try:
        lp = long_path(path)
        st = os.stat(lp)
        h = hashlib.blake2b(digest_size=16)
        h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
        with open(lp, "rb") as fh:
            h.update(fh.read(CONTENT_HASH_BYTES))
        return h.hexdigest()
    except OSError:
        return hashlib.blake2b(path.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


# ---------------------------------------------------------------------------
# Opening sources
# ---------------------------------------------------------------------------


@dataclass
class Source:
    """An opened input file, seen as a sequence of pages."""

    path: str
    kind: str  # "pdf" | "multipage" (TIFF) | "image"
    page_count: int
    doc: fitz.Document | None = None
    _image: Image.Image | None = field(default=None, repr=False)
    _as_pdf: fitz.Document | None = field(default=None, repr=False)

    @property
    def image(self) -> Image.Image:
        """The (EXIF-rotated) image of a single-image source, loaded lazily."""
        if self._image is None:
            with Image.open(long_path(self.path)) as im:
                self._image = ImageOps.exif_transpose(im)
                self._image.load()
        return self._image

    def frame(self, idx: int) -> Image.Image:
        """One page of a multi-page TIFF at its native resolution."""
        with Image.open(long_path(self.path)) as im:
            im.seek(idx)
            return im.copy()

    def as_pdf(self) -> fitz.Document:
        """A PDF rendition of a multi-page image (built once, on demand)."""
        if self._as_pdf is None:
            try:
                with fitz.open(long_path(self.path)) as img_doc:
                    self._as_pdf = fitz.open("pdf", img_doc.convert_to_pdf())
            except Exception:
                # MuPDF cannot read every TIFF flavour; Pillow can.
                frames = [self.frame(i).convert("RGB") for i in range(self.page_count)]
                buf = io.BytesIO()
                frames[0].save(buf, format="PDF", save_all=True, append_images=frames[1:])
                self._as_pdf = fitz.open("pdf", buf.getvalue())
        return self._as_pdf

    def close(self) -> None:
        for d in (self.doc, self._as_pdf):
            if d is not None:
                d.close()
        if self._image is not None:
            self._image.close()


@contextmanager
def open_source(path: str) -> Iterator[Source]:
    ext = os.path.splitext(path)[1].lower()
    if ext == PDF_EXTENSION:
        try:
            doc = fitz.open(long_path(path))
        except Exception as exc:
            raise SourceError(f"Not a readable PDF ({exc})") from exc
        if doc.needs_pass and not doc.authenticate(""):
            doc.close()
            raise SourceError("PDF is password-protected")
        if doc.page_count == 0:
            doc.close()
            raise SourceError("PDF has no pages")
        src = Source(path, "pdf", doc.page_count, doc=doc)
    elif ext in IMAGE_EXTENSIONS:
        try:
            with Image.open(long_path(path)) as im:
                frames = getattr(im, "n_frames", 1) if ext in MULTIPAGE_EXTENSIONS else 1
        except Exception as exc:
            raise SourceError(f"Not a readable image ({exc})") from exc
        src = Source(path, "multipage" if frames > 1 else "image", frames)
    else:
        raise SourceError(f"Unsupported file type {ext!r}")
    try:
        yield src
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Page text
# ---------------------------------------------------------------------------


def _visible_chars(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def page_needs_ocr(page: fitz.Page, native_text: str) -> bool:
    """Decide per page whether OCR is needed.

    Handles mixed PDFs (a typed cover sheet followed by scans) and scans
    that carry a short stamped text layer such as a document ID.
    """
    chars = _visible_chars(native_text)
    if chars >= TEXT_LAYER_FULL:
        return False
    try:
        images = page.get_image_info()
    except Exception:
        return chars < TEXT_LAYER_EMPTY
    if not images:
        return False  # nothing to read: blank or vector-only page
    if chars < TEXT_LAYER_EMPTY:
        return True
    area = abs(page.rect) or 1.0
    covered = max(abs(fitz.Rect(i["bbox"]) & page.rect) for i in images)
    return covered / area >= SCAN_COVERAGE


def combine_text(native: str, ocr_text: str) -> str:
    return f"{native.rstrip()}\n\n{ocr_text}" if native.strip() else ocr_text


def _native_text(page: fitz.Page) -> str:
    return page.get_text() or ""


def _render_for_ocr(src: Source, idx: int) -> Image.Image:
    if src.kind == "pdf":
        assert src.doc is not None
        page = src.doc.load_page(idx)
        longest_pt = max(page.rect.width, page.rect.height) or 1.0
        dpi = min(OCR_DPI, int(ocr.OCR_MAX_SIDE * 72 / longest_pt))
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
        return Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if src.kind == "multipage":
        return src.frame(idx)
    return src.image


def _check_cancel(cancel: Any) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


@dataclass
class PageTexts:
    texts: dict[int, str]
    ocr_pages: int = 0
    ocr_cached: int = 0
    ocr_failed: int = 0
    warnings: list[str] = field(default_factory=list)


def extract_texts(
    src: Source,
    file_hash: str,
    cache: OcrCache | None,
    *,
    cancel: Any = None,
    engine: str | None = None,
    strict: bool = False,
) -> PageTexts:
    """Text for every page: the native text layer where there is one, OCR
    (cached by content hash) where there is not. Cached text from ``engine``
    is preferred; with ``strict`` only that engine's cache counts."""
    out = PageTexts(texts={})
    native: dict[int, str] = {}
    need_ocr: list[int] = []

    if src.kind == "pdf":
        assert src.doc is not None
        for i in range(src.page_count):
            _check_cancel(cancel)
            try:
                page = src.doc.load_page(i)
                native[i] = _native_text(page)
                needs = page_needs_ocr(page, native[i])
            except Exception as exc:
                out.warnings.append(f"page {i + 1}: unreadable ({exc})")
                native[i], needs = "", False
            if needs:
                need_ocr.append(i)
            else:
                out.texts[i] = native[i]
    else:
        need_ocr = list(range(src.page_count))

    out.ocr_pages = len(need_ocr)
    if not need_ocr:
        return out

    cached = cache.get(file_hash, engine, strict) if cache else {}
    todo: list[int] = []
    for i in need_ocr:
        if i in cached:
            out.texts[i] = combine_text(native.get(i, ""), cached[i][0])
            out.ocr_cached += 1
        else:
            todo.append(i)

    for start in range(0, len(todo), OCR_BATCH_SIZE):
        _check_cancel(cancel)
        batch: list[int] = []
        images: list[Image.Image] = []
        for i in todo[start : start + OCR_BATCH_SIZE]:
            try:
                images.append(_render_for_ocr(src, i))
                batch.append(i)
            except Exception as exc:
                out.warnings.append(f"page {i + 1}: could not render for OCR ({exc})")
                out.texts[i] = native.get(i, "")
                out.ocr_failed += 1
        fresh: dict[int, tuple[str, str]] = {}
        for i, result in zip(batch, ocr.ocr_images(images), strict=True):
            if result is None:
                out.ocr_failed += 1
                out.texts[i] = native.get(i, "")
            else:
                fresh[i] = result
                out.texts[i] = combine_text(native.get(i, ""), result[0])
        if cache is not None:
            cache.put(file_hash, fresh)  # per batch: a crash keeps finished work
    if out.ocr_failed:
        out.warnings.append(
            f"OCR failed on {out.ocr_failed} page(s); they will be retried next run"
        )
    return out


@one_at_a_time
def review_text(
    file_path: str,
    file_hash: str,
    page_idx: int,
    cache: OcrCache | None,
    engine: str | None = None,
) -> tuple[str, str]:
    """The text a page was matched against, plus a label for where it came
    from. Used by the review panel; mirrors :func:`extract_texts` (pass the
    run's OCR ``engine`` so the same cached text is chosen)."""
    with open_source(file_path) as src:
        native = ""
        if src.kind == "pdf":
            assert src.doc is not None
            page = src.doc.load_page(page_idx)
            native = _native_text(page)
            if not page_needs_ocr(page, native):
                return native, "PDF text layer"
        hit = cache.page(file_hash, page_idx, engine) if cache else None
        if hit is None:
            return native, "No OCR text cached for this page"
        label = ocr.ENGINE_LABELS.get(hit[1], hit[1])
        return combine_text(native, hit[0]), (f"Text layer + {label}" if native.strip() else label)


@one_at_a_time
def render_preview(file_path: str, page_idx: int, max_side: int = PREVIEW_MAX_SIDE) -> Image.Image:
    """An RGB rendering of one page for display."""
    with open_source(file_path) as src:
        if src.kind == "pdf":
            assert src.doc is not None
            page = src.doc.load_page(page_idx)
            longest_pt = max(page.rect.width, page.rect.height) or 1.0
            dpi = max(36, min(PREVIEW_DPI, int(max_side * 72 / longest_pt)))
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img = src.frame(page_idx) if src.kind == "multipage" else src.image
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return img


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def output_path(item: ReviewItem, output_dir: str) -> str:
    return os.path.join(output_dir, item.folder, item.output_name)


def _single_page_pdf(doc: fitz.Document, page_idx: int) -> bytes:
    out = fitz.open()
    try:
        out.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
        return out.tobytes(garbage=3, deflate=True)
    finally:
        out.close()


class _OutputWriter:
    """Writes matched pages for one source, rendering each page only once
    even when several queries matched it."""

    def __init__(self, src: Source, output_dir: str) -> None:
        self.src = src
        self.output_dir = output_dir
        self._page_pdf: dict[int, bytes] = {}

    def write(self, item: ReviewItem) -> None:
        dest = output_path(item, self.output_dir)
        ensure_dir(os.path.dirname(dest))
        if self.src.kind == "image":
            copy_atomic(self.src.path, dest)
            return
        data = self._page_pdf.get(item.page_idx)
        if data is None:
            doc = self.src.doc if self.src.kind == "pdf" else self.src.as_pdf()
            assert doc is not None
            data = self._page_pdf[item.page_idx] = _single_page_pdf(doc, item.page_idx)
        write_bytes_atomic(dest, data)


@one_at_a_time
def export_item(item: ReviewItem, output_dir: str) -> str:
    """Write the output for one manually approved page. Returns its path."""
    with open_source(item.file_path) as src:
        _OutputWriter(src, output_dir).write(item)
    return output_path(item, output_dir)


def remove_item_output(item: ReviewItem, output_dir: str) -> bool:
    """Delete the output for one rejected page, if it exists."""
    try:
        os.remove(long_path(output_path(item, output_dir)))
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Worker entry points
# ---------------------------------------------------------------------------


@dataclass
class _WorkerState:
    matcher: Matcher
    cache: OcrCache | None
    cancel: Any
    engine: str  # the run's OCR engine; its cached results are preferred
    strict: bool  # RapidOCR was forced: ignore other engines' cached text


_state: _WorkerState | None = None


def init_worker(
    queries: list[Query],
    whole_words: bool,
    force_rapidocr: bool,
    cache_path: str | None,
    cancel_event: Any = None,
    log_queue: Any = None,
) -> None:
    """Process initializer: runs once per worker before any job."""
    global _state
    if log_queue is not None:
        import logging.handlers

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(logging.handlers.QueueHandler(log_queue))
        root.setLevel(logging.INFO)
        for noisy in ("PIL", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    ocr.set_backend(force_rapidocr)
    _state = _WorkerState(
        matcher=Matcher(queries, whole_words),
        cache=open_cache(cache_path),
        cancel=cancel_event,
        engine=ocr.backend_name(),
        strict=force_rapidocr,
    )


@one_at_a_time
def process_file(path: str, output_dir: str) -> FileResult:
    """Extract, match and write outputs for one file. Never raises except
    :class:`Cancelled`; failures are reported in the result."""
    if _state is None:
        raise RuntimeError("init_worker() must run before process_file()")
    started = time.perf_counter()
    res = FileResult(path=path)
    try:
        res.file_hash = content_hash(path)
        with open_source(path) as src:
            res.pages = src.page_count
            texts = extract_texts(
                src,
                res.file_hash,
                _state.cache,
                cancel=_state.cancel,
                engine=_state.engine,
                strict=_state.strict,
            )
            res.ocr_pages, res.ocr_cached, res.ocr_failed = (
                texts.ocr_pages,
                texts.ocr_cached,
                texts.ocr_failed,
            )
            res.warnings.extend(texts.warnings)
            res.counts, res.items = _state.matcher.match_pages(
                texts.texts, path, res.file_hash, src.page_count
            )
            writer = _OutputWriter(src, output_dir)
            for item in res.items:
                if item.auto_approved:
                    _check_cancel(_state.cancel)
                    try:
                        writer.write(item)
                    except Exception as exc:
                        res.warnings.append(f"page {item.page_idx + 1}: output not written ({exc})")
    except Cancelled:
        raise
    except SourceError as exc:
        res.error = str(exc)
    except Exception as exc:
        log.exception("Failed on %s", path)
        res.error = f"{type(exc).__name__}: {exc}"
    finally:
        fitz.TOOLS.store_shrink(100)  # free MuPDF's resource cache between files
    res.seconds = time.perf_counter() - started
    if res.error:
        log.warning("Skipped %s: %s", path, res.error)
    return res


def ping() -> bool:
    """No-op job used to check that worker processes start."""
    return _state is not None


def run_job(paths: Sequence[str], output_dir: str) -> list[FileResult]:
    """One unit of pool work: a large file on its own or a batch of small ones."""
    return [process_file(p, output_dir) for p in paths]
