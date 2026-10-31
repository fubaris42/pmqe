"""Run records, previous-run cleanup and the merged PDF."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import fitz
import pytest

from pmqe import store
from pmqe.merge import merge_outputs_to_pdf
from pmqe.pipeline import RunOptions, run_pipeline
from pmqe.text import prepare_queries

from .conftest import make_pdf


def run(tmp_path: Path, queries: list[str], docs: dict[str, list[str]]):
    for rel, pages in docs.items():
        make_pdf(tmp_path / "in" / rel, pages)
    opts = RunOptions(
        str(tmp_path / "in"), str(tmp_path / "out"), prepare_queries(queries).queries, workers=0
    )
    return run_pipeline(opts)


def test_run_record_roundtrip_and_legacy_columns(tmp_path: Path):
    res = run(tmp_path, ["invoice total", "audit"], {"a.pdf": ["invoice total", "total only"]})
    out = str(tmp_path / "out")
    with store.RunStore.open(out) as s:
        info = s.info()
        assert info.status == "complete" and [q.text for q in info.queries] == [
            "invoice total",
            "audit",
        ]
        assert info.stats["files"] == 1
        items = s.items()
    assert [(i.page_idx, i.status) for i in items] == [(0, "matched"), (1, "candidate")]
    assert items[1].missing_words == ["invoice"] and items[1].folder == "invoice total"
    assert [i.id for i in items] == [i.id for i in res.items]

    con = sqlite3.connect(Path(out) / store.DB_NAME)  # the columns older tooling reads
    assert dict(con.execute("SELECT query, hits FROM summary_metrics")) == {
        "invoice total": 1,
        "audit": 0,
    }
    assert con.execute(
        "SELECT id, query, file_path, page_index, pre_approved FROM candidates_log"
    ).fetchall()
    con.close()


def test_record_decisions_updates_counts_and_reports(tmp_path: Path):
    run(tmp_path, ["invoice total"], {"a.pdf": ["invoice total", "total only"]})
    out = str(tmp_path / "out")
    with store.RunStore.open(out) as s:
        items = s.items()
        items[1].approved, items[1].decided = True, True
        s.record_decisions([items[1]])
        assert [i.status for i in s.items()] == ["matched", "approved"]
    with open(Path(out) / store.REPORT_NAME, encoding="utf-8-sig") as f:
        assert list(csv.reader(f)) == [
            ["Query", "Total Approved Hits", "Awaiting Review"],
            ["invoice total", "2", "0"],
        ]


def test_failures_are_reported(tmp_path: Path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "broken.pdf").write_bytes(b"nope")
    res = run(tmp_path, ["keyword"], {"ok.pdf": ["keyword"]})
    assert [Path(p).name for p, _ in res.failures] == ["broken.pdf"]
    with open(tmp_path / "out" / store.ISSUES_NAME, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["File", "Issue"] and rows[1][0].endswith("broken.pdf")


def test_clear_previous_run_removes_only_pmqe_files(tmp_path: Path):
    run(tmp_path, ["keyword"], {"a.pdf": ["keyword"]})
    out = tmp_path / "out"
    mine = out / "keyword" / "my notes.pdf"
    mine.write_bytes(b"keep me")
    prev = store.find_previous_run(str(out))
    assert prev and prev.output_files == 1 and prev.queries == 1
    assert store.clear_previous_run(str(out)) == 1
    assert mine.exists()
    assert not (out / store.DB_NAME).exists() and not (out / store.REPORT_NAME).exists()
    assert (
        store.find_previous_run(str(out)) is None
        or store.find_previous_run(str(out)).output_files == 0
    )


def test_find_previous_run_on_empty_or_foreign_folder(tmp_path: Path):
    assert store.find_previous_run(str(tmp_path / "missing")) is None
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "report.pdf").write_bytes(b"x")
    assert store.find_previous_run(str(tmp_path)) is None


def test_merge_orders_pages_naturally_with_query_bookmarks(tmp_path: Path):
    pages = [f"page {n} keyword" if n in (1, 2, 10) else "filler" for n in range(1, 11)]
    run(tmp_path, ["keyword", "absent"], {"report.pdf": pages})
    out = tmp_path / "out"
    (out / "keyword" / "unrelated.pdf").write_bytes(b"not ours")  # must be ignored
    res = merge_outputs_to_pdf(str(out))
    assert (res.files, res.pages, res.skipped) == (3, 3, [])
    with fitz.open(res.path) as doc:
        texts = [p.get_text().split("\n")[0] for p in doc]
        toc = doc.get_toc()
    assert texts == ["page 1 keyword", "page 2 keyword", "page 10 keyword"]
    assert toc[0] == [1, "keyword", 1]
    assert [t[1] for t in toc[1:]] == [f"report.pdf — page {n}" for n in (1, 2, 10)]


def test_merge_legacy_output_folder(tmp_path: Path):
    h = "a" * 32
    for q, names in {
        "beta": [f"doc_p10_{h}.pdf", f"doc_p2_{h}.pdf"],
        "alpha": [f"x_p1_{h}.pdf"],
    }.items():
        for name in names:
            make_pdf(tmp_path / q / name, [name])
    (tmp_path / "beta" / "random.pdf").write_bytes(b"x")
    res = merge_outputs_to_pdf(str(tmp_path))
    with fitz.open(res.path) as doc:
        toc = doc.get_toc()
    assert [t[1] for t in toc] == ["alpha", "x — page 1", "beta", "doc — page 2", "doc — page 10"]


def test_merge_with_nothing_approved_writes_nothing(tmp_path: Path):
    run(tmp_path, ["absent"], {"a.pdf": ["nothing here"]})
    res = merge_outputs_to_pdf(str(tmp_path / "out"))
    assert res.files == 0 and not Path(res.path).exists()


def test_open_missing_store_raises(tmp_path: Path):
    with pytest.raises(store.NoRunFound):
        store.RunStore.open(str(tmp_path))


def test_folder_names_from_the_record_cannot_escape_the_output_folder(tmp_path: Path):
    run(tmp_path, ["keyword"], {"a.pdf": ["keyword"]})
    out = tmp_path / "out"
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    bait = victim / f"x_p1_{'a' * 32}.pdf"
    bait.write_bytes(b"precious")
    con = sqlite3.connect(out / store.DB_NAME)
    con.execute("UPDATE queries SET folder = ?", (str(Path("..") / "elsewhere"),))
    con.commit()
    con.close()
    with store.RunStore.open(str(out)) as s:
        assert [q.folder for q in s.info().queries] == ["keyword"]
        assert {i.folder for i in s.items()} == {"keyword"}
    store.clear_previous_run(str(out))
    assert bait.exists()


def test_folders_without_a_run_record_are_never_cleaned(tmp_path: Path):
    sub = tmp_path / "Work"
    sub.mkdir()
    keep = [sub / "budget.xlsx.tmp", sub / f"photo_{'b' * 32}.jpg"]
    for f in keep:
        f.write_bytes(b"mine")
    assert store.find_previous_run(str(tmp_path)) is None
    assert store.clear_previous_run(str(tmp_path)) == 0
    assert all(f.exists() for f in keep)


def test_previous_run_cleanup_only_takes_our_temp_files(tmp_path: Path):
    run(tmp_path, ["keyword"], {"a.pdf": ["keyword"]})
    folder = tmp_path / "out" / "keyword"
    ours = folder / f"a_p1_{'c' * 32}.pdf.123-456.tmp"
    theirs = folder / "notes.txt.tmp"
    ours.write_bytes(b"x")
    theirs.write_bytes(b"x")
    store.clear_previous_run(str(tmp_path / "out"))
    assert not ours.exists() and theirs.exists()


def test_cleanup_of_a_1x_output_folder_uses_its_query_list(tmp_path: Path):
    out = tmp_path / "out"
    (out / "Invoice Total").mkdir(parents=True)
    (out / "Unrelated").mkdir()
    h = "d" * 32
    old_page = out / "Invoice Total" / f"doc_p1_{h}.pdf"
    other = out / "Unrelated" / f"doc_p1_{h}.pdf"
    old_page.write_bytes(b"x")
    other.write_bytes(b"x")
    con = sqlite3.connect(out / store.DB_NAME)  # the 1.x schema
    con.execute("CREATE TABLE summary_metrics (query TEXT PRIMARY KEY, hits INTEGER)")
    con.execute("INSERT INTO summary_metrics VALUES ('Invoice: Total', 1)")
    con.commit()
    con.close()
    prev = store.find_previous_run(str(out))
    assert prev and prev.output_files == 1
    store.clear_previous_run(str(out))
    assert not old_page.exists() and other.exists()


def test_a_run_that_cannot_start_keeps_previous_results(tmp_path: Path):
    from pmqe.pipeline import NothingToProcess

    run(tmp_path, ["keyword"], {"a.pdf": ["keyword"]})
    (tmp_path / "empty").mkdir()
    opts = RunOptions(
        str(tmp_path / "empty"), str(tmp_path / "out"), prepare_queries(["x y"]).queries, workers=0
    )
    with pytest.raises(NothingToProcess):
        run_pipeline(opts)
    with store.RunStore.open(str(tmp_path / "out")) as s:
        assert len(s.items()) == 1


def test_merge_ignores_a_source_that_changed_since_the_run(tmp_path: Path):
    run(tmp_path, ["keyword"], {"a.pdf": ["original keyword page"]})
    make_pdf(tmp_path / "in" / "a.pdf", ["replaced content"])  # same path, new content
    res = merge_outputs_to_pdf(str(tmp_path / "out"))
    with fitz.open(res.path) as doc:
        assert "original keyword page" in doc[0].get_text()
