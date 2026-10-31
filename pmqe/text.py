"""Query preparation and page matching.

Matching is case-insensitive and, by default, whole-word: the query word
``art`` matches "art" and "Art." but not "start". Text is NFKC-normalised
first, which splits PDF ligatures (``ﬁ`` → ``fi``) and folds full-width
characters, and words hyphenated across a line break are matched in their
joined form too.
"""

from __future__ import annotations

import codecs
import hashlib
import locale
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import ahocorasick

from .models import ReviewItem

MIN_TOKEN_LEN = 2
MAX_FOLDER_CHARS = 80

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_HYPHEN_BREAK = re.compile(r"(\w+)-[ \t]*\r?\n[ \t]*(\w+)")
_HAS_HYPHEN_BREAK = re.compile(r"-[ \t]*\r?\n")


def normalize_text(text: str) -> str:
    """The form text is displayed and matched in: NFKC, without soft
    hyphens or NUL characters."""
    if not text:
        return ""
    if text.isascii():  # NFKC leaves ASCII unchanged; skip the costly call
        return text.replace("\x00", "") if "\x00" in text else text
    return unicodedata.normalize("NFKC", text).replace("­", "").replace("\x00", "")


def fold(text: str) -> str:
    """Lower-case ``text`` without changing its length, so offsets found in
    the folded string are valid in the original. (U+0130 is the only code
    point whose ``lower()`` is longer than one character.)"""
    return text.replace("İ", "I").lower()


def match_form(text: str) -> str:
    """Folded text plus the joined forms of words hyphenated across lines.

    The extras go after the real text, so offsets into the first
    ``len(text)`` characters still line up with the displayed text.
    """
    folded = fold(normalize_text(text))
    if not _HAS_HYPHEN_BREAK.search(folded):  # cheap literal scan first
        return folded
    extras = [f"{m[1]}{m[2]} {m[1]}-{m[2]}" for m in _HYPHEN_BREAK.finditer(folded)]
    return folded + "\n" + " ".join(extras)


# Scripts written without spaces between words: whole-word matching would
# make every query in them fail, so word boundaries are not enforced there.
_NO_SPACE_RANGES = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x2E80, 0x9FFF),  # CJK radicals, kana, CJK ideographs
    (0xA000, 0xA4CF),  # Yi
    (0xAC00, 0xD7AF),  # Hangul (particles attach to words)
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x20000, 0x3FFFF),  # CJK extensions
)


def is_word_char(c: str) -> bool:
    """Letters, digits, combining marks and ``_`` — except in scripts that
    do not separate words with spaces."""
    if c.isascii():
        return c.isalnum() or c == "_"
    if unicodedata.category(c)[0] not in "LNM":
        return False
    o = ord(c)
    return not any(lo <= o <= hi for lo, hi in _NO_SPACE_RANGES)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def query_tokens(query: str) -> list[str]:
    """Distinct folded words of a query that are long enough to match."""
    words = fold(normalize_text(query)).split()
    return list(dict.fromkeys(w for w in words if len(w) >= MIN_TOKEN_LEN))


@dataclass(frozen=True)
class Query:
    text: str  # as entered, whitespace-collapsed
    tokens: tuple[str, ...]
    folder: str  # unique output folder name


@dataclass
class PreparedQueries:
    queries: list[Query] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)  # same words as an earlier query
    unusable: list[str] = field(default_factory=list)  # no word of 2+ characters


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}


def safe_folder_name(name: str) -> str:
    """A folder name that is valid on Windows, macOS and Linux."""
    kept = "".join(c for c in name if c.isalnum() or c in " _-")
    cleaned = " ".join(kept.split())[:MAX_FOLDER_CHARS].strip()
    if not cleaned:
        return "untitled_query"
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned += "_"
    return cleaned


def prepare_queries(lines: Iterable[str]) -> PreparedQueries:
    """Clean user-entered queries.

    Blank lines are skipped, queries with the same words as an earlier one
    are dropped (they would double-count hits), and every query gets an
    output folder name that is unique even on case-insensitive filesystems.
    """
    out = PreparedQueries()
    seen: set[tuple[str, ...]] = set()
    used_folders: set[str] = set()
    for raw in lines:
        text = " ".join(normalize_text(raw).split())
        if not text:
            continue
        tokens = tuple(query_tokens(text))
        if not tokens:
            out.unusable.append(text)
            continue
        key = tuple(sorted(tokens))
        if key in seen:
            out.duplicates.append(text)
            continue
        seen.add(key)
        base = safe_folder_name(text)
        folder, n = base, 2
        while folder.casefold() in used_folders:
            folder = f"{base} ({n})"
            n += 1
        used_folders.add(folder.casefold())
        out.queries.append(Query(text, tokens, folder))
    return out


def decode_text_file(data: bytes) -> str:
    """Decode a text file honouring BOMs (UTF-8, UTF-16) and falling back to
    the system code page for legacy non-UTF-8 files."""
    for bom, enc in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom) :].decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "latin-1", errors="replace")


def parse_query_file(path: str | Path) -> list[str]:
    """Queries from a text file: one per line; blank lines and lines starting
    with ``[`` (section headers) are ignored."""
    text = decode_text_file(Path(path).read_bytes())
    lines = (ln.strip() for ln in text.splitlines())
    return [ln for ln in lines if ln and not ln.startswith("[")]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _build_automaton(tokens: Iterable[str]) -> ahocorasick.Automaton | None:
    automaton = ahocorasick.Automaton()
    for t in tokens:
        automaton.add_word(t, t)
    if len(automaton) == 0:
        return None  # an empty automaton cannot be iterated
    automaton.make_automaton()
    return automaton


def _iter_hits(
    automaton: ahocorasick.Automaton | None,
    haystack: str,
    edges: Mapping[str, tuple[bool, bool]],
    whole_words: bool,
) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start, end_exclusive, token)`` for every accepted occurrence."""
    if automaton is None:
        return
    n = len(haystack)
    for end, token in automaton.iter(haystack):
        start = end - len(token) + 1
        if whole_words:
            check_start, check_end = edges[token]
            if check_start and start > 0 and is_word_char(haystack[start - 1]):
                continue
            if check_end and end + 1 < n and is_word_char(haystack[end + 1]):
                continue
        yield start, end + 1, token


class Matcher:
    """Matches a fixed set of queries against page text."""

    def __init__(self, queries: Sequence[Query], whole_words: bool = True) -> None:
        self.queries = list(queries)
        self.whole_words = whole_words
        self._queries_by_token: dict[str, set[int]] = defaultdict(set)
        for qi, q in enumerate(self.queries):
            for t in q.tokens:
                self._queries_by_token[t].add(qi)
        self._automaton = _build_automaton(self._queries_by_token)
        # Only enforce a boundary on a token edge that is itself a word char:
        # "at&t" needs boundaries, the "%" end of "100%" does not.
        self._edges = {t: (is_word_char(t[0]), is_word_char(t[-1])) for t in self._queries_by_token}

    def tokens_found(self, text: str) -> set[str]:
        found: set[str] = set()
        if self._automaton is None:
            return found
        hay = match_form(text)
        if not self.whole_words:
            found.update(token for _, token in self._automaton.iter(hay))
            return found
        # Hot loop: _iter_hits inlined, and tokens already found are skipped
        # before their (comparatively costly) boundary checks.
        n, edges = len(hay), self._edges
        for end, token in self._automaton.iter(hay):
            if token in found:
                continue
            check_start, check_end = edges[token]
            start = end - len(token) + 1
            if check_start and start > 0 and is_word_char(hay[start - 1]):
                continue
            if check_end and end + 1 < n and is_word_char(hay[end + 1]):
                continue
            found.add(token)
        return found

    def match_pages(
        self,
        page_texts: Mapping[int, str],
        file_path: str,
        file_hash: str,
        page_count: int | None = None,
    ) -> tuple[dict[str, int], list[ReviewItem]]:
        """Match every query against every page.

        Returns (approved-page counts per query, review items). A page is
        auto-approved for a query when all of the query's words are on it;
        a page with some but not all words of a multi-word query becomes a
        candidate for manual review.
        """
        counts: dict[str, int] = {}
        items: list[ReviewItem] = []
        page_count = page_count if page_count is not None else len(page_texts)
        # Distinguishes identical copies of a file stored in different places
        path_tag = hashlib.blake2b(
            file_path.encode("utf-8", "surrogatepass"), digest_size=4
        ).hexdigest()
        for page_idx in sorted(page_texts):
            found = self.tokens_found(page_texts[page_idx])
            if not found:
                continue
            touched = set().union(*(self._queries_by_token[t] for t in found))
            for qi in sorted(touched):
                q = self.queries[qi]
                missing = [t for t in q.tokens if t not in found]
                approved = not missing
                if approved:
                    counts[q.text] = counts.get(q.text, 0) + 1
                items.append(
                    ReviewItem(
                        id=f"{qi}:{file_hash}:{path_tag}:{page_idx}",
                        query=q.text,
                        query_idx=qi,
                        folder=q.folder,
                        file_path=file_path,
                        file_hash=file_hash,
                        page_idx=page_idx,
                        page_count=page_count,
                        auto_approved=approved,
                        missing_words=missing,
                    )
                )
        return counts, items


def highlight_spans(
    text: str, tokens: Iterable[str], whole_words: bool = True
) -> list[tuple[int, int]]:
    """Merged ``(start, end)`` spans of ``tokens`` inside ``text``.

    ``text`` must already be :func:`normalize_text`-ed (that is what the
    review panel displays), so the spans index straight into it.
    """
    toks = [t for t in dict.fromkeys(tokens) if t]
    automaton = _build_automaton(toks)
    edges = {t: (is_word_char(t[0]), is_word_char(t[-1])) for t in toks}
    hay = fold(text)
    spans = sorted((s, e) for s, e, _ in _iter_hits(automaton, hay, edges, whole_words))
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged
