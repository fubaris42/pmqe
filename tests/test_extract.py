"""Per-file processing: discovery, OCR decisions, caching, outputs."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import fitz
import pytest
from PIL import Image

from pmqe import extract
from pmqe.cache import OcrCache
from pmqe.models import output_name
from pmqe.text import prepare_queries

from .conftest import FakeOcr, make_pdf, make_scanned_pdf, scan_image


def init(queries: list[str], cache_path: Path | None = None, whole_words: bool = True) -> None:
    extract.init_worker(
        prepare_queries(queries).queries,
        whole_words,
        False,
        str(cache_path) if cache_path else None,
    )


# -- discovery and hashing ---------------------------------------------------------


def test_discover_files_recurses_sorts_and_excludes(tmp_path: Path):
    root = tmp_path / "in"
    for rel in [
        "b.pdf",
        "a/z.JPG",
        "a/y.heic",
        "a/x.tiff",
        "notes.txt",
        "._b.pdf",
        "~$lock.pdf",
        "out/old.pdf",
    ]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    files, problems = extract.discover_files(str(root), exclude=[str(root / "out")])
    rel = [Path(f).relative_to(root).as_posix() for f in files]
    assert rel == ["b.pdf", "a/x.tiff", "a/y.heic", "a/z.JPG"]
    assert problems == []


def test_content_hash_tracks_content_and_tolerates_missing_files(tmp_path: Path):
    f = tmp_path / "a.bin"
    f.write_text("hello")
    h1 = extract.content_hash(str(f))
    assert h1 == extract.content_hash(str(f)) and len(h1) == 32
    f.write_text("goodbye")
    assert extract.content_hash(str(f)) != h1
    assert extract.content_hash("no/such/file") == extract.content_hash("no/such/file")


# -- per-page OCR decision -------------------------------------------------------------


def test_page_needs_ocr(tmp_path: Path):
    native = make_pdf(tmp_path / "n.pdf", ["plenty of text"])
    blank = fitz.open()
    blank.new_page()
    scanned = make_scanned_pdf(tmp_path / "s.pdf", 1)
    with fitz.open(native) as d:
        assert not extract.page_needs_ocr(d[0], d[0].get_text())
    assert not extract.page_needs_ocr(blank[0], "")  # nothing to read
    with fitz.open(scanned) as d:
        assert extract.page_needs_ocr(d[0], "")
        assert extract.page_needs_ocr(d[0], "DOC-ID 12345 stamped on a scan")


def test_mixed_pdf_ocrs_only_its_scanned_pages(tmp_path: Path, fake_ocr: FakeOcr):
    pdf = make_scanned_pdf(tmp_path / "mixed.pdf", 2, native_first="typed cover sheet")
    fake_ocr.queue = ["scanned keyword", "more scanned text"]
    with extract.open_source(str(pdf)) as src:
        texts = extract.extract_texts(src, "h" * 32, None)
    assert fake_ocr.calls == 2
    assert texts.ocr_pages == 2
    assert "typed cover sheet" in texts.texts[0]
    assert texts.texts[1] == "scanned keyword"


# -- OCR cache -----------------------------------------------------------------------------


def test_ocr_results_are_cached_and_failures_are_retried(tmp_path: Path, fake_ocr: FakeOcr):
    pdf = make_scanned_pdf(tmp_path / "scan.pdf", 2)
    cache = OcrCache(tmp_path / "cache.db")
    fake_ocr.queue = ["page one", None]  # second page fails
    with extract.open_source(str(pdf)) as src:
        first = extract.extract_texts(src, "h" * 32, cache)
    assert (first.ocr_failed, first.ocr_cached) == (1, 0)
    assert set(cache.get("h" * 32)) == {0}  # the failure was not cached

    fake_ocr.queue = ["page two"]
    with extract.open_source(str(pdf)) as src:
        second = extract.extract_texts(src, "h" * 32, cache)
    assert fake_ocr.calls == 3  # only the failed page was OCR'd again
    assert (second.ocr_cached, second.ocr_failed) == (1, 0)
    assert second.texts == {0: "page one", 1: "page two"}
    cache.close()


def test_cache_migrates_old_schema(tmp_path: Path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE ocr_cache (content_hash TEXT, page_num INT, text TEXT, timestamp REAL)"
    )
    con.execute("INSERT INTO ocr_cache VALUES ('h', 0, '', 0)")
    con.commit()
    con.close()
    cache = OcrCache(db)
    assert cache.get("h") == {}
    cache.put("h", {0: ("text", "fake")})
    assert cache.page("h", 0) == ("text", "fake")
    assert cache.stats() == (1, 1)
    cache.close()


# -- process_file ------------------------------------------------------------------------------


def test_process_file_writes_matching_pages(tmp_path: Path):
    src = make_pdf(
        tmp_path / "in" / "doc.pdf", ["the yearly invoice total", "unrelated", "invoice only"]
    )
    init(["invoice total"])
    res = extract.process_file(str(src), str(tmp_path / "out"))
    assert res.error is None and res.pages == 3
    assert res.counts == {"invoice total": 1}
    assert {i.page_idx: i.auto_approved for i in res.items} == {0: True, 2: False}
    out = tmp_path / "out" / "invoice total" / output_name(str(src), 0, res.file_hash, 3)
    with fitz.open(out) as doc:
        assert doc.page_count == 1 and "yearly invoice total" in doc[0].get_text()
    assert not list((tmp_path / "out").rglob("*.tmp"))


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda p: p.write_bytes(b"not a pdf"), "Not a readable PDF"),
        (lambda p: _encrypted_pdf(p), "password-protected"),
    ],
)
def test_process_file_reports_unreadable_files(tmp_path: Path, make, message: str):
    bad = tmp_path / "bad.pdf"
    make(bad)
    init(["keyword"])
    res = extract.process_file(str(bad), str(tmp_path / "out"))
    assert res.error and message in res.error and res.items == []


def _encrypted_pdf(path: Path) -> None:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret keyword")
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    doc.close()


def test_one_bad_file_does_not_sink_its_batch(tmp_path: Path):
    good1 = make_pdf(tmp_path / "a.pdf", ["keyword here"])
    bad = tmp_path / "b.pdf"
    bad.write_bytes(b"garbage")
    good2 = make_pdf(tmp_path / "c.pdf", ["keyword again"])
    init(["keyword"])
    results = extract.run_job([str(good1), str(bad), str(good2)], str(tmp_path / "out"))
    assert [r.error is None for r in results] == [True, False, True]
    assert sum(r.counts.get("keyword", 0) for r in results) == 2


def test_multipage_tiff_pages_become_single_page_pdfs(tmp_path: Path, fake_ocr: FakeOcr):
    tif = tmp_path / "fax.tif"
    frames = [scan_image(), scan_image(), scan_image()]
    frames[0].save(tif, save_all=True, append_images=frames[1:], compression="tiff_lzw")
    fake_ocr.queue = ["nothing", "the keyword", "nothing"]
    init(["keyword"])
    res = extract.process_file(str(tif), str(tmp_path / "out"))
    assert res.pages == 3 and res.counts == {"keyword": 1}
    out = tmp_path / "out" / "keyword" / f"fax_p2_{res.file_hash}.pdf"
    with fitz.open(out) as doc:
        assert doc.page_count == 1


def test_heic_images_are_read_and_copied(tmp_path: Path, fake_ocr: FakeOcr):
    heic = tmp_path / "IMG_0001.heic"
    scan_image().convert("RGB").save(heic)
    fake_ocr.queue = ["photo of an invoice"]
    init(["invoice"])
    res = extract.process_file(str(heic), str(tmp_path / "out"))
    assert res.error is None and res.counts == {"invoice": 1}
    assert (tmp_path / "out" / "invoice" / f"IMG_0001_{res.file_hash}.heic").exists()


def test_cancellation_stops_a_file(tmp_path: Path):
    import threading

    src = make_pdf(tmp_path / "doc.pdf", ["keyword"] * 3)
    ev = threading.Event()
    ev.set()
    extract.init_worker(prepare_queries(["keyword"]).queries, True, False, None, ev)
    with pytest.raises(extract.Cancelled):
        extract.process_file(str(src), str(tmp_path / "out"))


# -- review helpers --------------------------------------------------------------------------------


def test_export_and_remove_item_output(tmp_path: Path):
    src = make_pdf(tmp_path / "doc.pdf", ["total only", "nothing"])
    init(["invoice total"])
    res = extract.process_file(str(src), str(tmp_path / "out"))
    (candidate,) = res.items
    path = extract.export_item(candidate, str(tmp_path / "out"))
    assert os.path.exists(path)
    assert extract.remove_item_output(candidate, str(tmp_path / "out"))
    assert not os.path.exists(path)
    assert not extract.remove_item_output(candidate, str(tmp_path / "out"))


def test_review_text_reports_its_source(tmp_path: Path, fake_ocr: FakeOcr):
    native = make_pdf(tmp_path / "n.pdf", ["native words"])
    text, label = extract.review_text(str(native), "x", 0, None)
    assert "native words" in text and label == "PDF text layer"

    scan = make_scanned_pdf(tmp_path / "s.pdf", 1)
    cache = OcrCache(tmp_path / "c.db")
    assert extract.review_text(str(scan), "h", 0, cache)[1].startswith("No OCR text")
    cache.put("h", {0: ("ocr words", "rapidocr")})
    assert extract.review_text(str(scan), "h", 0, cache) == ("ocr words", "RapidOCR")
    cache.close()


def test_render_preview_bounds_size(tmp_path: Path):
    pdf = make_pdf(tmp_path / "doc.pdf", ["x"])
    img = extract.render_preview(str(pdf), 0, max_side=600)
    assert img.mode == "RGB" and max(img.size) <= 600
    big = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), "white").save(big)
    assert max(extract.render_preview(str(big), 0, max_side=1000).size) == 1000


def test_cache_prefers_the_runs_engine(tmp_path: Path):
    cache = OcrCache(tmp_path / "c.db")
    cache.put("h", {0: ("windows text", "winrt"), 1: ("windows only", "winrt")})
    cache.put("h", {0: ("rapid text", "rapidocr")})  # newer, other engine
    assert cache.get("h", "winrt")[0] == ("windows text", "winrt")
    assert cache.get("h", "rapidocr")[1] == ("windows only", "winrt")  # fallback
    assert cache.get("h", "rapidocr", strict=True) == {0: ("rapid text", "rapidocr")}
    assert cache.page("h", 0, "winrt") == ("windows text", "winrt")
    assert cache.page("h", 0, "rapidocr") == ("rapid text", "rapidocr")
    cache.close()


def test_pymupdf_calls_from_different_threads_take_turns(tmp_path: Path):
    """PyMuPDF releases the GIL but is not thread-safe: a preview render must
    wait while another thread (say, approving a page) is inside it."""
    pdf = make_pdf(tmp_path / "a.pdf", ["hello"])
    done = threading.Event()

    def render() -> None:
        extract.render_preview(str(pdf), 0)
        done.set()

    with extract.MUPDF_LOCK:
        worker = threading.Thread(target=render)
        worker.start()
        assert not done.wait(0.3)
    assert done.wait(30)
    worker.join()
