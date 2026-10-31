"""The application window: sidebar navigation, the two pages, and the
orchestration of runs and merges."""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QStackedWidget,
    QWidget,
)

from .. import __version__
from ..cache import cache_size, default_cache_path, delete_cache
from ..pipeline import RunOptions, RunResult
from ..store import NoRunFound, find_previous_run
from ..util import format_bytes, is_within, long_path, plural
from . import theme
from .extract_page import ExtractPage
from .review_page import ReviewPage
from .theme import LG, MD, SM
from .widgets import NavButton, button, column, icon_button, label, row
from .workers import MergeWorker, RunWorker

log = logging.getLogger(__name__)

EXTRACT, REVIEW = 0, 1
THEME_ICONS = {"system": "monitor", "light": "sun", "dark": "moon"}


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self.settings = settings
        self._run: RunWorker | None = None
        self._merge: MergeWorker | None = None
        self._merge_dialog: QProgressDialog | None = None
        self.setWindowTitle("pmqe")
        self.setMinimumSize(1080, 720)

        self.extract = ExtractPage()
        self.review = ReviewPage()
        self.pages = QStackedWidget()
        self.pages.setObjectName("Pages")
        self.pages.addWidget(self.extract)
        self.pages.addWidget(self.review)

        root = QWidget()
        root.setObjectName("AppRoot")
        root.setLayout(row(self._build_sidebar(), self.pages, spacing=0))
        root.layout().setStretch(1, 1)
        self.setCentralWidget(root)
        self.statusBar().setSizeGripEnabled(False)

        self._wire()
        self._load_settings()
        self.go(EXTRACT)
        self._refresh_cache_label()

    # -- construction ----------------------------------------------------------------

    def _build_sidebar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(236)
        logo = QLabel()
        logo.setPixmap(theme.app_icon_pixmap(36))
        brand = column(
            label("pmqe", "BrandName"), label("Multi-query extractor", "BrandTag"), spacing=0
        )
        self.nav_extract = NavButton("Extract", "scan-search")
        self.nav_review = NavButton("Review", "list-checks")
        self.nav_extract.setToolTip("Set up and run an extraction (Ctrl+1)")
        self.nav_review.setToolTip("Approve or reject matched pages (Ctrl+2)")
        open_results = NavButton("Open results…", "history", checkable=False)
        open_results.setToolTip("Open the output folder of an earlier run (Ctrl+O)")
        open_results.clicked.connect(self.open_results)

        self.cache_label = label("", "Helper")
        self.btn_clear_cache = button("Clear", None, "link", tooltip="Delete cached OCR text")
        self.btn_clear_cache.clicked.connect(self.clear_cache)
        self.theme_btn = icon_button("monitor", "Theme")
        self.theme_btn.clicked.connect(self._cycle_theme)
        footer = QWidget()
        footer.setObjectName("SidebarFooter")
        footer.setLayout(
            column(
                row(
                    theme_icon_label("database"),
                    self.cache_label,
                    None,
                    self.btn_clear_cache,
                    spacing=6,
                ),
                row(label(f"Version {__version__}"), None, self.theme_btn),
                spacing=SM,
            )
        )
        lay = column(
            row(logo, brand, None, spacing=MD),
            LG + 4,
            label("WORKSPACE", "SidebarSection"),
            self.nav_extract,
            self.nav_review,
            SM,
            open_results,
            None,
            footer,
            spacing=4,
            margins=LG,
        )
        lay.setContentsMargins(LG, LG + 4, LG, LG)
        bar.setLayout(lay)
        return bar

    def _wire(self) -> None:
        self.nav_extract.clicked.connect(lambda: self.go(EXTRACT))
        self.nav_review.clicked.connect(lambda: self.go(REVIEW))
        e, r = self.extract, self.review
        e.startRequested.connect(self.start_run)
        e.cancelRequested.connect(self.cancel_run)
        e.reviewRequested.connect(lambda: self.go(REVIEW))
        e.mergeRequested.connect(self.merge)
        e.openFolderRequested.connect(self.open_path)
        r.mergeRequested.connect(self.merge)
        r.openFolderRequested.connect(self.open_path)
        r.openResultsRequested.connect(self.open_results)
        r.extractRequested.connect(lambda: self.go(EXTRACT))
        r.statusMessage.connect(lambda m: self.statusBar().showMessage(m, 6000))
        r.countsChanged.connect(lambda n: self.nav_review.set_badge(f"{n:,}" if n else ""))
        r.countsChanged.connect(lambda _: self._sync_hits())
        QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self.go(EXTRACT))
        QShortcut(QKeySequence("Ctrl+2"), self, activated=lambda: self.go(REVIEW))
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_results)
        theme.current().changed.connect(self._on_theme_changed)

    def go(self, page: int) -> None:
        self.pages.setCurrentIndex(page)
        self.nav_extract.setChecked(page == EXTRACT)
        self.nav_review.setChecked(page == REVIEW)
        if page == REVIEW and self.review.output_dir:
            self.review.tree.setFocus()

    # -- settings -----------------------------------------------------------------------

    def _load_settings(self) -> None:
        s = self.settings
        self.extract.input.set_path(s.value("input_dir", "", str))
        self.extract.output.set_path(s.value("output_dir", "", str))
        self.extract.queries.setPlainText(s.value("queries", "", str))
        self.extract.whole_words.setChecked(s.value("whole_words", True, bool))
        self.extract.engine.setCurrentIndex(
            min(s.value("engine_index", 0, int), self.extract.engine.count() - 1)
        )
        workers = s.value("workers", 0, int)
        if workers:
            self.extract.workers.setValue(workers)
        self.review.auto_advance.setChecked(s.value("auto_advance", True, bool))
        if geometry := s.value("geometry"):
            self.restoreGeometry(geometry)
        else:
            self.resize(1360, 860)
        if sizes := s.value("review_splitter"):
            self.review.splitter.setSizes([int(x) for x in sizes])
        self._update_theme_button()

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("input_dir", self.extract.input.path())
        s.setValue("output_dir", self.extract.output.path())
        s.setValue("queries", self.extract.queries.toPlainText())
        s.setValue("whole_words", self.extract.whole_words.isChecked())
        s.setValue("engine_index", self.extract.engine.currentIndex())
        s.setValue("workers", self.extract.workers.value())
        s.setValue("auto_advance", self.review.auto_advance.isChecked())
        s.setValue("geometry", self.saveGeometry())
        s.setValue("review_splitter", self.review.splitter.sizes())
        s.setValue("theme", theme.current().mode)
        s.sync()

    # -- runs ------------------------------------------------------------------------------

    def _validate(self) -> RunOptions | None:
        e = self.extract
        inp, out = e.input.path(), e.output.path()
        ok = True
        if not inp:
            e.input.set_error("Choose the folder to search.")
            ok = False
        elif not os.path.isdir(long_path(inp)):
            e.input.set_error("This folder does not exist.")
            ok = False
        if not out:
            e.output.set_error("Choose where to save matching pages.")
            ok = False
        elif ok and is_within(inp, out):
            e.output.set_error(
                "The output folder can't be the input folder or contain it. "
                "Choose another folder (a subfolder of the input folder is fine)."
            )
            ok = False
        prepared = e.prepared()
        if ok and not prepared.queries:
            e.queries.setFocus()
            QMessageBox.information(
                self, "Add queries", "Add at least one query (one per line) to search for."
            )
            ok = False
        if not ok:
            return None
        return RunOptions(
            input_dir=os.path.normpath(inp),
            output_dir=os.path.normpath(out),
            queries=prepared.queries,
            whole_words=e.whole_words.isChecked(),
            force_rapidocr=bool(e.engine.currentData()),
            workers=e.workers.value(),
        )

    def start_run(self) -> None:
        if self._run is not None:
            return
        opts = self._validate()
        if opts is None:
            return
        prev = find_previous_run(opts.output_dir)
        if prev and not self._confirm_replace(prev):
            return
        if self.review.output_dir and is_within(self.review.output_dir, opts.output_dir):
            self.review.close_run()  # its files are about to be replaced
        self._save_settings()
        self._run = RunWorker(opts)
        self._run.progress.connect(self.extract.update_progress)
        self._run.succeeded.connect(self._run_succeeded)
        self._run.failed.connect(self._run_failed)
        self._run.finished.connect(self._run_finished)
        self.extract.show_running()
        self.btn_clear_cache.setEnabled(False)
        self.statusBar().showMessage("Extraction started")
        self._run.start()

    def _confirm_replace(self, prev) -> bool:
        details = []
        if prev.created:
            details.append(
                f"from a run on {datetime.fromtimestamp(prev.created).strftime('%d %b %Y at %H:%M')}"
            )
        if prev.queries is not None:
            details.append(plural(prev.queries, "query", "queries"))
        details.append(plural(prev.output_files, "extracted page file"))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Replace previous results?")
        box.setText("This output folder already has results.")
        box.setInformativeText(
            f"They are {', '.join(details)}.\n\nStarting will replace them, including any review "
            "decisions. Other files in the folder are not touched."
        )
        replace = box.addButton("Replace results", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is replace

    def cancel_run(self) -> None:
        if self._run is not None and not self._run.cancelling:
            self._run.cancel()
            self.extract.show_cancelling()
            self.statusBar().showMessage("Stopping… pages already in progress will finish first")

    def _run_succeeded(self, res: RunResult) -> None:
        self.extract.show_result(res)
        try:
            self.review.load_run(res.output_dir)
        except NoRunFound:
            log.exception("Run finished but its record could not be opened")
        todo = sum(1 for i in res.items if not i.auto_approved)
        verb = "stopped" if res.cancelled else "finished"
        self.statusBar().showMessage(
            f"Extraction {verb}: {plural(len(res.items) - todo, 'matched page')}, {todo:,} to review",
            10000,
        )
        QApplication.alert(self)  # flash the taskbar if the window is in the background
        self.setWindowTitle(f"pmqe — {os.path.basename(res.output_dir) or res.output_dir}")

    def _run_failed(self, message: str) -> None:
        self.extract.show_idle()
        QMessageBox.warning(self, "Extraction did not run", message)

    def _run_finished(self) -> None:
        if self._run is not None:
            self._run.deleteLater()
        self._run = None
        self.btn_clear_cache.setEnabled(True)
        self._refresh_cache_label()

    def _sync_hits(self) -> None:
        """Keep the Extract page's per-query table in step with review decisions."""
        model = self.review.model
        if not model.queries:
            return
        approved = Counter(i.query for i in model.items if i.approved)
        self.extract.set_hits(
            [(q.text, approved.get(q.text, 0)) for q in model.queries],
            model.items,
            self.review.output_dir,
        )

    # -- merge ----------------------------------------------------------------------------

    def merge(self) -> None:
        out = self.review.output_dir or self.extract._last_output
        if not out:
            QMessageBox.information(
                self, "Merge to PDF", "Run an extraction or open earlier results first."
            )
            return
        if self._merge is not None:
            return
        self._merge = MergeWorker(out)
        dlg = QProgressDialog("Merging approved pages into one PDF…", "Cancel", 0, 0, self)
        dlg.setWindowTitle("Merge to PDF")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(400)
        dlg.canceled.connect(self._merge.cancel)
        self._merge_dialog = dlg
        self._merge.progress.connect(lambda d, t: (dlg.setMaximum(t), dlg.setValue(d)))
        self._merge.succeeded.connect(self._merge_succeeded)
        self._merge.failed.connect(self._merge_failed)
        self._merge.finished.connect(self._merge_finished)
        self._merge.start()

    def _merge_succeeded(self, res) -> None:
        if self._merge_dialog:
            self._merge_dialog.reset()
        if res.files == 0:
            QMessageBox.information(
                self, "Merge to PDF", "There are no approved pages to merge yet."
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle("Merge complete")
        box.setText(f"Merged {plural(res.pages, 'page')} into one PDF.")
        info = f"{res.path}\n\nEach query is a bookmark, with one entry per page."
        if res.skipped:
            info += f"\n\n{plural(len(res.skipped), 'page')} could not be added; see the log."
        box.setInformativeText(info)
        open_btn = box.addButton("Open PDF", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            self.open_path(res.path)

    def _merge_failed(self, message: str) -> None:
        if self._merge_dialog:
            self._merge_dialog.reset()
        if message != "Merge cancelled.":
            QMessageBox.warning(self, "Merge failed", message)
        self.statusBar().showMessage(message, 5000)

    def _merge_finished(self) -> None:
        if self._merge is not None:
            self._merge.deleteLater()
        self._merge = None
        if self._merge_dialog:
            self._merge_dialog.deleteLater()
        self._merge_dialog = None

    # -- other actions -------------------------------------------------------------------

    def open_results(self) -> None:
        start = self.review.output_dir or self.extract.output.path() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Open the output folder of a run", start)
        if not folder:
            return
        try:
            self.review.load_run(os.path.normpath(folder))
        except NoRunFound as exc:
            QMessageBox.information(
                self, "No results here", f"{exc}.\n\nChoose the output folder of a pmqe run."
            )
            return
        self.go(REVIEW)
        self.setWindowTitle(f"pmqe — {os.path.basename(folder)}")

    def open_path(self, path: str) -> None:
        if path and os.path.exists(long_path(path)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.statusBar().showMessage(f"Not found: {path}", 5000)

    def clear_cache(self) -> None:
        path = default_cache_path()
        size = cache_size(path)
        answer = QMessageBox.question(
            self,
            "Clear the OCR cache?",
            f"Scanned pages will be OCR'd again the next time they are searched.\n\n{format_bytes(size)} will be freed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.review.release_cache()
        try:
            delete_cache(path)
            self.statusBar().showMessage("OCR cache cleared", 5000)
        except OSError as exc:
            QMessageBox.warning(self, "Could not clear the cache", str(exc))
        self._refresh_cache_label()

    def _refresh_cache_label(self) -> None:
        size = cache_size(default_cache_path())
        self.cache_label.setText(f"OCR cache · {format_bytes(size)}")
        self.btn_clear_cache.setVisible(size > 0)

    def _cycle_theme(self) -> None:
        modes = theme.Theme.MODES
        th = theme.current()
        th.set_mode(modes[(modes.index(th.mode) + 1) % len(modes)])
        self._update_theme_button()

    def _update_theme_button(self) -> None:
        mode = theme.current().mode
        theme.current().bind_icon(self.theme_btn, THEME_ICONS[mode], "muted", 18)
        self.theme_btn.setToolTip(f"Theme: {mode.capitalize()} (click to change)")

    def _on_theme_changed(self) -> None:
        for w in self.findChildren(QWidget):
            w.update()

    # -- shutdown ------------------------------------------------------------------------

    def closeEvent(self, e: QCloseEvent) -> None:
        if self._run is not None:
            answer = QMessageBox.question(
                self,
                "Stop the extraction?",
                "An extraction is running. Stop it and quit?\n\nPages finished so far are kept.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self._run.cancel()
            self.statusBar().showMessage("Stopping the extraction…")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                while self._run is not None and not self._run.wait(100):
                    QApplication.processEvents()
            finally:
                QApplication.restoreOverrideCursor()
        if self._merge is not None:
            # Cancel is checked between pages; saving a big PDF can take a
            # while, and the thread must not be destroyed while it runs.
            self._merge.succeeded.disconnect()  # no result dialogs while quitting
            self._merge.failed.disconnect()
            self._merge.cancel()
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                while self._merge is not None and not self._merge.wait(100):
                    QApplication.processEvents()
            finally:
                QApplication.restoreOverrideCursor()
        self._save_settings()
        self.review.shutdown()
        e.accept()


def theme_icon_label(name: str) -> QLabel:
    lab = QLabel()
    theme.current().bind_icon(lab, name, "faint", 14)
    return lab
