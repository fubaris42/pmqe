"""The platform's real OCR engine (Windows OCR, Apple Vision or RapidOCR)."""

from __future__ import annotations

import fitz
import pytest
from PIL import Image

from pmqe import ocr
from pmqe.ocr import ocr_images as real_ocr_images  # captured before conftest blocks it


def _rendered_page(text: str) -> Image.Image:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(60, 60, 540, 400), text, fontsize=16)
    pix = page.get_pixmap(dpi=200, colorspace=fitz.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


@pytest.mark.ocr
def test_platform_engine_reads_a_page():
    (result,) = real_ocr_images([_rendered_page("Invoice total amount\nquarterly report")])
    if result is None:
        pytest.skip("no OCR engine available on this machine")
    text, engine = result
    assert {"invoice", "total", "quarterly", "report"} <= set(text.lower().split())
    expected = ocr.backend_name()
    if expected == ocr.WINRT and ocr._winrt_get_engine() is None:
        expected = ocr.RAPIDOCR  # no Windows OCR language pack (e.g. CI servers)
    # A silent fallback would hide a broken primary engine, so insist on it.
    assert engine == expected


def test_prepare_bounds_size_and_mode():
    big = Image.new("RGB", (ocr.OCR_MAX_SIDE * 2, 100))
    out = ocr._prepare(big)
    assert out.mode == "L" and max(out.size) == ocr.OCR_MAX_SIDE
