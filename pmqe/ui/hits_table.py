"""Hits per query as a read-only table: search it, filter it, sort it by
clicking a header, copy cells into Excel or a document, export it as CSV."""

from __future__ import annotations

import csv
import html
import os
from collections.abc import Sequence
from typing import NamedTuple

from PySide6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QFont,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHeaderView,
    QLineEdit,
    QMenu,
    QMessageBox,
    QTableView,
    QWidget,
)

from ..util import atomic_write, natural_key, plural
from . import theme
from .theme import SM, color
from .widgets import column, icon_button, label, row

Index = QModelIndex | QPersistentModelIndex
_NO_PARENT = QModelIndex()


class HitRow(NamedTuple):
    query: str
    hits: int  # approved pages
    todo: int  # pages waiting for a decision


QUERY, HITS, TODO = range(3)
HEADERS = ("Query", "Hits", "To review")
HEADER_TIPS = (
    "The query as you entered it",
    "Approved pages: matched automatically or approved in review",
    "Pages with only some of the query's words, waiting for your decision",
)
RAW_ROLE = Qt.ItemDataRole.UserRole  # the value that is copied and exported
EXPORT_NAME = "hits_per_query.csv"

ALL, WITH_HITS, NO_HITS, TO_REVIEW = range(4)
FILTER_LABELS = ("All", "With hits", "No hits", "To review")

_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_ALIGN = (_LEFT, _RIGHT, _RIGHT)
# Qt 6.10 replaced invalidateRowsFilter() with begin/endFilterChange().
_FILTER_CHANGE_API = hasattr(QSortFilterProxyModel, "Direction")


def keep(r: HitRow, mode: int) -> bool:
    """Whether a row belongs in the given filter."""
    return (
        mode == ALL
        or (mode == WITH_HITS and r.hits > 0)
        or (mode == NO_HITS and r.hits == 0)
        or (mode == TO_REVIEW and r.todo > 0)
    )


# ---------------------------------------------------------------------------
# Plain-data helpers (clipboard formats and CSV), kept free of widgets
# ---------------------------------------------------------------------------


def _cell(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else str(value)


def to_tsv(grid: Sequence[Sequence[object]]) -> str:
    """Tab-separated text: what Excel and Google Sheets paste as cells."""
    return "\n".join("\t".join(_cell(v) for v in r) for r in grid)


def to_html(grid: Sequence[Sequence[object]], header: bool = False) -> str:
    """An HTML table: what Word, Outlook and Google Docs paste as a table."""
    out = ["<table>"]
    for i, r in enumerate(grid):
        tag = "th" if header and i == 0 else "td"
        out.append("<tr>" + "".join(f"<{tag}>{html.escape(_cell(v))}</{tag}>" for v in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def write_csv(path: str, rows: Sequence[HitRow]) -> None:
    """UTF-8 with a BOM, like report.csv, so Excel shows accented queries correctly."""

    def write(tmp: str) -> None:
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(HEADERS)
            w.writerows(rows)

    atomic_write(path, write)


def _set_clipboard(grid: Sequence[Sequence[object]], header: bool = False) -> None:
    data = QMimeData()
    data.setText(to_tsv(grid))
    if len(grid) > 1 or len(grid[0]) > 1:  # a single cell pastes as plain text
        data.setHtml(to_html(grid, header))
    QGuiApplication.clipboard().setMimeData(data)


# ---------------------------------------------------------------------------
# Model and filter
# ---------------------------------------------------------------------------


class HitsModel(QAbstractTableModel):
    """One read-only row per query."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[HitRow] = []
        self.keys: list[tuple] = []  # natural sort keys ("p2" before "p10")
        self.folded: list[str] = []  # case-folded queries, for searching

    def set_rows(self, rows: Sequence[HitRow]) -> None:
        rows = list(rows)
        if [r.query for r in rows] == [r.query for r in self.rows]:
            # Same queries with new counts (a review decision): update in place so
            # the selection, sort order and scroll position stay where they are.
            changed = [i for i, (a, b) in enumerate(zip(self.rows, rows, strict=True)) if a != b]
            self.rows = rows
            if changed:
                self.dataChanged.emit(self.index(changed[0], HITS), self.index(changed[-1], TODO))
            return
        self.beginResetModel()
        self.rows = rows
        self.keys = [natural_key(r.query) for r in rows]
        self.folded = [r.query.casefold() for r in rows]
        self.endResetModel()

    def rowCount(self, parent: Index = _NO_PARENT) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: Index = _NO_PARENT) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def flags(self, index: Index) -> Qt.ItemFlag:
        # Selectable so cells can be copied; never editable.
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemNeverHasChildren
        )

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        col = index.column()
        value = self.rows[index.row()][col]
        if role == Qt.ItemDataRole.DisplayRole:
            return value if col == QUERY else f"{value:,}"
        if role == RAW_ROLE:
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _ALIGN[col]
        if role == Qt.ItemDataRole.ForegroundRole and col != QUERY:
            p = theme.current().p
            if col == HITS:
                return None if value else color(p.accent_text)
            return color(p.accent_text if value else p.muted)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == QUERY:
                return value
            if col == HITS:
                return plural(value, "approved page") if value else "No approved pages"
            return f"{plural(value, 'page')} to review" if value else "Nothing to review"
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _ALIGN[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return HEADER_TIPS[section]
        return None


class HitsFilter(QSortFilterProxyModel):
    """Filter by category and search words; sort naturally by any column."""

    def __init__(self, source: HitsModel) -> None:
        super().__init__(source)
        self.source = source
        self.setSourceModel(source)
        self.setDynamicSortFilter(True)  # re-filter and re-sort when counts change
        self.mode = ALL
        self.terms: tuple[str, ...] = ()

    def set_filter(self, mode: int | None = None, text: str | None = None) -> None:
        mode = self.mode if mode is None else mode
        terms = self.terms if text is None else tuple(text.casefold().split())
        if (mode, terms) == (self.mode, self.terms):
            return
        if _FILTER_CHANGE_API:
            self.beginFilterChange()
        self.mode, self.terms = mode, terms
        if _FILTER_CHANGE_API:
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: Index) -> bool:
        if not keep(self.source.rows[source_row], self.mode):
            return False
        query = self.source.folded[source_row]
        return all(t in query for t in self.terms)

    def lessThan(self, left: Index, right: Index) -> bool:
        a, b, col = left.row(), right.row(), left.column()
        if col == QUERY:
            return self.source.keys[a] < self.source.keys[b]
        # Ties keep the entered order: Qt's sort is stable.
        return self.source.rows[a][col] < self.source.rows[b][col]

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        super().sort(column, order)
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(HEADERS) - 1)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        # The sort arrow is part of the label (on the side away from the numbers),
        # so columns sized to their contents always leave room for it.
        value = super().headerData(section, orientation, role)
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and section == self.sortColumn()
        ):
            arrow = "↑" if self.sortOrder() == Qt.SortOrder.AscendingOrder else "↓"
            return f"{value} {arrow}" if section == QUERY else f"{arrow} {value}"
        return value

    def row_at(self, proxy_row: int) -> HitRow:
        return self.source.rows[self.mapToSource(self.index(proxy_row, 0)).row()]


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class HitsTable(QTableView):
    """Spreadsheet-style selection, no editing, Ctrl+C copies the selection."""

    copyRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setAccessibleName("Hits per query")
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setCornerButtonEnabled(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        rows = self.verticalHeader()
        rows.hide()
        rows.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        rows.setDefaultSectionSize(self.fontMetrics().height() + 16)
        head = self.horizontalHeader()
        head.setHighlightSections(False)
        head.setSectionsMovable(False)
        head.setMinimumSectionSize(72)
        head.setCursor(Qt.CursorShape.PointingHandCursor)
        bold = head.font()
        bold.setWeight(QFont.Weight.DemiBold)
        head.setFont(bold)
        head.setSortIndicatorClearable(True)  # a third click restores the entered order
        head.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.setSortingEnabled(True)
        self.empty_text = ""  # shown in place of rows when the filter leaves none

    def setModel(self, model: QSortFilterProxyModel) -> None:  # type: ignore[override]
        super().setModel(model)
        head = self.horizontalHeader()
        head.setSectionResizeMode(QUERY, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(HITS, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(TODO, QHeaderView.ResizeMode.ResizeToContents)

    def keyPressEvent(self, e: QKeyEvent) -> None:
        # QTableView's own Ctrl+C copies only the current cell.
        if e.matches(QKeySequence.StandardKey.Copy):
            self.copyRequested.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e: QPaintEvent) -> None:
        super().paintEvent(e)
        model = self.model()
        if self.empty_text and model is not None and model.rowCount() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(color(theme.current().p.muted))
            painter.drawText(
                self.viewport().rect().adjusted(SM, 28, -SM, 0),
                Qt.AlignmentFlag.AlignHCenter
                | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap,
                self.empty_text,
            )


class HitsPanel(QWidget):
    """Search, filter, copy and export controls, the table, and a row count."""

    message = Signal(str)  # short confirmations for the status bar

    def __init__(self) -> None:
        super().__init__()
        self.export_dir = ""
        self.model = HitsModel(self)
        self.proxy = HitsFilter(self.model)
        self.table = HitsTable()
        self.table.setModel(self.proxy)
        self.table.setMinimumHeight(220)
        self.table.copyRequested.connect(self.copy_selection)
        self.table.customContextMenuRequested.connect(self._show_menu)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search queries")
        self.search.setAccessibleName("Search queries")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(120)
        icon = self.search.addAction(
            theme.current().icon("search", "faint", 16), QLineEdit.ActionPosition.LeadingPosition
        )
        theme.current().bind_icon(icon, "search", "faint", 16)
        self.search.textChanged.connect(lambda text: self._refilter(text=text))
        self.filter = QComboBox()
        self.filter.addItems(FILTER_LABELS)
        self.filter.setAccessibleName("Show")
        self.filter.setToolTip("Show all queries, only those with or without hits, or to review")
        self.filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.filter.currentIndexChanged.connect(lambda mode: self._refilter(mode=mode))
        self.btn_copy = icon_button(
            "copy", "Copy table: the rows shown, with headers (Ctrl+C copies selected cells)"
        )
        self.btn_copy.clicked.connect(self.copy_table)
        self.btn_export = icon_button("download", "Export CSV…: save the rows shown")
        self.btn_export.clicked.connect(self.export)

        self.count = label("", "Helper")
        self.setLayout(
            column(
                row(self.search, self.filter, self.btn_copy, self.btn_export, spacing=SM),
                self.table,
                self.count,
                spacing=SM,
            )
        )

    # -- data -------------------------------------------------------------------------

    def set_rows(self, rows: Sequence[HitRow]) -> None:
        self.model.set_rows(rows)
        for mode, text in enumerate(FILTER_LABELS):
            n = sum(1 for r in self.model.rows if keep(r, mode))
            self.filter.setItemText(mode, f"{text} · {n:,}")
        self._update_count()

    def no_hits(self) -> int:
        return sum(1 for r in self.model.rows if r.hits == 0)

    def shown_rows(self) -> list[HitRow]:
        """The rows on screen, in their on-screen order."""
        return [self.proxy.row_at(i) for i in range(self.proxy.rowCount())]

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _refilter(self, mode: int | None = None, text: str | None = None) -> None:
        self.proxy.set_filter(mode, text)
        self._update_count()

    def _update_count(self) -> None:
        shown, total = self.proxy.rowCount(), self.model.rowCount()
        queries = plural(total, "query", "queries")
        self.count.setText(queries if shown == total else f"Showing {shown:,} of {queries}")
        self.table.empty_text = (
            "No queries match this " + ("search." if self.proxy.terms else "filter.")
            if total and not shown
            else ""
        )
        self.table.viewport().update()
        for b in (self.btn_copy, self.btn_export):
            b.setEnabled(shown > 0)

    # -- copy and export ------------------------------------------------------------------

    def copy_selection(self) -> None:
        """Copy the selected cells (or, with nothing selected, the whole table)."""
        picked = self.table.selectionModel().selectedIndexes()
        if not picked:
            self.copy_table()
            return
        cells = {(i.row(), i.column()) for i in picked}
        rows = sorted({r for r, _ in cells})
        cols = sorted({c for _, c in cells})
        grid = [
            [self.proxy.index(r, c).data(RAW_ROLE) if (r, c) in cells else "" for c in cols]
            for r in rows
        ]
        _set_clipboard(grid)
        self.message.emit(f"Copied {plural(len(cells), 'cell')}")

    def copy_queries(self) -> None:
        """Copy just the query text of the selected rows (or of every row shown),
        one per line, ready to paste back into the query box."""
        picked = self.table.selectionModel().selectedIndexes()
        rows = sorted({i.row() for i in picked}) if picked else range(self.proxy.rowCount())
        queries = [self.proxy.row_at(r).query for r in rows]
        QGuiApplication.clipboard().setText("\n".join(queries))
        self.message.emit(f"Copied {plural(len(queries), 'query', 'queries')}")

    def copy_table(self) -> None:
        """Copy the rows shown, with headers."""
        rows = self.shown_rows()
        _set_clipboard([HEADERS, *rows], header=True)
        self.message.emit(f"Copied {plural(len(rows), 'row')} with headers")

    def export_csv(self, path: str) -> None:
        rows = self.shown_rows()
        write_csv(path, rows)
        self.message.emit(f"Exported {plural(len(rows), 'row')} to {os.path.basename(path)}")

    def export(self) -> None:
        start = os.path.join(self.export_dir or os.path.expanduser("~"), EXPORT_NAME)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export hits per query", start, "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            self.export_csv(path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Export failed",
                f"Could not save {os.path.basename(path)}: {exc.strerror or exc}",
            )

    def _show_menu(self, pos: QPoint) -> None:
        menu = self.build_menu()
        menu.exec(self.table.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def build_menu(self) -> QMenu:
        """The right-click menu, with actions enabled for the current state."""
        selected = self.table.selectionModel().hasSelection()
        any_rows = self.proxy.rowCount() > 0
        menu = QMenu(self)
        for text, slot, shortcut, enabled in (
            ("Copy", self.copy_selection, QKeySequence.StandardKey.Copy, selected),
            ("Copy queries only", self.copy_queries, None, any_rows),
            (None, None, None, None),
            ("Copy table", self.copy_table, None, any_rows),
            ("Export CSV…", self.export, None, any_rows),
            (None, None, None, None),
            ("Select all", self.table.selectAll, QKeySequence.StandardKey.SelectAll, any_rows),
        ):
            if text is None:
                menu.addSeparator()
                continue
            action = menu.addAction(text)
            action.triggered.connect(slot)
            action.setEnabled(bool(enabled))
            if shortcut is not None:
                action.setShortcut(shortcut)
                action.setShortcutVisibleInContextMenu(True)
        return menu
