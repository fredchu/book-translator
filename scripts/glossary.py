"""Glossary extraction prompt + parse/validate helpers.

The actual LLM call happens in the main Claude Code session (not in this script),
because the model is already loaded and the call should be inline. This module
gives the main session:
  - GLOSSARY_PROMPT: the system+user prompt template
  - parse_glossary(): validate and load a JSON glossary returned by the model
  - canonical_form(): merge a user-edited glossary into the on-disk file
"""

from __future__ import annotations

import json
from pathlib import Path

GLOSSARY_PROMPT = """\
You are building a translation glossary for a literary work being translated
from {source_lang} to {target_lang} (台灣繁體中文 if target is zh-tw — use 台灣用語).

Read the full text below and extract:
  - characters: every named character. Translate each name in the target
    language register (e.g. transliteration for English names unless a Chinese
    convention exists).
  - places: named places, organizations, factions.
  - terms: domain-specific terminology, invented words, songs/poems referenced
    by name. Anything that must translate consistently across chapters.
  - style_anchor: a JSON object describing the author's register so subagent
    translators can match the tone. Fields:
      register: one short phrase (e.g. "literary plain prose (Orwell)")
      avoid:   2-4 things the translation should NOT do (e.g. "翻譯腔")
      prefer:  2-4 stylistic choices the translation SHOULD make

    Common book-type hints:
      • Literary fiction (e.g. Orwell, McCarthy):
          register="literary plain prose"
          prefer=["口語節奏","短句","略諷刺"]
          avoid=["翻譯腔","四字結構過多","過度書面化"]
      • 商管科普 narrative (e.g. Mollick, Pink, Duhigg, Kahneman popularizations):
          register="商管科普 narrative，第一人稱保留"
          prefer=["口語節奏","具體例子優先","保留作者第一人稱『我』","保留幽默/反諷"]
          avoid=["學術化","翻譯腔","把 I 改成『筆者』/『作者』","四字成語堆疊"]
      • Academic / technical:
          register="學術論述，精確優先"
          prefer=["精確術語","邏輯連接詞","完整句"]
          avoid=["口語縮略","俏皮幽默","主觀色彩"]

    Pick the closest match (or invent a new register description) based on what
    you read.

Return ONLY valid JSON in this exact shape:
{{
  "characters": {{"<source name>": "<target translation>"}},
  "places":     {{"<source name>": "<target translation>"}},
  "terms":      {{"<source phrase>": "<target translation>"}},
  "style_anchor": {{
    "register": "<one phrase>",
    "avoid":    ["...", "..."],
    "prefer":   ["...", "..."]
  }}
}}

Do not add explanation or commentary. JSON only.

=== FULL BOOK TEXT ===
{full_text}
"""


def parse_glossary(text: str) -> dict:
    """Parse glossary JSON returned by the LLM.

    Tolerates a leading/trailing code fence. Raises ValueError on missing keys.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[len("json"):]
        cleaned = cleaned.strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].strip()
    data = json.loads(cleaned)
    for key in ("characters", "places", "terms", "style_anchor"):
        if key not in data:
            raise ValueError(f"glossary missing key: {key}")
    anchor = data["style_anchor"]
    for key in ("register", "avoid", "prefer"):
        if key not in anchor:
            raise ValueError(f"style_anchor missing key: {key}")
    return data


def canonical_form(glossary: dict) -> dict:
    """Return a glossary with stable key ordering and trimmed whitespace."""
    return {
        "characters": _trim_dict(glossary.get("characters", {})),
        "places": _trim_dict(glossary.get("places", {})),
        "terms": _trim_dict(glossary.get("terms", {})),
        "style_anchor": {
            "register": str(glossary["style_anchor"]["register"]).strip(),
            "avoid": [str(x).strip() for x in glossary["style_anchor"]["avoid"]],
            "prefer": [str(x).strip() for x in glossary["style_anchor"]["prefer"]],
        },
    }


def write_glossary(out_path: Path, glossary: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(canonical_form(glossary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _trim_dict(d: dict) -> dict:
    return {str(k).strip(): str(v).strip() for k, v in d.items() if str(k).strip()}
