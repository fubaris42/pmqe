"""End-to-end runs: job planning, process pools, crashes, cancellation."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from pmqe import pipeline
from pmqe.pipeline import RunOptions, run_pipeline
from pmqe.store import RunStore
from pmqe.text import prepare_queries

from . import crashy
from .conftest import make_pdf


def opts(tmp_path: Path, queries: list[str], **kw) -> RunOptions:
    return RunOptions(
        str(tmp_path / "in"), str(tmp_path / "out"), prepare_queries(queries).queries, **kw
    )


def test_plan_jobs_keeps_every_worker_busy(tmp_path: Path):
    files = []
    for i in range(60):
        f = tmp_path / f"s{i}.pdf"
        f.write_bytes(b"x" * 100)
        files.append(str(f))
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * (pipeline.SMALL_FILE_BYTES + 10))
    jobs = pipeline.plan_jobs([*files, str(big)], workers=5)
    assert jobs[0].paths == (str(big),)  # large files first
    assert len(jobs) >= 5 * pipeline.JOBS_PER_WORKER
    assert sorted(p for j in jobs for p in j.paths) == sorted([*files, str(big)])


def test_inline_run_end_to_end(tmp_path: Path):
    make_pdf(tmp_path / "in" / "a.pdf", ["alpha keyword", "nothing"])
    make_pdf(tmp_path / "in" / "sub" / "b.pdf", ["keyword"])
    seen = []
    res = run_pipeline(opts(tmp_path, ["keyword"], workers=0), progress=seen.append)
    assert (res.files, res.pages, res.counts) == (2, 3, {"keyword": 2})
    assert seen[-1].files_done == 2 and seen[-1].files_total == 2
    assert len(list((tmp_path / "out" / "keyword").glob("*.pdf"))) == 2
    assert (tmp_path / "out" / "extractor.log").read_text(encoding="utf-8")


def test_previous_results_are_replaced_or_protected(tmp_path: Path):
    make_pdf(tmp_path / "in" / "a.pdf", ["keyword"])
    run_pipeline(opts(tmp_path, ["keyword"], workers=0))
    with pytest.raises(pipeline.PreviousRunExists):
        run_pipeline(opts(tmp_path, ["other"], workers=0, replace_previous=False))
    res = run_pipeline(opts(tmp_path, ["absent words"], workers=0))
    assert res.counts == {"absent words": 0}
    assert not (tmp_path / "out" / "keyword").exists()  # old query folder cleaned up


def test_output_inside_input_is_not_reingested(tmp_path: Path):
    make_pdf(tmp_path / "in" / "a.pdf", ["keyword"])
    o = RunOptions(
        str(tmp_path / "in"),
        str(tmp_path / "in" / "out"),
        prepare_queries(["keyword"]).queries,
        workers=0,
    )
    run_pipeline(o)
    assert run_pipeline(o).files == 1


@pytest.mark.parametrize(
    ("make_opts", "error"),
    [
        (lambda t: RunOptions(str(t / "missing"), str(t / "out"), []), pipeline.InvalidFolders),
        (lambda t: RunOptions(str(t / "in"), str(t), []), pipeline.InvalidFolders),
        (lambda t: RunOptions(str(t / "in"), str(t / "out"), []), pipeline.NothingToProcess),
    ],
    ids=["missing-input", "output-contains-input", "no-files"],
)
def test_invalid_runs_are_rejected(tmp_path: Path, make_opts, error):
    (tmp_path / "in").mkdir()
    with pytest.raises(error):
        run_pipeline(make_opts(tmp_path))


def test_process_pool_run(tmp_path: Path):
    for i in range(6):
        make_pdf(tmp_path / "in" / f"d{i}.pdf", ["keyword" if i % 2 else "nothing"])
    res = run_pipeline(opts(tmp_path, ["keyword"], workers=2))
    assert res.files == 6 and res.counts == {"keyword": 3} and not res.failures


def test_worker_crash_is_isolated_and_reported(tmp_path: Path):
    for name in ("a", "b", "crash", "c", "d"):
        make_pdf(tmp_path / "in" / f"{name}.pdf", ["keyword"])
    res = run_pipeline(opts(tmp_path, ["keyword"], workers=2), job_fn=crashy.run_job)
    assert res.counts == {"keyword": 4}
    assert [(Path(p).name, "crashed" in e) for p, e in res.failures] == [("crash.pdf", True)]


def test_cancel_stops_early_and_is_recorded(tmp_path: Path):
    for i in range(8):
        make_pdf(tmp_path / "in" / f"d{i}.pdf", ["keyword"])
    cancel = threading.Event()
    res = run_pipeline(
        opts(tmp_path, ["keyword"], workers=0), progress=lambda p: cancel.set(), cancel=cancel
    )
    assert res.cancelled and 0 < res.files < 8
    with RunStore.open(str(tmp_path / "out")) as s:
        assert s.info().status == "cancelled"
