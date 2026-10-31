"""Design tokens, light/dark palettes, the application stylesheet and icons.

Every color in the UI comes from a :class:`Palette`; nothing else hard-codes
hex values. Text colors meet WCAG AA (4.5:1) on the surfaces they sit on.
The theme follows the operating system's light/dark setting unless the user
picks one, and widgets that show icons re-render them when it changes.
"""

from __future__ import annotations

import functools
import sys
import weakref
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

from ..util import user_cache_dir
from .lucide import PATHS

# Spacing (4px rhythm) and radii
XS, SM, MD, LG, XL, XXL = 4, 8, 12, 16, 24, 32
RADIUS_SM, RADIUS_MD, RADIUS_LG = 6, 8, 12


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str  # window background
    surface: str  # cards
    surface_alt: str  # inputs, wells
    hover: str
    border: str
    border_strong: str
    text: str
    muted: str
    faint: str
    primary: str  # filled primary buttons
    primary_hover: str
    primary_pressed: str
    on_primary: str
    primary_soft: str  # tinted backgrounds (selection, active nav)
    primary_text: str  # links and accents on surfaces
    success: str
    success_hover: str
    on_success: str
    success_soft: str
    success_text: str
    accent: str  # "needs review" (amber)
    accent_soft: str
    accent_text: str
    danger: str
    danger_soft: str
    danger_text: str
    focus: str
    canvas: str  # behind previewed pages
    mark_bg: str  # highlighted matches in text
    mark_text: str
    shadow: str


LIGHT = Palette(
    name="light",
    bg="#F3F6F6",
    surface="#FFFFFF",
    surface_alt="#F8FAFA",
    hover="#EDF2F2",
    border="#DCE4E3",
    border_strong="#C3CFCE",
    text="#0F1D1C",
    muted="#4A5B5A",
    faint="#5F706F",
    primary="#0F766E",
    primary_hover="#115E59",
    primary_pressed="#134E4A",
    on_primary="#FFFFFF",
    primary_soft="#E3F3F1",
    primary_text="#0F766E",
    success="#15803D",
    success_hover="#166534",
    on_success="#FFFFFF",
    success_soft="#E6F5EB",
    success_text="#166534",
    accent="#B45309",
    accent_soft="#FDF1DE",
    accent_text="#92400E",
    danger="#B91C1C",
    danger_soft="#FCEBEB",
    danger_text="#991B1B",
    focus="#14B8A6",
    canvas="#E6ECEC",
    mark_bg="#FDE68A",
    mark_text="#3B2A00",
    shadow="#1A2B2A",
)

DARK = Palette(
    name="dark",
    bg="#0A1111",
    surface="#111A1A",
    surface_alt="#152020",
    hover="#1A2828",
    border="#223131",
    border_strong="#2F4342",
    text="#E4EDEC",
    muted="#9CB0AE",
    faint="#869997",
    primary="#2DD4BF",
    primary_hover="#5EEAD4",
    primary_pressed="#14B8A6",
    on_primary="#042F2C",
    primary_soft="#123532",
    primary_text="#5EEAD4",
    success="#4ADE80",
    success_hover="#86EFAC",
    on_success="#052E16",
    success_soft="#11291B",
    success_text="#86EFAC",
    accent="#FBBF24",
    accent_soft="#2C220B",
    accent_text="#FCD34D",
    danger="#F87171",
    danger_soft="#2D1515",
    danger_text="#FCA5A5",
    focus="#5EEAD4",
    canvas="#0C1515",
    mark_bg="#854D0E",
    mark_text="#FEF3C7",
    shadow="#000000",
)


def color(hex_value: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_value)
    c.setAlphaF(alpha)
    return c


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------


def ui_font() -> QFont:
    font = QFont(QApplication.font())
    if sys.platform == "win32":
        font.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
        font.setPointSizeF(10)
    elif sys.platform.startswith("linux"):
        font.setFamilies(["Inter", "Cantarell", "Noto Sans", font.family()])
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def scaled_font(base: QFont, factor: float, weight: QFont.Weight | None = None) -> QFont:
    f = QFont(base)
    f.setPointSizeF(base.pointSizeF() * factor)
    if weight is not None:
        f.setWeight(weight)
    return f


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def _svg(name: str, stroke_color: str, stroke_width: float = 2.0) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
        f'fill="none" stroke="{stroke_color}" stroke-width="{stroke_width}" '
        f'stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>'
    ).encode()


def render_icon(name: str, stroke_color: str, size: int, stroke_width: float = 2.0) -> QPixmap:
    """An icon as a crisp pixmap for the current screen. Cached: list rows
    and nav buttons ask for the same few icons on every repaint."""
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    return _render_icon(name, stroke_color, size, stroke_width, dpr)


@functools.lru_cache(maxsize=512)
def _render_icon(
    name: str, stroke_color: str, size: int, stroke_width: float, dpr: float
) -> QPixmap:
    px = max(1, round(size * dpr))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(_svg(name, stroke_color, stroke_width))).render(
        painter, QRectF(0, 0, px, px)
    )
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------


def _asset(name: str, stroke_color: str) -> str:
    """An icon as a file, for the few QSS properties that need an image URL."""
    folder = user_cache_dir() / "ui"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{stroke_color.lstrip('#')}.svg"
    if not path.exists():
        path.write_bytes(_svg(name, stroke_color, 2.25))
    return path.as_posix()


def stylesheet(p: Palette, base_pt: float) -> str:
    chevron = _asset("chevron-down", p.muted)
    up, down = _asset("chevron-up", p.muted), _asset("chevron-down", p.muted)
    title_pt, h2_pt, small_pt = base_pt * 1.75, base_pt * 1.15, base_pt * 0.9
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {p.text}; }}
QMainWindow, #AppRoot, QStackedWidget#Pages > QWidget, QScrollArea#PageScroll,
QScrollArea#PageScroll > QWidget > QWidget {{ background: {p.bg}; }}
QToolTip {{ background: {p.text}; color: {p.surface}; border: none; border-radius: {RADIUS_SM}px;
           padding: 5px 8px; }}

/* Sidebar ------------------------------------------------------------- */
#Sidebar {{ background: {p.surface}; border-right: 1px solid {p.border}; }}
#BrandName {{ font-size: {h2_pt:.1f}pt; font-weight: 700; }}
#BrandTag, #SidebarFooter QLabel {{ color: {p.muted}; font-size: {small_pt:.1f}pt; }}
#SidebarSection {{ color: {p.faint}; font-size: {small_pt:.1f}pt; font-weight: 600; }}

/* Page chrome ----------------------------------------------------------- */
#PageTitle {{ font-size: {title_pt:.1f}pt; font-weight: 700; }}
#PageSubtitle, #Muted, #Helper {{ color: {p.muted}; }}
#Helper, #Caption {{ font-size: {small_pt:.1f}pt; }}
#Caption {{ color: {p.faint}; font-weight: 600; }}
#CardTitle {{ font-size: {h2_pt:.1f}pt; font-weight: 650; }}
#Card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {RADIUS_LG}px; }}
#Well {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: {RADIUS_MD}px; }}
#Divider {{ background: {p.border}; max-height: 1px; min-height: 1px; border: none; }}
#StatValue {{ font-size: {title_pt * 0.82:.1f}pt; font-weight: 700; }}
#StatValue[tone="accent"] {{ color: {p.accent_text}; }}
#StatValue[tone="danger"] {{ color: {p.danger_text}; }}
#StatValue[tone="success"] {{ color: {p.success_text}; }}
#BigNumber {{ font-size: {title_pt * 1.25:.1f}pt; font-weight: 700; color: {p.primary_text}; }}
#Warn {{ color: {p.accent_text}; }}
#Ok {{ color: {p.success_text}; }}
#Error {{ color: {p.danger_text}; }}
#EmptyTitle {{ font-size: {h2_pt * 1.1:.1f}pt; font-weight: 650; }}

/* Inputs ------------------------------------------------------------------ */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {p.surface_alt}; border: 1px solid {p.border_strong};
    border-radius: {RADIUS_MD}px; padding: 7px 10px; selection-background-color: {p.primary_soft};
    selection-color: {p.text};
}}
QPlainTextEdit, QTextEdit {{ padding: 8px 10px; }}
QLineEdit:hover, QPlainTextEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {p.muted}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 2px solid {p.focus}; padding: 6px 9px;
}}
QPlainTextEdit:focus, QTextEdit:focus {{ padding: 7px 9px; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {p.faint}; background: {p.hover}; }}
QLineEdit[invalid="true"] {{ border-color: {p.danger}; }}
QTextBrowser#TextLayer {{ background: {p.surface}; border: none; padding: 16px 20px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{ background: {p.surface}; border: 1px solid {p.border};
    border-radius: {RADIUS_MD}px; padding: 4px; selection-background-color: {p.primary_soft};
    selection-color: {p.text}; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 20px; }}
QSpinBox::up-arrow {{ image: url("{up}"); width: 12px; height: 12px; }}
QSpinBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}

/* Buttons ------------------------------------------------------------------ */
QPushButton {{
    background: {p.surface}; color: {p.text}; border: 1px solid {p.border_strong};
    border-radius: {RADIUS_MD}px; padding: 7px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: {p.hover}; }}
QPushButton:pressed {{ background: {p.border}; }}
QPushButton:focus {{ border: 2px solid {p.focus}; padding: 6px 13px; }}
QPushButton:disabled {{ color: {p.faint}; background: {p.surface_alt}; border-color: {p.border}; }}
QPushButton[variant="primary"] {{ background: {p.primary}; color: {p.on_primary}; border: 1px solid {p.primary}; }}
QPushButton[variant="primary"]:hover {{ background: {p.primary_hover}; border-color: {p.primary_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {p.primary_pressed}; }}
QPushButton[variant="primary"]:focus {{ border: 2px solid {p.focus}; }}
QPushButton[variant="primary"]:disabled {{ background: {p.border}; color: {p.faint}; border-color: {p.border}; }}
QPushButton[variant="success"] {{ background: {p.success}; color: {p.on_success}; border: 1px solid {p.success}; }}
QPushButton[variant="success"]:hover {{ background: {p.success_hover}; border-color: {p.success_hover}; }}
QPushButton[variant="success"]:disabled {{ background: {p.border}; color: {p.faint}; border-color: {p.border}; }}
QPushButton[variant="danger"] {{ background: {p.surface}; color: {p.danger_text}; border: 1px solid {p.danger}; }}
QPushButton[variant="danger"]:hover {{ background: {p.danger_soft}; }}
QPushButton[variant="danger"]:disabled {{ color: {p.faint}; border-color: {p.border}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; color: {p.muted}; }}
QPushButton[variant="ghost"]:hover {{ background: {p.hover}; color: {p.text}; }}
QPushButton[variant="ghost"]:focus {{ border: 2px solid {p.focus}; }}
QPushButton[variant="link"] {{ background: transparent; border: none; color: {p.primary_text};
    padding: 0; font-weight: 600; text-align: left; }}
QPushButton[variant="link"]:hover {{ text-decoration: underline; }}
QPushButton[size="lg"] {{ padding: 11px 18px; font-size: {base_pt * 1.05:.1f}pt; }}
QPushButton[size="lg"]:focus {{ padding: 10px 17px; }}
QToolButton#IconButton {{ background: transparent; border: 1px solid transparent; border-radius: {RADIUS_SM}px;
    padding: 5px; }}
QToolButton#IconButton:hover {{ background: {p.hover}; }}
QToolButton#IconButton:pressed {{ background: {p.border}; }}
QToolButton#IconButton:focus {{ border: 2px solid {p.focus}; padding: 4px; }}

/* Segmented control -------------------------------------------------------- */
#Segmented {{ background: {p.hover}; border-radius: {RADIUS_MD}px; }}
#Segmented QPushButton {{ background: transparent; border: 1px solid transparent; color: {p.muted};
    padding: 5px 12px; border-radius: {RADIUS_SM}px; font-weight: 600; }}
#Segmented QPushButton:hover {{ color: {p.text}; }}
#Segmented QPushButton:checked {{ background: {p.surface}; color: {p.text}; border-color: {p.border}; }}
#Segmented QPushButton:focus {{ border: 2px solid {p.focus}; padding: 4px 11px; }}

/* Chips, key caps --------------------------------------------------------------- */
QLabel#Chip {{ border-radius: 10px; padding: 2px 9px; font-size: {small_pt:.1f}pt; font-weight: 600;
    background: {p.hover}; color: {p.muted}; }}
QLabel#Chip[tone="primary"] {{ background: {p.primary_soft}; color: {p.primary_text}; }}
QLabel#Chip[tone="success"] {{ background: {p.success_soft}; color: {p.success_text}; }}
QLabel#Chip[tone="accent"] {{ background: {p.accent_soft}; color: {p.accent_text}; }}
QLabel#Chip[tone="danger"] {{ background: {p.danger_soft}; color: {p.danger_text}; }}
QLabel#KeyCap {{ border: 1px solid {p.border_strong}; border-bottom-width: 2px; border-radius: 4px;
    padding: 0px 5px; color: {p.muted}; font-size: {small_pt * 0.95:.1f}pt; font-weight: 600;
    background: {p.surface}; }}

/* Progress, lists, splitters, scroll bars ------------------------------------------ */
QProgressBar {{ background: {p.hover}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px;
    text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {p.primary}; border-radius: 4px; }}
QTreeView, QListView {{ background: transparent; border: none; }}
QTableView {{ background: transparent; border: none; selection-background-color: {p.primary_soft};
    selection-color: {p.text}; }}
QTableView::item {{ padding: 0 {SM + 2}px; border-bottom: 1px solid {p.border}; }}
QTableView::item:selected {{ background: {p.primary_soft}; color: {p.text}; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{ background: transparent; color: {p.muted}; border: none;
    border-bottom: 1px solid {p.border_strong}; padding: 6px {SM + 5}px; }}
QHeaderView::section:hover {{ color: {p.text}; }}
QHeaderView::up-arrow, QHeaderView::down-arrow {{ image: none; width: 0; height: 0; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 12px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {p.border_strong}; border-radius: 3px; min-height: 32px; min-width: 32px; }}
QScrollBar::handle:hover {{ background: {p.faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QGraphicsView#PageView {{ background: {p.canvas}; border: none; }}
QStatusBar {{ background: {p.surface}; border-top: 1px solid {p.border}; color: {p.muted}; }}
QStatusBar::item {{ border: none; }}
QMenu {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {RADIUS_MD}px; padding: 4px; }}
QMenu::item {{ padding: 6px 14px; border-radius: {RADIUS_SM}px; }}
QMenu::item:selected {{ background: {p.primary_soft}; }}
QMessageBox QLabel {{ min-width: 280px; }}
"""


def qpalette(p: Palette) -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.bg,
        QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.surface_alt,
        QPalette.ColorRole.AlternateBase: p.surface,
        QPalette.ColorRole.Text: p.text,
        QPalette.ColorRole.Button: p.surface,
        QPalette.ColorRole.ButtonText: p.text,
        QPalette.ColorRole.Highlight: p.primary_soft,
        QPalette.ColorRole.HighlightedText: p.text,
        QPalette.ColorRole.ToolTipBase: p.text,
        QPalette.ColorRole.ToolTipText: p.surface,
        QPalette.ColorRole.PlaceholderText: p.faint,
        QPalette.ColorRole.Link: p.primary_text,
        QPalette.ColorRole.Mid: p.border,
        QPalette.ColorRole.Midlight: p.hover,
        QPalette.ColorRole.Light: p.surface,
        QPalette.ColorRole.Dark: p.border_strong,
        QPalette.ColorRole.Shadow: p.shadow,
    }
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.faint))
    return pal


# ---------------------------------------------------------------------------
# Theme manager
# ---------------------------------------------------------------------------

ICON_ROLES = {
    "text": "text",
    "muted": "muted",
    "faint": "faint",
    "primary": "primary_text",
    "on_primary": "on_primary",
    "on_success": "on_success",
    "success": "success_text",
    "accent": "accent_text",
    "danger": "danger_text",
}


class Theme(QObject):
    """Owns the active palette; re-styles the app when it changes."""

    changed = Signal()
    MODES = ("system", "light", "dark")

    def __init__(self, app: QApplication, mode: str = "system") -> None:
        super().__init__(app)
        self._app = app
        self._mode = mode if mode in self.MODES else "system"
        self._bound: list[tuple[weakref.ref, str, str, int]] = []
        self.font = ui_font()
        app.setFont(self.font)
        app.setStyle("Fusion")
        hints = app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):  # Qt 6.5+
            hints.colorSchemeChanged.connect(lambda *_: self.mode == "system" and self.apply())
        self.apply()

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in self.MODES else "system"
        self.apply()

    def is_dark(self) -> bool:
        if self._mode != "system":
            return self._mode == "dark"
        hints = self._app.styleHints()
        scheme = getattr(hints, "colorScheme", lambda: Qt.ColorScheme.Unknown)()
        return scheme == Qt.ColorScheme.Dark

    @property
    def p(self) -> Palette:
        return DARK if self.is_dark() else LIGHT

    def apply(self) -> None:
        p = self.p
        self._app.setPalette(qpalette(p))
        self._app.setStyleSheet(stylesheet(p, self.font.pointSizeF()))
        self._refresh_icons()
        self.changed.emit()

    # -- icons -----------------------------------------------------------------

    def icon(self, name: str, role: str = "text", size: int = 18) -> QIcon:
        p = self.p
        ico = QIcon()
        ico.addPixmap(render_icon(name, getattr(p, ICON_ROLES[role]), size), QIcon.Mode.Normal)
        ico.addPixmap(render_icon(name, p.faint, size), QIcon.Mode.Disabled)
        return ico

    def pixmap(self, name: str, role: str = "text", size: int = 18) -> QPixmap:
        return render_icon(name, getattr(self.p, ICON_ROLES[role]), size)

    def bind_icon(
        self, widget: QWidget | QAction, name: str, role: str = "text", size: int = 18
    ) -> None:
        """Give a button, label or action an icon that follows theme changes."""
        self._bound = [b for b in self._bound if b[0]() is not None and b[0]() is not widget]
        self._bound.append((weakref.ref(widget), name, role, size))
        self._set_icon(widget, name, role, size)

    def _set_icon(self, widget: QWidget | QAction, name: str, role: str, size: int) -> None:
        if isinstance(widget, QAction):
            widget.setIcon(self.icon(name, role, size))
        elif isinstance(widget, QAbstractButton):
            widget.setIcon(self.icon(name, role, size))
            widget.setIconSize(QSize(size, size))
        elif isinstance(widget, QLabel):
            widget.setPixmap(self.pixmap(name, role, size))

    def _refresh_icons(self) -> None:
        alive = []
        for ref, name, role, size in self._bound:
            w = ref()
            if w is not None:
                self._set_icon(w, name, role, size)
                alive.append((ref, name, role, size))
        self._bound = alive


_current: Theme | None = None


def install(app: QApplication, mode: str = "system") -> Theme:
    global _current
    _current = Theme(app, mode)
    return _current


def current() -> Theme:
    if _current is None:
        raise RuntimeError("theme.install() has not been called")
    return _current


def asset_dir() -> Path:
    return user_cache_dir() / "ui"


# ---------------------------------------------------------------------------
# Brand mark
# ---------------------------------------------------------------------------


def app_icon_pixmap(size: int) -> QPixmap:
    """The app mark: the scan-search glyph on a rounded teal tile."""
    screen = QApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(dpr)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color(LIGHT.primary))
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.26, size * 0.26)
    glyph = render_icon("scan-search", "#FFFFFF", round(size * 0.62), 2.2)
    off = (size - size * 0.62) / 2
    painter.drawPixmap(round(off), round(off), glyph)
    painter.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_icon_pixmap(s))
    return icon
