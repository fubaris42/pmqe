"""Render the app's screens to PNG for the README (and for design review).

Builds a small, realistic demo corpus (invoices, a scanned page), runs a real
extraction, then renders the Extract and Review pages in light and dark mode
without showing a window::

    uv run python tools/screenshots.py --out screenshots
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
from pathlib import Path

import fitz
from PIL import Image

CUSTOMERS = [
    ("Aaron Hawkins", "Troy, New York"),
    ("Bart Watters", "Seattle, Washington"),
    ("Claire Gute", "Henderson, Kentucky"),
    ("Darrin Van Huff", "Los Angeles, California"),
    ("Sean O'Donnell", "Fort Lauderdale, Florida"),
    ("Brosina Hoffman", "Concord, North Carolina"),
]
ITEMS = [
    ("Staples", "Fasteners, Office Supplies", 247.84),
    ("Avery Binders", "Binders, Office Supplies", 18.24),
    ("Bretford Table", "Tables, Furniture", 957.58),
    ("Logitech Mouse", "Accessories, Technology", 39.99),
    ("Xerox Paper", "Paper, Office Supplies", 15.55),
    ("Hon Chair", "Chairs, Furniture", 731.94),
]


SHOWN_INPUT = r"D:\Documents\Invoices" if sys.platform == "win32" else "~/Documents/Invoices"
SHOWN_OUTPUT = (
    r"D:\Documents\Invoice matches" if sys.platform == "win32" else "~/Documents/Invoice matches"
)


def invoice_text(rng: random.Random, number: int, customer: str, city: str) -> str:
    lines = [
        "SuperStore",
        "",
        f"INVOICE  # {number}",
        "",
        f"Bill To: {customer}",
        f"Ship To: {city}, United States",
        f"Date: {rng.choice(['Mar', 'Apr', 'Jun', 'Sep'])} {rng.randint(1, 28)} 2026",
        "Ship Mode: Second Class",
        "",
        "Item            Quantity     Rate      Amount",
    ]
    total = 0.0
    for name, cat, price in rng.sample(ITEMS, 2):
        qty = rng.randint(1, 8)
        total += qty * price
        lines += [f"{name:<16}{qty:>4}   ${price:>8.2f}   ${qty * price:>9.2f}", f"  {cat}"]
    lines += [
        "",
        f"Subtotal: ${total:,.2f}",
        f"Total: ${total + 55.2:,.2f}",
        "",
        "Notes: Thanks for your business!",
        f"Order ID: CA-2026-{number}",
    ]
    return "\n".join(lines)


def build_corpus(root: Path) -> list[str]:
    rng = random.Random(7)
    queries = []
    for i, (customer, city) in enumerate(CUSTOMERS):
        for k in range(3):
            number = 38400 + i * 37 + k * 11
            doc = fitz.open()
            page = doc.new_page()
            page.insert_textbox(
                fitz.Rect(56, 60, 540, 780),
                invoice_text(rng, number, customer, city),
                fontsize=11.5,
            )
            folder = root / ("2025" if k == 2 else "2026")
            folder.mkdir(parents=True, exist_ok=True)
            doc.save(str(folder / f"invoice_{customer.replace(' ', '_')}_{number}.pdf"))
            doc.close()
            if k == 0:
                queries.append(f"{customer} {number}")
        queries.append(customer)
    # a scanned invoice: rendered to an image, no text layer
    doc = fitz.open()
    doc.new_page().insert_textbox(
        fitz.Rect(56, 60, 540, 780),
        invoice_text(rng, 38461, "Aaron Hawkins", "Troy, New York"),
        fontsize=12,
    )
    pix = doc[0].get_pixmap(dpi=150, colorspace=fitz.csGRAY)
    Image.frombytes("L", (pix.width, pix.height), pix.samples).save(
        root / "scan_hawkins.jpg", quality=80
    )
    queries.append("Aaron Hawkins 38461")
    queries.append("quarterly audit report")
    return queries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("screenshots"))
    ap.add_argument("--size", default="1440x900")
    ap.add_argument("--format", default="webp", choices=["webp", "png"])
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="pmqe-shots-"))
    os.environ.setdefault("PMQE_CACHE_DIR", str(work / "cache"))
    os.environ.setdefault("PMQE_LOG_DIR", str(work / "logs"))

    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication

    from pmqe.gui import create_app
    from pmqe.pipeline import RunOptions, run_pipeline
    from pmqe.text import prepare_queries
    from pmqe.ui import theme
    from pmqe.ui.main_window import EXTRACT, REVIEW, MainWindow

    queries = build_corpus(work / "Invoices")
    out_dir = work / "Extracted"
    opts = RunOptions(
        str(work / "Invoices"), str(out_dir), prepare_queries(queries).queries, workers=0
    )
    result = run_pipeline(opts)

    app = create_app([sys.argv[0]])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(work / "settings"))
    w, h = (int(x) for x in args.size.split("x"))
    args.out.mkdir(parents=True, exist_ok=True)
    th = theme.install(app, "light")
    app.setWindowIcon(theme.app_icon())

    for mode in ("light", "dark"):
        th.set_mode(mode)
        win = MainWindow(QSettings())
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        win.resize(w, h)
        # Neutral paths: screenshots are published, temp paths contain user names
        win.extract.input.set_path(SHOWN_INPUT)
        win.extract.output.set_path(SHOWN_OUTPUT)
        win.extract.queries.setPlainText("\n".join(queries))
        win.extract._validate_queries()
        win.show()
        win.extract.show_result(result)
        win.review.load_run(str(out_dir))
        sub = win.review.header.subtitle
        sub.setText(sub.text().replace(str(out_dir), SHOWN_OUTPUT))

        win.go(EXTRACT)
        _shoot(app, win, args.out / f"extract-{mode}.{args.format}")
        win.go(REVIEW)
        win.review.filter.set_current(3)
        win.review.model.set_filter(3)
        win.review._after_refilter(1)
        _shoot(app, win, args.out / f"review-{mode}.{args.format}")
        win.review._toggle_view()
        _shoot(app, win, args.out / f"text-{mode}.{args.format}")
        win.review._toggle_view()
        win.close()
        QApplication.processEvents()


def _shoot(app, win, path: Path) -> None:
    for _ in range(20):
        app.processEvents()
    win.review.loader.wait()  # let the page preview finish rendering
    for _ in range(20):
        app.processEvents()
    win.grab().save(str(path), quality=90)
    print("wrote", path)


if __name__ == "__main__":
    main()
