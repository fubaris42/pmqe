# PyInstaller spec for pmqe (cross-platform).
# Windows:  .\build.ps1        -> dist\pmqe\pmqe.exe
# macOS:    ./build.sh         -> dist/pmqe.app
# Must be built on the target OS — PyInstaller does not cross-compile.

import sys

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

if sys.platform == "win32":
    # Imported lazily inside worker functions, invisible to static analysis.
    # winrt only exists on Windows, so keep it off other platforms.
    hiddenimports += [
        "winrt.windows.media.ocr",
        "winrt.windows.graphics.imaging",
        "winrt.windows.security.cryptography",
        "winrt.windows.storage.streams",
        "winrt.windows.foundation",
    ]

# RapidOCR ships ONNX models and YAML config as package data
for pkg in ("rapidocr_onnxruntime", "pillow_heif"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["pmqe/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pmqe",
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="pmqe",
)

# On macOS, wrap the COLLECT tree into a proper .app bundle.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="pmqe.app",
        icon=None,
        bundle_identifier="com.fubaris42.pmqe",
    )
