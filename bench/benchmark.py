"""End-to-end benchmark for pmqe.

Generates a deterministic synthetic corpus (native-text PDFs, scanned PDFs,
mixed PDFs, JPEG/TIFF/HEIC scans) with known ground truth, then measures:

* matching throughput (pages/s) for a small and a large query set,
* full pipeline wall time with a cold and a warm OCR cache,
* match quality against ground truth (precision / recall of approved pages),
* merge time.

Usage::

    uv run python bench/benchmark.py                 # full run
    uv run python bench/benchmark.py --quick         # smaller corpus
    uv run python bench/benchmark.py --json out.json # also save results

The corpus is cached in ``--corpus`` (default: a temp dir) so repeated runs
compare like with like. Ground truth uses whole-word, case-insensitive
semantics: a page matches a query when every word of the query appears on it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import fitz
from PIL import Image

# ---------------------------------------------------------------------------
# Corpus generation
# ---------------------------------------------------------------------------

SEED = 1234

# Query words plus "trap" words that contain them as substrings, so substring
# matching shows up as false positives.
QUERY_WORDS = [
    "invoice",
    "total",
    "amount",
    "contract",
    "payment",
    "account",
    "budget",
    "report",
    "quarter",
    "revenue",
    "audit",
    "policy",
    "client",
    "supplier",
    "delivery",
    "warranty",
    "license",
    "renewal",
    "balance",
    "credit",
    "art",
    "tax",
    "rate",
    "port",
    "net",
    "cat",
    "use",
    "end",
]
TRAP_WORDS = [
    "start",
    "party",
    "smart",
    "syntax",
    "taxi",
    "separate",
    "rated",
    "important",
    "portal",
    "planet",
    "internet",
    "category",
    "locate",
    "because",
    "house",
    "calendar",
    "weekend",
    "subtotal",
    "accountant",
    "reporter",
    "endless",
]

NATIVE_FONT_SIZE = 10
SCAN_FONT_SIZE = 13
SCAN_DPI = 150


def _pseudo_words(rng: random.Random, n: int) -> list[str]:
    onsets = [
        "b",
        "d",
        "f",
        "g",
        "k",
        "l",
        "m",
        "n",
        "p",
        "r",
        "s",
        "t",
        "v",
        "br",
        "dr",
        "gl",
        "pl",
        "st",
        "tr",
        "sh",
        "ch",
    ]
    vowels = ["a", "e", "i", "o", "u", "ai", "ou", "ea"]
    codas = ["", "", "n", "r", "s", "l", "m", "x"]
    blocked = set(QUERY_WORDS) | set(TRAP_WORDS)
    words: set[str] = set()
    while len(words) < n:
        w = "".join(
            rng.choice(onsets) + rng.choice(vowels) + rng.choice(codas)
            for _ in range(rng.randint(2, 3))
        )
        if 4 <= len(w) <= 10 and w not in blocked:
            words.add(w)
    return sorted(words)


@dataclass
class Corpus:
    root: Path
    queries: list[str]
    truth: dict[str, list[list[str]]]  # relative path -> per-page word lists
    kinds: dict[str, str]  # relative path -> kind label

    def expected(self) -> set[tuple[str, int, str]]:
        """(relative path, page index, query) triples that must be approved."""
        out = set()
        for rel, pages in self.truth.items():
            for pi, words in enumerate(pages):
                present = {w.lower() for w in words}
                for q in self.queries:
                    toks = [t.lower() for t in q.split() if len(t) > 1]
                    if toks and all(t in present for t in toks):
                        out.add((rel, pi, q))
        return out


def _make_queries(rng: random.Random) -> list[str]:
    singles = [
        "art",
        "tax",
        "rate",
        "port",
        "net",
        "cat",
        "use",
        "end",
        "invoice",
        "audit",
        "warranty",
        "license",
    ]
    multi = []
    pool = [w for w in QUERY_WORDS if len(w) > 3]
    while len(multi) < 24:
        k = 2 if len(multi) < 14 else 3
        q = " ".join(rng.sample(pool, k))
        if q not in multi:
            multi.append(q)
    return singles + multi


def _page_words(rng: random.Random, filler: list[str], queries: list[str], n: int) -> list[str]:
    words = rng.choices(filler, k=n)
    for q in queries:
        r = rng.random()
        parts = q.split()
        if r < 0.04:
            words.extend(parts)  # full plant
        elif r < 0.08 and len(parts) > 1:
            words.append(rng.choice(parts))  # partial plant -> review candidate
    for t in TRAP_WORDS:
        if rng.random() < 0.08:
            words.append(t)
    rng.shuffle(words)
    # Mixed case so matching must be case-insensitive
    return [w.capitalize() if rng.random() < 0.15 else w for w in words]


def _text_page(doc: fitz.Document, words: list[str], fontsize: float) -> None:
    page = doc.new_page()
    rect = fitz.Rect(50, 50, page.rect.width - 50, page.rect.height - 50)
    rc = page.insert_textbox(rect, " ".join(words), fontsize=fontsize, fontname="helv")
    if rc < 0:
        raise RuntimeError("benchmark page overflowed; reduce words per page")


def _scan_image(words: list[str]) -> Image.Image:
    """Render words to a grayscale page image, i.e. a 'scan' with no text layer."""
    doc = fitz.open()
    _text_page(doc, words, SCAN_FONT_SIZE)
    pix = doc[0].get_pixmap(dpi=SCAN_DPI, colorspace=fitz.csGRAY)
    doc.close()
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def _image_pdf(
    path: Path, images: list[Image.Image], native_first: list[str] | None = None
) -> None:
    doc = fitz.open()
    if native_first is not None:
        _text_page(doc, native_first, NATIVE_FONT_SIZE)
    for img in images:
        page = doc.new_page()
        tmp = path.with_suffix(".tmp.png")
        img.save(tmp)
        page.insert_image(page.rect, filename=str(tmp))
        tmp.unlink()
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()


def generate_corpus(root: Path, quick: bool) -> Corpus:
    rng = random.Random(SEED)
    filler = _pseudo_words(rng, 3000)
    queries = _make_queries(rng)
    truth: dict[str, list[list[str]]] = {}
    kinds: dict[str, str] = {}
    root.mkdir(parents=True, exist_ok=True)

    n_reports, report_pages, n_letters = (8, 20, 40) if quick else (30, 40, 150)
    n_jpeg, n_scanpdf, n_mixed = (8, 1, 1) if quick else (24, 3, 3)

    def add_native(rel: str, n_pages: int, words_per_page: int = 200) -> None:
        pages = [_page_words(rng, filler, queries, words_per_page) for _ in range(n_pages)]
        doc = fitz.open()
        for words in pages:
            _text_page(doc, words, NATIVE_FONT_SIZE)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(path), garbage=3, deflate=True)
        doc.close()
        truth[rel] = pages
        kinds[rel] = "native"

    for i in range(n_reports):
        add_native(f"reports/report_{i:03d}.pdf", report_pages)
    for i in range(n_letters):
        add_native(f"letters/batch_{i % 5}/letter_{i:03d}.pdf", 2)

    scan_words = 70  # fewer, larger words keep OCR ground truth reliable
    for i in range(n_jpeg):
        words = _page_words(rng, filler, queries, scan_words)
        rel = f"scans/jpeg/scan_{i:03d}.jpg"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        _scan_image(words).save(root / rel, quality=75)
        truth[rel], kinds[rel] = [words], "ocr-image"

    for i in range(n_scanpdf):
        pages = [_page_words(rng, filler, queries, scan_words) for _ in range(4)]
        rel = f"scans/pdf/scanned_{i:02d}.pdf"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        _image_pdf(root / rel, [_scan_image(w) for w in pages])
        truth[rel], kinds[rel] = pages, "ocr-pdf"

    for i in range(n_mixed):
        cover = _page_words(rng, filler, queries, 200)
        scanned = [_page_words(rng, filler, queries, scan_words) for _ in range(3)]
        rel = f"scans/mixed/mixed_{i:02d}.pdf"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        _image_pdf(root / rel, [_scan_image(w) for w in scanned], native_first=cover)
        truth[rel], kinds[rel] = [cover, *scanned], "mixed-pdf"

    # Multi-page TIFF (classic scanner output)
    pages = [_page_words(rng, filler, queries, scan_words) for _ in range(3)]
    rel = "scans/tiff/fax_000.tif"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    frames = [_scan_image(w) for w in pages]
    frames[0].save(root / rel, save_all=True, append_images=frames[1:], compression="tiff_lzw")
    truth[rel], kinds[rel] = pages, "ocr-tiff"

    # iPhone-style HEIC photos
    import pillow_heif

    pillow_heif.register_heif_opener()
    for i in range(2):
        words = _page_words(rng, filler, queries, scan_words)
        rel = f"scans/phone/IMG_{i:04d}.heic"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        _scan_image(words).convert("RGB").save(root / rel, quality=90)
        truth[rel], kinds[rel] = [words], "ocr-heic"

    corpus = Corpus(root, queries, truth, kinds)
    (root / "corpus.json").write_text(
        json.dumps({"queries": queries, "truth": truth, "kinds": kinds})
    )
    return corpus


def load_or_generate(root: Path, quick: bool) -> Corpus:
    meta = root / "corpus.json"
    if meta.exists():
        data = json.loads(meta.read_text())
        return Corpus(root, data["queries"], data["truth"], data["kinds"])
    return generate_corpus(root, quick)


# ---------------------------------------------------------------------------
# Drivers: one per implementation under test
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    seconds: float
    approved: set[tuple[str, int, str]]  # (abs path, page index, query)
    candidates: int
    failures: int = 0
    ocr_pages: int | None = None


class Driver:
    name = "abstract"

    def run(
        self, input_dir: Path, output_dir: Path, queries: list[str], workers: int, cold: bool
    ) -> RunOutcome:  # pragma: no cover
        raise NotImplementedError

    def match_seconds(self, queries: list[str], pages: dict[int, str]) -> float:
        raise NotImplementedError  # pragma: no cover

    def merge(self, output_dir: Path) -> tuple[float, int]:
        raise NotImplementedError  # pragma: no cover


class CurrentDriver(Driver):
    """Drives the pmqe package installed in this environment."""

    name = "current"

    def __init__(self, cache_dir: Path) -> None:
        self.cache_path = cache_dir / "ocr_cache.db"

    def run(self, input_dir, output_dir, queries, workers, cold):
        from pmqe import cache
        from pmqe.pipeline import RunOptions, run_pipeline
        from pmqe.text import prepare_queries

        if cold:
            cache.delete_cache(self.cache_path)
        prepared = prepare_queries(queries).queries
        opts = RunOptions(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            queries=prepared,
            workers=workers,
            cache_path=str(self.cache_path),
        )
        t = time.perf_counter()
        res = run_pipeline(opts)
        dt = time.perf_counter() - t
        approved = {(_norm(i.file_path), i.page_idx, i.query) for i in res.items if i.approved}
        cands = sum(1 for i in res.items if not i.auto_approved)
        return RunOutcome(dt, approved, cands, len(res.failures), res.ocr_pages)

    def match_seconds(self, queries, pages):
        from pmqe.text import Matcher, prepare_queries

        m = Matcher(prepare_queries(queries).queries, whole_words=True)
        t = time.perf_counter()
        m.match_pages(pages, "f.pdf", "h" * 32)
        return time.perf_counter() - t

    def merge(self, output_dir):
        from pmqe.merge import merge_outputs_to_pdf

        t = time.perf_counter()
        res = merge_outputs_to_pdf(str(output_dir))
        return time.perf_counter() - t, res.pages


def _norm(p: str) -> str:
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(os.path.abspath(p))


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


@dataclass
class Results:
    driver: str
    python: str
    cpus: int
    workers: int
    corpus: dict = field(default_factory=dict)
    matching: dict = field(default_factory=dict)
    pipeline: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    merge: dict = field(default_factory=dict)


def _score(corpus: Corpus, approved: set, kinds: set[str] | None = None) -> dict:
    expected = {
        (_norm(str(corpus.root / r)), p, q)
        for r, p, q in corpus.expected()
        if kinds is None or corpus.kinds[r] in kinds
    }
    allowed = {
        _norm(str(corpus.root / r)) for r, k in corpus.kinds.items() if kinds is None or k in kinds
    }
    got = {a for a in approved if a[0] in allowed}
    tp = len(got & expected)
    return {
        "expected": len(expected),
        "approved": len(got),
        "true_pos": tp,
        "false_pos": len(got - expected),
        "missed": len(expected - got),
        "precision": round(tp / len(got), 4) if got else 1.0,
        "recall": round(tp / len(expected), 4) if expected else 1.0,
    }


def _synthetic_pages(n: int, words: int, rng: random.Random) -> dict[int, str]:
    filler = _pseudo_words(rng, 3000)
    qs = _make_queries(rng)
    return {i: " ".join(_page_words(rng, filler, qs, words)) for i in range(n)}


def run_benchmark(driver: Driver, corpus: Corpus, workers: int, repeats: int) -> Results:
    res = Results(driver.name, sys.version.split()[0], os.cpu_count() or 0, workers)
    total_pages = sum(len(p) for p in corpus.truth.values())
    by_kind: dict[str, int] = {}
    for rel, k in corpus.kinds.items():
        by_kind[k] = by_kind.get(k, 0) + len(corpus.truth[rel])
    res.corpus = {
        "files": len(corpus.truth),
        "pages": total_pages,
        "pages_by_kind": by_kind,
        "queries": len(corpus.queries),
    }

    # -- matching micro-benchmark ------------------------------------------
    rng = random.Random(SEED + 1)
    pages = _synthetic_pages(2000, 220, rng)
    extra = _pseudo_words(rng, 468)
    many_queries = corpus.queries + [
        f"{a} {b}" for a, b in zip(extra[::2], extra[1::2], strict=True)
    ]
    for label, qs in (("queries_40", corpus.queries), ("queries_270", many_queries)):
        times = [driver.match_seconds(qs, pages) for _ in range(3)]
        best = min(times)
        res.matching[label] = {
            "pages": len(pages),
            "seconds": round(best, 4),
            "pages_per_s": round(len(pages) / best),
        }

    # -- full pipeline, cold then warm cache --------------------------------
    work = Path(tempfile.mkdtemp(prefix="pmqe-bench-out-"))
    try:
        for label, cold in (("cold", True), ("warm", False)):
            runs = []
            for _ in range(repeats if not cold else 1):
                out = work / f"{label}-{len(runs)}"
                outcome = driver.run(corpus.root, out, corpus.queries, workers, cold=cold)
                runs.append(outcome)
                cold = False if label == "warm" else cold
            secs = [r.seconds for r in runs]
            best = runs[secs.index(min(secs))]
            res.pipeline[label] = {
                "seconds": round(min(secs), 2),
                "seconds_median": round(statistics.median(secs), 2),
                "pages_per_s": round(total_pages / min(secs), 1),
                "review_candidates": best.candidates,
                "failures": best.failures,
            }
            if label == "cold":
                res.quality = {
                    "all": _score(corpus, best.approved),
                    "native": _score(corpus, best.approved, {"native"}),
                    "ocr": _score(
                        corpus, best.approved, {"ocr-image", "ocr-pdf", "ocr-tiff", "ocr-heic"}
                    ),
                    "mixed_pdf": _score(corpus, best.approved, {"mixed-pdf"}),
                }
        secs, pages_out = driver.merge(work / "cold-0")
        res.merge = {"seconds": round(secs, 2), "pages": pages_out}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return res


def print_results(r: Results) -> None:
    print(
        f"\n== pmqe benchmark ({r.driver}) — Python {r.python}, {r.cpus} CPUs, "
        f"{r.workers} workers =="
    )
    c = r.corpus
    print(
        f"corpus: {c['files']} files, {c['pages']} pages {c['pages_by_kind']}, "
        f"{c['queries']} queries"
    )
    for k, v in r.matching.items():
        print(f"matching {k:12s}: {v['pages_per_s']:>8,} pages/s")
    for k, v in r.pipeline.items():
        print(
            f"pipeline {k:5s}: {v['seconds']:7.2f} s  ({v['pages_per_s']} pages/s, "
            f"{v['review_candidates']} candidates, {v['failures']} failures)"
        )
    for k, v in r.quality.items():
        print(
            f"quality {k:9s}: precision {v['precision']:.3f}  recall {v['recall']:.3f}  "
            f"(fp {v['false_pos']}, missed {v['missed']} of {v['expected']})"
        )
    print(f"merge: {r.merge['seconds']:.2f} s for {r.merge['pages']} pages")


def main(argv: list[str] | None = None, driver: Driver | None = None) -> Results:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--corpus", type=Path, default=Path(tempfile.gettempdir()) / "pmqe-bench-corpus"
    )
    ap.add_argument("--quick", action="store_true", help="smaller corpus")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--repeats", type=int, default=3, help="warm-cache repetitions")
    ap.add_argument("--json", type=Path, help="write results to this file")
    args = ap.parse_args(argv)

    corpus = load_or_generate(args.corpus, args.quick)
    if driver is None:
        cache_dir = Path(tempfile.mkdtemp(prefix="pmqe-bench-cache-"))
        driver = CurrentDriver(cache_dir)
    results = run_benchmark(driver, corpus, args.workers, args.repeats)
    print_results(results)
    if args.json:
        args.json.write_text(json.dumps(asdict(results), indent=2))
    return results


if __name__ == "__main__":
    main()
