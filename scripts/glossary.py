"""Glossary extraction prompt + parse/validate helpers.

The actual LLM call happens in the main Claude Code session (not in this script),
because the model is already loaded and the call should be inline. This module
gives the main session:
  - GLOSSARY_PROMPT: the system+user prompt template
  - parse_glossary(): validate and load a JSON glossary returned by the model
  - canonical_form(): merge a user-edited glossary into the on-disk file
  - write_translations_extra_nav_overrides(): derive nav labels from glossary
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REGISTER_HINTS_PATH = Path(__file__).parent.parent / "assets" / "register_hints.json"


def _load_register_hints() -> list[dict]:
    if not _REGISTER_HINTS_PATH.exists():
        return []
    with _REGISTER_HINTS_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    registers = data.get("registers", [])
    if not isinstance(registers, list):
        return []
    return [r for r in registers if isinstance(r, dict)]


def _format_register_hints(registers: list[dict]) -> str:
    lines: list[str] = []
    for register in registers:
        description = str(register.get("description", "")).strip()
        register_name = str(register.get("register", "")).strip()
        prefer = register.get("prefer", [])
        avoid = register.get("avoid", [])
        if not description:
            continue
        lines.extend(
            [
                f"      • {description}:",
                f"          register={json.dumps(register_name, ensure_ascii=False)}",
                f"          prefer={json.dumps(prefer, ensure_ascii=False)}",
                f"          avoid={json.dumps(avoid, ensure_ascii=False)}",
            ]
        )
    return "\n".join(lines) if lines else "      (No common register hints loaded.)"


_REGISTER_HINTS_BLOCK = _format_register_hints(_load_register_hints())

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
  - chapter_titles_zh: a JSON object mapping each provided spine first_heading
    from manifest.json::spine[] exactly as written to its {target_lang} chapter
    title. The main session passes those headings in the prompt context. Leave
    the value as "" if a heading is empty or looks like body prose over 120
    characters. Use standard 台灣繁中 for structural pages (Cover, Title Page,
    Copyright, Dedication, Introduction, Acknowledgments, Notes, About the
    Author, Index, Glossary). For numbered chapters, use clean readable forms
    such as "第一章　人工智慧的過去、現在與未來"; for part dividers, use forms
    such as "第一部　RenAIssance 的基礎".
  - style_anchor: a JSON object describing the author's register so subagent
    translators can match the tone. Fields:
      register: one short phrase (e.g. "literary plain prose")
      avoid:   2-4 things the translation should NOT do (e.g. "翻譯腔")
      prefer:  2-4 stylistic choices the translation SHOULD make

    Common book-type hints:
__REGISTER_HINTS_BLOCK__

    Pick the closest match (or invent a new register description) based on what
    you read.

Return ONLY valid JSON in this exact shape:
{{
  "characters": {{"<source name>": "<target translation>"}},
  "places":     {{"<source name>": "<target translation>"}},
  "terms":      {{"<source phrase>": "<target translation>"}},
  "chapter_titles_zh": {{"<source first_heading>": "<target chapter title>"}},
  "style_anchor": {{
    "register": "<one phrase>",
    "avoid":    ["...", "..."],
    "prefer":   ["...", "..."]
  }}
}}

Do not add explanation or commentary. JSON only.

=== FULL BOOK TEXT ===
{full_text}
""".replace("__REGISTER_HINTS_BLOCK__", _REGISTER_HINTS_BLOCK)


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
    if "chapter_titles_zh" in data and not isinstance(data["chapter_titles_zh"], dict):
        raise ValueError("chapter_titles_zh must be an object")
    return data


def resolve_register(glossary: dict, register_hints: dict | None = None) -> dict | None:
    """Resolve glossary style_anchor.register to a configured register hint."""
    if register_hints is None:
        if not _REGISTER_HINTS_PATH.exists():
            return None
        with _REGISTER_HINTS_PATH.open(encoding="utf-8") as fh:
            register_hints = json.load(fh)

    registers = register_hints.get("registers", [])
    if not isinstance(registers, list):
        return None

    style_anchor = glossary.get("style_anchor", {})
    if not isinstance(style_anchor, dict):
        return None
    requested = str(style_anchor.get("register", "")).strip().casefold()
    if not requested:
        return None

    for register in registers:
        if not isinstance(register, dict):
            continue
        hint = str(register.get("register", "")).strip().casefold()
        if not hint:
            continue
        if hint in requested or requested in hint:
            return register
        hint_tokens = [token for token in re.split(r"[\s/，,;；、]+", hint) if len(token) >= 2]
        if any(token in requested for token in hint_tokens):
            return register
    return None


def canonical_form(glossary: dict) -> dict:
    """Return a glossary with stable key ordering and trimmed whitespace."""
    out = {
        "characters": _trim_dict(glossary.get("characters", {})),
        "places": _trim_dict(glossary.get("places", {})),
        "terms": _trim_dict(glossary.get("terms", {})),
    }
    if "chapter_titles_zh" in glossary:
        out["chapter_titles_zh"] = _trim_dict(glossary.get("chapter_titles_zh", {}))
    out["style_anchor"] = {
        "register": str(glossary["style_anchor"]["register"]).strip(),
        "avoid": [str(x).strip() for x in glossary["style_anchor"]["avoid"]],
        "prefer": [str(x).strip() for x in glossary["style_anchor"]["prefer"]],
    }
    return out


def write_glossary(out_path: Path, glossary: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(canonical_form(glossary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_translations_extra_nav_overrides(
    glossary: dict,
    manifest: dict,
    book_dir: Path,
    *,
    overwrite_existing: bool = False,
) -> dict:
    """Populate translations_extra.json nav_overrides from chapter_titles_zh."""
    titles = glossary.get("chapter_titles_zh", {})
    if titles is None:
        titles = {}
    if not isinstance(titles, dict):
        raise ValueError("glossary chapter_titles_zh must be an object")

    path = book_dir / "translations_extra.json"
    if path.is_file():
        extra = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(extra, dict):
            raise ValueError(f"{path}: translations_extra must be a JSON object")
    else:
        extra = {}

    nav_overrides = extra.get("nav_overrides", {})
    if nav_overrides is None:
        nav_overrides = {}
    if not isinstance(nav_overrides, dict):
        raise ValueError(f"{path}: nav_overrides must be an object")
    nav_overrides = dict(nav_overrides)

    for entry in _manifest_spine_entries(manifest):
        if not isinstance(entry, dict):
            continue
        heading = str(entry.get("first_heading") or "")
        if not heading:
            continue
        zh = str(titles.get(heading) or "").strip()
        if not zh:
            continue
        original_idref = str(entry.get("original_idref") or "")
        if not original_idref:
            continue
        if overwrite_existing or original_idref not in nav_overrides:
            nav_overrides[original_idref] = zh

    extra["nav_overrides"] = nav_overrides
    book_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")
    return nav_overrides


def _trim_dict(d: dict) -> dict:
    return {str(k).strip(): str(v).strip() for k, v in d.items() if str(k).strip()}


def _manifest_spine_entries(manifest: dict) -> list[dict]:
    entries = manifest.get("spine")
    if isinstance(entries, list):
        return entries
    legacy = manifest.get("chapters")
    if isinstance(legacy, list):
        return legacy
    return []
