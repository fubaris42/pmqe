#!/usr/bin/env bash
# Build the macOS / Linux app with PyInstaller, then verify it with its self-test.
#
# Usage:
#   ./build.sh              incremental build + self-test
#   ./build.sh --clean      wipe dist/ and build/ first
#   ./build.sh --skip-test  build only
#
# Output: dist/pmqe/pmqe (on macOS also dist/pmqe.app).
# Must be built on the target OS — PyInstaller does not cross-compile.

set -euo pipefail
cd "$(dirname "$0")"

clean=0 test=1
for arg in "$@"; do
    case "$arg" in
        --clean) clean=1 ;;
        --skip-test) test=0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
[[ $clean == 1 ]] && rm -rf dist build

uv run --group build pyinstaller pmqe.spec --noconfirm

exe="dist/pmqe/pmqe"
if [[ ! -x "$exe" ]]; then
    echo "Build finished but $exe is missing" >&2
    exit 1
fi
echo "Built $exe (folder size $(du -sh dist/pmqe | cut -f1))"

if [[ $test == 1 ]]; then
    QT_QPA_PLATFORM=offscreen "$exe" --self-test build/selftest.json
    echo "Self-test passed"
fi
