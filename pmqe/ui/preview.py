"""Page preview: a zoomable, rotatable page image and a highlighted text view."""

from __future__ import annotations

import html

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QTextBrowser,
    QWidget,
)

from ..text import highlight_spans, normalize_text
from . import theme
from .theme import MD, color
from .widgets import Chip, column, icon_button, row

MIN_SCALE, MAX_SCALE, STEP = 0.05, 8.0, 1.25
FIT_MARGIN = 24


class PageView(QGraphicsView):
    """Shows one page. Zoom is relative (it starts from fit-to-window, and
    never jumps), rotation persists across pages, Ctrl+wheel zooms at the
    cursor and dragging pans."""

    zoomChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("PageView")
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._page: QGraphicsPixmapItem | None = None
        self._shadow: QGraphicsRectItem | None = None
        self._rotation = 0
        self._scale = 1.0
        self._fit = True
        self._message = QLabel(self)
        self._message.setObjectName("Muted")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.hide()

    def show_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.resize(self.viewport().size())
        self._message.show()
        self._message.raise_()

    def clear(self) -> None:
        self._scene.clear()
        self._page = self._shadow = None

    def set_image(self, image: QImage) -> None:
        self._message.hide()
        self.clear()
        pm = QPixmap.fromImage(image)
        p = theme.current().p
        shadow = QGraphicsRectItem(QRectF(pm.rect()).translated(0, 3).adjusted(-2, -1, 2, 3))
        shadow.setBrush(color(p.shadow, 0.16))
        shadow.setPen(Qt.PenStyle.NoPen)
        self._shadow = shadow
        self._scene.addItem(shadow)
        self._page = self._scene.addPixmap(pm)
        self._page.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.setSceneRect(QRectF(pm.rect()).adjusted(-40, -40, 40, 40))
        self.fit()

    # -- transforms -----------------------------------------------------------------

    def _apply(self) -> None:
        self.resetTransform()
        self.scale(self._scale, self._scale)
        self.rotate(self._rotation)
        self.zoomChanged.emit("Fit" if self._fit else f"{round(self._scale * 100)}%")

    def fit(self) -> None:
        self._fit = True
        if self._page is None:
            return
        r = self._page.boundingRect()
        w, h = (r.height(), r.width()) if self._rotation % 180 else (r.width(), r.height())
        vw = max(1, self.viewport().width() - 2 * FIT_MARGIN)
        vh = max(1, self.viewport().height() - 2 * FIT_MARGIN)
        self._scale = max(MIN_SCALE, min(vw / w, vh / h))
        self._apply()
        self.centerOn(self._page)

    def zoom(self, factor: float) -> None:
        if self._page is None:
            return
        self._fit = False
        self._scale = max(MIN_SCALE, min(MAX_SCALE, self._scale * factor))
        center = self.mapToScene(self.viewport().rect().center())
        self._apply()
        self.centerOn(center)

    def zoom_in(self) -> None:
        self.zoom(STEP)

    def zoom_out(self) -> None:
        self.zoom(1 / STEP)

    def rotate_by(self, degrees: int) -> None:
        self._rotation = (self._rotation + degrees) % 360
        if self._fit:
            self.fit()
        else:
            self._apply()

    def current_scale(self) -> float:
        return self._scale

    def wheelEvent(self, e: QWheelEvent) -> None:
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(STEP if e.angleDelta().y() > 0 else 1 / STEP)
            e.accept()
        else:
            super().wheelEvent(e)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._message.resize(self.viewport().size())
        if self._fit:
            self.fit()


class TextView(QWidget):
    """The text a page was matched against, with the query's words marked."""

    def __init__(self) -> None:
        super().__init__()
        self.source = Chip("", "neutral")
        self.copy = icon_button("copy", "Copy text")
        self.copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.browser.toPlainText())
        )
        self.browser = QTextBrowser()
        self.browser.setObjectName("TextLayer")
        self.browser.setOpenLinks(False)
        lay = column(
            row(self.source, None, self.copy, margins=0), self.browser, spacing=MD, margins=MD
        )
        self.setLayout(lay)

    def set_text(self, text: str, source: str, tokens: list[str], whole_words: bool) -> None:
        self.source.setText(source)
        shown = normalize_text(text)
        if not shown.strip():
            self.browser.setHtml(
                f"<p style='color:{theme.current().p.muted}'>This page has no text to show.</p>"
            )
            return
        p = theme.current().p
        parts, last = [], 0
        for s, e in highlight_spans(shown, tokens, whole_words):
            parts.append(html.escape(shown[last:s]))
            parts.append(
                f"<span style='background:{p.mark_bg}; color:{p.mark_text}; font-weight:600'>"
                f"{html.escape(shown[s:e])}</span>"
            )
            last = e
        parts.append(html.escape(shown[last:]))
        self.browser.setHtml(
            f"<div style='white-space:pre-wrap; line-height:145%; color:{p.text}'>{''.join(parts)}</div>"
        )
