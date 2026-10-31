"""OCR backends.

=========  ==========================  ======================
Platform   Primary engine              Fallback
=========  ==========================  ======================
Windows    Windows.Media.Ocr (WinRT)   RapidOCR, per page
macOS      Apple Vision (ocrmac)       RapidOCR, per page
Linux      RapidOCR                    —
=========  ==========================  ======================

Engines are created lazily, once per worker process. :func:`ocr_images`
returns ``None`` for a page whose OCR failed, so callers can avoid caching
a failure as "this page has no text".
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from PIL import Image

log = logging.getLogger(__name__)

# Windows OCR rejects images above 10,000 px on a side; stay well below.
OCR_MAX_SIDE = 8000

WINRT = "winrt"
VISION = "vision"
RAPIDOCR = "rapidocr"

ENGINE_LABELS = {WINRT: "Windows OCR", VISION: "Apple Vision", RAPIDOCR: "RapidOCR"}

OcrText = tuple[str, str]  # (text, engine id)

_force_rapidocr = False
_winrt_engine: Any = None
_winrt_loop: asyncio.AbstractEventLoop | None = None
_rapid_engine: Any = None
_unavailable: set[str] = set()


def set_backend(force_rapidocr: bool) -> None:
    global _force_rapidocr
    _force_rapidocr = force_rapidocr


def native_engine() -> str:
    """The platform's preferred engine, ignoring the RapidOCR override."""
    if sys.platform == "win32":
        return WINRT
    if sys.platform == "darwin":
        return VISION
    return RAPIDOCR


def backend_name() -> str:
    return RAPIDOCR if _force_rapidocr else native_engine()


def _prepare(img: Image.Image) -> Image.Image:
    """Grayscale, bounded size: what every engine handles best."""
    if img.mode != "L":
        img = img.convert("L")
    w, h = img.size
    longest = max(w, h)
    if longest > OCR_MAX_SIDE:
        scale = OCR_MAX_SIDE / longest
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    return img


def ocr_images(images: list[Image.Image]) -> list[OcrText | None]:
    """OCR each image. ``None`` marks a page whose OCR failed everywhere."""
    if not images:
        return []
    prepared = [_prepare(img) for img in images]
    primary = backend_name()
    results: list[OcrText | None] = [None] * len(prepared)
    if primary == WINRT:
        results = _ocr_winrt(prepared)
    elif primary == VISION:
        results = _ocr_vision(prepared)
    retry = [i for i, r in enumerate(results) if r is None]
    if retry:
        if primary != RAPIDOCR:
            log.info(
                "Using RapidOCR for %d page(s) the %s engine could not read",
                len(retry),
                ENGINE_LABELS[primary],
            )
        for i, r in zip(retry, _ocr_rapid([prepared[i] for i in retry]), strict=True):
            results[i] = r
    return results


# -- Windows.Media.Ocr ------------------------------------------------------


def _winrt_get_engine() -> Any:
    global _winrt_engine
    if _winrt_engine is None and WINRT not in _unavailable:
        try:
            from winrt.windows.media.ocr import OcrEngine  # type: ignore[import-not-found]

            _winrt_engine = OcrEngine.try_create_from_user_profile_languages()
        except Exception:
            log.warning("Windows OCR could not be loaded", exc_info=True)
        if _winrt_engine is None:
            log.warning("Windows OCR is unavailable (no OCR language installed?)")
            _unavailable.add(WINRT)
    return _winrt_engine


async def _winrt_recognize(engine: Any, img: Image.Image) -> str:
    from winrt.windows.graphics.imaging import (  # type: ignore[import-not-found]
        BitmapPixelFormat,
        SoftwareBitmap,
    )
    from winrt.windows.security.cryptography import (  # type: ignore[import-not-found]
        CryptographicBuffer,
    )

    # Hand raw 8-bit gray pixels straight to the engine: no PNG round-trip.
    buffer = CryptographicBuffer.create_from_byte_array(img.tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        buffer, BitmapPixelFormat.GRAY8, img.width, img.height
    )
    result = await engine.recognize_async(bitmap)
    if result is None:
        return ""
    try:  # one line per text line (needs the Foundation.Collections projection)
        return "\n".join(line.text for line in result.lines)
    except ImportError:
        return result.text


def _ocr_winrt(images: list[Image.Image]) -> list[OcrText | None]:
    global _winrt_loop
    engine = _winrt_get_engine()
    if engine is None:
        return [None] * len(images)
    if _winrt_loop is None or _winrt_loop.is_closed():
        _winrt_loop = asyncio.new_event_loop()
    out: list[OcrText | None] = []
    for img in images:
        try:
            text = _winrt_loop.run_until_complete(_winrt_recognize(engine, img))
            out.append((text, WINRT))
        except Exception:
            log.warning("Windows OCR failed on a %dx%d page", *img.size, exc_info=True)
            out.append(None)
    return out


# -- Apple Vision (ocrmac) ---------------------------------------------------


def _ocr_vision(images: list[Image.Image]) -> list[OcrText | None]:
    if VISION in _unavailable:
        return [None] * len(images)
    try:
        from ocrmac import ocrmac  # type: ignore[import-not-found]
    except Exception:
        log.warning("Apple Vision OCR (ocrmac) could not be loaded", exc_info=True)
        _unavailable.add(VISION)
        return [None] * len(images)
    out: list[OcrText | None] = []
    for img in images:
        try:
            annotations = ocrmac.OCR(img.convert("RGB")).recognize()
            out.append(("\n".join(a[0] for a in annotations), VISION))
        except Exception:
            log.warning("Apple Vision OCR failed on a page", exc_info=True)
            out.append(None)
    return out


# -- RapidOCR -------------------------------------------------------------------


def _rapid_get_engine() -> Any:
    global _rapid_engine
    if _rapid_engine is None and RAPIDOCR not in _unavailable:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

            _rapid_engine = RapidOCR()
        except Exception:
            log.error("RapidOCR could not be loaded", exc_info=True)
            _unavailable.add(RAPIDOCR)
    return _rapid_engine


def _ocr_rapid(images: list[Image.Image]) -> list[OcrText | None]:
    engine = _rapid_get_engine()
    if engine is None:
        return [None] * len(images)
    import numpy as np

    out: list[OcrText | None] = []
    for img in images:
        try:
            result, _ = engine(np.asarray(img.convert("RGB")))
            out.append(("\n".join(r[1] for r in result) if result else "", RAPIDOCR))
        except Exception:
            log.warning("RapidOCR failed on a page", exc_info=True)
            out.append(None)
    return out
