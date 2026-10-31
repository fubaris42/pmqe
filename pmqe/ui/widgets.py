"""Reusable building blocks styled by :mod:`pmqe.ui.theme`."""

from __future__ import annotations

import os
from collections.abc import Sequence

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontMetrics, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .theme import LG, MD, SM, XL, XS, color


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    if wrap:
        lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lab


def button(
    text: str,
    icon: str | None = None,
    variant: str | None = None,
    size: str | None = None,
    tooltip: str | None = None,
) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if variant:
        btn.setProperty("variant", variant)
    if size:
        btn.setProperty("size", size)
    if icon:
        role = {
            "primary": "on_primary",
            "success": "on_success",
            "danger": "danger",
            "link": "primary",
            "ghost": "muted",
        }.get(variant or "", "text")
        theme.current().bind_icon(btn, icon, role, 18 if size == "lg" else 16)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def icon_button(icon: str, tooltip: str, size: int = 18) -> QToolButton:
    """A ghost, icon-only button. The tooltip doubles as its accessible name."""
    btn = QToolButton()
    btn.setObjectName("IconButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setToolTip(tooltip)
    btn.setAccessibleName(tooltip.split(" (")[0])
    theme.current().bind_icon(btn, icon, "muted", size)
    return btn


def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    return line


def row(*items: QWidget | QLayout | int | None, spacing: int = SM, margins: int = 0) -> QHBoxLayout:
    """Horizontal layout. ``None`` adds a stretch, an int adds fixed spacing."""
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(margins, margins, margins, margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


def column(
    *items: QWidget | QLayout | int | None, spacing: int = SM, margins: int = 0
) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(margins, margins, margins, margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


class Card(QFrame):
    """A rounded surface with an optional title row and right-hand actions."""

    def __init__(
        self, title: str | None = None, subtitle: str | None = None, icon: str | None = None
    ) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(XL, XL - 4, XL, XL)
        self.body.setSpacing(MD)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(XS)
        if title:
            head = QHBoxLayout()
            head.setSpacing(SM + 2)
            if icon:
                ic = QLabel()
                theme.current().bind_icon(ic, icon, "primary", 18)
                head.addWidget(ic)
            self.title = label(title, "CardTitle")
            head.addWidget(self.title)
            head.addStretch(1)
            head.addLayout(self.actions)
            self.body.addLayout(head)
            if subtitle:
                self.body.addWidget(label(subtitle, "Helper", wrap=True))


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__()
        self.title = label(title, "PageTitle")
        self.subtitle = label(subtitle, "PageSubtitle", wrap=True)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(SM)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(column(self.title, self.subtitle, spacing=2), 1)
        lay.addLayout(self.actions)


class StatTile(QFrame):
    def __init__(self, caption: str, value: str = "—", tone: str | None = None) -> None:
        super().__init__()
        self.setObjectName("Well")
        self.value = label(value, "StatValue")
        if tone:
            self.value.setProperty("tone", tone)
        self.caption = label(caption.upper(), "Caption")
        lay = column(self.value, self.caption, spacing=2, margins=MD)
        self.setLayout(lay)
        self.setAccessibleName(caption)

    def set_value(self, value: str) -> None:
        self.value.setText(value)
        self.setAccessibleDescription(value)


class EmptyState(QWidget):
    """Friendly placeholder: icon, title, explanation and next actions."""

    def __init__(self, icon: str, title: str, body: str, actions: Sequence[QWidget] = ()) -> None:
        super().__init__()
        ic = QLabel()
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.current().bind_icon(ic, icon, "faint", 44)
        t = label(title, "EmptyTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b = label(body, "Muted", wrap=True)
        b.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b.setMaximumWidth(420)
        lay = column(None, ic, SM, t, b, spacing=SM)
        lay.setAlignment(b, Qt.AlignmentFlag.AlignHCenter)
        if actions:
            lay.addSpacing(MD)
            lay.addLayout(row(None, *actions, None))
        lay.addStretch(1)
        self.setLayout(lay)


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------


class Chip(QLabel):
    def __init__(self, text: str = "", tone: str = "neutral") -> None:
        super().__init__(text)
        self.setObjectName("Chip")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        repolish(self)


class KeyCap(QLabel):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.setObjectName("KeyCap")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName(f"Shortcut {key}")


class Switch(QCheckBox):
    """An on/off switch (a styled, fully keyboard-accessible QCheckBox)."""

    TRACK_W, TRACK_H = 36, 20

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def setChecked(self, checked: bool) -> None:
        super().setChecked(checked)
        self._anim.stop()
        self._pos = 1.0 if checked else 0.0

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(
            self.TRACK_W + SM + 4 + fm.horizontalAdvance(self.text()) + 4,
            max(self.TRACK_H + 4, fm.height() + 4),
        )

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _: QPaintEvent) -> None:
        p = theme.current().p
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = (self.height() - self.TRACK_H) / 2
        track = QRectF(1, y, self.TRACK_W, self.TRACK_H)
        on = color(p.primary)
        off = color(p.border_strong)
        mix = self._pos
        track_color = color(p.border) if not self.isEnabled() else (on if mix >= 0.5 else off)
        if self.hasFocus():
            painter.setPen(color(p.focus))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                track.adjusted(-2, -2, 2, 2), self.TRACK_H / 2 + 2, self.TRACK_H / 2 + 2
            )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        d = self.TRACK_H - 6
        x = track.left() + 3 + mix * (self.TRACK_W - d - 6)
        painter.setBrush(color(p.on_primary if mix >= 0.5 and self.isEnabled() else p.surface))
        painter.drawEllipse(QRectF(x, y + 3, d, d))
        painter.setPen(color(p.text if self.isEnabled() else p.faint))
        painter.setFont(self.font())
        text_rect = self.rect().adjusted(self.TRACK_W + SM + 4, 0, 0, 0)
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text()
        )


class Segmented(QFrame):
    """A row of mutually exclusive options (like a tab strip for filters)."""

    currentChanged = Signal(int)

    def __init__(self, labels: Sequence[str]) -> None:
        super().__init__()
        self.setObjectName("Segmented")
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.buttons: list[QPushButton] = []
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(b, i)
            lay.addWidget(b)
            self.buttons.append(b)
        self.buttons[0].setChecked(True)
        self.group.idClicked.connect(self.currentChanged.emit)

    def current(self) -> int:
        return self.group.checkedId()

    def set_current(self, index: int) -> None:
        self.buttons[index].setChecked(True)

    def set_label(self, index: int, text: str) -> None:
        self.buttons[index].setText(text)


class PathField(QWidget):
    """Folder picker: a labelled path box with a browse button. Accepts a
    folder dropped from the file manager and pasted/typed paths."""

    pathChanged = Signal(str)

    def __init__(
        self, title: str, icon: str, placeholder: str, helper: str, dialog_title: str
    ) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self._dialog_title = dialog_title
        ic = QLabel()
        theme.current().bind_icon(ic, icon, "muted", 16)
        heading = label(title)
        heading.setStyleSheet("font-weight: 600;")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.setAccessibleName(title)
        heading.setBuddy(self.edit)
        self.browse = button("Choose…", "folder-open")
        self.browse.clicked.connect(self._choose)
        self._helper_text = helper
        self.helper = label(helper, "Helper", wrap=True)
        lay = column(
            row(ic, heading, None, spacing=6), row(self.edit, self.browse), self.helper, spacing=6
        )
        self.setLayout(lay)
        self.edit.textChanged.connect(lambda t: self.pathChanged.emit(t.strip()))
        self.edit.textChanged.connect(lambda _: self.set_error(None))

    def set_error(self, message: str | None) -> None:
        """Show a problem right under the field (or restore the helper)."""
        self.helper.setText(message or self._helper_text)
        self.helper.setObjectName("Error" if message else "Helper")
        repolish(self.helper)
        self.set_invalid(bool(message))
        if message:
            self.edit.setFocus()

    def path(self) -> str:
        return os.path.expanduser(self.edit.text().strip().strip('"'))

    def set_path(self, path: str) -> None:
        self.edit.setText(path)

    def set_invalid(self, invalid: bool) -> None:
        self.edit.setProperty("invalid", invalid)
        repolish(self.edit)

    def _choose(self) -> None:
        start = self.path() if os.path.isdir(self.path()) else os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, self._dialog_title, start)
        if chosen:
            self.set_path(os.path.normpath(chosen))

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        urls = e.mimeData().urls()
        if urls and urls[0].isLocalFile() and os.path.isdir(urls[0].toLocalFile()):
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        self.set_path(os.path.normpath(e.mimeData().urls()[0].toLocalFile()))


class NavButton(QAbstractButton):
    """Sidebar navigation entry: icon, label and an optional count badge."""

    HEIGHT = 40

    def __init__(self, text: str, icon: str, checkable: bool = True) -> None:
        super().__init__()
        self.setText(text)
        self.setAccessibleName(text)
        self._icon = icon
        self._badge = ""
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(self.HEIGHT)
        self._hover = False

    def set_badge(self, text: str) -> None:
        self._badge = text
        self.setAccessibleDescription(f"{text} to review" if text else "")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(180, self.HEIGHT)

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _: QPaintEvent) -> None:
        th = theme.current()
        p = th.p
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isChecked() or self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color(p.primary_soft if self.isChecked() else p.hover))
            painter.drawRoundedRect(r, 8, 8)
        if self.hasFocus():
            painter.setPen(color(p.focus))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(r.adjusted(1, 1, -1, -1), 8, 8)
        role = "primary" if self.isChecked() else "muted"
        pm = th.pixmap(self._icon, role, 18)
        painter.drawPixmap(LG - 4, (self.height() - 18) // 2, pm)
        f = self.font()
        f.setWeight(f.Weight.DemiBold if self.isChecked() else f.Weight.Medium)
        painter.setFont(f)
        painter.setPen(color(p.primary_text if self.isChecked() else p.text))
        text_rect = self.rect().adjusted(LG + 24, 0, -LG, 0)
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text()
        )
        if self._badge:
            fm = QFontMetrics(f)
            w = max(22, fm.horizontalAdvance(self._badge) + 12)
            badge = QRectF(self.width() - w - 10, (self.height() - 20) / 2, w, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color(p.accent_soft))
            painter.drawRoundedRect(badge, 10, 10)
            painter.setPen(color(p.accent_text))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._badge)
