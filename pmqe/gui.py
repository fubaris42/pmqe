"""Qt GUI for pmqe: dashboard, background processing threads, review panel.

All heavy lifting lives in :mod:`pmqe.core`; worker processes import only
that module, never this one.
"""

from __future__ import annotations

import concurrent.futures
import io
import multiprocessing
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageOps
from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, QThread, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .core import (
    MERGED_PDF_NAME,
    OCR_CACHE_NAME,
    SMALL_FILE_BATCH,
    SMALL_FILE_BYTES,
    SUPPORTED_EXTENSIONS,
    _setup_logging,
    _unc,
    _worker_init,
    cache_load_page,
    log,
    merge_outputs_to_pdf,
    output_page_name,
    process_file,
    process_file_batch,
    query_tokens,
    safe_folder_name,
    write_reports,
)


class MergeWorker(QThread):
    done_signal = Signal(str, int, int)
    error_signal = Signal(str)

    def __init__(self, output_dir: str) -> None:
        super().__init__()
        self.output_dir = output_dir

    def run(self) -> None:
        try:
            path, pages, files = merge_outputs_to_pdf(self.output_dir)
            if files == 0:
                self.error_signal.emit(
                    "No extracted outputs found to merge. Run an extraction first."
                )
            else:
                self.done_signal.emit(path, pages, files)
        except Exception as exc:
            log.exception("Merge failed")
            self.error_signal.emit(str(exc))


# ---------------------------------------------------------------------------
# Background processing thread
# ---------------------------------------------------------------------------


class ProcessingWorker(QThread):
    progress_signal = Signal(int, int, str)
    finished_signal = Signal(dict, list)
    error_signal = Signal(str)

    def __init__(
        self,
        input_dir: str,
        queries: list[str],
        output_dir: str,
        max_workers: int | None = None,
        use_rapidocr: bool = False,
    ) -> None:
        super().__init__()
        self.input_dir = input_dir
        self.queries = queries
        self.output_dir = output_dir
        self.max_workers = max_workers or max(1, (os.cpu_count() or 4) - 1)
        self.use_rapidocr = use_rapidocr
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            # A crash here would otherwise die silently and leave the UI stuck
            log.exception("Processing run failed")
            self.error_signal.emit(f"Unexpected failure: {exc}")

    def _run(self) -> None:
        out_root = Path(self.output_dir).resolve()
        all_files: list[str] = []
        for p in Path(self.input_dir).rglob("*"):
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                # Don't re-ingest our own outputs when output_dir is inside input_dir
                if p.resolve().is_relative_to(out_root):
                    continue
            except (OSError, ValueError):
                pass
            all_files.append(str(p))
        if not all_files:
            self.error_signal.emit("No supported assets found inside target folder.")
            return

        cache_db = _unc(os.path.join(self.input_dir, OCR_CACHE_NAME))
        log_path = _unc(os.path.join(self.output_dir, "extractor.log"))
        os.makedirs(self.output_dir, exist_ok=True)
        _setup_logging(log_path)

        # Split into large files (one job each) and small-file batches
        large_files = []
        small_files = []
        for f in all_files:
            try:
                size = os.path.getsize(f)
            except OSError:
                size = SMALL_FILE_BYTES + 1
            if size <= SMALL_FILE_BYTES:
                small_files.append(f)
            else:
                large_files.append(f)

        small_batches = [
            small_files[i : i + SMALL_FILE_BATCH]
            for i in range(0, len(small_files), SMALL_FILE_BATCH)
        ]

        aggregate_counts: dict[str, int] = {q: 0 for q in self.queries}
        master_review_data: list[dict] = []

        # Total "units" for progress: each large file = 1, each batch = 1
        total = len(large_files) + len(small_batches)

        # forkserver on POSIX; spawn on Windows
        mp_method = "spawn" if sys.platform == "win32" else "forkserver"
        ctx = multiprocessing.get_context(mp_method)

        with concurrent.futures.ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=ctx,
            initializer=_worker_init,
            initargs=(self.queries, log_path, self.use_rapidocr),
        ) as executor:
            future_map: dict[concurrent.futures.Future, str] = {}

            # Submit large files individually
            for f in large_files:
                fut = executor.submit(process_file, f, self.output_dir, cache_db)
                future_map[fut] = f

            # Submit small-file batches
            for batch in small_batches:
                fut = executor.submit(
                    process_file_batch, batch, self.output_dir, cache_db
                )
                future_map[fut] = f"batch({len(batch)} files)"

            done = 0
            for future in concurrent.futures.as_completed(future_map):
                if self._cancel:
                    executor.shutdown(wait=False, cancel_futures=True)
                    self.error_signal.emit("Run cancelled.")
                    return
                label = future_map[future]
                done += 1
                # Backslash not allowed in f-string expressions before 3.12
                display = Path(label).name if os.sep in label or "/" in label else label
                try:
                    res = future.result()
                    for q, c in res["counts"].items():
                        aggregate_counts[q] += c
                    master_review_data.extend(res["review_data"])
                    self.progress_signal.emit(
                        done, total, f"({done}/{total}) {display}"
                    )
                except Exception as exc:
                    log.error("Worker failed on %s: %s", label, exc)
                    self.progress_signal.emit(done, total, f"[FAIL] {display}: {exc}")

        write_reports(self.output_dir, aggregate_counts, master_review_data)
        self.finished_signal.emit(aggregate_counts, master_review_data)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------


class ExtractorApp(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._worker: ProcessingWorker | None = None
        self._merge_worker: MergeWorker | None = None
        self.review_records: list[dict] = []
        self.current_review_item: dict | None = None
        self._live_counts: dict[str, int] = {}
        self.zoom_factor = 1.0
        self.current_rotation = 0
        self.view_text_mode = False
        self._cache_db: str = ""  # path to OCR cache for on-demand text fetch
        self._init_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        self.setWindowTitle(
            f"PDF & Image Multi-Query Extractor & Review HIL Pipeline v{__version__}"
        )
        self.setMinimumSize(1150, 700)

        window_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        window_layout.addWidget(self.tabs)

        self.tabs.addTab(self._build_dashboard_tab(), "Data Extraction Engine")
        self.tabs.addTab(self._build_review_tab(), "Manual Review Panel")

    def _build_dashboard_tab(self) -> QWidget:
        tab = QWidget()
        dash = QHBoxLayout(tab)
        dash.setSpacing(15)

        # -- left panel --------------------------------------------------
        left = QVBoxLayout()

        row_in = QHBoxLayout()
        self.input_edit = QLineEdit(placeholderText="Input folder...", readOnly=True)
        btn_in = QPushButton("Browse Input")
        btn_in.clicked.connect(self._browse_input)
        row_in.addWidget(self.input_edit)
        row_in.addWidget(btn_in)
        left.addLayout(row_in)

        row_out = QHBoxLayout()
        self.output_edit = QLineEdit(
            placeholderText="Output destination...", readOnly=True
        )
        btn_out = QPushButton("Browse Output")
        btn_out.clicked.connect(self._browse_output)
        row_out.addWidget(self.output_edit)
        row_out.addWidget(btn_out)
        left.addLayout(row_out)

        left.addWidget(QLabel("Queries:"))
        self.query_edit = QTextEdit(placeholderText="Queries (one per line)...")
        left.addWidget(self.query_edit, stretch=1)

        btn_import = QPushButton("Import Queries from .txt File")
        btn_import.clicked.connect(self._import_txt)
        left.addWidget(btn_import)

        self.chk_rapidocr = QCheckBox("Use RapidOCR engine (backup backend)")
        self.chk_rapidocr.setToolTip(
            "Force RapidOCR instead of the native OCR engine.\n"
            "RapidOCR is also used automatically when the native engine "
            "(Windows Media OCR / Apple Vision) is unavailable."
        )
        if sys.platform.startswith("linux"):
            self.chk_rapidocr.setChecked(True)
            self.chk_rapidocr.setEnabled(False)
            self.chk_rapidocr.setText("RapidOCR engine (default on Linux)")
        left.addWidget(self.chk_rapidocr)

        self.status_label = QLabel("Status: Ready")
        self.progress_bar = QProgressBar()
        left.addWidget(self.status_label)
        left.addWidget(self.progress_bar)

        row_act = QHBoxLayout()
        self.btn_run = QPushButton("Start Processing")
        self.btn_run.clicked.connect(self._start)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        row_act.addWidget(self.btn_run)
        row_act.addWidget(self.btn_cancel)
        left.addLayout(row_act)

        # -- right panel -------------------------------------------------
        right = QVBoxLayout()
        self.result_table = QTableWidget(0, 2)
        self.result_table.setHorizontalHeaderLabels(["Query", "Hits"])
        self.result_table.horizontalHeader().setStretchLastSection(True)
        right.addWidget(QLabel("Metrics Overview"))
        right.addWidget(self.result_table)

        self.btn_merge = QPushButton("Merge All to Single PDF (bookmarked by query)")
        self.btn_merge.setToolTip(
            "Combine every extracted output in the destination folder into one\n"
            f"PDF ({MERGED_PDF_NAME}) with a bookmark per query folder."
        )
        self.btn_merge.clicked.connect(self._merge_outputs)
        right.addWidget(self.btn_merge)

        dash.addLayout(left, stretch=4)
        dash.addLayout(right, stretch=5)
        return tab

    def _build_review_tab(self) -> QWidget:
        self.tab_review = QWidget()
        layout = QHBoxLayout(self.tab_review)
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # -- left: tree panel --------------------------------------------
        left_w = QWidget()
        left_l = QVBoxLayout(left_w)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Filter Query:"))
        self.search_bar = QLineEdit(
            placeholderText="Type query to filter tree panel..."
        )
        self.search_bar.textChanged.connect(self._rebuild_review_tree)
        search_row.addWidget(self.search_bar)
        left_l.addLayout(search_row)

        self.review_tree = QTreeView()
        self.tree_model = QStandardItemModel()
        self.tree_model.setHorizontalHeaderLabels(
            ["Query Elements / Page Index Hierarchy"]
        )
        self.review_tree.setModel(self.tree_model)
        self.review_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.review_tree.setStyleSheet("""
QTreeView                   { outline: 0; }
QTreeView::item             { border: 0px solid transparent; }
QTreeView::item:hover       { background: rgba(70,130,200,0.45); color: white; }
QTreeView::item:selected    { background: rgba(70,130,200,0.70); color: white; }
QTreeView::item:selected:!focus { background: rgba(70,130,200,0.50); color: white; }
QTreeView::item:focus       { background: rgba(70,130,200,0.70); color: white;
                               border: 0px solid transparent; }
        """)
        # Single activation path: currentChanged covers clicks and keyboard nav
        self.review_tree.selectionModel().currentChanged.connect(
            lambda cur, _prev: self._activate_index(cur)
        )

        tree_btn_row = QHBoxLayout()
        btn_expand = QPushButton("Expand All")
        btn_expand.clicked.connect(self.review_tree.expandAll)
        btn_collapse = QPushButton("Collapse All")
        btn_collapse.clicked.connect(self.review_tree.collapseAll)
        tree_btn_row.addWidget(btn_expand)
        tree_btn_row.addWidget(btn_collapse)
        left_l.addLayout(tree_btn_row)

        left_l.addWidget(QLabel("Extracted Targets / Candidates Tree View"))
        left_l.addWidget(self.review_tree)
        splitter.addWidget(left_w)

        # -- right: preview + controls -----------------------------------
        right_w = QWidget()
        right_l = QVBoxLayout(right_w)

        ctrl_row = QHBoxLayout()
        for label, fn in [
            ("Zoom In", lambda: self._adjust_zoom(1.2)),
            ("Zoom Out", lambda: self._adjust_zoom(0.8)),
            ("Rotate CCW", lambda: self._rotate(-90)),
            ("Rotate CW", lambda: self._rotate(90)),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            ctrl_row.addWidget(btn)

        self.btn_toggle_view = QPushButton("Switch to Text Layer View")
        self.btn_toggle_view.clicked.connect(self._toggle_view_mode)
        ctrl_row.addWidget(self.btn_toggle_view)
        right_l.addLayout(ctrl_row)

        self.gfx_scene = QGraphicsScene()
        self.gfx_view = QGraphicsView(self.gfx_scene)
        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.hide()
        right_l.addWidget(self.gfx_view, stretch=1)
        right_l.addWidget(self.text_preview, stretch=1)

        dec_row = QHBoxLayout()
        self.btn_approve = QPushButton("Approve Candidate Page")
        self.btn_approve.setStyleSheet(
            "background-color:#27ae60;color:white;font-weight:bold;padding:6px;"
        )
        self.btn_reject = QPushButton("Reject / Extirpate Page")
        self.btn_reject.setStyleSheet(
            "background-color:#c0392b;color:white;font-weight:bold;padding:6px;"
        )
        self.btn_approve.clicked.connect(self._approve_current_item)
        self.btn_reject.clicked.connect(self._reject_current_item)
        dec_row.addWidget(self.btn_approve)
        dec_row.addWidget(self.btn_reject)
        right_l.addLayout(dec_row)

        splitter.addWidget(right_w)
        splitter.setSizes([400, 750])
        return self.tab_review

    # ------------------------------------------------------------------
    # Directory / query helpers
    # ------------------------------------------------------------------

    def _browse_input(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Input")
        if d:
            self.input_edit.setText(d)

    def _browse_output(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Output")
        if d:
            self.output_edit.setText(d)

    def _import_txt(self) -> None:
        fp, _ = QFileDialog.getOpenFileName(
            self, "Open Query File", "", "Text Files (*.txt)"
        )
        if not fp:
            return
        try:
            with open(fp, encoding="utf-8", errors="ignore") as fh:
                lines = [
                    ln.strip()
                    for ln in fh
                    if ln.strip() and not ln.strip().startswith("[")
                ]
            self.query_edit.setPlainText("\n".join(lines))
            QMessageBox.information(self, "Imported", f"Loaded {len(lines)} queries.")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to read file:\n{exc}")

    # ------------------------------------------------------------------
    # Processing control
    # ------------------------------------------------------------------

    def _start(self) -> None:
        input_dir = self.input_edit.text().strip()
        output_dir = self.output_edit.text().strip()
        queries = [
            q.strip() for q in self.query_edit.toPlainText().splitlines() if q.strip()
        ]

        if not input_dir or not output_dir or not queries:
            QMessageBox.warning(
                self,
                "Configuration Check",
                "Ensure directories and queries are defined completely.",
            )
            return
        if not os.path.isdir(input_dir):
            QMessageBox.warning(
                self,
                "Configuration Check",
                f"Input folder does not exist:\n{input_dir}",
            )
            return
        if not any(query_tokens(q) for q in queries):
            QMessageBox.warning(
                self,
                "Configuration Check",
                "No usable queries — every word must be at least 2 characters.",
            )
            return

        # Store cache DB path for on-demand text fetches in the review panel
        self._cache_db = _unc(os.path.join(input_dir, OCR_CACHE_NAME))

        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setRange(0, 0)

        self._worker = ProcessingWorker(
            input_dir,
            queries,
            output_dir,
            use_rapidocr=self.chk_rapidocr.isChecked(),
        )
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.error_signal.connect(self._on_error)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker:
            self._worker.cancel()

    # ------------------------------------------------------------------
    # Merge all outputs into one bookmarked PDF
    # ------------------------------------------------------------------

    def _merge_outputs(self) -> None:
        output_dir = self.output_edit.text().strip()
        if not output_dir or not os.path.isdir(output_dir):
            QMessageBox.warning(
                self, "Merge", "Select an existing output destination first."
            )
            return
        if self._merge_worker and self._merge_worker.isRunning():
            return
        self.btn_merge.setEnabled(False)
        self.status_label.setText("Merging outputs into a single PDF...")
        self._merge_worker = MergeWorker(output_dir)
        self._merge_worker.done_signal.connect(self._on_merge_done)
        self._merge_worker.error_signal.connect(self._on_merge_error)
        self._merge_worker.start()

    def _on_merge_done(self, path: str, pages: int, files: int) -> None:
        self.btn_merge.setEnabled(True)
        self.status_label.setText(f"Merged {files} files ({pages} pages).")
        QMessageBox.information(
            self,
            "Merge Complete",
            f"Merged {files} files ({pages} pages) into:\n{path}\n\n"
            "Each query folder is a top-level bookmark.",
        )

    def _on_merge_error(self, msg: str) -> None:
        self.btn_merge.setEnabled(True)
        self.status_label.setText("Merge failed.")
        QMessageBox.warning(self, "Merge Failed", msg)

    def _on_progress(self, current: int, total: int, msg: str) -> None:
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)
        self.status_label.setText(msg)

    def _on_finished(self, counts: dict[str, int], review_data: list[dict]) -> None:
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText(
            "Batch run finished. Reports generated in destination output path."
        )

        self.review_records = review_data
        self._live_counts = dict(counts)

        self._refresh_metrics_table()
        self._rebuild_review_tree()
        self.tabs.setCurrentWidget(self.tab_review)

    def _on_error(self, msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText(f"Error: {msg}")
        QMessageBox.warning(self, "Processing Error", msg)

    # ------------------------------------------------------------------
    # Metrics table  (majority-value highlighting)
    # ------------------------------------------------------------------

    def _refresh_metrics_table(self) -> None:
        counts = self._live_counts
        self.result_table.setRowCount(len(counts))

        majority_value: int = 0
        if counts:
            frequency_map = Counter(counts.values())
            majority_value = frequency_map.most_common(1)[0][0]

        for idx, (q, c) in enumerate(sorted(counts.items())):
            self.result_table.setItem(idx, 0, QTableWidgetItem(q))

            count_item = QTableWidgetItem(str(c))
            if c != majority_value:
                if c < majority_value:
                    count_item.setBackground(QBrush(QColor("#f1c40f")))
                else:
                    count_item.setBackground(QBrush(QColor("#e67e22")))
                count_item.setForeground(QBrush(QColor("#000000")))

            self.result_table.setItem(idx, 1, count_item)

    # ------------------------------------------------------------------
    # Persist metrics (CSV + SQLite) after every approve/reject action
    # ------------------------------------------------------------------

    def _persist_metrics(self) -> None:
        output_dir = self.output_edit.text().strip()
        if not output_dir:
            return
        write_reports(output_dir, self._live_counts, self.review_records)

    # ------------------------------------------------------------------
    # Review tree
    # ------------------------------------------------------------------

    def _rebuild_review_tree(self) -> None:
        selected_id = (
            self.current_review_item["id"] if self.current_review_item else None
        )

        self.tree_model.clear()
        self.tree_model.setHorizontalHeaderLabels(["Query Target Layout Mapping Tree"])

        search_filter = self.search_bar.text().strip().lower()
        query_buckets: dict[str, list[dict]] = defaultdict(list)
        for item in self.review_records:
            if search_filter and search_filter not in item["query"].lower():
                continue
            query_buckets[item["query"]].append(item)

        target_node: QStandardItem | None = None

        for query, items in query_buckets.items():
            query_node = QStandardItem(query)
            query_node.setSelectable(False)
            self.tree_model.appendRow(query_node)

            for item in sorted(items, key=lambda x: len(x["missing_words"])):
                page_node = QStandardItem()
                p_name = Path(item["file_path"]).name

                if item["pre_approved"]:
                    page_node.setText(
                        f"Page {item['page_idx'] + 1} -> {p_name} [APPROVED]"
                    )
                    page_node.setForeground(QBrush(QColor("#27ae60")))
                else:
                    missing = ", ".join(item["missing_words"])
                    page_node.setText(
                        f"Page {item['page_idx'] + 1} -> {p_name} [CANDIDATE] (Missing: {missing})"
                    )
                    page_node.setToolTip(f"Missing terms: {missing}")
                    page_node.setForeground(QBrush(QColor("#d35400")))

                page_node.setData(item, Qt.UserRole)
                query_node.appendRow(page_node)

                if selected_id and item["id"] == selected_id:
                    target_node = page_node

        self.review_tree.expandAll()

        if target_node is not None:
            idx = self.tree_model.indexFromItem(target_node)
            self.review_tree.selectionModel().setCurrentIndex(
                idx, QItemSelectionModel.SelectionFlag.ClearAndSelect
            )

    def _activate_index(self, index: QModelIndex) -> None:
        node = self.tree_model.itemFromIndex(index)
        if not node:
            return
        item_data = node.data(Qt.UserRole)
        if not item_data:
            return
        self.current_review_item = item_data
        self._render_selected_element()

    # ------------------------------------------------------------------
    # Page preview rendering
    # ------------------------------------------------------------------

    def _render_selected_element(self) -> None:
        if not self.current_review_item:
            return
        item = self.current_review_item

        if self.view_text_mode:
            self.gfx_view.hide()
            self.text_preview.show()
            self._render_text_view(item)
        else:
            self.text_preview.hide()
            self.gfx_view.show()
            self._render_graphics_view(item)

    def _render_text_view(self, item: dict) -> None:
        # Fetch text from the OCR cache on demand. Derive the cache DB from
        # the source file's directory so this works even when _start() hasn't
        # been called this session (e.g. app restart).
        raw_text = ""
        file_dir = str(Path(item["file_path"]).parent)
        cache_db = _unc(os.path.join(file_dir, OCR_CACHE_NAME))
        # Fall back to the stored path if the per-file-dir DB doesn't exist yet
        if not os.path.exists(cache_db) and self._cache_db:
            cache_db = self._cache_db
        fetched = cache_load_page(cache_db, item["file_hash"], item["page_idx"])
        if fetched is not None:
            raw_text = fetched

        if not raw_text.strip():
            self.text_preview.setPlainText("[No text layer found]")
            return

        tokens = query_tokens(item["query"])
        html_text = (
            raw_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

        if tokens:
            # Single combined pass so one token can't match inside the <span>
            # markup inserted for another
            pattern = re.compile(
                "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)),
                re.IGNORECASE,
            )
            html_text = pattern.sub(
                lambda m: (
                    f'<span style="background-color:#ffff00;color:#000000;font-weight:bold;">'
                    f"{m.group(0)}</span>"
                ),
                html_text,
            )

        html_text = html_text.replace("\n", "<br>")
        self.text_preview.setHtml(
            f"<div style='font-family:monospace;font-size:12px;'>{html_text}</div>"
        )

    def _render_graphics_view(self, item: dict) -> None:
        self.gfx_scene.clear()
        f_path = item["file_path"]
        is_pdf = Path(f_path).suffix.lower() == ".pdf"
        q_pixmap: QPixmap | None = None

        try:
            if is_pdf:
                doc = fitz.open(_unc(f_path))
                page = doc[item["page_idx"]]
                pix = page.get_pixmap(dpi=150)
                # Keep the sample buffer alive in a local: QImage wraps the
                # pointer without owning it, and fromImage copies from it
                samples = pix.samples
                q_img = QImage(
                    samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888
                )
                q_pixmap = QPixmap.fromImage(q_img)
                doc.close()
            else:
                with Image.open(f_path) as img:
                    img_corr = ImageOps.exif_transpose(img).convert("RGB")
                    buf = io.BytesIO()
                    img_corr.save(buf, format="PNG")
                    q_pixmap = QPixmap()
                    q_pixmap.loadFromData(buf.getvalue(), "PNG")
        except Exception as exc:
            log.error("Failed to render page preview: %s", exc)
            return

        if q_pixmap and not q_pixmap.isNull():
            pix_item = self.gfx_scene.addPixmap(q_pixmap)
            self.gfx_scene.setSceneRect(pix_item.boundingRect())
            self._apply_transformations()
            self.gfx_view.fitInView(pix_item, Qt.KeepAspectRatio)

    def _apply_transformations(self) -> None:
        t = QTransform()
        t.scale(self.zoom_factor, self.zoom_factor)
        t.rotate(self.current_rotation)
        self.gfx_view.setTransform(t)

    def _adjust_zoom(self, factor: float) -> None:
        self.zoom_factor *= factor
        self._apply_transformations()

    def _rotate(self, degrees: int) -> None:
        self.current_rotation = (self.current_rotation + degrees) % 360
        self._apply_transformations()

    def _toggle_view_mode(self) -> None:
        self.view_text_mode = not self.view_text_mode
        self.btn_toggle_view.setText(
            "Switch to Graphics Image View"
            if self.view_text_mode
            else "Switch to Text Layer View"
        )
        self._render_selected_element()

    # ------------------------------------------------------------------
    # Approve / Reject actions
    # ------------------------------------------------------------------

    def _approve_current_item(self) -> None:
        if not self.current_review_item:
            return
        item = self.current_review_item
        if item["pre_approved"]:
            return

        try:
            folder = Path(self.output_edit.text().strip()) / safe_folder_name(
                item["query"]
            )
            folder.mkdir(parents=True, exist_ok=True)
            p_obj = Path(item["file_path"])

            if p_obj.suffix.lower() == ".pdf":
                doc = fitz.open(_unc(item["file_path"]))
                out_doc = fitz.open()
                out_doc.insert_pdf(
                    doc, from_page=item["page_idx"], to_page=item["page_idx"]
                )
                # Same canonical per-page name the batch run uses, so
                # approve/reject cycles always target one file
                out_doc.save(
                    _unc(
                        str(
                            folder
                            / output_page_name(
                                p_obj.stem, item["page_idx"], item["file_hash"]
                            )
                        )
                    ),
                    garbage=4,
                    deflate=True,
                )
                out_doc.close()
                doc.close()
            else:
                shutil.copy2(
                    item["file_path"],
                    folder / f"{p_obj.stem}_{item['file_hash']}{p_obj.suffix.lower()}",
                )

            item["pre_approved"] = True
            self._live_counts[item["query"]] = (
                self._live_counts.get(item["query"], 0) + 1
            )
            self._refresh_metrics_table()
            self._persist_metrics()
            self._update_tree_node_style(item)

        except Exception as exc:
            QMessageBox.critical(self, "I/O Error", f"Failed manual export: {exc}")

    def _reject_current_item(self) -> None:
        if not self.current_review_item:
            return
        item = self.current_review_item
        if not item["pre_approved"]:
            return

        try:
            folder = Path(self.output_edit.text().strip()) / safe_folder_name(
                item["query"]
            )
            p_obj = Path(item["file_path"])
            ext = p_obj.suffix.lower()

            if ext == ".pdf":
                names = [
                    output_page_name(p_obj.stem, item["page_idx"], item["file_hash"]),
                    # Legacy names from runs before individual-page output
                    f"{p_obj.stem}_{item['file_hash']}.pdf",
                    f"{p_obj.stem}_approved_{item['file_hash']}.pdf",
                ]
            else:
                names = [
                    f"{p_obj.stem}_{item['file_hash']}{ext}",
                    f"{p_obj.stem}_approved_{item['file_hash']}{ext}",
                ]

            for name in names:
                candidate = folder / name
                if candidate.exists():
                    candidate.unlink()

            item["pre_approved"] = False
            self._live_counts[item["query"]] = max(
                0, self._live_counts.get(item["query"], 1) - 1
            )
            self._refresh_metrics_table()
            self._persist_metrics()
            self._update_tree_node_style(item)

        except Exception as exc:
            QMessageBox.critical(self, "I/O Error", f"Failed cleanup: {exc}")

    def _update_tree_node_style(self, item: dict) -> None:
        current_index = self.review_tree.selectionModel().currentIndex()
        if not current_index.isValid():
            return
        node = self.tree_model.itemFromIndex(current_index)
        if not node:
            return
        p_name = Path(item["file_path"]).name
        if item["pre_approved"]:
            node.setText(f"Page {item['page_idx'] + 1} -> {p_name} [APPROVED]")
            node.setForeground(QBrush(QColor("#27ae60")))
        else:
            missing = ", ".join(item["missing_words"])
            node.setText(
                f"Page {item['page_idx'] + 1} -> {p_name} [CANDIDATE] (Missing: {missing})"
            )
            node.setForeground(QBrush(QColor("#d35400")))
        node.setData(item, Qt.UserRole)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    multiprocessing.freeze_support()
    _setup_logging()
    app = QApplication(sys.argv)
    window = ExtractorApp()
    window.showNormal()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
