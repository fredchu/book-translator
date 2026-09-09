"""Tests for offline post-processing helpers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import offline_postprocess  # type: ignore  # noqa: E402


def _chapters(book_dir: Path) -> Path:
    chapters = book_dir / "chapters"
    chapters.mkdir(parents=True, exist_ok=True)
    return chapters


def _write_translation(chapters: Path, item_id: str, segments: list[str]) -> Path:
    path = chapters / f"{item_id}_translation.txt"
    path.write_text("\n\n".join(segments), encoding="utf-8")
    return path


def _manifest(item_id: str, idref: str) -> dict:
    return {
        "spine": [
            {
                "id": item_id,
                "original_idref": idref,
                "output_strategy": "translate",
            }
        ]
    }


def _nav_overrides(book_dir: Path) -> dict:
    data = json.loads((book_dir / "translations_extra.json").read_text("utf-8"))
    return data["nav_overrides"]


def test_to_traditional_empty_string_returns_empty_without_opencc() -> None:
    assert offline_postprocess.to_traditional("") == ""


def test_to_traditional_converts_simplified_and_is_idempotent() -> None:
    pytest.importorskip("opencc")

    text = "我开始与玛德琳工作两小时"
    out = offline_postprocess.to_traditional(text)

    assert "开" not in out
    assert "玛" not in out
    assert "與" in out
    assert offline_postprocess.to_traditional(out) == out


# --- regression: s2twp corrupted already-Traditional prose (2026-09-08) --------
# Ch.7 of The Mind-Gut Connection went through s2twp and came back with 血液迴圈,
# 隨時呼叫, 易感視窗, 排洩 and 幹擾. The model had translated all five correctly;
# the "p" vocabulary table rewrote them. A reader caught 幹擾 on first read.

CORRECT_TRADITIONAL_PROSE = [
    ("另一些則進入血液循環，參與長距離訊號傳遞", "迴圈"),   # not a program loop
    ("在日後做決策時可以隨時調用", "呼叫"),                # not a function call
    ("神經發展障礙的易感窗口", "視窗"),                    # a time window, not a GUI
    ("2014 年國家健康訪視的最新數據顯示", "資料"),
    ("透過重建與優化大腦與腸道的溝通", "最佳化"),
    ("大腸則透過排泄處理剩餘物質", "排洩"),
    ("當這些器官之間的交談受到干擾時", "幹擾"),
]


@pytest.mark.parametrize("prose,must_not_appear", CORRECT_TRADITIONAL_PROSE)
def test_to_traditional_leaves_correct_traditional_prose_alone(
    prose: str, must_not_appear: str
) -> None:
    pytest.importorskip("opencc")

    out = offline_postprocess.to_traditional(prose)

    assert must_not_appear not in out, f"{prose!r} was corrupted into {out!r}"
    assert out == prose, f"expected no change, got {out!r}"


@pytest.mark.parametrize(
    "simplified,expected",
    [
        ("血液循环系统", "血液循環系統"),   # s2twp gives 血液迴圈系統
        ("干扰素治疗", "干擾素治療"),       # protected term reached via Simplified
        ("肠道菌群失调", "腸道菌群失調"),
    ],
)
def test_to_traditional_still_converts_real_simplified(
    simplified: str, expected: str
) -> None:
    pytest.importorskip("opencc")

    assert offline_postprocess.to_traditional(simplified) == expected


def test_to_traditional_leaves_no_placeholder_residue() -> None:
    pytest.importorskip("opencc")

    text = "干擾、干預與若干污染問題，以及排泄功能"
    out = offline_postprocess.to_traditional(text)

    assert out == text
    assert not any(chr(0xE000 + i) in out for i in range(len(offline_postprocess._PROTECTED_TERMS)))


# Regression corpus from review-01 §5. These deliberately mix valid Taiwan
# characters with Simplified residuals and punctuation-delimited sentences.
@pytest.mark.parametrize(
    "source,expected",
    [
        ("范·雷文霍克說：这很重要", "范·雷文霍克說：這很重要"),
        ("盧卡斯·范·歐登霍夫", "盧卡斯·范·歐登霍夫"),
        ("受到干擾的血液循環", "受到干擾的血液循環"),
        ("肥皂劇明星", "肥皂劇明星"),
        ("最多只能撥打三次", "最多只能撥打三次"),
        ("疱疹發作次數減少了", "疱疹發作次數減少了"),
        ("雇主們對這個平台", "雇主們對這個平台"),
        ("我们在公司里工作", "我們在公司裡工作"),
        ("她穿着礼服", "她穿著禮服"),
        ("第七章", "第七章"),
        ("什么是腸道直覺", "什麼是腸道直覺"),
        ("范·雷文霍克的發現\n\n这很重要。", "范·雷文霍克的發現\n\n這很重要。"),
        ("Mayer, E. A. 2011. 范·雷文霍克. 这是引文", "Mayer, E. A. 2011. 范·雷文霍克. 這是引文"),
    ],
)
def test_to_traditional_sentence_gated_fixtures(source: str, expected: str) -> None:
    pytest.importorskip("opencc")

    assert offline_postprocess.to_traditional(source) == expected


def test_to_traditional_known_ambiguous_character_miss() -> None:
    """种 is also a Traditional surname, so the accepted ~3% miss stays visible."""
    pytest.importorskip("opencc")

    assert offline_postprocess.to_traditional("某种程度上") == "某种程度上"


def test_simplified_trigger_definition_rejects_ambiguous_traditional() -> None:
    pytest.importorskip("opencc")

    cc = offline_postprocess._converter()
    triggers = offline_postprocess._simplified_triggers(cc)

    assert triggers is not None
    assert "这" in triggers
    assert "着" in triggers
    assert "范" not in triggers
    assert "疱" not in triggers
    assert "雇" not in triggers
    assert "苧" not in triggers
    assert "洼" not in triggers


def test_normalize_character_names_merges_dominant_hamming_variant(tmp_path: Path) -> None:
    chapters = _chapters(tmp_path)
    canonical_mentions = "。".join(["瑪德琳"] * 8)
    variant_mentions = "。".join(["梅德琳"] * 3)
    first = _write_translation(
        chapters,
        "item_001",
        [
            f"瑪德琳·亞靈頓走進房間。{canonical_mentions}",
            "旁白。",
        ],
    )
    second = _write_translation(
        chapters,
        "item_002",
        [
            f"瑪德琳。瑪德琳。瑪德琳。瑪德琳。{variant_mentions}",
            "結尾。",
        ],
    )

    merges = offline_postprocess.normalize_character_names(tmp_path)

    assert ("梅德琳", "瑪德琳", 3) in merges
    combined = first.read_text("utf-8") + second.read_text("utf-8")
    assert "梅德琳" not in combined
    assert combined.count("瑪德琳") == 16


def test_normalize_character_names_never_merges_two_canonical_names(tmp_path: Path) -> None:
    chapters = _chapters(tmp_path)
    variant_mentions = "。".join(["梅德琳"] * 3)
    path = _write_translation(
        chapters,
        "item_001",
        [
            "瑪德琳·亞靈頓出場。梅德琳·某某也出場。",
            f"{'。'.join(['瑪德琳'] * 12)}。{variant_mentions}",
        ],
    )

    merges = offline_postprocess.normalize_character_names(tmp_path)

    assert all(variant != "梅德琳" for variant, _, _ in merges)
    assert path.read_text("utf-8").count("梅德琳") == 4


def test_normalize_character_names_keeps_below_threshold_variant(tmp_path: Path) -> None:
    chapters = _chapters(tmp_path)
    path = _write_translation(
        chapters,
        "item_001",
        [
            "瑪德琳·亞靈頓出場。",
            f"{'。'.join(['瑪德琳'] * 12)}。梅德琳。梅德琳",
        ],
    )

    merges = offline_postprocess.normalize_character_names(tmp_path)

    assert merges == []
    assert path.read_text("utf-8").count("梅德琳") == 2


def test_normalize_character_names_empty_book_dir_returns_empty(tmp_path: Path) -> None:
    assert offline_postprocess.normalize_character_names(tmp_path) == []


def test_normalize_character_names_skips_two_char_names(tmp_path: Path) -> None:
    # 2-char names are too collision-prone (士尼⊂迪士尼, 曼尼⊂曼尼托巴) to auto-merge.
    chapters = _chapters(tmp_path)
    _write_translation(
        chapters,
        "item_001",
        [
            "丹尼·莫里森登場。",
            f"{'。'.join(['丹尼'] * 20)}。唐尼。唐尼。唐尼",
        ],
    )

    merges = offline_postprocess.normalize_character_names(tmp_path)

    assert merges == []


def test_normalize_character_names_ignores_embedded_substring(tmp_path: Path) -> None:
    # 瑪德蓮 is hamming-1 from canonical 瑪德琳 but only ever appears inside the
    # compound 瑪德蓮娜 (always Han-flanked) -> must NOT be merged.
    chapters = _chapters(tmp_path)
    _write_translation(
        chapters,
        "item_001",
        [
            "瑪德琳·亞靈頓登場。",
            f"{'。'.join(['瑪德琳'] * 20)}。",
            "聖瑪德蓮娜教堂。聖瑪德蓮娜教堂。聖瑪德蓮娜教堂。",
        ],
    )

    merges = offline_postprocess.normalize_character_names(tmp_path)

    assert all(variant != "瑪德蓮" for variant, _, _ in merges)
    combined = "".join(
        (chapters / f).read_text("utf-8") for f in ["item_001_translation.txt"]
    )
    assert "瑪德蓮娜" in combined


def test_build_nav_overrides_adds_heading_led_chapter_title(tmp_path: Path) -> None:
    chapters = _chapters(tmp_path)
    (chapters / "item_001.html").write_text(
        "<html><body><header><h1>1</h1></header><h1>SURROUNDED</h1><p>Body.</p></body></html>",
        encoding="utf-8",
    )
    _write_translation(chapters, "item_001", ["被村裡的傻瓜們包圍", "正文。"])

    added = offline_postprocess.build_nav_overrides(tmp_path, _manifest("item_001", "chap1"))

    assert added == 1
    assert _nav_overrides(tmp_path)["chap1"] == "被村裡的傻瓜們包圍"


def test_build_nav_overrides_skips_prose_led_item(tmp_path: Path) -> None:
    chapters = _chapters(tmp_path)
    (chapters / "item_002.html").write_text(
        "<html><body><header><h1>LAURA</h1></header><p>Epigraph.</p><h1>PART ONE</h1></body></html>",
        encoding="utf-8",
    )
    _write_translation(chapters, "item_002", ["卷首引文。", "第一部"])

    added = offline_postprocess.build_nav_overrides(tmp_path, _manifest("item_002", "part1"))

    assert added == 0
    assert not (tmp_path / "translations_extra.json").exists()


def test_build_nav_overrides_preserves_existing_nav_overrides(tmp_path: Path) -> None:
    chapters = _chapters(tmp_path)
    (tmp_path / "translations_extra.json").write_text(
        json.dumps({"nav_overrides": {"chap1": "既有"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (chapters / "item_001.html").write_text(
        "<html><body><h1>SURROUNDED</h1><p>Body.</p></body></html>",
        encoding="utf-8",
    )
    _write_translation(chapters, "item_001", ["新的標題", "正文。"])

    added = offline_postprocess.build_nav_overrides(tmp_path, _manifest("item_001", "chap1"))

    assert added == 0
    assert _nav_overrides(tmp_path) == {"chap1": "既有"}
