#!/usr/bin/env bash
# Build a distributable pmqe executable with PyInstaller (macOS / Linux).
#
# Usage:
#   ./build.sh          incremental build
#   ./build.sh --clean  wipe dist/ and build/ first
#
# Output: dist/pmqe/pmqe  (on macOS, PyInstaller also produces dist/pmqe.app).
# Must be built on the target OS — PyInstaller does not cross-compile.

set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "--clean" ]]; then
    rm -rf dist build
fi

uv run --with pyinstaller pyinstaller pmqe.spec --noconfirm

exe="dist/pmqe/pmqe"
if [[ ! -x "$exe" ]]; then
    echo "Build finished but $exe is missing" >&2
    exit 1
fi

size="$(du -sh dist/pmqe | cut -f1)"
echo "Build complete: $exe (folder size $size)"
