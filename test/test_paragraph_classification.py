from __future__ import annotations

from bs4 import BeautifulSoup

from scripts.paragraph_classification import (
    is_english_content,
    reasoned_source_only_entries,
    untranslated_reason,
)


def _node(markup: str):
    return BeautifulSoup(markup, "html.parser").find()


def test_untranslated_reason_uses_position_not_missing_sibling():
    bibliography = _node('<p class="bib">Reference</p>')
    prose = _node('<p class="body">Prose</p>')

    assert untranslated_reason("OEBPS/Bibliography.xhtml", bibliography, "Aagaard et al.") == "bibliography"
    assert untranslated_reason("OEBPS/chPS/c03.xhtml", bibliography, "Aagaard et al.") == "bibliography"
    assert untranslated_reason("OEBPS/c03.xhtml", prose, "A long ordinary English paragraph.") is None


def test_untranslated_reason_handles_opaque_notes_page_identifier_and_non_latin():
    node = _node("<p>Text</p>")

    # `b05` was one book's (The Next Renaissance) notes filename hardcoded into a
    # classifier every book shares. A shared rule cannot key on one publisher's
    # opaque stem: it exempts any book that happens to name a chapter b05, and
    # every misread here weakens the gate. That book's notes page must now be
    # declared via source_only instead of recognised by filename.
    assert untranslated_reason("OEBPS/b05.xhtml", node, "Long note") is None
    # Substring matching produced four false exemptions, all weakening the gate.
    assert untranslated_reason("OEBPS/preferences.xhtml", node, "Long note") is None
    assert untranslated_reason("OEBPS/index.xhtml", node, "Long note") is None
    assert untranslated_reason("OEBPS/notes_on_contributors.xhtml", node, "Long note") is None
    # ...without breaking the real pages.
    assert untranslated_reason("OEBPS/86_Bibliography.xhtml", node, "Long note") == "bibliography"
    assert untranslated_reason("OEBPS/Superagency_Notes.xhtml", node, "Long note") == "notes"
    assert untranslated_reason("OEBPS/endnotes_split_006.xhtml", node, "Long note") == "notes"
    assert untranslated_reason("OEBPS/87_Index.xhtml", node, "Long note") == "index"
    assert untranslated_reason("OEBPS/chPS/chapter.xhtml", node, "10") == "identifier"
    assert untranslated_reason("OEBPS/chapter.xhtml", node, "* ἀπορία .") == "non_latin"


def test_reasoned_source_only_entries_rejects_legacy_strings_and_blank_reasons():
    data = [
        "unreasoned",
        {"src_text": "blank", "reason": "  "},
        {"src_text": "reference", "reason": "bibliography"},
    ]

    assert reasoned_source_only_entries(data) == {"reference": "bibliography"}


def test_is_english_content_matches_coverage_threshold():
    assert is_english_content(
        "This is a long English paragraph that requires a translated sibling in the bilingual output."
    )
    assert not is_english_content("1")
    assert not is_english_content("* ἀπορία .")
