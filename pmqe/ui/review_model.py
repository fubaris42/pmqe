"""The review list: queries as groups, matched pages as rows.

A lightweight custom model (no per-item Qt objects) keeps filtering instant
for runs with 100k+ items. Rows refer to :class:`ReviewItem` objects by
index, so decisions made in the panel update the same objects the run
store saves — never copies.
"""

from __future__ import annotations

import bisect

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..models import ReviewItem
from ..text import Query
from . import theme
from .theme import color

TODO, APPROVED, REJECTED, ALL = range(4)
FILTER_STATUSES = {
    TODO: {"candidate"},
    APPROVED: {"matched", "approved"},
    REJECTED: {"rejected"},
    ALL: {"matched", "candidate", "approved", "rejected"},
}

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
GROUP_ROLE = Qt.ItemDataRole.UserRole + 2

STATUS_LABEL = {
    "matched": "Matched",
    "candidate": "To review",
    "approved": "Approved",
    "rejected": "Rejected",
}
STATUS_TONE = {
    "matched": "primary",
    "candidate": "accent",
    "approved": "success",
    "rejected": "danger",
}

_ROOT = 0  # internalId of top-level (group) indexes; items use group row + 1
_NO_PARENT = QModelIndex()


class _Group:
    __slots__ = ("query_idx", "rows", "todo")

    def __init__(self, query_idx: int) -> None:
        self.query_idx = query_idx
        self.rows: list[int] = []  # indexes into ReviewModel.items
        self.todo = 0


class ReviewModel(QAbstractItemModel):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[ReviewItem] = []
        self.queries: list[Query] = []
        self._groups: list[_Group] = []
        self._filter = TODO
        self._text = ""
        self._by_id: dict[str, int] = {}
        self._where: dict[int, tuple[int, int]] = {}  # item index -> (group row, row)
        self._offsets: list[int] = []  # flat position of each group's first row

    # -- data management -----------------------------------------------------

    def set_run(self, queries: list[Query], items: list[ReviewItem]) -> None:
        self.queries = queries
        self.items = items
        self._by_id = {it.id: i for i, it in enumerate(items)}
        self.refilter()

    def set_filter(self, status_filter: int) -> None:
        self._filter = status_filter
        self.refilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().casefold()
        self.refilter()

    def refilter(self) -> None:
        self.beginResetModel()
        wanted = FILTER_STATUSES[self._filter]
        text = self._text
        groups: dict[int, _Group] = {}
        for i, it in enumerate(self.items):
            if it.status not in wanted:
                continue
            if text and text not in it.query.casefold() and text not in it.file_name.casefold():
                continue
            g = groups.get(it.query_idx)
            if g is None:
                g = groups[it.query_idx] = _Group(it.query_idx)
            g.rows.append(i)
            g.todo += it.status == "candidate"
        self._groups = [groups[k] for k in sorted(groups)]
        self._where = {
            pos: (gi, r) for gi, g in enumerate(self._groups) for r, pos in enumerate(g.rows)
        }
        self._offsets = []
        total = 0
        for g in self._groups:
            self._offsets.append(total)
            total += len(g.rows)
        self.endResetModel()

    def status_counts(self) -> dict[int, int]:
        counts = dict.fromkeys(range(4), 0)
        for it in self.items:
            s = it.status
            counts[ALL] += 1
            if s == "candidate":
                counts[TODO] += 1
            elif s == "rejected":
                counts[REJECTED] += 1
            else:
                counts[APPROVED] += 1
        return counts

    def items_changed(self, changed: list[ReviewItem]) -> bool:
        """Refresh after decisions. Returns True when rows had to be
        re-filtered (items left the current filter)."""
        wanted = FILTER_STATUSES[self._filter]
        if any(it.status not in wanted for it in changed):
            self.refilter()
            return True
        touched: set[int] = set()
        for it in changed:
            idx = self.index_of(it.id)
            if idx.isValid():
                self.dataChanged.emit(idx, idx)
                touched.add(idx.internalId() - 1)
        for gi in touched:  # group headers show "N to review"
            g = self._groups[gi]
            g.todo = sum(self.items[i].status == "candidate" for i in g.rows)
            head = self.createIndex(gi, 0, _ROOT)
            self.dataChanged.emit(head, head)
        return False

    # -- lookups -------------------------------------------------------------------

    def item(self, index: QModelIndex | QPersistentModelIndex) -> ReviewItem | None:
        if not index.isValid() or index.internalId() == _ROOT:
            return None
        g = self._groups[index.internalId() - 1]
        return self.items[g.rows[index.row()]]

    def index_of(self, item_id: str) -> QModelIndex:
        where = self._where.get(self._by_id.get(item_id, -1))
        if where is None:
            return QModelIndex()
        gi, r = where
        return self.createIndex(r, 0, gi + 1)

    # Flat positions number the visible item rows 0..visible_count()-1 in
    # display order; the list uses them for next/previous navigation.

    def visible_count(self) -> int:
        return (self._offsets[-1] + len(self._groups[-1].rows)) if self._groups else 0

    def flat_position(self, index: QModelIndex | QPersistentModelIndex) -> int:
        if not index.isValid() or index.internalId() == _ROOT:
            return 0
        return self._offsets[index.internalId() - 1] + index.row()

    def index_at(self, flat: int) -> QModelIndex:
        n = self.visible_count()
        if not n:
            return QModelIndex()
        flat = max(0, min(n - 1, flat))
        gi = bisect.bisect_right(self._offsets, flat) - 1
        return self.createIndex(flat - self._offsets[gi], 0, gi + 1)

    def is_empty(self) -> bool:
        return not self._groups

    # -- QAbstractItemModel --------------------------------------------------------

    def index(
        self, row: int, column: int, parent: QModelIndex | QPersistentModelIndex = _NO_PARENT
    ) -> QModelIndex:
        if column != 0 or row < 0:
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, 0, _ROOT) if row < len(self._groups) else QModelIndex()
        if parent.internalId() == _ROOT and parent.row() < len(self._groups):
            g = self._groups[parent.row()]
            return (
                self.createIndex(row, 0, parent.row() + 1) if row < len(g.rows) else QModelIndex()
            )
        return QModelIndex()

    def parent(self, index: QModelIndex | QPersistentModelIndex = _NO_PARENT) -> QModelIndex:
        if not index.isValid() or index.internalId() == _ROOT:
            return QModelIndex()
        return self.createIndex(index.internalId() - 1, 0, _ROOT)

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _NO_PARENT) -> int:
        if not parent.isValid():
            return len(self._groups)
        if parent.internalId() == _ROOT:
            return len(self._groups[parent.row()].rows)
        return 0

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _NO_PARENT) -> int:
        return 1

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if index.internalId() == _ROOT:
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ):
        if not index.isValid():
            return None
        if index.internalId() == _ROOT:
            g = self._groups[index.row()]
            if role == GROUP_ROLE:
                return g
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
                q = self.queries[g.query_idx].text
                return f"{q}, {len(g.rows)} pages, {g.todo} to review"
            return None
        it = self.item(index)
        if it is None:
            return None
        if role == ITEM_ROLE:
            return it
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return f"{it.file_name}, page {it.page_idx + 1}, {STATUS_LABEL[it.status]}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return it.file_path
        return None


class ReviewDelegate(QStyledItemDelegate):
    GROUP_H, ITEM_H = 38, 56

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(
            option.rect.width(), self.GROUP_H if index.internalId() == _ROOT else self.ITEM_H
        )

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        p = theme.current().p
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect: QRect = option.rect
        base = QFont(option.font)
        if index.internalId() == _ROOT:
            self._paint_group(painter, rect, index, base, option, p)
        else:
            self._paint_item(painter, rect, index, base, option, p)
        painter.restore()

    def _paint_group(self, painter, rect, index, base, option, p) -> None:
        model: ReviewModel = index.model()
        g: _Group = index.data(GROUP_ROLE)
        expanded = bool(option.state & QStyle.StateFlag.State_Open)
        chevron = theme.current().pixmap(
            "chevron-down" if expanded else "chevron-right", "faint", 14
        )
        painter.drawPixmap(rect.left() + 6, rect.center().y() - 6, chevron)
        bold = QFont(base)
        bold.setWeight(QFont.Weight.DemiBold)
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.9)
        right = f"{len(g.rows):,}"
        if g.todo:
            right = f"{g.todo:,} to review · {right}"
        painter.setFont(small)
        fm_small = QFontMetrics(small)
        rw = fm_small.horizontalAdvance(right) + 4
        painter.setPen(color(p.accent_text if g.todo else p.faint))
        painter.drawText(
            QRect(rect.right() - rw - 10, rect.top(), rw, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            right,
        )
        painter.setFont(bold)
        painter.setPen(color(p.text))
        title = model.queries[g.query_idx].text
        avail = rect.width() - rw - 44
        painter.drawText(
            QRect(rect.left() + 26, rect.top(), avail, rect.height()),
            Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(bold).elidedText(title, Qt.TextElideMode.ElideRight, avail),
        )

    def _paint_item(self, painter, rect, index, base, option, p) -> None:
        it: ReviewItem = index.data(ITEM_ROLE)
        card = QRectF(rect).adjusted(4, 2, -4, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color(p.primary_soft if selected else p.hover))
            painter.drawRoundedRect(card, 8, 8)
        if option.state & QStyle.StateFlag.State_HasFocus and selected:
            painter.setPen(color(p.focus))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        status = it.status
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.88)
        small.setWeight(QFont.Weight.DemiBold)
        pill_text = STATUS_LABEL[status]
        fm_pill = QFontMetrics(small)
        pw = fm_pill.horizontalAdvance(pill_text) + 16
        pill = QRectF(card.right() - pw - 10, card.center().y() - 10, pw, 20)
        tone = STATUS_TONE[status]
        bg, fg = {
            "primary": (p.primary_soft, p.primary_text),
            "accent": (p.accent_soft, p.accent_text),
            "success": (p.success_soft, p.success_text),
            "danger": (p.danger_soft, p.danger_text),
        }[tone]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color(bg if not selected else p.surface))
        painter.drawRoundedRect(pill, 10, 10)
        painter.setPen(color(fg))
        painter.setFont(small)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, pill_text)

        left = int(card.left()) + 14
        avail = int(pill.left()) - left - 10
        title = QFont(base)
        title.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title)
        painter.setPen(color(p.text))
        top = int(card.top())
        painter.drawText(
            QRect(left, top + 8, avail, 20),
            Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(title).elidedText(it.file_name, Qt.TextElideMode.ElideMiddle, avail),
        )
        sub = f"Page {it.page_idx + 1}"
        if it.page_count > 1:
            sub += f" of {it.page_count}"
        if it.missing_words:
            sub += " · missing " + ", ".join(it.missing_words)
        meta = QFont(base)
        meta.setPointSizeF(base.pointSizeF() * 0.9)
        painter.setFont(meta)
        painter.setPen(color(p.muted))
        painter.drawText(
            QRect(left, top + 28, avail, 18),
            Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(meta).elidedText(sub, Qt.TextElideMode.ElideRight, avail),
        )
