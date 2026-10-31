"""``pmqe --self-test [REPORT.json]``: an end-to-end check of an installed or
frozen build.

Runs a real extraction through the process pool on two generated pages (one
with a text layer, one scanned, so the OCR engine must work), merges the
result, and verifies that worker processes never load Qt's GUI modules even
though this process has. (In frozen builds PyInstaller's own PySide6 runtime
hook imports QtCore into every process; QtGui/QtWidgets are what matter.) Exits 0 when everything passes; writes a JSON report when a
path is given (frozen windowed builds have no console).
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import multiprocessing
import sys
import tempfile
import time
from pathlib import Path

import fitz
from PIL import Image


def _qt_gui_loaded() -> bool:
    return any(m in sys.modules for m in ("PySide6.QtGui", "PySide6.QtWidgets"))


def _scan(path: Path, text: str) -> None:
    doc = fitz.open()
    doc.new_page().insert_textbox(fitz.Rect(60, 60, 540, 400), text, fontsize=18)
    pix = doc[0].get_pixmap(dpi=200, colorspace=fitz.csGRAY)
    Image.frombytes("L", (pix.width, pix.height), pix.samples).save(path)
    doc.close()


def _gui_renders(settings_path: Path) -> bool:
    """Build the real main window and render it once: catches missing Qt
    plugins (e.g. the SVG image format the stylesheet uses) in a build."""
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QImageReader

    from .gui import create_app
    from .ui import theme
    from .ui.main_window import MainWindow

    app = create_app([sys.argv[0]])
    theme.install(app, "light")
    window = MainWindow(QSettings(str(settings_path), QSettings.Format.IniFormat))
    image = window.grab()
    formats = {bytes(f).decode() for f in QImageReader.supportedImageFormats()}
    window.close()
    return not image.isNull() and image.width() > 0 and "svg" in formats


def run(report_path: str | None = None) -> int:
    from . import __version__, ocr
    from .merge import merge_outputs_to_pdf
    from .pipeline import RunOptions, run_pipeline
    from .text import prepare_queries
    from .util import configure_logging

    configure_logging()

    started = time.perf_counter()
    work = Path(tempfile.mkdtemp(prefix="pmqe-selftest-"))
    src = work / "in"
    src.mkdir()
    doc = fitz.open()
    doc.new_page().insert_textbox(
        fitz.Rect(60, 60, 540, 780), "Self test invoice total due", fontsize=12
    )
    doc.save(str(src / "native.pdf"))
    doc.close()
    _scan(src / "scan.png", "Scanned keyword appears here")

    checks: dict[str, bool] = {}
    details: dict[str, object] = {"version": __version__, "ocr_engine": ocr.native_engine()}
    try:
        res = run_pipeline(
            RunOptions(
                str(src),
                str(work / "out"),
                prepare_queries(["invoice total", "scanned keyword"]).queries,
                workers=2,
                cache_path=str(work / "cache.db"),
            )
        )
        checks["text_layer_match"] = res.counts.get("invoice total") == 1
        checks["ocr_match"] = res.counts.get("scanned keyword") == 1
        checks["no_failures"] = not res.failures and not res.ocr_failed
        details["counts"] = res.counts
        details["failures"] = res.failures
        checks["merge"] = merge_outputs_to_pdf(str(work / "out")).files == 2
        checks["gui_renders"] = _gui_renders(work / "settings.ini")
        ctx = multiprocessing.get_context("spawn" if sys.platform == "win32" else "forkserver")
        with cf.ProcessPoolExecutor(1, mp_context=ctx) as pool:
            checks["workers_without_qt_gui"] = not pool.submit(_qt_gui_loaded).result(timeout=120)
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
        checks.setdefault("pipeline", False)

    ok = bool(checks) and all(checks.values())
    report = {
        "ok": ok,
        "checks": checks,
        **details,
        "seconds": round(time.perf_counter() - started, 2),
    }
    text = json.dumps(report, indent=2, default=str)
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text)
    return 0 if ok else 1
