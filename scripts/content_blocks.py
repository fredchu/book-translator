"""Canonical content block extraction for EPUB chapter HTML.

This module owns the shared "what counts as content" rules for translation
dispatch, assembly, and coverage audits. Cleanup is explicit for walkers:
callers that want navigation/chrome removed should call ``strip_non_content``
before ``walk_text_nodes``. ``extract_blocks`` handles that cleanup itself
because it is the canonical source-HTML extractor.

Note: ``figcaption`` is not part of the canonical text tag set. A figure with
caption text still emits its image block, but the caption text is not emitted as
a text block unless it is wrapped in one of ``TEXT_TAGS``.
"""

from __future__ import annotations

import re
from typing import Iterator, Literal, TypedDict

from bs4 import BeautifulSoup
from bs4.element import Tag

TEXT_TAGS: tuple[str, ...] = (
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "li",
    "pre",
    "dt",
    "dd",
)
BLOCK_TAGS: tuple[str, ...] = TEXT_TAGS + ("div", "figure", "img")


class TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class ImageBlock(TypedDict):
    type: Literal["image"]
    src: str
    alt: str


Block = TextBlock | ImageBlock


def strip_non_content(soup: BeautifulSoup) -> None:
    """Decompose script/style/nav/header/footer in place. Idempotent."""
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()


def walk_text_nodes(soup: BeautifulSoup, *, skip_empty: bool = True) -> Iterator[Tag]:
    """Yield each TEXT_TAGS node exactly once in document order."""
    emitted_node_ids: set[int] = set()
    for node in soup.find_all(TEXT_TAGS):
        if any(id(ancestor) in emitted_node_ids for ancestor in node.parents):
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if skip_empty and not text:
            continue
        yield node
        emitted_node_ids.add(id(node))


def extract_blocks(html: str) -> list[Block]:
    """Walk chapter HTML and return ordered text/image content blocks.

    Text-bearing div/figure containers are skipped so their descendant content
    can be considered. Pure image containers emit image blocks. Inline images
    inside text-bearing nodes are dropped.
    """
    soup = BeautifulSoup(html, "html.parser")
    strip_non_content(soup)

    blocks: list[Block] = []
    emitted_node_ids: set[int] = set()
    for node in soup.find_all(BLOCK_TAGS):
        if any(id(ancestor) in emitted_node_ids for ancestor in node.parents):
            continue

        if node.name == "img":
            block = _image_block(node)
            if block is not None:
                blocks.append(block)
                emitted_node_ids.add(id(node))
            continue

        text = _clean_text(node.get_text(" ", strip=True))

        if node.name in ("div", "figure"):
            if text:
                continue
            inner_imgs = node.find_all("img", recursive=True)
            for img in inner_imgs:
                block = _image_block(img)
                if block is not None:
                    blocks.append(block)
            if inner_imgs:
                emitted_node_ids.add(id(node))
            continue

        if text:
            blocks.append({"type": "text", "text": text})
            emitted_node_ids.add(id(node))
            continue

        inner_imgs = node.find_all("img", recursive=True)
        for img in inner_imgs:
            block = _image_block(img)
            if block is not None:
                blocks.append(block)
        if inner_imgs:
            emitted_node_ids.add(id(node))

    return blocks


def extract_paragraphs(html: str) -> list[str]:
    """Return text from text blocks only, derived from ``extract_blocks``."""
    return [block["text"] for block in extract_blocks(html) if block["type"] == "text"]


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _image_block(node: Tag) -> ImageBlock | None:
    src = _bare_filename(str(node.get("src") or ""))
    if not src:
        return None
    return {
        "type": "image",
        "src": src,
        "alt": str(node.get("alt") or ""),
    }


def _bare_filename(src: str) -> str:
    """Strip directory prefix from an img src: '../images/page_16.jpg' -> 'page_16.jpg'."""
    if not src:
        return ""
    return src.rsplit("/", 1)[-1]
