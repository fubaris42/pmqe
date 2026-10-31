"""Run orchestration: discover files, fan work out to worker processes,
collect results and record the run. Qt-free, so it backs the GUI, the
benchmark and the tests alike.

Robustness:

* Cancellation reaches workers through a shared event; they stop within one
  OCR batch instead of finishing their current file.
* If a worker process dies (a malformed file crashing a native library, or
  running out of memory), the pool is restarted and the affected files are
  retried on their own; a file that crashes a worker in isolation is
  reported as failed and the run carries on.
* Worker log records travel over a queue to the main process, which writes
  one log file.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import logging.handlers
import math
import multiprocessing
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from typing import Any

from . import extract, ocr
from .cache import default_cache_path, open_cache
from .models import FileResult, ReviewItem
from .store import LOG_NAME, RunInfo, RunStore, clear_previous_run, find_previous_run
from .text import Query
from .util import LOG_FORMAT, display_path, is_within, long_path, natural_key

log = logging.getLogger(__name__)

SMALL_FILE_BYTES = 512_000  # smaller files are grouped into batches...
MAX_BATCH = 50  # ...of at most this many files,
JOBS_PER_WORKER = 4  # ...sized so each worker gets about this many jobs.
MAX_POOL_RESTARTS = 25
POLL_SECONDS = 0.2

JobFn = Callable[[Sequence[str], str], list[FileResult]]


class PreviousRunExists(Exception):
    pass


class NothingToProcess(Exception):
    pass


class InvalidFolders(ValueError):
    pass


class WorkerStartupError(RuntimeError):
    """Worker processes could not start at all (environment problem)."""


@dataclass
class RunOptions:
    input_dir: str
    output_dir: str
    queries: list[Query]
    whole_words: bool = True
    force_rapidocr: bool = False
    workers: int | None = None  # None: one per CPU but one; 0: run in-process
    cache_path: str | None = None  # None: per-user default; "": no cache
    replace_previous: bool = True


@dataclass
class Progress:
    files_done: int
    files_total: int
    pages_done: int
    elapsed: float
    eta: float | None
    current: str = ""


@dataclass
class RunResult:
    output_dir: str
    queries: list[Query]
    items: list[ReviewItem]
    counts: dict[str, int]
    failures: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    files: int = 0
    pages: int = 0
    ocr_pages: int = 0
    ocr_cached: int = 0
    ocr_failed: int = 0
    elapsed: float = 0.0
    cancelled: bool = False


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


# ---------------------------------------------------------------------------
# Job planning
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    paths: tuple[str, ...]
    crashes: int = 0


def _size(path: str) -> int:
    try:
        return os.path.getsize(long_path(path))
    except OSError:
        return SMALL_FILE_BYTES + 1


def plan_jobs(files: Sequence[str], workers: int) -> list[_Job]:
    """Large files run alone, biggest first; small files are batched, with
    batches small enough that every worker stays busy."""
    sizes = {f: _size(f) for f in files}
    large = sorted((f for f in files if sizes[f] > SMALL_FILE_BYTES), key=lambda f: -sizes[f])
    small = [f for f in files if sizes[f] <= SMALL_FILE_BYTES]
    batch = max(1, min(MAX_BATCH, math.ceil(len(small) / (max(1, workers) * JOBS_PER_WORKER))))
    return [_Job((f,)) for f in large] + [
        _Job(tuple(small[i : i + batch])) for i in range(0, len(small), batch)
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class _Collector:
    """Aggregates results and reports progress."""

    def __init__(self, total: int, progress: Callable[[Progress], None] | None) -> None:
        self.total = total
        self.results: list[FileResult] = []
        self.pages = 0
        self.started = time.perf_counter()
        self._progress = progress

    def add(self, results: list[FileResult]) -> None:
        self.results.extend(results)
        self.pages += sum(r.pages for r in results)
        if self._progress:
            done = len(self.results)
            elapsed = time.perf_counter() - self.started
            eta = elapsed / done * (self.total - done) if done else None
            current = os.path.basename(results[-1].path) if results else ""
            self._progress(Progress(done, self.total, self.pages, elapsed, eta, current))


def _crash_result(path: str) -> FileResult:
    return FileResult(path=path, error="The worker process crashed while reading this file")


def _drive_pool(
    ctx: Any,
    n_workers: int,
    pending: deque[_Job],
    initargs: tuple,
    job_fn: JobFn,
    output_dir: str,
    collector: _Collector,
    cancelled: Callable[[], bool],
    stop_workers: Any,
    probe: bool = False,
) -> list[_Job]:
    """Run jobs until done, cancelled, or the pool breaks. Returns the jobs
    that were in flight when a worker died (empty otherwise)."""
    in_flight: dict[cf.Future, _Job] = {}
    with cf.ProcessPoolExecutor(
        max_workers=n_workers, mp_context=ctx, initializer=extract.init_worker, initargs=initargs
    ) as pool:
        if probe:  # fail fast and clearly if workers cannot start at all
            try:
                pool.submit(extract.ping).result()
            except BrokenProcessPool as exc:
                raise WorkerStartupError(
                    "The worker processes could not start. See the log for details."
                ) from exc
        while pending or in_flight:
            if cancelled():
                stop_workers.set()
                pool.shutdown(wait=True, cancel_futures=True)
                return []
            # At most one job per worker in flight, so a crash implicates
            # only jobs that were actually running.
            while pending and len(in_flight) < n_workers:
                job = pending.popleft()
                try:
                    in_flight[pool.submit(job_fn, list(job.paths), output_dir)] = job
                except BrokenProcessPool:  # broke since the last poll
                    pending.appendleft(job)
                    crashed = list(in_flight.values())
                    in_flight.clear()
                    return crashed
            done, _ = cf.wait(in_flight, timeout=POLL_SECONDS, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                job = in_flight.pop(fut)
                try:
                    collector.add(fut.result())
                except BrokenProcessPool:
                    crashed = [job, *in_flight.values()]
                    in_flight.clear()
                    return crashed
                except extract.Cancelled:
                    pass
                except Exception as exc:  # job_fn itself failed: report its files
                    log.error("Job failed: %s", exc, exc_info=exc)
                    collector.add(
                        [
                            FileResult(path=p, error=f"{type(exc).__name__}: {exc}")
                            for p in job.paths
                        ]
                    )
    return []


def worker_context() -> multiprocessing.context.BaseContext:
    """How worker processes start. On Linux they fork from a clean server
    process (fast). Windows can only spawn; on macOS forking is unsafe once
    Apple's frameworks are loaded, and the packaged app loads them in every
    process (PyInstaller's Qt hook), so both spawn a fresh interpreter."""
    return multiprocessing.get_context(
        "spawn" if sys.platform in ("win32", "darwin") else "forkserver"
    )


def _run_pool(
    jobs: list[_Job],
    n_workers: int,
    init: tuple,
    job_fn: JobFn,
    output_dir: str,
    collector: _Collector,
    cancelled: Callable[[], bool],
) -> None:
    ctx = worker_context()
    stop_workers = ctx.Event()
    log_queue = ctx.Queue()
    listener = logging.handlers.QueueListener(
        log_queue, *logging.getLogger().handlers, respect_handler_level=True
    )
    listener.start()
    initargs = (*init, stop_workers, log_queue)
    queue: deque[_Job] = deque(jobs)
    quarantine: deque[_Job] = deque()
    restarts = 0
    try:
        while (queue or quarantine) and not cancelled():
            isolated = not queue
            pending = deque([quarantine.popleft()]) if isolated else queue
            crashed = _drive_pool(
                ctx,
                1 if isolated else n_workers,
                pending,
                initargs,
                job_fn,
                output_dir,
                collector,
                cancelled,
                stop_workers,
                probe=restarts == 0 and not isolated,
            )
            if not crashed:
                continue
            restarts += 1
            log.warning(
                "A worker process died; restarting the pool (%d jobs affected)", len(crashed)
            )
            for job in crashed:
                job.crashes += 1
                if isolated:
                    collector.add([_crash_result(p) for p in job.paths])
                elif len(job.paths) > 1:
                    queue.extendleft(_Job((p,), job.crashes) for p in reversed(job.paths))
                elif job.crashes >= 2:
                    quarantine.append(job)  # retried alone to find the culprit
                else:
                    queue.appendleft(job)
            if restarts >= MAX_POOL_RESTARTS:
                log.error("Too many worker crashes; giving up on the remaining files")
                for job in (*queue, *quarantine):
                    collector.add([_crash_result(p) for p in job.paths])
                queue.clear()
                quarantine.clear()
    finally:
        listener.stop()
        log_queue.close()
        log_queue.join_thread()


def _run_inline(
    jobs: list[_Job],
    init: tuple,
    job_fn: JobFn,
    output_dir: str,
    collector: _Collector,
    cancel: Any,
) -> None:
    extract.init_worker(*init, cancel, None)
    for job in jobs:
        if cancel is not None and cancel.is_set():
            return
        try:
            collector.add(job_fn(list(job.paths), output_dir))
        except extract.Cancelled:
            return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_pipeline(
    opts: RunOptions,
    progress: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
    *,
    job_fn: JobFn | None = None,
) -> RunResult:
    """Process every supported file under ``opts.input_dir``.

    Raises :class:`PreviousRunExists` when the output folder already holds
    results and ``replace_previous`` is false, and :class:`NothingToProcess`
    when there are no supported files.
    """
    started = time.perf_counter()
    job_fn = job_fn or extract.run_job
    cancel = cancel or threading.Event()
    input_dir = display_path(os.path.abspath(opts.input_dir))
    output_dir = display_path(os.path.abspath(opts.output_dir))
    if not os.path.isdir(long_path(input_dir)):
        raise InvalidFolders(f"The input folder does not exist: {input_dir}")
    if is_within(input_dir, output_dir):
        raise InvalidFolders("The output folder must not be the input folder or contain it.")

    previous = find_previous_run(output_dir)
    if previous and not opts.replace_previous:
        raise PreviousRunExists(output_dir)

    files, problems = extract.discover_files(input_dir, exclude=[output_dir])
    if not files:
        raise NothingToProcess("No PDFs or images found in the input folder.")

    cache_path = str(default_cache_path()) if opts.cache_path is None else opts.cache_path
    if cache_path:
        cache = open_cache(cache_path)  # migrates once, before workers race to
        if cache is None:
            cache_path = ""
        else:
            cache.close()

    # Only now, with a run that can actually start, replace the old results.
    if previous:
        removed = clear_previous_run(output_dir)
        log.info("Cleared previous results from %s (%d page files)", output_dir, removed)

    engine = ocr.RAPIDOCR if opts.force_rapidocr else ocr.native_engine()
    info = RunInfo(input_dir, output_dir, opts.queries, opts.whole_words, engine)
    store = RunStore.create(info)

    run_log = logging.FileHandler(long_path(os.path.join(output_dir, LOG_NAME)), "w", "utf-8")
    run_log.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(run_log)
    saved_level = root.level
    root.setLevel(logging.INFO if saved_level > logging.INFO else saved_level)  # run log wants INFO
    try:
        n_workers = default_workers() if opts.workers is None else max(0, opts.workers)
        jobs = plan_jobs(files, max(1, n_workers))
        log.info(
            "Run started: %d files in %d jobs, %d queries, %s workers, OCR %s",
            len(files),
            len(jobs),
            len(opts.queries),
            n_workers or "inline",
            engine,
        )
        collector = _Collector(len(files), progress)
        init = (opts.queries, opts.whole_words, opts.force_rapidocr, cache_path or None)
        if n_workers == 0:
            _run_inline(jobs, init, job_fn, output_dir, collector, cancel)
        else:
            _run_pool(jobs, n_workers, init, job_fn, output_dir, collector, cancel.is_set)
        result = _assemble(opts, output_dir, collector.results, problems, cancel.is_set())
        result.elapsed = time.perf_counter() - started
        info.status = "cancelled" if result.cancelled else "complete"
        info.finished = time.time()
        info.stats = {
            "files": result.files,
            "pages": result.pages,
            "ocr_pages": result.ocr_pages,
            "ocr_cached": result.ocr_cached,
            "ocr_failed": result.ocr_failed,
            "seconds": round(result.elapsed, 2),
            "workers": n_workers,
        }
        store.finish(info, result.items, result.failures + result.warnings)
        log.info(
            "Run %s in %.1fs: %d files, %d pages (%d OCR, %d from cache), %d matches, %d to review, %d failed",
            info.status,
            result.elapsed,
            result.files,
            result.pages,
            result.ocr_pages,
            result.ocr_cached,
            sum(result.counts.values()),
            sum(1 for i in result.items if not i.auto_approved),
            len(result.failures),
        )
        return result
    finally:
        root.removeHandler(run_log)
        root.setLevel(saved_level)
        run_log.close()
        store.close()


def _assemble(
    opts: RunOptions,
    output_dir: str,
    results: list[FileResult],
    problems: list[tuple[str, str]],
    cancelled: bool,
) -> RunResult:
    counts = {q.text: 0 for q in opts.queries}
    items: list[ReviewItem] = []
    failures = list(problems)
    warnings: list[tuple[str, str]] = []
    totals = dict.fromkeys(("pages", "ocr_pages", "ocr_cached", "ocr_failed"), 0)
    for r in results:
        for k in totals:
            totals[k] += getattr(r, k)
        if r.error:
            failures.append((r.path, r.error))
            continue
        for q, c in r.counts.items():
            counts[q] += c
        items.extend(r.items)
        warnings.extend((r.path, f"Partly processed: {w}") for w in r.warnings)
    items.sort(key=lambda i: (i.query_idx, natural_key(i.file_path), i.page_idx))
    failures.sort(key=lambda f: natural_key(f[0]))
    return RunResult(
        output_dir=output_dir,
        queries=opts.queries,
        items=items,
        counts=counts,
        failures=failures,
        warnings=warnings,
        files=len(results),
        cancelled=cancelled,
        **totals,
    )
