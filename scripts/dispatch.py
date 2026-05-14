"""Subagent dispatch prompt + helpers.

The Agent tool call happens in the main Claude Code session. This module gives
the main session:
  - build_subagent_prompt(): assemble the full per-chapter translation prompt
  - extract_translation_text(): clean a chapter HTML down to plain text for input
  - validate_translation(): cheap structural checks on returned translation
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

SUBAGENT_PROMPT_TEMPLATE = """\
You are translating Chapter {chapter_label} of "{book_title}" from English to
{target_lang} ({target_lang_long}).

GLOSSARY (mandatory — use these exact translations for every occurrence):
{glossary_json}

STYLE ANCHOR — match this register, sentence rhythm, and tone:
---
{style_sample}
---

CARRYOVER — the last paragraph of the previous chapter's translation. Your
opening sentence should flow naturally from this. Do NOT repeat or summarize it:
---
{carryover}
---

CHAPTER {chapter_label} SOURCE TEXT (paragraph boundaries preserved with blank
lines between paragraphs):
---
{chapter_text}
---

Requirements:
  1. Output ONLY the translated text. No commentary, no markdown headings, no
     "Translation:" prefix.
  2. Preserve paragraph boundaries with a single blank line between paragraphs.
  3. Translate every character / place / term from the glossary using the
     glossary's exact target form.
  4. Do not omit any paragraph. Translate every paragraph in order.
  5. 專有名詞 / 縮寫 / 技術詞首次在本章出現時，用「中文（English）」並列格式
     （例：「大型語言模型（LLM）」、「人類回饋強化學習（RLHF）」、「通用人工智慧
     （AGI）」），後續再出現只用中文。同章內保持一致。
  6. 保留作者第一人稱敘事口吻。"I asked AI...", "I tried...", "I found..." 翻成
     「我問 AI⋯」「我試了⋯」「我發現⋯」。**不要**改成「筆者」「作者」「我們」
     ——會失去 narrative 親近感、把口語回憶錄變成論文。
  7. 例句、AI 對話、打油詩、引述：保留原作風格與幽默。AI 生成的 limerick / 詩可
     重組押韻不必逐字（中文押韻為主），但機智 / 反差 / 自嘲口吻必須留住。對話翻
     成口語體（不要文謅謅、不要「閣下」「在下」）。
  8. 整體 register：商管科普 narrative。**不要學術化**。短句優於長句、口語優於
     書面、具體例子優於抽象詞、白話優於成語堆疊。
  9. {custom_instructions}
"""

DEFAULT_CUSTOM_INSTRUCTIONS = (
    "If the target is 台灣繁體中文, use 台灣用語 (e.g. 「軟體」not「软件」, "
    "「網路」not「网络」). Avoid 翻譯腔. Prefer short sentences over four-character "
    "literary clichés."
)


def html_to_paragraphs(html: str) -> list[str]:
    """Convert chapter HTML to a list of plain-text paragraphs.

    Strips nav/header/footer/script. Treats each of these as a paragraph and
    preserves them all for translation:
      - <p>, <h1>..<h6>          headings + body paragraphs
      - <blockquote>             pull quotes, indented citations
      - <li>                     list items (bulleted / numbered)
      - <pre>                    preformatted blocks — often poems / limericks
      - <dt>, <dd>               definition lists (glossaries inside text)

    Collapses internal whitespace. Each returned string is one paragraph.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    # find_all walks document order: a parent (e.g. <blockquote>) appears before
    # its child (e.g. <p> inside it). If we process the parent first and then
    # also process the child, the child's text is included twice (once in the
    # parent's get_text, once on its own). Dedup by skipping any node whose
    # ancestor has already been emitted.
    paragraphs: list[str] = []
    emitted_node_ids: set[int] = set()
    for node in soup.find_all([
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "li", "pre", "dt", "dd",
    ]):
        if any(id(anc) in emitted_node_ids for anc in node.parents):
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if text:
            paragraphs.append(text)
            emitted_node_ids.add(id(node))
    return paragraphs


def html_to_blocks(html: str) -> list[dict]:
    """Walk chapter HTML and return an ordered list of content blocks.

    Each block is one of:
      {"type": "text",  "text": "..."}
      {"type": "image", "src":  "filename.jpg", "alt": "..."}

    Text blocks correspond 1:1 to the paragraphs returned by
    ``html_to_paragraphs`` — same nodes, same dedup rules (skip a node if any
    ancestor was already emitted).

    Image blocks are emitted for:
      - ``<img>`` whose parent <p>/<div>/<figure> has no other significant text
        (typically standalone illustrations or full-width diagrams)
      - any other standalone ``<img>`` not inside a text-emitting node

    Inline ``<img>`` markers next to text (e.g. emoji-as-speaker-indicator in a
    paragraph like ``<p><img class="height_1em"/> Sure, I'd be happy...</p>``)
    are dropped — the surrounding text is what carries the meaning.

    Paths in ``src`` are normalized to the bare filename (``page_16.jpg``), not
    the EPUB-internal relative path (``../images/page_16.jpg``).
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    blocks: list[dict] = []
    emitted_node_ids: set[int] = set()
    # NOTE on TARGETS: <div>/<figure> are pure containers — many EPUBs wrap an
    # entire chapter in <div role="doc-introduction">, so emitting them as text
    # blocks would swallow every descendant paragraph. They are included here
    # ONLY so we can detect empty-text image-wrapper divs and emit image
    # blocks; text-bearing divs are skipped and their descendants picked up.
    TARGETS = [
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "li", "pre", "dt", "dd",
        "div", "figure", "img",
    ]
    for node in soup.find_all(TARGETS):
        if any(id(anc) in emitted_node_ids for anc in node.parents):
            continue

        if node.name == "img":
            src = _bare_filename(str(node.get("src") or ""))
            if src:
                blocks.append({
                    "type": "image",
                    "src": src,
                    "alt": str(node.get("alt") or ""),
                })
                emitted_node_ids.add(id(node))
            continue

        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))

        if node.name in ("div", "figure"):
            # Text-bearing div/figure: skip — let descendant <p>/<h*>/etc. be
            # picked up by their own iteration. Only emit if the container is
            # pure-image (e.g. full-width standalone illustration).
            if text:
                continue
            inner_imgs = node.find_all("img", recursive=True)
            for img in inner_imgs:
                src = _bare_filename(str(img.get("src") or ""))
                if src:
                    blocks.append({
                        "type": "image",
                        "src": src,
                        "alt": str(img.get("alt") or ""),
                    })
            if inner_imgs:
                emitted_node_ids.add(id(node))
            continue

        # Text-container tag (p, h1-h6, blockquote, li, pre, dt, dd).
        if text:
            blocks.append({"type": "text", "text": text})
            emitted_node_ids.add(id(node))
            continue

        # Empty text-container — emit any wrapped image (e.g. <p><img/></p>).
        inner_imgs = node.find_all("img", recursive=True)
        for img in inner_imgs:
            src = _bare_filename(str(img.get("src") or ""))
            if src:
                blocks.append({
                    "type": "image",
                    "src": src,
                    "alt": str(img.get("alt") or ""),
                })
        if inner_imgs:
            emitted_node_ids.add(id(node))

    return blocks


def _bare_filename(src: str) -> str:
    """Strip directory prefix from an img src: '../images/page_16.jpg' -> 'page_16.jpg'."""
    if not src:
        return ""
    return src.rsplit("/", 1)[-1]


def chapter_text_for_prompt(html: str) -> str:
    return "\n\n".join(html_to_paragraphs(html))


def build_subagent_prompt(
    *,
    chapter_label: str,
    book_title: str,
    target_lang: str,
    glossary: dict,
    style_sample: str,
    carryover: str,
    chapter_html: str,
    custom_instructions: str | None = None,
) -> str:
    target_lang_long = _target_long(target_lang)
    chapter_text = chapter_text_for_prompt(chapter_html)
    return SUBAGENT_PROMPT_TEMPLATE.format(
        chapter_label=chapter_label,
        book_title=book_title,
        target_lang=target_lang,
        target_lang_long=target_lang_long,
        glossary_json=json.dumps(glossary, ensure_ascii=False, indent=2),
        style_sample=style_sample or "(no style sample yet — chapter 1)",
        carryover=carryover or "(this is the first chapter — no carryover)",
        chapter_text=chapter_text,
        custom_instructions=custom_instructions or DEFAULT_CUSTOM_INSTRUCTIONS,
    )


def validate_translation(translation: str, chapter_html: str, *, min_ratio: float = 0.5) -> list[str]:
    """Cheap sanity checks. Returns a list of human-readable warnings; empty list = OK.

    Checks:
      - non-empty
      - paragraph count >= min_ratio * source paragraph count
      - no obvious refusal patterns ("I cannot", "I'm sorry", "As an AI")
    """
    warnings: list[str] = []
    translation = (translation or "").strip()
    if not translation:
        warnings.append("translation is empty")
        return warnings
    src_paras = html_to_paragraphs(chapter_html)
    tgt_paras = [p for p in translation.split("\n\n") if p.strip()]
    if src_paras and len(tgt_paras) < max(1, int(len(src_paras) * min_ratio)):
        warnings.append(
            f"translation has {len(tgt_paras)} paragraphs vs source {len(src_paras)} "
            f"(below {min_ratio:.0%} ratio)"
        )
    refusal_re = re.compile(r"\b(I (cannot|can't|won't|am unable)|I'm sorry|As an AI)\b", re.IGNORECASE)
    if refusal_re.search(translation[:500]):
        warnings.append("translation contains apparent refusal language")
    return warnings


def _target_long(target_lang: str) -> str:
    table = {
        "zh-tw": "台灣繁體中文",
        "zh-cn": "簡體中文（中國大陸）",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어",
    }
    return table.get(target_lang.lower(), target_lang)
