"""Third title structure: chapter titles styled as <p>, not <h1>-<h6>.

Publishers mark titles with CSS classes (<p class="CN">CHAPTER 4</p> +
<p class="CT">THE TRIUMPH…</p>). Neither the heading-tag scan in extract_epub
nor the heading check in build_nav_overrides saw those, so the ToC fell back to
the first 80 characters of body text on the English side and produced nothing on
the Chinese side. Hit on five separate real books before this fix.

ALL CAPS is the discriminator. Corpus measurement over the locally extracted
books: 52 chapters use the styled-<p> shape and are all-caps; the 43 body-prose
openings are long and mixed-case; the 12 short mixed-case leading blocks are
epigraphs and dedications that must NOT become chapter titles.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from bs4 import BeautifulSoup  # noqa: E402

import extract_epub  # noqa: E402
import offline_postprocess as pp  # noqa: E402


def _soup(body_html: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><body>{body_html}</body></html>", "html.parser")


CHAPTER = ('<p class="CN">CHAPTER 4</p>'
           '<p class="CT">THE TRIUMPH OF THE PRIVATE COMMONS</p>'
           '<p>Sure, superhumane mental health support sounds great, but what is the cost?</p>')
SINGLE = ('<p class="CN">INTRODUCTION</p>'
          '<p>Throughout history, new technologies have regularly sparked visions of doom.</p>')
EPIGRAPH = ('<p>My heart is not a home for cowards.</p>'
            '<p>D. ANTOINETTE FOY</p>')
BODY_FIRST = ('<p>Early in its existence, Google realized that every action users took '
              'on its site was trackable, and that changed everything about the web.</p>')


def test_number_and_title_are_joined() -> None:
    assert extract_epub.styled_paragraph_title(_soup(CHAPTER)) == (
        "CHAPTER 4: THE TRIUMPH OF THE PRIVATE COMMONS")


def test_single_title_block_stays_single() -> None:
    assert extract_epub.styled_paragraph_title(_soup(SINGLE)) == "INTRODUCTION"


def test_mixed_case_epigraph_is_not_a_title() -> None:
    """The dedication/epigraph case that makes ALL CAPS non-negotiable."""
    assert extract_epub.styled_paragraph_title(_soup(EPIGRAPH)) is None


def test_body_prose_is_not_a_title() -> None:
    assert extract_epub.styled_paragraph_title(_soup(BODY_FIRST)) is None


def test_long_all_caps_line_is_not_a_title() -> None:
    shout = "<p>" + "A" * 80 + "</p>"
    assert extract_epub.styled_paragraph_title(_soup(shout)) is None


def test_first_heading_prefers_real_headings_then_falls_back() -> None:
    with_h1 = _soup('<h1>Real Heading</h1><p class="CN">CHAPTER 9</p>')
    assert extract_epub._first_heading(with_h1) == "Real Heading"
    assert extract_epub._first_heading(_soup(CHAPTER)) == (
        "CHAPTER 4: THE TRIUMPH OF THE PRIVATE COMMONS")


def test_nav_block_count_matches_the_english_side() -> None:
    """Both sides of a bilingual label must agree on what a title block is."""
    assert pp._leading_title_block_count(_soup(CHAPTER)) == 2
    assert pp._leading_title_block_count(_soup(SINGLE)) == 1
    assert pp._leading_title_block_count(_soup(EPIGRAPH)) == 0
    assert pp._leading_title_block_count(_soup(BODY_FIRST)) == 0


def test_heading_tags_still_count() -> None:
    assert pp._leading_title_block_count(_soup("<h1>Chapter One</h1><p>Body.</p>")) == 1
