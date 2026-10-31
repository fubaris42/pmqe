"""The Extract page: folders, queries and options on the left; the run's
progress and results on the right."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QWidget,
)

from .. import ocr
from ..models import ReviewItem
from ..pipeline import Progress, RunResult, default_workers
from ..text import PreparedQueries, parse_query_file, prepare_queries
from ..util import format_duration, plural
from . import theme
from .hits_table import HitRow, HitsPanel
from .theme import LG, MD, SM, XL, XXL
from .widgets import (
    Card,
    Chip,
    EmptyState,
    KeyCap,
    PageHeader,
    PathField,
    StatTile,
    Switch,
    button,
    column,
    label,
    row,
)


class ExtractPage(QWidget):
    startRequested = Signal()
    cancelRequested = Signal()
    reviewRequested = Signal()
    mergeRequested = Signal()
    openFolderRequested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._prepared = PreparedQueries()
        self._last_output = ""

        # -- left column: what to do -------------------------------------------
        self.input = PathField(
            "Input folder",
            "folder-input",
            "Folder with PDFs and images…",
            "Searched with all subfolders · PDF, JPG, PNG, TIFF, HEIC, WebP, BMP",
            "Choose the folder to search",
        )
        self.output = PathField(
            "Output folder",
            "folder-output",
            "Where matching pages are saved…",
            "One subfolder per query, plus a report and the review history",
            "Choose where to save matching pages",
        )
        folders = Card("Folders", icon="folder-open")
        folders.body.addWidget(self.input)
        folders.body.addSpacing(SM)
        folders.body.addWidget(self.output)

        self.queries = QPlainTextEdit()
        self.queries.setPlaceholderText(
            "One query per line, for example:\nAaron Hawkins 38461\nquarterly audit report"
        )
        self.queries.setAccessibleName("Queries, one per line")
        self.queries.setMinimumHeight(170)
        self.queries.setTabChangesFocus(True)
        self.query_count = Chip("0 queries")
        self.btn_import = button(
            "Import", "upload", "ghost", tooltip="Load queries from a .txt file"
        )
        self.btn_import.clicked.connect(self._import)
        self.btn_clear = button("Clear", "eraser", "ghost", tooltip="Remove all queries")
        self.btn_clear.clicked.connect(self.queries.clear)
        self.validation_icon = QLabel()
        self.validation = label("", "Helper", wrap=True)
        queries = Card("Queries", icon="search")
        queries.actions.addWidget(self.query_count)
        queries.actions.addSpacing(SM)
        queries.actions.addWidget(self.btn_import)
        queries.actions.addWidget(self.btn_clear)
        queries.body.addWidget(
            label(
                "A page matches a query when it contains every one of its words.",
                "Helper",
                wrap=True,
            )
        )
        queries.body.addWidget(self.queries, 1)
        queries.body.addLayout(row(self.validation_icon, self.validation, None, spacing=6))
        self._validate_timer = QTimer(self, singleShot=True, interval=150)
        self._validate_timer.timeout.connect(self._validate_queries)
        self.queries.textChanged.connect(self._validate_timer.start)

        self.whole_words = Switch("Match whole words only")
        self.whole_words.setChecked(True)
        engine_label = {
            ocr.WINRT: "Windows OCR",
            ocr.VISION: "Apple Vision",
            ocr.RAPIDOCR: "RapidOCR",
        }[ocr.native_engine()]
        self.engine = QComboBox()
        self.engine.setAccessibleName("OCR engine")
        self.engine.addItem(f"{engine_label} (recommended)", False)
        if ocr.native_engine() != ocr.RAPIDOCR:
            self.engine.addItem("RapidOCR (slower; for difficult scans)", True)
        else:
            self.engine.setEnabled(False)
        self.workers = QSpinBox()
        self.workers.setAccessibleName("Parallel workers")
        self.workers.setRange(1, max(1, os.cpu_count() or 1))
        self.workers.setValue(default_workers())
        self.workers.setSuffix(f" of {os.cpu_count() or 1} cores")
        options = Card("Options", icon="sliders-horizontal")
        options.body.addWidget(self.whole_words)
        options.body.addWidget(
            label(
                "“art” finds “Art.” but not “start”. Turn off to match inside words.",
                "Helper",
                wrap=True,
            )
        )
        options.body.addSpacing(SM)
        grid = QGridLayout()
        grid.setHorizontalSpacing(LG)
        grid.setVerticalSpacing(SM)
        ocr_label = label("OCR engine")
        ocr_label.setBuddy(self.engine)
        workers_label = label("Parallel workers")
        workers_label.setBuddy(self.workers)
        grid.addWidget(ocr_label, 0, 0)
        grid.addWidget(self.engine, 0, 1)
        grid.addWidget(workers_label, 1, 0)
        grid.addWidget(self.workers, 1, 1)
        grid.setColumnStretch(1, 1)
        options.body.addLayout(grid)

        self.btn_start = button(
            "Start extraction", "play", "primary", "lg", tooltip="Start extraction (Ctrl+Enter)"
        )
        self.btn_start.clicked.connect(self.startRequested.emit)

        left = column(folders, queries, options, None, spacing=LG)

        # -- right column: progress and results ------------------------------------
        self.run_stack = QStackedWidget()
        self.run_stack.addWidget(self._build_idle())
        self.run_stack.addWidget(self._build_running())
        self.run_stack.addWidget(self._build_done())
        run_card = Card("Run", icon="scan-search")
        run_card.body.addWidget(self.run_stack)

        self.hits = HitsPanel()
        self.hits.message.connect(self._status)
        self.hits_empty = EmptyState(
            "list-checks", "No results yet", "Hits per query appear here after a run."
        )
        self.hits_stack = QStackedWidget()
        self.hits_stack.addWidget(self.hits_empty)
        self.hits_stack.addWidget(self.hits)
        self.zero_chip = Chip("", "accent")
        self.zero_chip.hide()
        hits_card = Card("Hits per query", icon="file-search")
        hits_card.actions.addWidget(self.zero_chip)
        hits_card.body.addWidget(self.hits_stack, 1)
        right = column(run_card, hits_card, spacing=LG)
        right.setStretch(1, 1)

        content = QWidget()
        body = row(left, right, spacing=XL)
        body.setStretch(0, 5)
        body.setStretch(1, 6)
        body.setContentsMargins(XXL, 0, XXL, XL)
        content.setLayout(body)

        # The header, with the main action, stays put; only the cards scroll.
        header = PageHeader(
            "Extract",
            "Find every page that mentions your queries across a folder of PDFs and images.",
        )
        header.actions.addLayout(
            column(self.btn_start, row(None, KeyCap("Ctrl"), KeyCap("Enter"), spacing=4), spacing=6)
        )
        header.layout().setContentsMargins(XXL, XL + 4, XXL, LG + 4)
        scroll = QScrollArea()
        scroll.setObjectName("PageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self.setLayout(column(header, scroll, spacing=0))

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.startRequested.emit)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.startRequested.emit)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._find)
        self._validate_queries()

    # -- run panel states ---------------------------------------------------------------

    def _build_idle(self) -> QWidget:
        return EmptyState(
            "scan-search",
            "Ready when you are",
            "Choose an input folder and an output folder, add at least one query, then start.",
        )

    def _build_running(self) -> QWidget:
        w = QWidget()
        self.percent = label("0%", "BigNumber")
        self.files_label = label("", "Muted")
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Extraction progress")
        self.pages_stat = StatTile("Pages read")
        self.elapsed_stat = StatTile("Elapsed")
        self.eta_stat = StatTile("Time left")
        self.current_file = label("", "Helper")
        self.btn_cancel = button("Cancel", "x", "danger")
        self.btn_cancel.clicked.connect(self.cancelRequested.emit)
        stats = row(self.pages_stat, self.elapsed_stat, self.eta_stat, spacing=SM)
        w.setLayout(
            column(
                row(self.percent, None, self.btn_cancel),
                self.files_label,
                self.progress,
                MD,
                stats,
                self.current_file,
                spacing=SM,
            )
        )
        return w

    def _build_done(self) -> QWidget:
        w = QWidget()
        self.done_title = label("", "CardTitle")
        self.done_chip = Chip("", "success")
        self.stat_files = StatTile("Files")
        self.stat_pages = StatTile("Pages")
        self.stat_ocr = StatTile("Pages OCR'd")
        self.stat_matches = StatTile("Matched pages")
        self.stat_todo = StatTile("To review", tone="accent")
        self.stat_issues = StatTile("Issues")
        grid = QGridLayout()
        grid.setSpacing(SM)
        for i, tile in enumerate(
            (
                self.stat_files,
                self.stat_pages,
                self.stat_ocr,
                self.stat_matches,
                self.stat_todo,
                self.stat_issues,
            )
        ):
            grid.addWidget(tile, i // 3, i % 3)
        self.btn_review = button("Review", "arrow-right", "primary")
        self.btn_review.clicked.connect(self.reviewRequested.emit)
        self.btn_merge = button("Merge to PDF", "file-stack")
        self.btn_merge.clicked.connect(self.mergeRequested.emit)
        self.btn_open = button("Open folder", "external-link", "ghost")
        self.btn_open.clicked.connect(lambda: self.openFolderRequested.emit(self._last_output))
        self.btn_issues = button("Open issues list", None, "link")
        self.btn_issues.clicked.connect(
            lambda: self.openFolderRequested.emit(os.path.join(self._last_output, "issues.csv"))
        )
        self.done_note = label("", "Helper", wrap=True)
        w.setLayout(
            column(
                row(self.done_title, self.done_chip, None),
                grid,
                self.done_note,
                self.btn_issues,
                SM,
                row(self.btn_review, self.btn_merge, None, self.btn_open),
                spacing=MD,
            )
        )
        return w

    def show_running(self) -> None:
        self.run_stack.setCurrentIndex(1)
        self.progress.setRange(0, 0)  # indeterminate while scanning folders
        self.percent.setText("…")
        self.files_label.setText("Scanning folders…")
        for t in (self.pages_stat, self.elapsed_stat, self.eta_stat):
            t.set_value("—")
        self.current_file.setText("")
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("Cancel")
        self.set_inputs_enabled(False)

    def show_cancelling(self) -> None:
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText("Stopping…")
        self.files_label.setText("Finishing the pages in progress…")

    def update_progress(self, p: Progress) -> None:
        if self.progress.maximum() != p.files_total:
            self.progress.setRange(0, max(1, p.files_total))
        self.progress.setValue(p.files_done)
        pct = int(100 * p.files_done / max(1, p.files_total))
        self.percent.setText(f"{pct}%")
        self.files_label.setText(f"{p.files_done:,} of {plural(p.files_total, 'file')}")
        self.pages_stat.set_value(f"{p.pages_done:,}")
        self.elapsed_stat.set_value(format_duration(p.elapsed))
        self.eta_stat.set_value(
            format_duration(p.eta) if p.eta is not None and p.files_done else "—"
        )
        fm = QFontMetrics(self.current_file.font())
        self.current_file.setText(
            fm.elidedText(
                p.current, Qt.TextElideMode.ElideMiddle, max(100, self.current_file.width())
            )
        )

    def show_result(self, res: RunResult) -> None:
        self._last_output = res.output_dir
        self.set_inputs_enabled(True)
        self.run_stack.setCurrentIndex(2)
        todo = sum(1 for i in res.items if not i.auto_approved)
        matches = sum(1 for i in res.items if i.auto_approved)
        issues = len(res.failures) + len(res.warnings)
        self.done_title.setText(
            ("Stopped after " if res.cancelled else "Finished in ") + format_duration(res.elapsed)
        )
        self.done_chip.setText("Partial results" if res.cancelled else "Complete")
        self.done_chip.set_tone("accent" if res.cancelled else "success")
        self.stat_files.set_value(f"{res.files:,}")
        self.stat_pages.set_value(f"{res.pages:,}")
        self.stat_ocr.set_value(f"{res.ocr_pages:,}")
        self.stat_matches.set_value(f"{matches:,}")
        self.stat_todo.set_value(f"{todo:,}")
        self.stat_issues.set_value(f"{issues:,}")
        notes = []
        if res.ocr_cached:
            notes.append(f"{res.ocr_cached:,} of the OCR'd pages came from the cache.")
        if res.failures:
            notes.append(f"{plural(len(res.failures), 'file')} could not be read.")
        if res.warnings:
            notes.append(f"{plural(len(res.warnings), 'file')} were only partly read.")
        self.done_note.setText(" ".join(notes))
        self.done_note.setVisible(bool(notes))
        self.btn_issues.setVisible(issues > 0)
        self.btn_review.setText(
            f"Review {todo:,} {'page' if todo == 1 else 'pages'}" if todo else "Open review"
        )
        self.set_hits(
            [(q.text, res.counts.get(q.text, 0)) for q in res.queries], res.items, res.output_dir
        )

    def show_idle(self) -> None:
        self.set_inputs_enabled(True)
        self.run_stack.setCurrentIndex(0)

    def set_hits(
        self,
        counts: list[tuple[str, int]],
        items: Iterable[ReviewItem],
        output_dir: str | None = None,
    ) -> None:
        """Show approved pages and pages awaiting review for each query."""
        todo = Counter(it.query for it in items if it.status == "candidate")
        rows = [HitRow(q, n, todo.get(q, 0)) for q, n in counts]
        self.hits.set_rows(rows)
        if output_dir:
            self.hits.export_dir = output_dir
        self.hits_stack.setCurrentIndex(1 if rows else 0)
        zero = self.hits.no_hits()
        self.zero_chip.setText(f"{zero:,} with no hits")
        self.zero_chip.setVisible(zero > 0)

    def _find(self) -> None:
        if self.hits_stack.currentIndex() == 1:
            self.hits.focus_search()

    def _status(self, message: str) -> None:
        win = self.window()
        if isinstance(win, QMainWindow):
            win.statusBar().showMessage(message, 5000)

    def set_inputs_enabled(self, enabled: bool) -> None:
        for w in (
            self.input,
            self.output,
            self.queries,
            self.btn_import,
            self.btn_clear,
            self.whole_words,
            self.engine,
            self.workers,
            self.btn_start,
        ):
            w.setEnabled(enabled)
        if self.engine.count() < 2:
            self.engine.setEnabled(False)

    # -- queries --------------------------------------------------------------------------

    def query_lines(self) -> list[str]:
        return self.queries.toPlainText().splitlines()

    def prepared(self) -> PreparedQueries:
        self._validate_queries()
        return self._prepared

    def _validate_queries(self) -> None:
        self._prepared = prepare_queries(self.query_lines())
        n = len(self._prepared.queries)
        self.query_count.setText(plural(n, "query", "queries"))
        self.query_count.set_tone("primary" if n else "neutral")
        notes = []
        if self._prepared.duplicates:
            notes.append(f"{plural(len(self._prepared.duplicates), 'duplicate')} will be skipped")
        if self._prepared.unusable:
            notes.append(
                f"{plural(len(self._prepared.unusable), 'line')} without a word of 2+ characters will be skipped"
            )
        th = theme.current()
        if notes:
            th.bind_icon(self.validation_icon, "triangle-alert", "accent", 14)
            self.validation.setText(" · ".join(notes) + ".")
            self.validation.setObjectName("Warn")
        elif n:
            th.bind_icon(self.validation_icon, "circle-check", "success", 14)
            self.validation.setText(f"{plural(n, 'query', 'queries')} ready.")
            self.validation.setObjectName("Ok")
        else:
            th.bind_icon(self.validation_icon, "info", "faint", 14)
            self.validation.setText("Add at least one query.")
            self.validation.setObjectName("Helper")
        self.validation.style().unpolish(self.validation)
        self.validation.style().polish(self.validation)

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import queries", "", "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            lines = parse_query_file(path)
        except OSError as exc:
            self.validation.setText(f"Could not read {os.path.basename(path)}: {exc.strerror}")
            return
        existing = self.queries.toPlainText().strip()
        self.queries.setPlainText("\n".join([existing, *lines] if existing else lines))
        self._status(
            f"Imported {plural(len(lines), 'query', 'queries')} from {os.path.basename(path)}"
        )
