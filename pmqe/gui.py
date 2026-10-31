"""Desktop app entry point."""

from __future__ import annotations

import logging
import sys
import traceback

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from . import __version__
from .util import configure_logging

log = logging.getLogger("pmqe")


def _excepthook(exc_type, exc, tb) -> None:
    """Log unexpected errors and tell the user, instead of dying silently."""
    log.critical("Unhandled error", exc_info=(exc_type, exc, tb))
    app = QApplication.instance()
    if app is not None:
        QMessageBox.critical(
            None,
            "Something went wrong",
            f"{exc_type.__name__}: {exc}\n\nDetails were written to the log.",
        )
    else:
        traceback.print_exception(exc_type, exc, tb)


def create_app(argv: list[str] | None = None) -> QApplication:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("pmqe")
    app.setOrganizationName("pmqe")
    app.setApplicationDisplayName("pmqe")
    app.setApplicationVersion(__version__)
    return app


def main() -> None:
    log_file = configure_logging()
    log.info("pmqe %s starting (log: %s)", __version__, log_file)
    app = create_app()
    from .ui import theme
    from .ui.main_window import MainWindow

    settings = QSettings()
    theme.install(app, settings.value("theme", "system", str))
    app.setWindowIcon(theme.app_icon())
    sys.excepthook = _excepthook
    window = MainWindow(settings)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
