"""The Review page: a filterable queue of matched pages, a preview, and
approve/reject decisions that are written straight to the run record."""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime

from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QStackedWidget,
    QTreeView,
    QWidget,
)

from ..cache import OcrCache, default_cache_path, open_cache
from ..extract import export_item, output_path, remove_item_output, review_text
from ..models import ReviewItem
from ..store import NoRunFound, RunInfo, RunStore
from ..util import long_path, plural
from . import theme
from .preview import PageView, TextView
from .review_model import ALL, APPROVED, REJECTED, TODO, ReviewDelegate, ReviewModel
from .theme import LG, MD, SM, XL, XXL
from .widgets import (
    Card,
    Chip,
    EmptyState,
    KeyCap,
    PageHeader,
    Segmented,
    Switch,
    button,
    column,
    hline,
    icon_button,
    label,
    row,
)
from .workers import PreviewLoader

log = logging.getLogger(__name__)

BULK_PROGRESS_THRESHOLD = 25
FILTER_LABELS = {TODO: "To review", APPROVED: "Approved", REJECTED: "Rejected", ALL: "All"}

SHORTCUTS = [
    ("A", "Approve the selected pages"),
    ("R or Delete", "Reject the selected pages"),
    ("J / K or ↓ / ↑", "Next / previous page"),
    ("Space", "Switch between page and text"),
    ("+ / − / 0", "Zoom in / out / fit"),
    ("[ / ]", "Rotate left / right"),
    ("Ctrl+Z", "Undo the last decision"),
    ("Ctrl+F", "Filter the list"),
    ("Shift/Ctrl+click", "Select several pages"),
]


class ReviewPage(QWidget):
    statusMessage = Signal(str)
    countsChanged = Signal(int)
    mergeRequested = Signal()
    openFolderRequested = Signal(str)
    openResultsRequested = Signal()
    extractRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.model = ReviewModel()
        self.store: RunStore | None = None
        self.info: RunInfo | None = None
        self._cache: OcrCache | None = None
        self._undo: list[list[tuple[ReviewItem, bool, bool]]] = []
        self._current: ReviewItem | None = None
        self.loader = PreviewLoader(self)
        self.loader.ready.connect(self._on_image)
        self.loader.failed.connect(
            lambda msg: self.page_view.show_message(f"Could not show this page.\n{msg}")
        )

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_empty())
        self.stack.addWidget(self._build_content())
        self.setLayout(column(self.stack))
        self._install_shortcuts()

    # -- construction ---------------------------------------------------------------

    def _build_empty(self) -> QWidget:
        start = button("Start an extraction", "scan-search", "primary")
        start.clicked.connect(self.extractRequested.emit)
        open_btn = button("Open earlier results…", "history")
        open_btn.clicked.connect(self.openResultsRequested.emit)
        return EmptyState(
            "list-checks",
            "Nothing to review yet",
            "Run an extraction, or open the output folder of an earlier run to pick up where you left off.",
            [start, open_btn],
        )

    def _build_content(self) -> QWidget:
        self.header = PageHeader("Review")
        self.partial_chip = Chip("Partial run", "accent")
        self.partial_chip.hide()
        btn_open = button(
            "Open results…", "history", "ghost", tooltip="Open the results of another run (Ctrl+O)"
        )
        btn_open.clicked.connect(self.openResultsRequested.emit)
        btn_folder = button("Open folder", "external-link", "ghost")
        btn_folder.clicked.connect(
            lambda: self.info and self.openFolderRequested.emit(self.info.output_dir)
        )
        btn_merge = button(
            "Merge to PDF",
            "file-stack",
            tooltip="Combine every approved page into one bookmarked PDF",
        )
        btn_merge.clicked.connect(self.mergeRequested.emit)
        for w in (self.partial_chip, btn_open, btn_folder, btn_merge):
            self.header.actions.addWidget(w)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by query or file name")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Filter the list")
        search_icon = self.search.addAction(
            theme.current().icon("search", "faint", 16), QLineEdit.ActionPosition.LeadingPosition
        )
        theme.current().bind_icon(search_icon, "search", "faint", 16)
        self._search_timer = QTimer(self, singleShot=True, interval=180)
        self._search_timer.timeout.connect(
            lambda: self._refilter(lambda: self.model.set_text(self.search.text()))
        )
        self.search.textChanged.connect(self._search_timer.start)
        self.filter = Segmented([FILTER_LABELS[i] for i in range(4)])
        self.filter.currentChanged.connect(
            lambda i: self._refilter(lambda: self.model.set_filter(i))
        )
        help_btn = icon_button("keyboard", "Keyboard shortcuts")
        help_btn.clicked.connect(self._show_shortcuts)
        toolbar = row(self.search, self.filter, help_btn, spacing=MD)
        toolbar.setStretch(0, 1)

        # list
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setItemDelegate(ReviewDelegate(self.tree))
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(0)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(False)
        self.tree.setMouseTracking(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setAccessibleName("Matched pages")
        self.tree.clicked.connect(self._toggle_group)
        self.model.modelReset.connect(self.tree.expandAll)
        self.tree.selectionModel().currentChanged.connect(
            lambda cur, _: self._show(self.model.item(cur))
        )
        self.tree.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        self.list_empty = EmptyState("inbox", "Nothing here", "No pages match this filter.")
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.tree)
        self.list_stack.addWidget(self.list_empty)
        list_card = Card()
        list_card.body.setContentsMargins(SM, SM, SM, SM)
        list_card.body.addWidget(self.list_stack)
        list_card.setMinimumWidth(320)

        # preview
        self.view_mode = Segmented(["Page", "Text"])
        self.view_mode.currentChanged.connect(self._on_view_mode)
        self.btn_zoom_out = icon_button("zoom-out", "Zoom out (−)")
        self.zoom_label = label("Fit", "Muted")
        self.zoom_label.setMinimumWidth(46)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_zoom_in = icon_button("zoom-in", "Zoom in (+)")
        self.btn_fit = icon_button("maximize-2", "Fit to window (0)")
        self.btn_rot_l = icon_button("rotate-ccw", "Rotate left ([)")
        self.btn_rot_r = icon_button("rotate-cw", "Rotate right (])")
        self.page_view = PageView()
        self.page_view.zoomChanged.connect(self.zoom_label.setText)
        self.btn_zoom_out.clicked.connect(self.page_view.zoom_out)
        self.btn_zoom_in.clicked.connect(self.page_view.zoom_in)
        self.btn_fit.clicked.connect(self.page_view.fit)
        self.btn_rot_l.clicked.connect(lambda: self.page_view.rotate_by(-90))
        self.btn_rot_r.clicked.connect(lambda: self.page_view.rotate_by(90))
        self.zoom_controls = QWidget()
        self.zoom_controls.setLayout(
            row(
                self.btn_zoom_out,
                self.zoom_label,
                self.btn_zoom_in,
                self.btn_fit,
                SM,
                self.btn_rot_l,
                self.btn_rot_r,
                spacing=2,
            )
        )
        self.text_view = TextView()
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.page_view)
        self.preview_stack.addWidget(self.text_view)

        # decision bar
        self.item_title = label("", "CardTitle")
        self.item_meta = label("", "Muted")
        self.item_meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.words = row(spacing=6)
        self.btn_show = button(
            "Show file", "folder", "link", tooltip="Open the folder that contains this file"
        )
        self.btn_show.clicked.connect(self._show_file)
        self.btn_undo = icon_button("undo-2", "Undo (Ctrl+Z)")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_reject = button("Reject", "x", "danger")
        self.btn_reject.clicked.connect(lambda: self.decide(False))
        self.btn_approve = button("Approve", "check", "success")
        self.btn_approve.clicked.connect(lambda: self.decide(True))
        for b in (self.btn_reject, self.btn_approve):
            b.setMinimumWidth(130)
            b.setProperty("size", "lg")
        self.auto_advance = Switch("Go to the next page after deciding")
        self.auto_advance.setChecked(True)
        decision = row(
            column(
                row(self.item_title, None),
                row(self.item_meta, self.btn_show, None, spacing=MD),
                self.words,
                spacing=4,
            ),
            None,
            column(
                row(None, self.btn_undo, self.btn_reject, self.btn_approve, spacing=SM),
                row(
                    None,
                    KeyCap("R"),
                    label("reject", "Helper"),
                    MD,
                    KeyCap("A"),
                    label("approve", "Helper"),
                    spacing=4,
                ),
                spacing=6,
            ),
            spacing=LG,
        )
        preview_card = Card()
        preview_card.body.setContentsMargins(LG, MD, LG, LG)
        preview_card.body.addLayout(row(self.view_mode, None, self.zoom_controls))
        preview_card.body.addWidget(self.preview_stack, 1)
        preview_card.body.addWidget(hline())
        preview_card.body.addLayout(decision)
        preview_card.body.addLayout(row(self.auto_advance, None))

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(list_card)
        self.splitter.addWidget(preview_card)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([400, 900])
        self.splitter.setChildrenCollapsible(False)

        w = QWidget()
        w.setLayout(column(self.header, toolbar, self.splitter, spacing=LG))
        w.layout().setContentsMargins(XXL, XL + 4, XXL, XL)
        w.layout().setStretch(2, 1)
        return w

    def _install_shortcuts(self) -> None:
        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        bindings = {
            "A": lambda: self.decide(True),
            "R": lambda: self.decide(False),
            "Delete": lambda: self.decide(False),
            "J": lambda: self._step(1),
            "K": lambda: self._step(-1),
            "Space": self._toggle_view,
            "+": self.page_view.zoom_in,
            "=": self.page_view.zoom_in,
            "-": self.page_view.zoom_out,
            "0": self.page_view.fit,
            "[": lambda: self.page_view.rotate_by(-90),
            "]": lambda: self.page_view.rotate_by(90),
            "Ctrl+Z": self.undo,
            "Ctrl+F": lambda: (self.search.setFocus(), self.search.selectAll()),
        }
        for key, fn in bindings.items():
            QShortcut(QKeySequence(key), self, activated=fn, context=ctx)

    # -- loading a run ------------------------------------------------------------------

    def load_run(self, output_dir: str) -> None:
        """Show a run's results. Raises :class:`NoRunFound`."""
        store = RunStore.open(output_dir)
        self.close_run()
        self.store = store
        self.info = store.info()
        items = store.items()
        self.model.set_run(self.info.queries, items)
        self._undo.clear()
        created = (
            datetime.fromtimestamp(self.info.created).strftime("%d %b %Y, %H:%M")
            if self.info.created
            else ""
        )
        files = self.info.stats.get("files")
        parts = [
            f"Run of {created}" if created else "Run",
            plural(len(self.info.queries), "query", "queries"),
        ]
        if files is not None:
            parts.append(plural(files, "file"))
        parts.append(output_dir)
        self.header.subtitle.setText(" · ".join(parts))
        self.partial_chip.setVisible(self.info.status != "complete")
        self.partial_chip.setText(
            "Partial run" if self.info.status == "cancelled" else "Incomplete run"
        )
        self.stack.setCurrentIndex(1)
        # Start on the queue that needs attention
        counts = self.model.status_counts()
        start = TODO if counts[TODO] else ALL
        self.filter.set_current(start)
        self.model.set_filter(start)
        self._after_refilter(0)
        self._update_counts()

    def close_run(self) -> None:
        if self.store is not None:
            self.store.close()
        self.store = self.info = None
        self._current = None
        self.model.set_run([], [])
        self.page_view.clear()
        self.stack.setCurrentIndex(0)
        self.countsChanged.emit(0)

    @property
    def output_dir(self) -> str | None:
        return self.info.output_dir if self.info else None

    # -- list behaviour ------------------------------------------------------------------

    def _flat_position(self) -> int:
        return self.model.flat_position(self.tree.currentIndex())

    def _refilter(self, apply) -> None:
        pos = self._flat_position()
        keep = self._current.id if self._current else None
        apply()
        self._after_refilter(pos, keep)

    def _after_refilter(self, pos: int, keep_id: str | None = None) -> None:
        visible = self.model.visible_count()
        self.list_stack.setCurrentIndex(0 if visible else 1)
        if not visible:
            self._show(None)
            return
        target = self.model.index_of(keep_id) if keep_id else QModelIndex()
        if not target.isValid():
            target = self.model.index_at(pos)
        self._select(target)

    def _select(self, index: QModelIndex) -> None:
        sm = self.tree.selectionModel()
        sm.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.tree.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)

    def _step(self, delta: int) -> None:
        if self.model.visible_count():
            self._select(self.model.index_at(self._flat_position() + delta))

    def _toggle_group(self, index: QModelIndex) -> None:
        if index.isValid() and self.model.item(index) is None:
            self.tree.setExpanded(index, not self.tree.isExpanded(index))

    def _selected_items(self) -> list[ReviewItem]:
        items = [self.model.item(i) for i in self.tree.selectionModel().selectedRows()]
        items = [i for i in items if i is not None]
        if not items and self._current is not None:
            items = [self._current]
        return items

    def _update_counts(self) -> None:
        counts = self.model.status_counts()
        for i in range(4):
            self.filter.set_label(i, f"{FILTER_LABELS[i]}  {counts[i]:,}")
        self.countsChanged.emit(counts[TODO])

    def _update_buttons(self) -> None:
        n = len(self._selected_items())
        has = n > 0 and self.store is not None
        self.btn_approve.setEnabled(has)
        self.btn_reject.setEnabled(has)
        self.btn_approve.setText(f"Approve {n}" if n > 1 else "Approve")
        self.btn_reject.setText(f"Reject {n}" if n > 1 else "Reject")
        self.btn_undo.setEnabled(bool(self._undo))

    # -- preview ----------------------------------------------------------------------------

    def _show(self, item: ReviewItem | None) -> None:
        self._current = item
        self._update_buttons()
        if item is None:
            self.item_title.setText("")
            self.item_meta.setText("")
            self._set_words([], [])
            self.page_view.clear()
            self.page_view.show_message("Select a page in the list.")
            self.btn_show.hide()
            return
        self.btn_show.show()
        self.item_title.setText(item.query)
        meta = f"{item.file_name} · page {item.page_idx + 1}" + (
            f" of {item.page_count}" if item.page_count > 1 else ""
        )
        fm = QFontMetrics(self.item_meta.font())
        self.item_meta.setText(fm.elidedText(meta, Qt.TextElideMode.ElideMiddle, 520))
        self.item_meta.setToolTip(item.file_path)
        tokens = self.model.queries[item.query_idx].tokens if self.model.queries else ()
        self._set_words([t for t in tokens if t not in item.missing_words], item.missing_words)
        self._load_preview()

    def _set_words(self, found: list[str], missing: list[str]) -> None:
        while self.words.count():
            w = self.words.takeAt(0).widget()
            if w:
                w.hide()  # deleteLater() is deferred; don't leave it painted meanwhile
                w.deleteLater()
        for t in found:
            chip = Chip(t, "success")
            chip.setToolTip("Found on this page")
            self.words.addWidget(chip)
        for t in missing:
            chip = Chip(f"{t} — missing", "accent")
            chip.setToolTip("Not found on this page")
            self.words.addWidget(chip)
        self.words.addStretch(1)

    def _load_preview(self) -> None:
        item = self._current
        if item is None:
            return
        if self.view_mode.current() == 0:
            if not os.path.exists(long_path(item.file_path)):
                self.page_view.clear()
                self.page_view.show_message("The source file has moved or been deleted.")
                return
            self.loader.load(item.file_path, item.page_idx)
        else:
            self._load_text(item)

    def _load_text(self, item: ReviewItem) -> None:
        if self._cache is None:
            self._cache = open_cache(default_cache_path())
        try:
            engine = self.info.engine if self.info else None
            text, source = review_text(
                item.file_path, item.file_hash, item.page_idx, self._cache, engine
            )
        except Exception as exc:
            text, source = "", f"Could not read the page ({exc})"
        tokens = list(self.model.queries[item.query_idx].tokens)
        self.text_view.set_text(text, source, tokens, self.info.whole_words if self.info else True)

    def _on_image(self, image) -> None:
        self.page_view.set_image(image)

    def _toggle_view(self) -> None:
        self.view_mode.set_current(1 - self.view_mode.current())
        self._on_view_mode(self.view_mode.current())

    def _on_view_mode(self, mode: int) -> None:
        self.preview_stack.setCurrentIndex(mode)
        self.zoom_controls.setVisible(mode == 0)
        self._load_preview()

    def _show_file(self) -> None:
        if self._current:
            self.openFolderRequested.emit(os.path.dirname(self._current.file_path))

    def _show_shortcuts(self) -> None:
        rows = "".join(
            f"<tr><td style='padding:4px 18px 4px 0'><b>{k}</b></td><td style='padding:4px 0'>{v}</td></tr>"
            for k, v in SHORTCUTS
        )
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard shortcuts")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<table>{rows}</table>")
        box.exec()

    # -- decisions ----------------------------------------------------------------------------

    def decide(self, approve: bool) -> None:
        if self.store is None or self.info is None:
            return
        items = self._selected_items()
        if not items:
            return
        if not approve and len(items) > 1:
            answer = QMessageBox.question(
                self,
                "Reject pages",
                f"Reject {len(items):,} pages?\n\nTheir files will be removed from the output folder. "
                "You can undo this with Ctrl+Z.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        record = [(it, bool(it.approved), it.decided) for it in items]
        pos = self._flat_position()
        changed, errors = self._apply(items, approve)
        if not changed:
            self._report_errors(errors)
            return
        self._undo.append(record)
        self._finish_decision(changed, pos, advance=True)
        verb = "Approved" if approve else "Rejected"
        what = (
            f"{changed[0].file_name}, page {changed[0].page_idx + 1}"
            if len(changed) == 1
            else plural(len(changed), "page")
        )
        self.statusMessage.emit(f"{verb} {what} · Ctrl+Z to undo")
        self._report_errors(errors)

    def undo(self) -> None:
        if not self._undo or self.store is None:
            return
        record = self._undo.pop()
        restored: list[ReviewItem] = []
        shared = self._approved_outputs()
        for item, was_approved, was_decided in record:
            try:
                self._sync_file(item, was_approved, shared)
            except OSError as exc:
                log.warning("Undo could not restore %s: %s", item.output_name, exc)
            item.approved, item.decided = was_approved, was_decided
            restored.append(item)
        self.store.record_decisions(restored)
        self.model.items_changed(restored)
        self._after_refilter(self._flat_position(), restored[0].id)
        hidden = not self.model.index_of(restored[0].id).isValid()
        self.statusMessage.emit(
            f"Undone: {plural(len(restored), 'decision')}"
            + (" · hidden by the current filter" if hidden else "")
        )
        self._update_counts()
        self._update_buttons()

    def _apply(self, items: list[ReviewItem], approve: bool) -> tuple[list[ReviewItem], list[str]]:
        changed: list[ReviewItem] = []
        errors: list[str] = []
        progress = None
        if len(items) > BULK_PROGRESS_THRESHOLD:
            progress = QProgressDialog("Saving decisions…", "Stop", 0, len(items), self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(300)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        shared = self._approved_outputs()
        try:
            for n, item in enumerate(items):
                if progress:
                    progress.setValue(n)
                    if progress.wasCanceled():
                        break
                try:
                    self._sync_file(item, approve, shared)
                except Exception as exc:
                    errors.append(f"{item.file_name} p{item.page_idx + 1}: {exc}")
                    continue
                item.approved, item.decided = approve, True
                changed.append(item)
            if changed:
                self.store.record_decisions(changed)  # type: ignore[union-attr]
        finally:
            QApplication.restoreOverrideCursor()
            if progress:
                progress.close()
        return changed, errors

    def _approved_outputs(self) -> Counter:
        """How many approved items use each output file (identical copies of
        one file share an output)."""
        return Counter((o.folder, o.output_name) for o in self.model.items if o.approved)

    def _sync_file(self, item: ReviewItem, approved: bool, shared: Counter) -> None:
        """Make the output folder agree with a decision; ``shared`` is kept
        up to date as decisions are applied."""
        assert self.info is not None
        out_dir = self.info.output_dir
        key = (item.folder, item.output_name)
        if approved:
            if not os.path.exists(long_path(output_path(item, out_dir))):
                export_item(item, out_dir)
            if not item.approved:
                shared[key] += 1
            return
        if item.approved:
            shared[key] -= 1
        if shared[key] <= 0:  # no other approved item still needs the file
            remove_item_output(item, out_dir)

    def _finish_decision(self, changed: list[ReviewItem], pos: int, advance: bool) -> None:
        refiltered = self.model.items_changed(changed)
        if refiltered:
            self._after_refilter(pos)  # the next page slides into place
        elif advance and self.auto_advance.isChecked():
            last = self.model.index_of(changed[-1].id)
            nxt = self.model.flat_position(last) + 1 if last.isValid() else pos + 1
            if nxt < self.model.visible_count():
                self._select(self.model.index_at(nxt))
        else:
            self._show(self._current)
        self._update_counts()
        self._update_buttons()

    def _report_errors(self, errors: list[str]) -> None:
        if errors:
            QMessageBox.warning(
                self,
                "Some pages were not updated",
                "These pages could not be written or removed:\n\n"
                + "\n".join(errors[:12])
                + ("\n…" if len(errors) > 12 else ""),
            )

    def release_cache(self) -> None:
        """Close the OCR cache (e.g. before it is deleted)."""
        if self._cache is not None:
            self._cache.close()
            self._cache = None

    def shutdown(self) -> None:
        self.loader.wait()
        if self._cache is not None:
            self._cache.close()
        if self.store is not None:
            self.store.close()


__all__ = ["NoRunFound", "ReviewPage"]
