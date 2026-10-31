# Changelog

## 2.0.0

A rebuild of the processing core and a redesigned app. Results are more
accurate, runs are faster and survive bad files, and reviewing is keyboard
driven.

### Behaviour changes (read before upgrading)

- **Whole-word matching is the default.** `art` no longer matches "start" and
  `38461` no longer matches "384610". Turn off *Match whole words only* for
  the old substring behaviour.
- **The OCR cache moved** from `.ocr_cache.db` inside each input folder to a
  per-user cache folder (see the README). Old `.ocr_cache.db` files can be
  deleted. The first run after upgrading re-OCRs scanned pages once.
- **Starting a run into a folder that has results replaces them** (after
  asking). Only files pmqe created are removed.
- Duplicate queries (same words) are skipped instead of double-counting.
- Files that could not be read are listed in a new `issues.csv`;
  `report.csv` gained an *Awaiting Review* column. `extraction_history.db`
  keeps its old tables and columns and adds the full review state.

### Fixed

- Approve/reject decisions were applied to a copy of each item, so reports
  kept the old state, the tree reverted decisions when filtered, and a page
  could be counted twice.
- Scanned pages inside otherwise digital PDFs were never OCR'd (the decision
  was made once per file from its first five pages; it is now per page).
- The merged PDF ordered pages as p1, p10, p2.
- Query files saved as UTF-8 with BOM (or UTF-16) silently broke the first query.
- PDF ligatures (ﬁ, ﬂ) and words hyphenated across lines did not match.
- One corrupted file lost the results of its whole batch of up to 50 files.
- A failed OCR was cached as "no text" and never retried.
- Approve/reject used the output folder currently typed in, not the run's.
- Cancel waited for in-flight files; closing the window during a run could crash.
- Zoom and rotate jumped out of the fitted view.
- Network-share (UNC) paths were mangled by the long-path prefix.
- Merge swept up every PDF in every subfolder of the output folder.
- Worker processes of the packaged app loaded Qt's GUI libraries.

### Added

- Multi-page TIFF, HEIC, WebP and BMP input.
- Hits per query is a read-only table that can be searched, filtered (with
  hits, no hits, to review), sorted, copied into Excel or a document, and
  exported as CSV. It stays in step with review decisions.
- A redesigned UI with light and dark themes that follow the system,
  keyboard shortcuts (A/R/J/K/Space/Ctrl+Z…), bulk decisions, undo,
  auto-advance, a text view with highlighted matches and "Open results…" to
  continue reviewing an earlier run.
- Worker-crash isolation: a file that crashes a worker is retried alone and
  reported; the run continues.
- Prompt cancellation, an ETA, `issues.csv`, a per-run `extractor.log`.
- `pmqe --self-test` for checking an installation or a packaged build.
- `bench/benchmark.py` and `tools/screenshots.py`.
- A release workflow that builds and self-tests Windows and macOS apps.

### Performance

- Windows OCR receives raw grayscale pixels instead of a PNG round-trip
  (about 40% faster per page).
- Small files are batched so every CPU stays busy.
- Merging copies pages from their source documents (open once each) and
  de-duplicates fonts: several times faster and about 3× smaller.
