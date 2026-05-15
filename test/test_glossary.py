"""Unit tests for glossary.py — parse/canonical/write."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import glossary  # type: ignore  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
REGISTER_HINTS_PATH = ROOT / "assets" / "register_hints.json"
BANNED_AUTHOR_NAMES = ("Mollick", "Orwell", "McCarthy", "Pink", "Duhigg", "Kahneman")

MINIMAL = {
    "characters": {"Napoleon": "拿破崙"},
    "places": {"Manor Farm": "莊園農場"},
    "terms": {"Beasts of England": "英格蘭的野獸"},
    "style_anchor": {
        "register": "literary plain prose",
        "avoid": ["翻譯腔"],
        "prefer": ["短句"],
    },
}


def test_register_hints_file_exists_and_parses():
    assert REGISTER_HINTS_PATH.exists()
    data = json.loads(REGISTER_HINTS_PATH.read_text("utf-8"))
    assert [r["id"] for r in data["registers"]] == [
        "literary_fiction",
        "non_fiction_narrative",
        "academic_technical",
    ]


def test_glossary_prompt_contains_all_register_descriptions():
    data = json.loads(REGISTER_HINTS_PATH.read_text("utf-8"))
    for register in data["registers"]:
        assert register["description"] in glossary.GLOSSARY_PROMPT


def test_glossary_prompt_contains_no_banned_author_names():
    for author_name in BANNED_AUTHOR_NAMES:
        assert author_name not in glossary.GLOSSARY_PROMPT


def test_resolve_register_matches_configured_registers():
    literary = glossary.resolve_register(
        {"style_anchor": {"register": "literary plain prose with fable cadence"}}
    )
    narrative = glossary.resolve_register({"style_anchor": {"register": "商管科普 narrative"}})
    unknown = glossary.resolve_register({"style_anchor": {"register": "totally unknown register"}})

    assert literary and literary["id"] == "literary_fiction"
    assert narrative and narrative["id"] == "non_fiction_narrative"
    assert unknown is None


def test_parse_glossary_plain_json():
    assert glossary.parse_glossary(json.dumps(MINIMAL)) == MINIMAL


def test_parse_glossary_strips_code_fence():
    fenced = "```json\n" + json.dumps(MINIMAL) + "\n```"
    assert glossary.parse_glossary(fenced) == MINIMAL


def test_parse_glossary_strips_unlabeled_fence():
    fenced = "```\n" + json.dumps(MINIMAL) + "\n```"
    assert glossary.parse_glossary(fenced) == MINIMAL


def test_parse_glossary_rejects_missing_top_key():
    bad = {"characters": {}, "places": {}, "terms": {}}  # no style_anchor
    with pytest.raises(ValueError, match="style_anchor"):
        glossary.parse_glossary(json.dumps(bad))


def test_parse_glossary_rejects_missing_anchor_field():
    bad = dict(MINIMAL)
    bad["style_anchor"] = {"register": "x", "avoid": []}  # no prefer
    with pytest.raises(ValueError, match="prefer"):
        glossary.parse_glossary(json.dumps(bad))


def test_canonical_form_trims_whitespace():
    raw = {
        "characters": {" Napoleon ": "  拿破崙  "},
        "places": {},
        "terms": {},
        "style_anchor": {"register": "  x ", "avoid": [" a "], "prefer": [" b "]},
    }
    out = glossary.canonical_form(raw)
    assert out["characters"] == {"Napoleon": "拿破崙"}
    assert out["style_anchor"]["register"] == "x"
    assert out["style_anchor"]["avoid"] == ["a"]


def test_write_glossary_persists_canonical(tmp_path: Path):
    p = tmp_path / "g.json"
    glossary.write_glossary(p, MINIMAL)
    loaded = json.loads(p.read_text("utf-8"))
    assert loaded == glossary.canonical_form(MINIMAL)
