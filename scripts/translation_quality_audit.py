"""Audit bilingual paragraph quality markers in a generated EPUB."""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup

try:
    from scripts.glossary import resolve_register
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from glossary import resolve_register

BANNED_PATTERNS = [
    "版權頁說明",
    "本段保留",
    "本段介紹",
    "本段提供",
    "致謝：作者在此感謝",
    "關於作者：本段",
    "延伸閱讀：本段",
    "繁中：",
    "PARTI",
    "PARTII",
    "1CREATING",
    "2ALIGNING",
    "3FOUR",
    "4AI AS",
    "5AI AS",
    "6AI AS",
    "7AI AS",
    "8AI AS",
    "9AI AS",
    "(untitled)",
]

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def audit(output: Path, min_length_ratio: float = 0.22) -> tuple[bool, list[str]]:
    failures: list[str] = []
    with zipfile.ZipFile(output) as z:
        exceptions = _source_only_exceptions(z)
        opf_path = _find_opf_path(z)
        for path in _spine_xhtml_paths(z, opf_path):
            soup = BeautifulSoup(z.read(path), "html.parser")
            for src in soup.find_all(class_=_has_src_class):
                src_text = _clean(src.get_text(" ", strip=True))
                if not src_text:
                    continue
                tgt = _next_tag(src)
                if tgt is None or not _has_tgt_class(tgt):
                    if src_text not in exceptions:
                        failures.append(f"{path}: source-only paragraph not in exceptions: {src_text[:120]}")
                    continue
                tgt_text = _clean(tgt.get_text(" ", strip=True))
                if (
                    src.name not in HEADING_TAGS
                    and len(src_text) >= 50
                    and len(tgt_text) < min_length_ratio * len(src_text)
                ):
                    failures.append(
                        f"{path}: target too short ({len(tgt_text)}/{len(src_text)}): {src_text[:120]}"
                    )
                for pattern in BANNED_PATTERNS:
                    if pattern in tgt_text:
                        failures.append(f"{path}: banned pattern {pattern!r}: {src_text[:120]}")
    return not failures, failures


def _source_only_exceptions(z: zipfile.ZipFile) -> set[str]:
    for name in z.namelist():
        if name.endswith("translations/source_only.json"):
            data = json.loads(z.read(name).decode("utf-8"))
            if isinstance(data, list):
                result = set()
                for item in data:
                    if isinstance(item, str):
                        result.add(item)
                    elif isinstance(item, dict) and isinstance(item.get("src_text"), str):
                        result.add(item["src_text"])
                return result
    return set()


def _find_opf_path(z: zipfile.ZipFile) -> str:
    soup = BeautifulSoup(z.read("META-INF/container.xml"), "lxml-xml")
    rootfile = soup.find("rootfile", attrs={"media-type": "application/oebps-package+xml"})
    if rootfile and rootfile.get("full-path"):
        return str(rootfile.get("full-path"))
    return next(name for name in z.namelist() if name.lower().endswith(".opf"))


def _spine_xhtml_paths(z: zipfile.ZipFile, opf_path: str) -> list[str]:
    soup = BeautifulSoup(z.read(opf_path), "lxml-xml")
    opf_dir = posixpath.dirname(opf_path)
    manifest: dict[str, tuple[str, str, str]] = {}
    for item in soup.find_all("item"):
        item_id = str(item.get("id") or "")
        href = str(item.get("href") or "")
        media_type = str(item.get("media-type") or "")
        properties = str(item.get("properties") or "")
        if item_id:
            manifest[item_id] = (href, media_type, properties)
    paths: list[str] = []
    for itemref in soup.find_all("itemref"):
        idref = str(itemref.get("idref") or "")
        href, media_type, properties = manifest.get(idref, ("", "", ""))
        if media_type != "application/xhtml+xml" or "nav" in properties.split():
            continue
        full = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
        if full in z.namelist():
            paths.append(full)
    return paths


def _has_src_class(value) -> bool:
    if not value:
        return False
    classes = value if isinstance(value, list) else str(value).split()
    return "src" in classes


def _has_tgt_class(node) -> bool:
    classes = set(node.get("class", []))
    return bool(classes & {"tgt", "tgt-zh"})


def _next_tag(node):
    sibling = node.next_sibling
    while sibling is not None:
        if getattr(sibling, "name", None):
            return sibling
        sibling = sibling.next_sibling
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _min_length_ratio_from_book_dir(book_dir: Path | None) -> float:
    default = 0.22
    if book_dir is None:
        return default
    glossary_path = book_dir / "glossary.json"
    if not glossary_path.is_file():
        return default
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    register = resolve_register(glossary)
    if not register:
        return default
    ratio = register.get("min_length_ratio")
    if isinstance(ratio, (int, float)):
        return float(ratio)
    return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--book-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.output.is_file():
        print(f"not a file: {args.output}", file=sys.stderr)
        return 2
    min_length_ratio = _min_length_ratio_from_book_dir(args.book_dir)
    passed, failures = audit(args.output, min_length_ratio=min_length_ratio)
    print(f"translation_quality_audit: {'PASS' if passed else 'FAIL'}")
    if failures:
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("all src/tgt paragraph pairs meet length and banned-pattern checks")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
