"""Cross-cutting helpers: long paths, per-user directories, atomic writes,
natural sorting, formatting and logging setup. Qt-free."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path

APP_NAME = "pmqe"

log = logging.getLogger("pmqe")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_LONG_PREFIX = "\\\\?\\"
_LONG_UNC_PREFIX = "\\\\?\\UNC\\"


def long_path(path: str | os.PathLike[str]) -> str:
    """Absolute path that survives Windows' 260-character MAX_PATH limit.

    Adds the ``\\\\?\\`` prefix (``\\\\?\\UNC\\`` for network shares). A no-op
    on other platforms and for already-prefixed paths.
    """
    p = os.fspath(path)
    if sys.platform != "win32":
        return os.path.abspath(p)
    if p.startswith(_LONG_PREFIX):
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\"):  # \\server\share\... (abspath keeps UNC form)
        return _LONG_UNC_PREFIX + p[2:]
    return _LONG_PREFIX + p


def display_path(path: str | os.PathLike[str]) -> str:
    """Inverse of :func:`long_path`: the path as a user would type it."""
    p = os.fspath(path)
    if p.startswith(_LONG_UNC_PREFIX):
        return "\\\\" + p[len(_LONG_UNC_PREFIX) :]
    if p.startswith(_LONG_PREFIX):
        return p[len(_LONG_PREFIX) :]
    return p


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True when ``path`` is ``root`` or inside it (case-insensitive on Windows)."""
    p = os.path.normcase(display_path(os.path.abspath(display_path(path))))
    r = os.path.normcase(display_path(os.path.abspath(display_path(root))))
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def _env_dir(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def user_cache_dir() -> Path:
    """Per-user cache directory (override with ``PMQE_CACHE_DIR``)."""
    override = _env_dir("PMQE_CACHE_DIR")
    if override:
        return override
    home = Path.home()
    if sys.platform == "win32":
        base = _env_dir("LOCALAPPDATA") or home / "AppData" / "Local"
        return base / APP_NAME / "Cache"
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / APP_NAME
    return (_env_dir("XDG_CACHE_HOME") or home / ".cache") / APP_NAME


def user_log_dir() -> Path:
    """Per-user log directory (override with ``PMQE_LOG_DIR``)."""
    override = _env_dir("PMQE_LOG_DIR")
    if override:
        return override
    home = Path.home()
    if sys.platform == "win32":
        base = _env_dir("LOCALAPPDATA") or home / "AppData" / "Local"
        return base / APP_NAME / "Logs"
    if sys.platform == "darwin":
        return home / "Library" / "Logs" / APP_NAME
    return (_env_dir("XDG_STATE_HOME") or home / ".local" / "state") / APP_NAME


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def atomic_write(dest: str | os.PathLike[str], write: Callable[[str], None]) -> None:
    """Call ``write(tmp_path)`` then atomically move the result onto ``dest``.

    Readers never see a half-written file, and a crash or cancellation
    leaves at most a stray ``.tmp`` file behind instead of a corrupt output.
    """
    dest = long_path(dest)
    tmp = f"{dest}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        write(tmp)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def write_bytes_atomic(dest: str | os.PathLike[str], data: bytes) -> None:
    def _write(tmp: str) -> None:
        with open(tmp, "wb") as fh:
            fh.write(data)

    atomic_write(dest, _write)


def copy_atomic(src: str | os.PathLike[str], dest: str | os.PathLike[str]) -> None:
    atomic_write(dest, lambda tmp: shutil.copy2(long_path(src), tmp))


def ensure_dir(path: str | os.PathLike[str]) -> None:
    os.makedirs(long_path(path), exist_ok=True)


def remove_file(path: str | os.PathLike[str]) -> bool:
    """Delete a file if it exists. Returns True when something was removed."""
    try:
        os.remove(long_path(path))
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Sorting and formatting
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Sort key that orders ``p2`` before ``p10``.

    ``re.split`` with a capture group alternates text/number parts, so two
    keys always compare str-with-str and int-with-int at the same index.
    """
    parts = _DIGITS.split(text)
    return tuple(int(p) if i % 2 else p.casefold() for i, p in enumerate(parts))


def format_duration(seconds: float) -> str:
    """``0:07``, ``12:34`` or ``1:02:03``."""
    s = max(0, round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (plural_word or word + 's')}"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(processName)s] %(name)s: %(message)s"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3
_configured = False


def configure_logging() -> Path | None:
    """Application logging: stderr (when there is one) plus a rotating file
    in :func:`user_log_dir`. Idempotent. Returns the log file path, if any.

    ``PMQE_LOG_LEVEL`` (e.g. ``DEBUG``) raises verbosity.
    """
    global _configured
    root = logging.getLogger()
    level = getattr(logging, os.environ.get("PMQE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(level)
    for noisy in ("PIL", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log_file: Path | None = user_log_dir() / "pmqe.log"
    if _configured:
        return log_file
    fmt = logging.Formatter(LOG_FORMAT)
    # Frozen windowed apps have no stderr at all.
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    try:
        assert log_file is not None
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        log.warning("Could not open log file %s: %s", log_file, exc)
        log_file = None
    _configured = True
    return log_file
