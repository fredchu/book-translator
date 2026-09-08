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


# --- fourth shape: mixed-case title split across three marker blocks -----------
# The Mind-Gut Connection splits every chapter heading into <p>Chapter</p> +
# <p>1</p> + <p>The Mind-Body Connection Is Real</p>, all Title Case. The
# all-caps rule above (correctly) rejects it, so all 20 ToC entries fell back to
# "first 80 chars of body text" and rendered as
#   "Chapter 1 The Mind-Body Connection Is Real W hen I started medical school in 197"
# — the stray "W hen" being the source's drop-cap span. Requiring the FIRST block
# to be a bare chapter marker is what keeps relaxing the case rule safe: an
# epigraph never opens with the word "Chapter".

THREE_BLOCK = ('<p class="h2-c">Chapter</p>'
               '<p class="h2-c1">1</p>'
               '<p class="h2-c2">The Mind-Body Connection Is Real</p>'
               '<p class="noindent">When I started medical school in 1970, doctors looked at '
               'the human body as a complicated machine with a finite number of parts.</p>')
THREE_BLOCK_LONG_TITLE = ('<p class="h2-c">Chapter</p>'
                          '<p class="h2-c1">5</p>'
                          '<p class="h2-c2">Unhealthy Memories: The Effects of Early Life '
                          'Experiences on the Gut-Brain Dialogue</p>'
                          '<p>The gut is exquisitely sensitive to emotions.</p>')
PART_WITH_NUMBER = ('<p class="pt">Part 1</p>'
                    '<p class="pt2">Our Body, the Intelligent Supercomputer</p>')
MARKER_WITHOUT_TITLE = ('<p class="h2-c">Chapter</p>'
                        '<p class="h2-c1">5</p>'
                        '<p>The gut is exquisitely sensitive to emotions, and it responds to '
                        'them in ways we are only beginning to map with any precision at all.</p>')
MIXED_CASE_EPIGRAPH = ('<p>All truths are easy to understand once discovered.</p>'
                       '<p>Galileo Galilei</p>')


def test_three_block_mixed_case_chapter_is_recovered() -> None:
    assert extract_epub.styled_paragraph_title(_soup(THREE_BLOCK)) == (
        "Chapter 1: The Mind-Body Connection Is Real")


def test_long_title_block_is_not_truncated() -> None:
    """The marker and number are short; the title itself may run long."""
    assert extract_epub.styled_paragraph_title(_soup(THREE_BLOCK_LONG_TITLE)) == (
        "Chapter 5: Unhealthy Memories: The Effects of Early Life Experiences "
        "on the Gut-Brain Dialogue")


def test_marker_already_carrying_its_number_joins_two_blocks() -> None:
    assert extract_epub.styled_paragraph_title(_soup(PART_WITH_NUMBER)) == (
        "Part 1: Our Body, the Intelligent Supercomputer")


def test_marker_without_a_title_block_yields_nothing() -> None:
    """"Chapter: 5" is worse than falling through to the existing fallbacks."""
    assert extract_epub.styled_paragraph_title(_soup(MARKER_WITHOUT_TITLE)) is None


def test_mixed_case_epigraph_still_rejected() -> None:
    """The relaxed case rule must not reopen the epigraph hole."""
    assert extract_epub.styled_paragraph_title(_soup(MIXED_CASE_EPIGRAPH)) is None


# --- fifth shape: a lone Title Case heading sitting on top of prose -----------
# Preface / Bibliography / Index / "Praise for <Title>" have no heading tag, are
# Title Case (so the all-caps rule rejects them) and carry no chapter marker.
# All four shipped in a real ToC as "Index The pagination of this digital edition
# does not match the print edition fr". The discriminator is the SECOND block:
# a heading is followed by prose, and prose ends in a full stop.

LONE_HEADING = ('<p class="h1">Preface</p>'
                '<p>Since the initial publication of The Mind-Gut Connection in the summer '
                'of 2016, the science has moved considerably faster than I expected.</p>')
LONE_HEADING_LONG_SECOND = ('<p class="h1">Bibliography</p>'
                            '<p>Aagaard, Kjersti, Jun Ma, Kathleen M. Antony, Radhika Ganu, '
                            'Joseph Petrosino, and James Versalovic. "The Placenta Harbors a '
                            'Unique Microbiome." Science Translational Medicine 6 (2014).</p>')
EPIGRAPH_TWO_SHORT = ('<p>My heart is not a home for cowards.</p>'
                      '<p>D. ANTOINETTE FOY</p>')
DEDICATION_SHORT_SECOND = ('<p class="h1">Dedication</p>'
                           '<p>To Minou and Dylan</p>')


def test_lone_heading_above_prose_is_recovered() -> None:
    assert extract_epub.styled_paragraph_title(_soup(LONE_HEADING)) == "Preface"


def test_lone_heading_above_a_long_citation_is_recovered() -> None:
    assert extract_epub.styled_paragraph_title(_soup(LONE_HEADING_LONG_SECOND)) == "Bibliography"


def test_epigraph_is_not_mistaken_for_a_lone_heading() -> None:
    """Its own first block ends in a full stop; the second is an attribution."""
    assert extract_epub.styled_paragraph_title(_soup(EPIGRAPH_TWO_SHORT)) is None


def test_short_second_block_is_not_prose() -> None:
    """Dedication is handled by _ROLE_TO_HEADING; do not guess it here."""
    assert extract_epub.styled_paragraph_title(_soup(DEDICATION_SHORT_SECOND)) is None
