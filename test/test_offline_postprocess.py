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
