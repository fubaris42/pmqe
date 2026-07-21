# pmqe — PDF & Image Multi-Query Extractor

Desktop app that scans a folder of PDFs and images for a list of search
queries, extracts every matching page into per-query folders, and gives you a
manual review panel to approve or reject borderline candidates. Outputs can be
merged into a single bookmarked PDF.

## Features

- **Multi-query search** — one query per line; a page matches when every word
  of the query appears on it (Aho-Corasick, case-insensitive). Partial matches
  become review candidates instead of being silently dropped.
- **OCR when needed** — PDFs with a native text layer are read directly;
  scanned PDFs and images (`.jpg`, `.jpeg`, `.png`, `.heif`) are OCR'd using
  the platform engine: Windows Media OCR on Windows, Apple Vision on macOS,
  RapidOCR on Linux (and as automatic fallback everywhere).
- **OCR cache** — results are cached in a SQLite database (`.ocr_cache.db` in
  the input folder), so re-runs with new queries skip OCR entirely.
- **Parallel processing** — files are distributed across worker processes;
  small files are batched to keep overhead low.
- **Manual review panel** — tree of approved pages and candidates per query,
  with image preview, text-layer view with highlighted matches, zoom/rotate,
  and one-click approve/reject that keeps the output folder in sync.
- **Reports** — `report.csv` and `extraction_history.db` written to the output
  folder after every run and every review decision.
- **Merge to one PDF** — combine all extracted outputs into
  `merged_all_queries.pdf` with a bookmark per query and per file.

## Running

Requires [uv](https://docs.astral.sh/uv/) (Python 3.10+ is fetched
automatically):

```console
uv run pmqe
```

Or install it as a tool:

```console
uv tool install .
pmqe
```

## Usage

1. **Browse Input** — folder to scan (recursively).
2. **Browse Output** — destination for extracted pages and reports.
3. Enter queries, one per line (or **Import Queries from .txt File**; lines
   starting with `[` are ignored). Words must be at least 2 characters.
4. **Start Processing.** When the run finishes the app switches to the review
   panel.
5. Review candidates: green entries are auto-approved (all query words found);
   orange entries are candidates missing some words. Approve/reject writes or
   removes the page file in the output folder immediately.
6. Optionally **Merge All to Single PDF**.

Each extracted PDF page is written as its own file,
`{stem}_p{page}_{hash}.pdf`, inside a folder named after the query.

## Development

```console
uv sync                # create .venv with all dependencies
uv run pytest          # run tests
uv run ruff check .    # lint
uv run ruff format .   # format
```

The code is split so that OCR worker processes never import Qt:

- `pmqe/core.py` — processing pipeline: hashing, matching, OCR backends,
  SQLite cache, report writing, PDF merge. No PySide6 imports.
- `pmqe/gui.py` — PySide6 UI and the background `QThread` workers.

Worker processes write their own log files (`extractor.w<pid>.log`) next to
the main `extractor.log` in the output folder, because rotating one shared
log file from multiple processes is unsafe on Windows.

## Building a Windows executable

```console
uv run --with pyinstaller pyinstaller pmqe.spec
```

The result lands in `dist/pmqe/pmqe.exe`. See [pmqe.spec](pmqe.spec).
