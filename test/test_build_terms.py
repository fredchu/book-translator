from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import build_terms  # noqa: E402


def _unit(text: str, path: str = "Text/chapter.xhtml", reason=None) -> dict:
    return {"text": text, "path": path, "excluded_reason": reason}


def _zipf(term: str) -> float:
    scores = {
        "gut": 4.5,
        "asthma": 3.73,
        "microbiota": 2.8,
        "microbiome": 2.7,
        "Lee Sedol": 2.0,
    }
    return scores.get(term, scores.get(term.casefold(), 2.5))


def test_build_queue_enforces_existence_boundaries_and_lists_hallucinations() -> None:
    units = [
        _unit("The microbiota differs about asthma; a microbiome is distinct. The gut matters. The hologenome matters."),
        _unit("microbiology appears here, but the chopped key must not match it."),
        _unit("Ghost Institute", "Text/Bibliography.xhtml", "bibliography"),
    ]
    proposals = [
        {"en": "microbiota", "zh": "菌相", "group": "concept", "distinguish_from": ["microbiome"]},
        {"en": "microbiome", "zh": "微生物體", "group": "concept", "distinguish_from": ["microbiota"]},
        {"en": "asthma", "zh": "氣喘", "group": "concept"},
        {"en": "gut", "zh": "腸道", "group": "concept"},
        {"en": "hologenome", "zh": "全基因組", "group": "concept"},
        {"en": "microbio", "zh": "死鍵", "group": "concept"},
        {"en": "Ghost Institute", "zh": "幽靈研究所", "group": "places"},
    ]

    report = build_terms.build_review_queue(units, proposals, zipf=_zipf, max_mechanical_references=0)
    by_key = {row["key"]: row for row in report["ranked_terms"]}

    assert by_key["microbiota"]["decision"] == "needs_user_decision"
    assert "concept_requires_distinction" in by_key["microbiota"]["ranking_reasons"]
    assert by_key["asthma"]["decision"] == "needs_user_decision"
    assert "wordfreq_middle:user_review" in by_key["asthma"]["ranking_reasons"]
    assert by_key["gut"]["decision"] == "excluded_common_word"
    assert by_key["hologenome"]["decision"] == "reference"
    assert {row["key"] for row in report["dropped_proposals"]} == {"microbio", "Ghost Institute"}
    assert report["summary"]["hallucinated_or_nonbody"] == 2


def test_typographic_variants_keep_the_source_spelling_and_are_injectable() -> None:
    text = ("Several weeks passed. WEEKS described Parkinson’s disease and the Arndt–Schulz rule. "
            "CHLOÉ agreed near the Amazon rain forest and an Okinawan village.")
    proposals = [
        {"en": "Parkinson's disease", "zh": "帕金森氏症", "group": "concept"},
        {"en": "Arndt-Schulz rule", "zh": "定律", "group": "concept"},
        {"en": "Chloé", "zh": "克蘿伊", "group": "people"},
        {"en": "Weeks", "zh": "威克斯", "group": "people"},
        {"en": "Amazon Rain Forest", "zh": "亞馬遜雨林", "group": "places"},
        {"en": "Okinawa", "zh": "沖繩", "group": "places"},
    ]
    report = build_terms.build_review_queue([_unit(text)], proposals, zipf=_zipf, max_mechanical_references=0)
    keys = {row["key"] for row in report["ranked_terms"]}

    assert {
        "Parkinson’s disease", "Arndt–Schulz rule", "CHLOÉ", "WEEKS",
        "Amazon rain forest", "Okinawan",
    } <= keys
    assert "weeks" not in keys
    from dispatch import select_terms_for_text
    terms = {row["key"]: row["proposed_zh"] for row in report["ranked_terms"]}
    assert set(select_terms_for_text(terms, text)) == keys


def test_lowercase_proposal_is_not_rewritten_by_first_titlecase_occurrence() -> None:
    report = build_terms.build_review_queue(
        [_unit("Gut feelings\n\nListen to your gut feelings")],
        [{"en": "gut feelings", "zh": "腸道直覺", "group": "concept"}],
        zipf=_zipf,
        max_mechanical_references=0,
    )
    row = report["ranked_terms"][0]
    assert row["key"] == "gut feelings"
    assert row["body_occurrences"] == 2


def test_paired_translation_change_is_primary_rank_signal_not_frequency() -> None:
    units = [_unit("microbiota gut"), _unit("microbiota gut"), _unit("gut")]
    proposals = [
        {"en": "microbiota", "zh": "菌相", "group": "concept"},
        {"en": "gut", "zh": "腸道", "group": "concept", "user_decided": True},
    ]
    comparison = (
        ["microbiota gut", "microbiota gut", "gut"],
        ["微生物群 腸道", "微生物群 腸道", "腸道"],
        ["菌相 腸道", "菌相 腸道", "腸道"],
    )

    report = build_terms.build_review_queue(
        units, proposals, zipf=_zipf, comparison=comparison, max_mechanical_references=0
    )
    rows = report["ranked_terms"]

    assert rows[0]["key"] == "microbiota"
    assert rows[0]["translation_change_count"] == 2
    assert rows[1]["key"] == "gut"
    assert rows[1]["translation_change_count"] == 0
    assert rows[0]["ranking_reasons"][0] == "paired_translation_changed:2"


def test_person_unique_forms_are_kept_and_true_collisions_are_manual() -> None:
    units = [_unit("Dr. Martin met Martin. Alice Miller disagreed with George Miller. Miller then left. Lee Sedol watched.")]
    proposals = [
        {"en": "Martin", "zh": "馬丁", "group": "people"},
        {"en": "Alice Miller", "zh": "愛麗絲・米勒", "group": "people"},
        {"en": "George Miller", "zh": "喬治・米勒", "group": "people"},
        {"en": "Lee Sedol", "zh": "李世乭", "group": "people"},
    ]

    report = build_terms.build_review_queue(units, proposals, zipf=_zipf, max_mechanical_references=20)
    keys = {row["key"] for row in report["ranked_terms"]}

    assert "Dr. Martin" in keys
    assert "Martin" in keys
    assert "Miller" not in keys
    assert any(row["token"] == "miller" for row in report["person_name_collisions"])
    assert report["summary"]["mechanical_person_form_coverage"]["ratio"] >= 0.95
    lee = next(row for row in report["ranked_terms"] if row["key"] == "Lee Sedol")
    assert lee["zh_review"] == "verify_original_hanzi"
    assert lee["decision"] == "needs_user_decision"


def test_same_person_variants_with_conflicting_zh_are_flagged() -> None:
    proposals = [
        {"en": "Emeran A. Mayer", "zh": "譯名甲", "group": "people"},
        {"en": "Emeran Mayer", "zh": "譯名乙", "group": "people"},
    ]
    report = build_terms.build_review_queue(
        [_unit("Emeran A. Mayer spoke. Emeran Mayer replied.")],
        proposals,
        zipf=_zipf,
        max_mechanical_references=0,
    )
    assert any(
        row.get("action") == "user_review_same_entity_translation"
        for row in report["zh_conflicts"]
    )


def test_zh_simplified_trigger_is_flagged_without_rewriting_proposal() -> None:
    proposal = {"en": "microbiome", "zh": "微生物组", "group": "concept"}
    report = build_terms.build_review_queue(
        [_unit("The microbiome matters.")], [proposal], zipf=_zipf, max_mechanical_references=0
    )
    row = report["ranked_terms"][0]
    assert row["proposed_zh"] == "微生物组"
    assert "组" in row["zh_simplified_triggers"]


def test_distinguish_from_outranks_frequency_without_paired_comparison() -> None:
    proposals = [
        {"en": "asthma", "zh": "氣喘", "group": "concept"},
        {"en": "microbiota", "zh": "菌相", "group": "concept", "distinguish_from": ["microbiome"]},
    ]
    units = [_unit("asthma asthma asthma asthma microbiota")]
    report = build_terms.build_review_queue(
        units, proposals, zipf=lambda _: 3.7, max_mechanical_references=0
    )
    assert report["ranked_terms"][0]["key"] == "microbiota"
    assert "concept_requires_distinction" in report["ranked_terms"][0]["ranking_reasons"]


def test_pure_mechanical_middle_single_is_reference_with_honest_tiebreak() -> None:
    report = build_terms.build_review_queue(
        [_unit("developers developers developers")], [],
        zipf=lambda _: 3.8, max_mechanical_references=20,
    )
    row = next(item for item in report["ranked_terms"] if item["key"].casefold() == "developers")
    assert row["decision"] == "reference"
    assert "wordfreq_middle:mechanical_single_reference" in row["ranking_reasons"]
    assert "frequency_tiebreak:not_impact_evidence" in row["ranking_reasons"]


def test_proposal_terms_list_shape_and_opencc_fail_closed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proposals.json"
    path.write_text(json.dumps({"terms": [{"en": "microbiota", "zh": "菌相"}]}), encoding="utf-8")
    assert build_terms.load_proposals([path])[0]["en"] == "microbiota"

    monkeypatch.setattr(build_terms, "_converter", lambda: None)
    with pytest.raises(RuntimeError, match="opencc is required"):
        build_terms.build_review_queue(
            [_unit("microbiota")], [], zipf=_zipf, max_mechanical_references=0
        )


def test_mechanical_extractor_emits_name_subwindows_and_concepts() -> None:
    text = ("OpenAI CEO Sam Altman met Sam Altman. microbiota microbiota microbiota. "
            "brain, gut. brain, gut. brain, gut. the gut. the gut. the gut. "
            "gut feelings. gut feelings. gut feelings. America’s America’s Google’s Google’s.")
    found = build_terms.extract_mechanical_candidates(text, _zipf)
    assert "Sam Altman" in found
    assert "microbiota" in found
    assert "brain gut" not in found
    assert "the gut" not in found
    assert "gut feelings" in found
    assert "America" in found and "America’s" not in found
    assert "Google" in found and "Google’s" not in found


# --- regression: which spelling becomes the key, when the proposal's own is absent ---
# `_preferred_spelling` picks the most frequent form, lowercase winning ties. Both
# rules shipped unguarded: reverting them to "first one found" left all 11 tests
# green, because the existing case test feeds a proposal whose spelling *does*
# exist in the source — that takes the short-circuit branch and never reaches
# `_preferred_spelling` at all. The two paths were covering for each other.
#
# It matters because the injector is case-sensitive for capitalised keys (so the
# person "Weeks" does not match 20 occurrences of "weeks"). Picking the Title
# Case form as the key therefore silently drops every lowercase occurrence:
# a key that should match twice matches once.

AMAZON_TEXT = (
    "The Amazon Rain Forest appeared first in a heading. "
    "Later the Amazon rain forest is discussed at length, and the Amazon rain forest again."
)


def test_key_takes_the_most_frequent_spelling_when_the_proposal_form_is_absent() -> None:
    """Proposal spelling absent from the source, so the short-circuit cannot fire."""
    proposals = [{"en": "amazon rain forest", "zh": "亞馬遜雨林", "group": "places"}]

    report = build_terms.build_review_queue(
        [_unit(AMAZON_TEXT)], proposals, zipf=_zipf, max_mechanical_references=0
    )
    rows = {row["key"]: row for row in report["ranked_terms"]}

    assert "Amazon rain forest" in rows, f"expected the twice-used lowercase form, got {list(rows)}"
    assert "Amazon Rain Forest" not in rows
    # And the chosen key must actually reach both occurrences through the injector.
    assert rows["Amazon rain forest"]["body_occurrences"] == 2


def test_key_takes_the_most_frequent_spelling_even_when_it_is_capitalised() -> None:
    """The count rule must beat the lowercase tiebreak, or neither is tested.

    The companion test above has the majority form *also* being the lowercase
    one, so both rules point at the same answer and removing either leaves the
    suite green — the same mutual-cover shape this file exists to prevent.
    Here the majority form is Title Case, so only the count rule can produce it.
    """
    text = (
        "The Amazon Rain Forest opened the chapter. The Amazon Rain Forest recurred. "
        "Later an Amazon rain forest appeared once."
    )
    proposals = [{"en": "amazon rain forest", "zh": "亞馬遜雨林", "group": "places"}]

    report = build_terms.build_review_queue(
        [_unit(text)], proposals, zipf=_zipf, max_mechanical_references=0
    )
    rows = {row["key"]: row for row in report["ranked_terms"]}

    assert "Amazon Rain Forest" in rows, f"count must win over the lowercase tiebreak, got {list(rows)}"
    assert rows["Amazon Rain Forest"]["body_occurrences"] == 2


def test_proposal_spelling_is_kept_even_when_another_form_is_more_frequent() -> None:
    """The short-circuit: a proposal that exists verbatim is never rewritten.

    Written so the short-circuit and `_preferred_spelling` disagree — the
    proposal appears once, the Title Case form twice. Without the short-circuit
    the frequency rule would pick `Gut Feeling`, whose case-sensitive key then
    misses the lowercase sentence entirely (3 occurrences become 2).
    """
    text = "A Gut Feeling opened it. Another Gut Feeling followed. Then a gut feeling returned."
    proposals = [{"en": "gut feeling", "zh": "腸道直覺", "group": "concept"}]

    report = build_terms.build_review_queue(
        [_unit(text)], proposals, zipf=_zipf, max_mechanical_references=0
    )
    rows = {row["key"]: row for row in report["ranked_terms"]}

    assert "gut feeling" in rows, f"the proposal's own spelling must survive, got {list(rows)}"
    assert "Gut Feeling" not in rows
    # Lowercase keys match case-insensitively, so it reaches all three.
    assert rows["gut feeling"]["body_occurrences"] == 3


def test_equal_counts_break_the_tie_towards_the_lowercase_spelling() -> None:
    """With counts equal, only the case tiebreak can decide.

    The two tests above both have a clear majority form, so the count rule
    alone produces the right answer and removing the case tiebreak leaves them
    green. This one removes the count signal entirely: one occurrence each,
    Title Case first. Falling back to insertion order would pick `Gut Feeling`,
    whose case-sensitive key then misses the lowercase sentence.
    """
    text = "A Gut Feeling opened it. Then a gut feeling returned."
    proposals = [{"en": "GUT FEELING", "zh": "腸道直覺", "group": "concept"}]

    report = build_terms.build_review_queue(
        [_unit(text)], proposals, zipf=_zipf, max_mechanical_references=0
    )
    rows = {row["key"]: row for row in report["ranked_terms"]}

    assert "gut feeling" in rows, f"a tie must fall to lowercase, got {list(rows)}"
    assert "Gut Feeling" not in rows
    assert rows["gut feeling"]["body_occurrences"] == 2
