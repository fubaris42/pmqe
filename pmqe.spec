# PyInstaller spec for pmqe. Build on the target OS (no cross-compiling):
#   Windows: .\build.ps1   -> dist\pmqe\pmqe.exe
#   macOS:   ./build.sh    -> dist/pmqe.app
#   Linux:   ./build.sh    -> dist/pmqe/pmqe
# Verify a build with:  <executable> --self-test report.json

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))
from pmqe import __version__  # noqa: E402

BUILD = ROOT / "build" / "assets"
BUILD.mkdir(parents=True, exist_ok=True)


def make_icons():
    """Render the app mark (pmqe.ui.theme.app_icon_pixmap) to .ico / .icns."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])  # noqa: F841
    from pmqe.ui.theme import app_icon_pixmap

    png = BUILD / "pmqe-1024.png"
    app_icon_pixmap(1024).save(str(png))
    img = Image.open(png)
    ico = BUILD / "pmqe.ico"
    img.save(ico, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    icns = None
    if sys.platform == "darwin":
        icns = BUILD / "pmqe.icns"
        img.save(icns)
    return str(ico), str(icns) if icns else None


def version_file():
    """Windows file properties (version, product name)."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
        VSVersionInfo,
    )

    nums = tuple(int(x) for x in (__version__.split(".") + ["0", "0", "0"])[:4])
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "pmqe"),
                StringStruct("FileDescription", "pmqe — PDF & Image Multi-Query Extractor"),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", "pmqe"),
                StringStruct("OriginalFilename", "pmqe.exe"),
                StringStruct("ProductName", "pmqe"),
                StringStruct("ProductVersion", __version__),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    path = BUILD / "version.txt"
    path.write_text(str(info), encoding="utf-8")
    return str(path)


ICO, ICNS = make_icons()

datas, binaries, hiddenimports = [], [], []
if sys.platform == "win32":
    # Imported lazily inside the OCR code, so invisible to static analysis.
    hiddenimports += [
        "winrt.windows.media.ocr",
        "winrt.windows.graphics.imaging",
        "winrt.windows.security.cryptography",
        "winrt.windows.storage.streams",
        "winrt.windows.foundation",
        "winrt.windows.foundation.collections",
    ]
# RapidOCR ships ONNX models and YAML config as package data
for pkg in ("rapidocr_onnxruntime", "pillow_heif"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["pmqe/__main__.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick"],
    noarchive=False,
)
# Drop large libraries a widgets app that only reads images never loads:
# OpenCV's video I/O, Qt's software OpenGL, and QML/Quick/PDF/virtual-keyboard
# libraries that some Qt plugins drag in. `--self-test` verifies the result.
_UNUSED = (
    "opencv_videoio_ffmpeg", "opengl32sw", "qt6quick", "qt6qml", "qt6virtualkeyboard",
    "qt6pdf", "qtvirtualkeyboard", "qpdf", "qtquick", "qml",
)
a.binaries = [b for b in a.binaries if not any(u in b[0].lower().replace("\\", "/").split("/")[-1] for u in _UNUSED)]
a.datas = [d for d in a.datas if "/qml/" not in d[0].replace("\\", "/").lower()]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pmqe",
    console=False,
    disable_windowed_traceback=False,
    icon=ICO if sys.platform == "win32" else None,
    version=version_file() if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="pmqe")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="pmqe.app",
        icon=ICNS,
        bundle_identifier="com.fubaris42.pmqe",
        version=__version__,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSRequiresAquaSystemAppearance": False,  # follow dark mode
        },
    )
