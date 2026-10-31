"""Background work for the UI: extraction runs, merges and page previews.

Long work never runs on the UI thread. Each worker reports through Qt
signals, which are delivered on the UI thread.
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal
from PySide6.QtGui import QImage

from ..extract import Cancelled, render_preview
from ..merge import merge_outputs_to_pdf
from ..pipeline import Progress, RunOptions, RunResult, run_pipeline

log = logging.getLogger(__name__)


class RunWorker(QThread):
    progress = Signal(object)  # Progress
    succeeded = Signal(object)  # RunResult
    failed = Signal(str)

    def __init__(self, opts: RunOptions) -> None:
        super().__init__()
        self.opts = opts
        self._cancel = threading.Event()
        self._last_emit = 0.0

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelling(self) -> bool:
        return self._cancel.is_set()

    def _on_progress(self, p: Progress) -> None:
        now = time.monotonic()
        # ~15 updates per second are plenty and keep the UI thread free
        if now - self._last_emit >= 1 / 15 or p.files_done == p.files_total:
            self._last_emit = now
            self.progress.emit(p)

    def run(self) -> None:
        try:
            result: RunResult = run_pipeline(self.opts, self._on_progress, self._cancel)
            self.succeeded.emit(result)
        except Exception as exc:  # surface everything; a dead thread looks like a hang
            log.exception("Run failed")
            self.failed.emit(str(exc) or type(exc).__name__)


class MergeWorker(QThread):
    progress = Signal(int, int)
    succeeded = Signal(object)  # MergeResult
    failed = Signal(str)

    def __init__(self, output_dir: str) -> None:
        super().__init__()
        self.output_dir = output_dir
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            self.succeeded.emit(
                merge_outputs_to_pdf(
                    self.output_dir, lambda d, t: self.progress.emit(d, t), self._cancel
                )
            )
        except Cancelled:
            self.failed.emit("Merge cancelled.")
        except Exception as exc:
            log.exception("Merge failed")
            self.failed.emit(str(exc) or type(exc).__name__)


class _PreviewSignals(QObject):
    ready = Signal(int, object)  # request id, QImage
    failed = Signal(int, str)


class _PreviewTask(QRunnable):
    def __init__(self, request: int, path: str, page: int, signals: _PreviewSignals) -> None:
        super().__init__()
        self.request, self.path, self.page, self.signals = request, path, page, signals

    def run(self) -> None:
        try:
            img = render_preview(self.path, self.page)
            data = img.tobytes()
            # QImage only borrows the buffer; copy() gives it its own.
            qimg = QImage(
                data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888
            ).copy()
            self.signals.ready.emit(self.request, qimg)
        except Exception as exc:
            self.signals.failed.emit(self.request, str(exc))


class PreviewLoader(QObject):
    """Renders pages off the UI thread. Only the newest request is shown, so
    flicking through the list never queues up stale renders."""

    ready = Signal(object)  # QImage
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _PreviewSignals(self)
        self._signals.ready.connect(self._on_ready)
        self._signals.failed.connect(self._on_failed)
        self._request = 0

    def load(self, path: str, page: int) -> None:
        self._request += 1
        self._pool.clear()  # drop queued (not yet started) renders
        self._pool.start(_PreviewTask(self._request, path, page, self._signals))

    def _on_ready(self, request: int, img: QImage) -> None:
        if request == self._request:
            self.ready.emit(img)

    def _on_failed(self, request: int, message: str) -> None:
        if request == self._request:
            self.failed.emit(message)

    def wait(self) -> None:
        self._pool.waitForDone(5000)
