"""UI behaviour (pytest-qt, offscreen)."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QItemSelectionModel, QSettings, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMessageBox

from pmqe.extract import output_path
from pmqe.pipeline import RunOptions, run_pipeline
from pmqe.store import RunStore
from pmqe.text import prepare_queries
from pmqe.ui.review_model import GROUP_ROLE

from .conftest import make_pdf


@pytest.fixture(scope="module", autouse=True)
def ui_theme(qapp):
    from pmqe.ui import theme

    theme.install(qapp, "light")


@pytest.fixture(autouse=True)
def clear_clipboard(qapp):
    yield
    # Qt's offscreen platform keeps clipboard data in a C++ global that is freed
    # after Python has shut down, so data set from Python crashes the test run on
    # exit. Desktop platforms free it while the app closes, so the app is fine.
    qapp.clipboard().clear()


@pytest.fixture
def run_dir(tmp_path: Path) -> str:
    make_pdf(tmp_path / "in" / "a.pdf", ["invoice total due", "total only", "invoice only"])
    make_pdf(tmp_path / "in" / "b.pdf", ["invoice total"])
    out = tmp_path / "out"
    opts = RunOptions(
        str(tmp_path / "in"), str(out), prepare_queries(["invoice total"]).queries, workers=0
    )
    run_pipeline(opts)
    return str(out)


@pytest.fixture
def review(qtbot, run_dir: str):
    from pmqe.ui.review_page import ReviewPage

    page = ReviewPage()
    qtbot.addWidget(page)
    page.resize(1200, 800)
    page.load_run(run_dir)
    yield page
    page.shutdown()


def select(page, items) -> None:
    sm = page.tree.selectionModel()
    sm.clearSelection()
    for i, it in enumerate(items):
        idx = page.model.index_of(it.id)
        flag = QItemSelectionModel.SelectionFlag.Select
        if i == 0:
            sm.setCurrentIndex(idx, flag | QItemSelectionModel.SelectionFlag.Current)
        else:
            sm.select(idx, flag)


def test_opens_on_the_review_queue(review):
    counts = review.model.status_counts()
    assert counts[0] == 2  # two candidates: "total only", "invoice only"
    assert review.filter.current() == 0
    assert review._current is not None and review._current.status == "candidate"


def test_approve_persists_to_store_file_and_survives_refilter(review, run_dir):
    item = review._current
    review.decide(True)
    assert item.status == "approved"
    assert os.path.exists(output_path(item, run_dir))
    review.search.setText("zzz")  # rebuild the list from the model's items
    review.search.setText("")
    review._search_timer.timeout.emit()
    assert next(i for i in review.model.items if i.id == item.id).status == "approved"
    with RunStore.open(run_dir) as store:
        assert {i.id: i.status for i in store.items()}[item.id] == "approved"


def test_reject_removes_output_and_undo_restores_it(review, run_dir):
    review.filter.set_current(1)
    review.model.set_filter(1)
    review._after_refilter(0)
    matched = review._current
    path = output_path(matched, run_dir)
    assert os.path.exists(path)
    review.decide(False)
    assert matched.status == "rejected" and not os.path.exists(path)
    review.undo()
    assert matched.status == "matched" and os.path.exists(path)
    with RunStore.open(run_dir) as store:
        assert {i.id: i.status for i in store.items()}[matched.id] == "matched"


def test_keyboard_review_flow(review, qtbot):
    from PySide6.QtCore import Qt

    review.show()  # shortcuts only fire in a visible, active window
    with qtbot.waitActive(review):
        review.activateWindow()
    review.tree.setFocus()
    first = review._current
    qtbot.keyClick(review.tree, Qt.Key.Key_A)  # approve: the next candidate slides in
    assert first.status == "approved"
    second = review._current
    assert second is not None and second is not first
    qtbot.keyClick(review.tree, Qt.Key.Key_R)
    assert second.status == "rejected"
    qtbot.keyClick(review.tree, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert second.status == "candidate"


def test_bulk_reject_asks_first(review, monkeypatch):
    review.filter.set_current(3)
    review.model.set_filter(3)
    review._after_refilter(0)
    items = list(review.model.items)
    select(review, items)
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Cancel,
    )
    review.decide(False)
    assert asked and all(i.status != "rejected" for i in items)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    select(review, items)
    review.decide(False)
    assert all(i.status == "rejected" for i in items)
    assert review.btn_approve.text().startswith("Approve")


def test_page_view_zoom_is_relative_and_rotation_refits(qtbot):
    from pmqe.ui.preview import PageView

    view = PageView()
    qtbot.addWidget(view)
    view.resize(800, 600)
    view.show()
    img = QImage(1000, 1400, QImage.Format.Format_RGB888)
    img.fill(0xFFFFFF)
    view.set_image(img)
    fitted = view.current_scale()
    assert fitted < 1
    view.zoom_in()
    assert view.current_scale() == pytest.approx(fitted * 1.25)
    view.fit()
    view.rotate_by(90)  # landscape now: fit recomputed, no jump to 100%
    assert view.current_scale() != pytest.approx(1.0)
    assert view.current_scale() == pytest.approx(
        min((800 - 48) / 1400, (600 - 48) / 1000), rel=0.05
    )


def test_text_view_marks_whole_words_only(qtbot):
    from pmqe.ui.preview import TextView

    tv = TextView()
    qtbot.addWidget(tv)
    tv.set_text("Start the art", "PDF text layer", ["art"], whole_words=True)
    from pmqe.ui.theme import LIGHT

    assert tv.browser.toHtml().lower().count(LIGHT.mark_bg.lower()) == 1  # "art", not "Start"


def test_extract_page_explains_skipped_queries(qtbot):
    from pmqe.ui.extract_page import ExtractPage

    page = ExtractPage()
    qtbot.addWidget(page)
    page.queries.setPlainText("Invoice\ninvoice\na b\nAudit")
    page._validate_queries()
    assert page.query_count.text() == "2 queries"
    assert "duplicate" in page.validation.text() and "2+ characters" in page.validation.text()


def _hits_page(qtbot, tmp_path: Path):
    from pmqe.ui.extract_page import ExtractPage

    page = ExtractPage()
    qtbot.addWidget(page)
    counts = [
        ("Aaron Hawkins 38461", 3),
        ("quarterly audit", 0),
        ("Bart Watters", 12),
        ("invoice p10", 1),
        ("invoice p2", 1),
    ]
    items = [
        SimpleNamespace(query="quarterly audit", status="candidate"),
        SimpleNamespace(query="Bart Watters", status="candidate"),
        SimpleNamespace(query="Bart Watters", status="approved"),
    ]
    page.set_hits(counts, items, str(tmp_path))
    return page, counts, items


def test_hits_table_is_read_only_and_can_be_searched_filtered_and_sorted(qtbot, tmp_path):
    from pmqe.ui.hits_table import HITS, NO_HITS, QUERY, TO_REVIEW

    page, _, _ = _hits_page(qtbot, tmp_path)
    hits = page.hits
    assert page.hits_stack.currentIndex() == 1
    assert page.zero_chip.text() == "1 with no hits" and not page.zero_chip.isHidden()

    index = hits.proxy.index(0, QUERY)
    assert not hits.proxy.flags(index) & Qt.ItemFlag.ItemIsEditable
    assert hits.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers

    hits.filter.setCurrentIndex(NO_HITS)
    assert hits.filter.itemText(NO_HITS) == "No hits · 1"
    assert [r.query for r in hits.shown_rows()] == ["quarterly audit"]
    hits.filter.setCurrentIndex(TO_REVIEW)
    assert [r.query for r in hits.shown_rows()] == ["quarterly audit", "Bart Watters"]
    hits.filter.setCurrentIndex(0)

    hits.search.setText("hawkins AARON")  # any order, any case
    assert [r.query for r in hits.shown_rows()] == ["Aaron Hawkins 38461"]
    assert hits.count.text() == "Showing 1 of 5 queries"
    hits.search.setText("zzz")
    assert hits.table.empty_text == "No queries match this search."
    hits.search.clear()
    assert hits.count.text() == "5 queries" and hits.table.empty_text == ""

    hits.table.sortByColumn(QUERY, Qt.SortOrder.AscendingOrder)
    assert [r.query for r in hits.shown_rows()] == [
        "Aaron Hawkins 38461",
        "Bart Watters",
        "invoice p2",
        "invoice p10",
        "quarterly audit",
    ]
    hits.table.sortByColumn(HITS, Qt.SortOrder.DescendingOrder)
    assert [r.hits for r in hits.shown_rows()] == [12, 3, 1, 1, 0]
    assert hits.proxy.headerData(HITS, Qt.Orientation.Horizontal) == "↓ Hits"
    assert hits.proxy.headerData(QUERY, Qt.Orientation.Horizontal) == "Query"


def test_hits_table_context_menu(qtbot, tmp_path):
    page, _, _ = _hits_page(qtbot, tmp_path)
    menu = page.hits.build_menu()
    assert [(a.text(), a.isEnabled()) for a in menu.actions() if a.text()] == [
        ("Copy", False),  # nothing selected yet
        ("Copy queries only", True),
        ("Copy table", True),
        ("Export CSV…", True),
        ("Select all", True),
    ]


def test_hits_table_copies_and_exports_what_is_shown(qtbot, tmp_path):
    from pmqe.ui.hits_table import HITS, QUERY, RAW_ROLE, TO_REVIEW

    page, counts, items = _hits_page(qtbot, tmp_path)
    hits = page.hits
    hits.table.sortByColumn(HITS, Qt.SortOrder.DescendingOrder)
    sm = hits.table.selectionModel()
    for r in (0, 1):
        for c in (QUERY, HITS):
            sm.select(hits.proxy.index(r, c), QItemSelectionModel.SelectionFlag.Select)
    qtbot.keyClick(hits.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    copied = QApplication.clipboard().mimeData()
    assert copied.text() == "Bart Watters\t12\nAaron Hawkins 38461\t3"  # raw numbers
    assert "<td>Bart Watters</td><td>12</td>" in copied.html()  # pastes as a table
    hits.copy_queries()
    assert QApplication.clipboard().text() == "Bart Watters\nAaron Hawkins 38461"
    hits.copy_table()
    assert QApplication.clipboard().text().splitlines()[:2] == [
        "Query\tHits\tTo review",
        "Bart Watters\t12\t1",
    ]

    # A review decision updates the counts in place: sorting and selection stay.
    sm.clearSelection()
    sm.select(hits.proxy.index(0, QUERY), QItemSelectionModel.SelectionFlag.Select)
    page.set_hits([(q, 20 if q == "invoice p2" else n) for q, n in counts], items)
    assert hits.shown_rows()[0].query == "invoice p2"
    assert [hits.proxy.data(i, RAW_ROLE) for i in sm.selectedIndexes()] == ["Bart Watters"]

    hits.filter.setCurrentIndex(TO_REVIEW)
    out = tmp_path / "hits.csv"
    hits.export_csv(str(out))
    assert out.read_bytes().startswith(codecs.BOM_UTF8)  # Excel reads it as UTF-8
    assert out.read_text("utf-8-sig").splitlines() == [
        "Query,Hits,To review",
        "Bart Watters,12,1",
        "quarterly audit,0,1",
    ]


def test_main_window_runs_end_to_end(qtbot, tmp_path: Path):
    from pmqe.ui.main_window import REVIEW, MainWindow

    make_pdf(tmp_path / "in" / "a.pdf", ["invoice total", "total only"])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings)
    qtbot.addWidget(win)
    win.start_run()  # nothing filled in: inline errors, no run
    assert win._run is None and win.extract.input.helper.objectName() == "Error"

    win.extract.input.set_path(str(tmp_path / "in"))
    win.extract.output.set_path(str(tmp_path / "out"))
    win.extract.queries.setPlainText("invoice total")
    win.extract.workers.setValue(1)
    win.start_run()
    assert win._run is not None
    with qtbot.waitSignal(win._run.finished, timeout=120_000):
        pass
    qtbot.waitUntil(lambda: win._run is None)
    assert win.review.output_dir == os.path.normpath(str(tmp_path / "out"))
    assert win.nav_review._badge == "1"
    hits = win.extract.hits
    assert [tuple(r) for r in hits.shown_rows()] == [("invoice total", 1, 1)]
    assert hits.export_dir == os.path.normpath(str(tmp_path / "out"))
    win.go(REVIEW)
    win.review.decide(True)
    assert win.nav_review._badge == ""
    assert [tuple(r) for r in hits.shown_rows()] == [("invoice total", 2, 0)]  # kept in step
    assert settings.value("output_dir") == os.path.normpath(str(tmp_path / "out"))


def test_group_counts_and_navigation_update_in_the_all_view(review):
    review.filter.set_current(3)
    review.model.set_filter(3)
    review._after_refilter(0)
    head = review.model.index(0, 0)
    assert review.model.data(head, GROUP_ROLE).todo == 2
    candidates = [i for i in review.model.items if i.status == "candidate"]
    select(review, candidates)
    review.decide(True)
    assert review.model.data(head, GROUP_ROLE).todo == 0
    n = review.model.visible_count()
    review._select(review.model.index_at(0))
    for _ in range(n + 2):  # J past the end stays on the last row
        review._step(1)
    assert review._flat_position() == n - 1
    review._step(-1)
    assert review._flat_position() == n - 2


def test_identical_copies_share_an_output_until_both_are_rejected(qtbot, tmp_path: Path):
    import shutil

    from pmqe.ui.review_page import ReviewPage

    src = make_pdf(tmp_path / "in" / "a" / "doc.pdf", ["keyword"])
    (tmp_path / "in" / "b").mkdir()
    shutil.copy2(src, tmp_path / "in" / "b" / "doc.pdf")  # same name, content and mtime
    out = tmp_path / "out"
    run_pipeline(
        RunOptions(str(tmp_path / "in"), str(out), prepare_queries(["keyword"]).queries, workers=0)
    )
    page = ReviewPage()
    qtbot.addWidget(page)
    page.load_run(str(out))
    first, second = page.model.items
    path = output_path(first, str(out))
    assert path == output_path(second, str(out)) and os.path.exists(path)
    page.filter.set_current(3)
    page.model.set_filter(3)
    select(page, [first])
    page.decide(False)
    assert os.path.exists(path)  # still needed by the other copy
    select(page, [second])
    page.decide(False)
    assert not os.path.exists(path)
    page.shutdown()
