"""Tests for pmqe.core — pure logic, cache, matching, and PDF pipeline.

These deliberately avoid the OCR backends and Qt: PDFs used here carry a
native text layer, so process_file takes the text-extraction path.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import fitz
import pytest

from pmqe import core

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_query_tokens_lowercases_and_drops_single_chars():
    assert core.query_tokens("Invoice A 42 Total") == ["invoice", "42", "total"]
    assert core.query_tokens("a b c") == []
    assert core.query_tokens("") == []


def test_safe_folder_name_strips_illegal_chars():
    assert core.safe_folder_name('inv/oice: "total"?') == "invoice total"
    assert core.safe_folder_name("???") == "untitled_query"
    assert core.safe_folder_name("plain_name-1") == "plain_name-1"


def test_output_page_name_is_canonical():
    assert core.output_page_name("doc", 0, "abc123") == "doc_p1_abc123.pdf"
    assert core.output_page_name("doc", 9, "ff") == "doc_p10_ff.pdf"


def test_build_automaton_maps_tokens_to_queries():
    _, token_map = core.build_automaton(["red apple", "apple pie"])
    assert token_map["apple"] == {0, 1}
    assert token_map["red"] == {0}
    assert token_map["pie"] == {1}


def test_content_hash_changes_with_content(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    h1 = core.content_hash(str(f))
    assert h1 == core.content_hash(str(f))
    f.write_text("goodbye")
    assert core.content_hash(str(f)) != h1


def test_content_hash_missing_file_is_stable():
    h = core.content_hash("no/such/file.pdf")
    assert h == core.content_hash("no/such/file.pdf")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _match(queries: list[str], pages: dict[int, str]):
    automaton, _ = core.build_automaton(queries)
    token_lists = [core.query_tokens(q) for q in queries]
    return core.match_pages(pages, automaton, queries, token_lists, "f.pdf", "hash")


def test_match_pages_full_hit_is_pre_approved():
    counts, items = _match(["total invoice"], {0: "The Total Invoice amount is due"})
    assert counts == {"total invoice": 1}
    assert len(items) == 1
    assert items[0]["pre_approved"] is True
    assert items[0]["missing_words"] == []


def test_match_pages_partial_hit_is_candidate_only():
    counts, items = _match(["total invoice"], {0: "the total is here"})
    assert counts == {"total invoice": 0}
    assert len(items) == 1
    assert items[0]["pre_approved"] is False
    assert items[0]["missing_words"] == ["invoice"]


def test_match_pages_single_token_query_has_no_partial_candidates():
    counts, items = _match(["invoice"], {0: "nothing relevant"})
    assert counts == {"invoice": 0}
    assert items == []


def test_match_pages_tokenless_query_matches_nothing():
    counts, items = _match(["a b"], {0: "any text at all"})
    assert counts == {"a b": 0}
    assert items == []


def test_match_pages_counts_multiple_pages():
    counts, items = _match(
        ["invoice"], {0: "an invoice", 1: "no match", 2: "INVOICE again"}
    )
    assert counts == {"invoice": 2}
    assert {i["page_idx"] for i in items} == {0, 2}


# ---------------------------------------------------------------------------
# OCR cache
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_db(tmp_path: Path) -> str:
    return str(tmp_path / "cache.db")


def test_cache_roundtrip(cache_db: str):
    pages = {0: "page zero", 1: "page one"}
    core.cache_save(cache_db, "hash1", pages)
    assert core.cache_load(cache_db, "hash1", 2) == pages


def test_cache_load_rejects_wrong_page_count(cache_db: str):
    core.cache_save(cache_db, "hash1", {0: "only page"})
    assert core.cache_load(cache_db, "hash1", 2) is None


def test_cache_load_missing_hash(cache_db: str):
    assert core.cache_load(cache_db, "nope", 1) is None


def test_cache_load_page_on_demand(cache_db: str):
    core.cache_save(cache_db, "hash1", {0: "zero", 1: "one"})
    assert core.cache_load_page(cache_db, "hash1", 1) == "one"
    assert core.cache_load_page(cache_db, "hash1", 5) is None


# ---------------------------------------------------------------------------
# End-to-end: process_file on native-text PDFs (no OCR involved)
# ---------------------------------------------------------------------------

# Long enough that the OCR-sampling heuristic sees native text
FILLER = "This document contains plenty of native text content. " * 5


def make_pdf(path: Path, page_texts: list[str]) -> None:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), f"{text}\n{FILLER}")
    doc.save(str(path))
    doc.close()


def test_process_file_extracts_matching_pages(tmp_path: Path, cache_db: str):
    src = tmp_path / "input" / "doc.pdf"
    src.parent.mkdir()
    make_pdf(src, ["the yearly invoice total", "unrelated page", "invoice only here"])
    out_dir = tmp_path / "out"

    core._worker_init(["invoice total"], str(tmp_path / "test.log"))
    result = core.process_file(str(src), str(out_dir), cache_db)

    assert result["counts"] == {"invoice total": 1}
    # Page 1 fully approved, page 3 partial candidate
    statuses = {i["page_idx"]: i["pre_approved"] for i in result["review_data"]}
    assert statuses == {0: True, 2: False}

    file_hash = core.content_hash(str(src))
    out_pdf = out_dir / "invoice total" / core.output_page_name("doc", 0, file_hash)
    assert out_pdf.exists()
    with fitz.open(str(out_pdf)) as extracted:
        assert extracted.page_count == 1
        assert "yearly invoice total" in extracted[0].get_text()

    # Full text was cached for the review panel
    assert core.cache_load_page(cache_db, file_hash, 1) is not None


def test_process_file_uses_cache_on_second_run(tmp_path: Path, cache_db: str):
    src = tmp_path / "doc.pdf"
    make_pdf(src, ["find the keyword here"])
    out_dir = tmp_path / "out"

    core._worker_init(["keyword"], str(tmp_path / "test.log"))
    first = core.process_file(str(src), str(out_dir), cache_db)
    second = core.process_file(str(src), str(out_dir), cache_db)
    assert first["counts"] == second["counts"] == {"keyword": 1}


def test_process_file_batch_aggregates(tmp_path: Path, cache_db: str):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    make_pdf(a, ["keyword on this page"])
    make_pdf(b, ["keyword again"])
    out_dir = tmp_path / "out"

    core._worker_init(["keyword"], str(tmp_path / "test.log"))
    result = core.process_file_batch([str(a), str(b)], str(out_dir), cache_db)
    assert result["counts"] == {"keyword": 2}
    assert len(result["review_data"]) == 2


def test_process_file_unreadable_pdf_returns_empty(tmp_path: Path, cache_db: str):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a real pdf")
    core._worker_init(["keyword"], str(tmp_path / "test.log"))
    result = core.process_file(str(bad), str(tmp_path / "out"), cache_db)
    assert result == {"counts": {}, "review_data": []}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_write_reports_creates_csv_and_sqlite(tmp_path: Path):
    counts = {"q1": 3, "q2": 0}
    review = [
        {
            "id": "h_0_0",
            "query": "q1",
            "file_path": "f.pdf",
            "page_idx": 0,
            "pre_approved": True,
        }
    ]
    core.write_reports(str(tmp_path), counts, review)

    with open(tmp_path / "report.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["Query", "Total Approved Hits"]
    assert ["q1", "3"] in rows and ["q2", "0"] in rows

    con = sqlite3.connect(str(tmp_path / "extraction_history.db"))
    try:
        metrics = dict(con.execute("SELECT query, hits FROM summary_metrics"))
        assert metrics == counts
        candidates = con.execute(
            "SELECT id, pre_approved FROM candidates_log"
        ).fetchall()
        assert candidates == [("h_0_0", 1)]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_merge_outputs_to_pdf_bookmarks_by_query(tmp_path: Path):
    for query, texts in [("alpha", ["a1", "a2"]), ("beta", ["b1"])]:
        folder = tmp_path / query
        folder.mkdir()
        for i, text in enumerate(texts):
            make_pdf(folder / f"{text}_p1_hash{i}.pdf", [text])

    merged_path, pages, files = core.merge_outputs_to_pdf(str(tmp_path))
    assert files == 3
    assert pages == 3
    assert Path(merged_path).name == core.MERGED_PDF_NAME

    with fitz.open(merged_path) as doc:
        toc = doc.get_toc()
    level1 = [t[1] for t in toc if t[0] == 1]
    assert level1 == ["alpha", "beta"]


def test_merge_outputs_empty_dir(tmp_path: Path):
    merged_path, pages, files = core.merge_outputs_to_pdf(str(tmp_path))
    assert files == 0
    assert pages == 0
    assert not Path(merged_path).exists()
