"""Query preparation, normalisation and matching."""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from pmqe.text import (
    Matcher,
    fold,
    highlight_spans,
    match_form,
    normalize_text,
    parse_query_file,
    prepare_queries,
    query_tokens,
    safe_folder_name,
)


def match(queries: list[str], pages: dict[int, str], whole_words: bool = True, path: str = "f.pdf"):
    prepared = prepare_queries(queries).queries
    return Matcher(prepared, whole_words).match_pages(pages, path, "h" * 32)


# -- normalisation ------------------------------------------------------------


def test_normalize_splits_ligatures_and_drops_soft_hyphens():
    assert normalize_text("ﬁnancial") == "financial"
    assert normalize_text("in­voice\x00") == "invoice"
    assert normalize_text("ＩＮＶＯＩＣＥ") == "INVOICE"  # full-width


def test_fold_preserves_length():
    s = "İstanbul ÉCOLE"
    assert len(fold(s)) == len(s)
    assert fold(s) == "istanbul école"


def test_match_form_joins_words_hyphenated_across_lines():
    form = match_form("the invoi-\nce and self-\nemployed")
    assert "invoice" in form and "self-employed" in form


# -- queries ---------------------------------------------------------------------


def test_query_tokens_lowercases_dedupes_and_drops_single_chars():
    assert query_tokens("Invoice A 42 Total invoice") == ["invoice", "42", "total"]
    assert query_tokens("a b c") == []
    assert query_tokens("") == []


def test_safe_folder_name():
    assert safe_folder_name('inv/oice: "total"?') == "invoice total"
    assert safe_folder_name("???") == "untitled_query"
    assert safe_folder_name("plain_name-1") == "plain_name-1"
    assert safe_folder_name("CON") == "CON_"  # reserved on Windows
    assert len(safe_folder_name("x" * 300)) == 80


def test_prepare_queries_drops_duplicates_and_unusable():
    p = prepare_queries(
        ["  Invoice   Total ", "invoice total", "total invoice", "a b", "", "audit"]
    )
    assert [q.text for q in p.queries] == ["Invoice Total", "audit"]
    assert p.duplicates == ["invoice total", "total invoice"]
    assert p.unusable == ["a b"]


def test_prepare_queries_gives_case_insensitively_unique_folders():
    p = prepare_queries(["AT&T bill", "ATT bill", "att-bill"])
    folders = [q.folder for q in p.queries]
    assert len({f.casefold() for f in folders}) == 3
    assert folders[0] == "ATT bill"


@pytest.mark.parametrize(
    "data",
    [
        codecs.BOM_UTF8 + "invoice total\nﬁnance\n".encode(),
        codecs.BOM_UTF16_LE + "invoice total\nﬁnance\n".encode("utf-16-le"),
        "invoice total\nﬁnance\n".encode(),
    ],
    ids=["utf8-bom", "utf16-bom", "utf8"],
)
def test_parse_query_file_handles_boms(tmp_path: Path, data: bytes):
    f = tmp_path / "q.txt"
    f.write_bytes(data)
    lines = parse_query_file(f)
    assert lines[0] == "invoice total"  # no invisible BOM glued to the first query
    assert match(lines, {0: "the invoice total"})[0]["invoice total"] == 1


def test_parse_query_file_skips_blank_and_section_lines(tmp_path: Path):
    f = tmp_path / "q.txt"
    f.write_text("[Customers]\nAaron Hawkins\n\n  Bart Simpson  \n[End]\n", encoding="utf-8")
    assert parse_query_file(f) == ["Aaron Hawkins", "Bart Simpson"]


def test_parse_query_file_falls_back_for_legacy_encodings(tmp_path: Path):
    f = tmp_path / "q.txt"
    f.write_bytes("café crème\n".encode("cp1252"))
    assert parse_query_file(f)[0].startswith("caf")


# -- matching --------------------------------------------------------------------


def test_full_hit_is_auto_approved():
    counts, items = match(["total invoice"], {0: "The Total Invoice amount is due"})
    assert counts == {"total invoice": 1}
    assert [(i.auto_approved, i.missing_words) for i in items] == [(True, [])]


def test_partial_hit_is_a_candidate():
    counts, items = match(["total invoice"], {0: "the total is here"})
    assert counts == {}
    assert [(i.auto_approved, i.missing_words) for i in items] == [(False, ["invoice"])]


def test_single_word_query_has_no_partial_candidates():
    assert match(["invoice"], {0: "nothing relevant"}) == ({}, [])


@pytest.mark.parametrize(
    ("query", "text", "hit"),
    [
        ("art", "start of the cartel", False),
        ("art", "Art, and more art.", True),
        ("total", "subtotal 5", False),
        ("total", "Total: 5", True),
        ("38461", "order 384610", False),
        ("38461", "order #38461.", True),
        ("at&t", "billed by AT&T today", True),
        ("100%", "100%s", True),  # a non-word edge needs no boundary
        ("東京", "東京都", True),  # no word boundaries in CJK text
    ],
)
def test_whole_word_matching(query: str, text: str, hit: bool):
    counts, _ = match([query], {0: text})
    assert bool(counts) is hit


def test_substring_mode_matches_inside_words():
    counts, _ = match(["art"], {0: "start"}, whole_words=False)
    assert counts == {"art": 1}


def test_ligatures_and_hyphenation_match():
    assert match(["financial"], {0: "ﬁnancial report"})[0] == {"financial": 1}
    assert match(["invoice"], {0: "an invoi-\nce here"})[0] == {"invoice": 1}


def test_counts_multiple_pages():
    counts, items = match(["invoice"], {0: "an invoice", 1: "no match", 2: "INVOICE again"})
    assert counts == {"invoice": 2}
    assert [i.page_idx for i in items] == [0, 2]


def test_duplicate_queries_do_not_double_count():
    counts, items = match(["invoice", "Invoice"], {0: "invoice"})
    assert counts == {"invoice": 1}
    assert len(items) == 1


def test_item_ids_differ_for_identical_copies_in_different_places():
    _, a = match(["invoice"], {0: "invoice"}, path="a/doc.pdf")
    _, b = match(["invoice"], {0: "invoice"}, path="b/doc.pdf")
    assert a[0].id != b[0].id


def test_no_usable_tokens_matches_nothing():
    m = Matcher([], whole_words=True)
    assert m.match_pages({0: "anything"}, "f.pdf", "h") == ({}, [])


def test_highlight_spans_follow_whole_word_rules():
    text = normalize_text("Start the art; ARTﬁ")
    assert [text[s:e] for s, e in highlight_spans(text, ["art"])] == ["art"]
    assert len(highlight_spans(text, ["art"], whole_words=False)) == 3


def test_highlight_spans_merge_overlaps():
    assert highlight_spans("invoice", ["invoice", "voice"], whole_words=False) == [(0, 7)]
