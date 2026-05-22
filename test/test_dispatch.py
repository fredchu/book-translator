"""Unit tests for dispatch.py — prompt builder + paragraph extraction + validation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dispatch  # type: ignore  # noqa: E402


SAMPLE_HTML = """
<html><body>
<nav>skip me</nav>
<header>also skip</header>
<h1>Chapter One</h1>
<p>First paragraph.</p>
<p>Second paragraph.</p>
<blockquote>A quote.</blockquote>
<ul><li>Item one</li><li>Item two</li></ul>
<footer>copyright</footer>
</body></html>
"""


def test_html_to_paragraphs_strips_navigation():
    paras = dispatch.html_to_paragraphs(SAMPLE_HTML)
    # nav, header, footer should be excluded
    assert all("skip me" not in p and "also skip" not in p and "copyright" not in p for p in paras)
    # heading + paragraphs + blockquote + list items included
    assert paras == [
        "Chapter One", "First paragraph.", "Second paragraph.",
        "A quote.", "Item one", "Item two",
    ]


def test_html_to_paragraphs_collapses_whitespace():
    html = "<p>hello\n\n   world  \tfoo</p>"
    assert dispatch.html_to_paragraphs(html) == ["hello world foo"]


def test_chapter_text_for_prompt_joins_with_blank_lines():
    text = dispatch.chapter_text_for_prompt(SAMPLE_HTML)
    assert "Chapter One\n\nFirst paragraph." in text


def test_build_subagent_prompt_includes_all_sections():
    prompt = dispatch.build_subagent_prompt(
        chapter_label="2", book_title="Animal Farm", target_lang="zh-tw",
        glossary={"characters": {"Napoleon": "拿破崙"}, "places": {}, "terms": {},
                  "style_anchor": {"register": "x", "avoid": [], "prefer": []}},
        style_sample="這是第一章開頭。",
        carryover="...上一章結尾...",
        chapter_html="<p>Hello.</p>",
    )
    assert "Chapter 2" in prompt
    assert "Animal Farm" in prompt
    assert "zh-tw" in prompt
    assert "台灣繁體中文" in prompt
    assert "Napoleon" in prompt and "拿破崙" in prompt
    assert "這是第一章開頭" in prompt
    assert "上一章結尾" in prompt
    assert "Hello." in prompt


def test_subagent_prompt_template_includes_legal_context():
    assert "legally obtained" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    assert "legally owns" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    assert "local filesystem" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    assert dispatch.SUBAGENT_PROMPT_TEMPLATE.startswith("CONTEXT:")


def test_subagent_prompt_template_makes_marker_contract_explicit():
    assert "[[PARA_N]]" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    assert "marker" in dispatch.SUBAGENT_PROMPT_TEMPLATE.lower()
    assert "Preserve every marker verbatim" in dispatch.SUBAGENT_PROMPT_TEMPLATE
    # the first characters of output must be [[PARA_1]]
    assert "[[PARA_1]]" in dispatch.SUBAGENT_PROMPT_TEMPLATE


def test_build_subagent_prompt_counts_paragraphs_and_renders_separator_check():
    prompt = dispatch.build_subagent_prompt(
        chapter_label="1", book_title="Test Book", target_lang="zh-tw",
        glossary={"characters": {}, "places": {}, "terms": {},
                  "style_anchor": {"register": "商管科普 narrative", "avoid": [], "prefer": []}},
        style_sample="", carryover="", chapter_html="<p>One.</p><p>Two.</p>",
    )
    assert "Source paragraph count: 2" in prompt
    assert "  10. Before returning, count `[[PARA_` occurrences in" in prompt


def test_build_subagent_prompt_uses_para_markers():
    """Subagent prompt should wrap source paragraphs with [[PARA_N]] markers."""
    glossary = {
        "characters": {}, "places": {}, "terms": {},
        "style_anchor": {"register": "x", "avoid": [], "prefer": []},
    }
    prompt = dispatch.build_subagent_prompt(
        chapter_label="2", book_title="X", target_lang="zh-tw",
        glossary=glossary, style_sample="", carryover="",
        chapter_html="<p>Alpha.</p><p>Beta.</p><p>Gamma.</p>",
    )
    # Source side wrapped with markers
    assert "[[PARA_1]]" in prompt
    assert "[[PARA_2]]" in prompt
    assert "[[PARA_3]]" in prompt
    # marker contract in the requirements
    assert "[[PARA_N]]" in prompt or "[[PARA_" in prompt
    assert "Preserve every [[PARA_" in prompt or "preserve every marker" in prompt.lower()


def test_build_subagent_prompt_marker_count_matches_source():
    """Marker count == source paragraph count."""
    glossary = {
        "characters": {}, "places": {}, "terms": {},
        "style_anchor": {"register": "x", "avoid": [], "prefer": []},
    }
    html = "<p>A.</p><p>B.</p><p>C.</p><p>D.</p><p>E.</p>"
    prompt = dispatch.build_subagent_prompt(
        chapter_label="3", book_title="Y", target_lang="zh-tw",
        glossary=glossary, style_sample="", carryover="",
        chapter_html=html,
    )
    import re
    markers = re.findall(r"\[\[PARA_(\d+)\]\]", prompt)
    # source side should have 5 markers (PARA_1..PARA_5)
    # Note: prompt may mention [[PARA_N]] generically in rules, count distinct numbers
    numeric = [int(m) for m in markers if m.isdigit()]
    assert max(numeric) == 5
    assert set(numeric) >= {1, 2, 3, 4, 5}


def test_build_subagent_prompt_handles_empty_carryover():
    prompt = dispatch.build_subagent_prompt(
        chapter_label="1", book_title="X", target_lang="zh-tw",
        glossary={"characters": {}, "places": {}, "terms": {},
                  "style_anchor": {"register": "x", "avoid": [], "prefer": []}},
        style_sample="", carryover="", chapter_html="<p>Hi.</p>",
    )
    assert "no carryover" in prompt
    assert "no style sample yet" in prompt


def test_validate_translation_flags_empty():
    warnings = dispatch.validate_translation("", "<p>source</p>")
    assert any("empty" in w for w in warnings)


def test_validate_translation_flags_omission():
    src = "<p>p1</p><p>p2</p><p>p3</p><p>p4</p>"
    # only one target paragraph but 4 source -> ratio 0.25 < 0.5
    warnings = dispatch.validate_translation("只有一段。", src, min_ratio=0.5)
    assert any("paragraph" in w for w in warnings)


def test_validate_translation_flags_refusal():
    warnings = dispatch.validate_translation(
        "I cannot translate this content for you.", "<p>x</p>"
    )
    assert any("refusal" in w for w in warnings)


def test_validate_translation_passes_clean_output():
    src = "<p>p1</p><p>p2</p>"
    tgt = "第一段。\n\n第二段。"
    assert dispatch.validate_translation(tgt, src) == []


def test_validate_translation_detects_missing_markers():
    src_html = "<p>A.</p><p>B.</p><p>C.</p>"
    # subagent dropped PARA_2
    translation = "[[PARA_1]]\n甲\n\n[[PARA_3]]\n丙"
    warnings = dispatch.validate_translation(translation, src_html)
    assert any("missing markers" in w.lower() or "PARA_2" in w for w in warnings)


def test_validate_translation_passes_aligned_marker_output():
    src_html = "<p>A.</p><p>B.</p>"
    translation = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    warnings = dispatch.validate_translation(translation, src_html)
    assert warnings == []


def test_validate_translation_detects_invented_marker():
    src_html = "<p>A.</p><p>B.</p>"
    # subagent added PARA_3
    translation = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙\n\n[[PARA_3]]\n丙?"
    warnings = dispatch.validate_translation(translation, src_html)
    assert any("extra" in w.lower() or "PARA_3" in w for w in warnings)


def test_validate_translation_reports_duplicate_markers():
    src_html = "<p>A.</p><p>B.</p><p>C.</p>"
    translation = "[[PARA_1]] 甲\n\n[[PARA_2]] 乙\n\n[[PARA_2]] 乙2\n\n[[PARA_3]] 丙"
    warnings = dispatch.validate_translation(translation, src_html)
    assert any(w.startswith("duplicate markers: PARA_2") for w in warnings)


def test_extract_aligned_translation_joins_paragraphs_with_blank_line():
    output = "[[PARA_1]]\n第一段。\n\n[[PARA_2]]\n第二段。"
    text = dispatch.extract_aligned_translation(output, expected_count=2)
    assert text == "第一段。\n\n第二段。"


def test_extract_aligned_translation_raises_when_misaligned():
    output = "[[PARA_1]]\n第一段。"  # missing PARA_2
    with pytest.raises(ValueError) as exc:
        dispatch.extract_aligned_translation(output, expected_count=2)
    assert "missing" in str(exc.value).lower() or "PARA_2" in str(exc.value)


def test_extract_aligned_translation_raises_on_duplicate():
    output = "[[PARA_1]] 甲\n\n[[PARA_2]] 乙\n\n[[PARA_2]] 乙2\n\n[[PARA_3]] 丙"
    with pytest.raises(ValueError) as exc:
        dispatch.extract_aligned_translation(output, expected_count=3)
    assert "duplicate markers in subagent output: [2]" in str(exc.value)


def test_extract_aligned_translation_strips_marker_lines_from_body():
    output = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    text = dispatch.extract_aligned_translation(output, expected_count=2)
    # the marker tokens themselves must not appear in the joined output
    assert "[[PARA_1]]" not in text
    assert "[[PARA_2]]" not in text


def test_strip_known_leak_prefixes_removes_here_is_translation():
    raw = "Here is the translation:\n\n[[PARA_1]]\n甲"
    cleaned = dispatch.strip_known_leak_prefixes(raw)
    assert cleaned.startswith("[[PARA_1]]")


def test_strip_known_leak_prefixes_removes_markdown_fence():
    raw = "```\n[[PARA_1]]\n甲\n```"
    cleaned = dispatch.strip_known_leak_prefixes(raw)
    assert "```" not in cleaned
    assert cleaned.startswith("[[PARA_1]]")


def test_strip_known_leak_prefixes_leaves_clean_output_unchanged():
    raw = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    assert dispatch.strip_known_leak_prefixes(raw) == raw


def test_detect_aup_refusal_returns_reason_on_known_phrases():
    cases = [
        "I cannot help with translating copyrighted material.",
        "I'm unable to provide a translation of this text because it appears to be from a copyrighted work.",
        "I won't be able to assist with this task.",
        "As an AI, I cannot reproduce this content.",
    ]
    for raw in cases:
        reason = dispatch.detect_aup_refusal(raw)
        assert reason is not None, f"failed to detect: {raw}"
        assert len(reason) > 0


def test_detect_aup_refusal_returns_none_for_clean_translation():
    raw = "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙"
    assert dispatch.detect_aup_refusal(raw) is None


def test_subagent_prompt_includes_bocky_style_rules():
    """Bocky's verified style rules (5-8) must reach every subagent prompt:
    並列格式 / 第一人稱保留 / 對話與打油詩保幽默 / 不學術化.
    """
    prompt = dispatch.build_subagent_prompt(
        chapter_label="3", book_title="Animal Farm", target_lang="zh-tw",
        glossary={"characters": {}, "places": {}, "terms": {},
                  "style_anchor": {"register": "商管科普 narrative", "avoid": [], "prefer": []}},
        style_sample="範本。", carryover="", chapter_html="<p>I asked AI.</p>",
    )
    # Rule 5: 中英並列 format example
    assert "中文（English）" in prompt
    assert "LLM" in prompt and "RLHF" in prompt
    # Rule 6: 保留第一人稱 - explicit negative
    assert "筆者" in prompt or "我問 AI" in prompt
    # Rule 7: 例句/AI 對話/打油詩
    assert "limerick" in prompt or "打油詩" in prompt
    assert "對話" in prompt
    # Rule 8: 不學術化
    assert "學術" in prompt
    assert "商管科普" in prompt


def test_subagent_prompt_literary_register_rejects_parallel_names():
    prompt = dispatch.build_subagent_prompt(
        chapter_label="1", book_title="Test Book", target_lang="zh-tw",
        glossary={"characters": {"Source Name": "譯名"}, "places": {}, "terms": {},
                  "style_anchor": {"register": "literary plain prose with fable cadence", "avoid": [], "prefer": []}},
        style_sample="", carryover="", chapter_html="<p>Source Name looked.</p>",
    )
    assert "NO 中英並列" in prompt
    assert "Plain prose with fable cadence" in prompt
    assert "商管" not in prompt


def test_subagent_prompt_non_fiction_register_uses_parallel_terms():
    prompt = dispatch.build_subagent_prompt(
        chapter_label="1", book_title="Test Book", target_lang="zh-tw",
        glossary={"characters": {}, "places": {}, "terms": {},
                  "style_anchor": {"register": "商管科普 narrative", "avoid": [], "prefer": []}},
        style_sample="", carryover="", chapter_html="<p>LLM.</p>",
    )
    assert "中英並列" in prompt
    assert "中文（English）" in prompt
    assert "商管/社科 narrative" in prompt


def test_subagent_prompt_academic_register_uses_precision_rules():
    prompt = dispatch.build_subagent_prompt(
        chapter_label="1", book_title="Test Book", target_lang="zh-tw",
        glossary={"characters": {}, "places": {}, "terms": {},
                  "style_anchor": {"register": "x", "avoid": [], "prefer": []}},
        style_sample="", carryover="", chapter_html="<p>Term.</p>",
        register_override="academic_technical",
    )
    assert "precise technical terminology" in prompt
    assert "學術論述，精確優先" in prompt
    assert "precision dominates over readability" in prompt


def test_html_to_paragraphs_dedups_blockquote_nested_p():
    """Real EPUBs wrap quoted material in <blockquote><p>...</p></blockquote>.
    The blockquote AND the inner p must NOT both produce paragraphs."""
    html = """
    <html><body>
    <p>Before quote.</p>
    <blockquote><p>The actual quote text.</p></blockquote>
    <p>After quote.</p>
    </body></html>
    """
    paras = dispatch.html_to_paragraphs(html)
    # Should be exactly 3, not 4 (blockquote + nested p deduped)
    assert paras == ["Before quote.", "The actual quote text.", "After quote."]


def test_html_to_paragraphs_dedups_li_nested_p():
    """Some EPUBs use <li><p>...</p></li>. Same dedup rule applies."""
    html = "<ul><li><p>Item one.</p></li><li><p>Item two.</p></li></ul>"
    paras = dispatch.html_to_paragraphs(html)
    assert paras == ["Item one.", "Item two."]


def test_html_to_blocks_text_only():
    """HTML with no images returns only text blocks."""
    html = "<p>First.</p><p>Second.</p>"
    blocks = dispatch.html_to_blocks(html)
    assert blocks == [
        {"type": "text", "text": "First."},
        {"type": "text", "text": "Second."},
    ]


def test_html_to_blocks_standalone_div_image_becomes_image_block():
    """<div><img/></div> with no text → image block (preserved for assemble)."""
    html = '<p>Before.</p><div><img src="../images/diagram.jpg" alt="diagram"/></div><p>After.</p>'
    blocks = dispatch.html_to_blocks(html)
    types = [b["type"] for b in blocks]
    assert types == ["text", "image", "text"]
    assert blocks[1] == {"type": "image", "src": "diagram.jpg", "alt": "diagram"}


def test_html_to_blocks_inline_img_in_p_with_text_drops_img():
    """<p><img/> text</p> → single text block; the inline img is decorative."""
    html = '<p><img src="../images/icon.jpg" class="height_1em" alt=""/> Sure, I would be happy.</p>'
    blocks = dispatch.html_to_blocks(html)
    assert blocks == [{"type": "text", "text": "Sure, I would be happy."}]


def test_html_to_blocks_p_with_only_img_becomes_image_block():
    """<p><img/></p> (no other text) → image block."""
    html = '<p><img src="../images/figure.png" alt="fig 1"/></p>'
    blocks = dispatch.html_to_blocks(html)
    assert blocks == [{"type": "image", "src": "figure.png", "alt": "fig 1"}]


def test_html_to_blocks_bare_img_becomes_image_block():
    """<img/> not inside any text container → image block."""
    html = '<body><img src="https://x.com/path/standalone.gif"/></body>'
    blocks = dispatch.html_to_blocks(html)
    assert blocks == [{"type": "image", "src": "standalone.gif", "alt": ""}]


def test_html_to_blocks_preserves_order_with_mixed_content():
    """Order matters — images at the right narrative position."""
    html = """
    <p>Intro.</p>
    <div><img src="diagram1.jpg" alt=""/></div>
    <p>Middle commentary.</p>
    <div><img src="diagram2.jpg" alt=""/></div>
    <p>Conclusion.</p>
    """
    blocks = dispatch.html_to_blocks(html)
    types = [b["type"] for b in blocks]
    assert types == ["text", "image", "text", "image", "text"]
    assert blocks[1]["src"] == "diagram1.jpg"
    assert blocks[3]["src"] == "diagram2.jpg"


def test_html_to_paragraphs_includes_pre_and_definition_lists():
    """pre (often used for poems / limericks) and dt/dd must not be dropped."""
    html = """
    <html><body>
    <p>Body paragraph.</p>
    <pre>There once was an AI named Claude
Whose answers were never quite flawed.
But when asked for a rhyme,
It took its sweet time,
And output was sometimes too broad.</pre>
    <dl>
      <dt>LLM</dt>
      <dd>Large Language Model</dd>
    </dl>
    </body></html>
    """
    paras = dispatch.html_to_paragraphs(html)
    assert "Body paragraph." in paras
    # The whole limerick (collapsed whitespace) is one paragraph from <pre>
    limerick_para = next((p for p in paras if "AI named Claude" in p), None)
    assert limerick_para is not None
    assert "too broad" in limerick_para
    # dt + dd extracted
    assert "LLM" in paras
    assert "Large Language Model" in paras
