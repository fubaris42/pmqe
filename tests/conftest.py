"""Shared fixtures.

Real OCR engines are blocked by default so tests are fast and identical on
every platform; tests that need OCR use the ``fake_ocr`` fixture, and the
one real-engine test is marked ``ocr``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import fitz
import pytest
from PIL import Image

from pmqe import ocr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FILLER = "This document contains plenty of native text content for the layer. " * 4


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("PMQE_CACHE_DIR", str(home / "cache"))
    monkeypatch.setenv("PMQE_LOG_DIR", str(home / "logs"))

    def _no_real_ocr(images: list) -> list:
        raise AssertionError(f"real OCR invoked on {len(images)} page(s); use the fake_ocr fixture")

    monkeypatch.setattr(ocr, "ocr_images", _no_real_ocr)


class FakeOcr:
    """Returns queued texts in call order; ``None`` entries simulate failures."""

    def __init__(self) -> None:
        self.queue: list[str | None] = []
        self.default: str | None = ""
        self.calls = 0

    def __call__(self, images: list) -> list:
        out = []
        for _ in images:
            self.calls += 1
            text = self.queue.pop(0) if self.queue else self.default
            out.append(None if text is None else (text, "fake"))
        return out


@pytest.fixture
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOcr]:
    fake = FakeOcr()
    monkeypatch.setattr(ocr, "ocr_images", fake)
    yield fake


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------


def make_pdf(path: Path, page_texts: list[str], filler: bool = True) -> Path:
    """A PDF with a real text layer (wrapped, so nothing is clipped)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        body = f"{text}\n{FILLER}" if filler else text
        assert page.insert_textbox(fitz.Rect(50, 50, 545, 790), body, fontsize=10) >= 0
    doc.save(str(path))
    doc.close()
    return path


def scan_image(size: tuple[int, int] = (850, 1100)) -> Image.Image:
    """A blank 'scanned page' (content is supplied by the fake OCR)."""
    img = Image.new("L", size, 255)
    img.paste(0, (100, 100, 700, 120))  # a stroke, so it is not empty
    return img


def make_scanned_pdf(path: Path, n_pages: int, native_first: str | None = None) -> Path:
    """Image-only pages, optionally after one native-text cover page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    if native_first is not None:
        doc.new_page().insert_textbox(
            fitz.Rect(50, 50, 545, 790), f"{native_first}\n{FILLER}", fontsize=10
        )
    png = path.with_suffix(".scan.png")
    scan_image().save(png)
    for _ in range(n_pages):
        page = doc.new_page()
        page.insert_image(page.rect, filename=str(png))
    png.unlink()
    doc.save(str(path))
    doc.close()
    return path
