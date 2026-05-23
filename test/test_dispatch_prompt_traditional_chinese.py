from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dispatch  # type: ignore  # noqa: E402


def test_subagent_prompt_enforces_traditional_chinese_no_simplified() -> None:
    prompt = dispatch.build_subagent_prompt(
        chapter_label="6",
        book_title="The Next Renaissance",
        target_lang="zh-tw",
        glossary={
            "characters": {},
            "places": {},
            "terms": {},
            "style_anchor": {"register": "non_fiction_narrative", "avoid": [], "prefer": []},
        },
        style_sample="這是一段繁體中文樣本。",
        carryover="",
        chapter_html="<p>AI changes institutions.</p>",
    )

    assert "Traditional Chinese" in prompt
    assert "Taiwan locale" in prompt
    assert "禁止使用任何簡體字" in prompt
    assert "『学』→『學』" in prompt
    assert "『为』→『為』" in prompt
    assert "『这』→『這』" in prompt
    assert "『们』→『們』" in prompt
    assert "『实』→『實』" in prompt


def test_ollama_chunk_prompt_enforces_traditional_chinese_no_simplified() -> None:
    system, user = dispatch.build_ollama_chunk_prompt(
        chunk_paragraphs=["AI changes institutions."],
        target_lang="zh-tw",
    )

    assert "輸出必須使用繁體中文" in system
    assert "Traditional Chinese / Taiwan locale" in system
    assert "禁止使用任何簡體字" in system
    assert "『时』→『時』" in system
    assert "『国』→『國』" in system
    assert "[[PARA_1]]" in user


def test_minimal_paragraph_prompt_enforces_traditional_chinese_no_simplified() -> None:
    system, user = dispatch.build_minimal_paragraph_prompt(
        paragraph="AI changes institutions.",
        target_lang="zh-tw",
    )

    assert "輸出必須使用繁體中文" in system
    assert "Traditional Chinese / Taiwan locale" in system
    assert "禁止使用任何簡體字" in system
    assert "『学』→『學』" in system
    assert "『实』→『實』" in system
    assert user == "AI changes institutions."
