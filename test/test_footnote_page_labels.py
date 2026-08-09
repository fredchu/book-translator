"""Per-chapter footnote pages get a ToC label without changing whether they translate.

These pages open with the footnote text itself, so there is no title to extract and
the ToC showed a sentence fragment ("* Some AI researchers assert that…"). The label
is derived from the filename instead.

The critical constraint: this must NOT feed into infer_role. Classifying these as
"notes" would flip output_strategy to source_only and silently stop translating 12
pages of real content.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import extract_epub  # noqa: E402
import nav_builder  # noqa: E402


def test_filename_yields_a_label() -> None:
    fn = extract_epub._footnote_page_heading
    assert fn("Superagency_FN001.xhtml", "OEBPS/Text/Superagency_FN001.xhtml") == "Footnote 1"
    assert fn("", "Text/fn12.xhtml") == "Footnote 12"
    assert fn("footnote-3", "") == "Footnote 3"


def test_ordinary_pages_are_untouched() -> None:
    fn = extract_epub._footnote_page_heading
    assert fn("Superagency_Ch004.xhtml", "OEBPS/Text/Superagency_Ch004.xhtml") is None
    assert fn("Superagency_Introduction.xhtml", "") is None
    # a word merely containing the letters must not match
    assert fn("confounding7.xhtml", "") is None
    assert fn("Superagency_FN.xhtml", "") is None, "needs a number"


def test_role_inference_is_not_affected() -> None:
    """The guard that keeps these pages translatable."""
    role = extract_epub.infer_role(
        src_idref="Superagency_FN001.xhtml",
        src_href="OEBPS/Text/Superagency_FN001.xhtml",
        first_heading="* Some AI researchers assert that confabulation is a better term.",
    )
    assert role == "body", "footnote pages must stay body so they keep being translated"
    assert extract_epub.default_output_strategy(role, char_count=400) == "translate"


def test_chinese_label_pairs_with_the_english_one() -> None:
    assert nav_builder._nav_zh_label("Footnote 1", {}) == "註腳 1"
    assert nav_builder._nav_zh_label("Footnote 12", {}) == "註腳 12"


def test_explicit_override_still_wins() -> None:
    entry = {"original_idref": "Superagency_FN001.xhtml",
             "_translations_extra": {"nav_overrides": {"Superagency_FN001.xhtml": "自訂標籤"}}}
    assert nav_builder._nav_zh_label("Footnote 1", entry) == "自訂標籤"


def test_prose_beginning_with_the_word_is_not_matched() -> None:
    label = nav_builder._nav_zh_label("Footnote numbering restarts each chapter", {})
    assert label != "註腳 1"


def test_prefix_labels_catch_run_on_front_matter() -> None:
    """Praise pages run title straight into a blurb, so exact matching cannot work."""
    long_first = ('Praise for Superagency “Artificial Intelligence is a set of ideas '
                  'humanity has pursued for decades…')
    assert nav_builder._nav_zh_label(long_first, {}) == "各界讚譽"
    assert nav_builder._nav_zh_label("Also by Reid Hoffman The Startup of You", {}) == "作者其他著作"


def test_prefix_label_does_not_hijack_a_real_chapter() -> None:
    assert nav_builder._nav_zh_label("CHAPTER 4: THE TRIUMPH OF THE PRIVATE COMMONS", {}) != "各界讚譽"
