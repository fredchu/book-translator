"""Audit bilingual paragraph quality markers in a generated EPUB."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

try:
    from scripts.audit_result import AuditResult
    from scripts.epub_reader import EPUBReader
    from scripts.glossary import resolve_register
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from audit_result import AuditResult
    from epub_reader import EPUBReader
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
    with EPUBReader(output) as reader:
        exceptions = _source_only_exceptions(reader)
        package = reader.opf_package()
        if package is None:
            return False, [f"{output}: missing OPF package"]
        for path in reader.spine_xhtml_paths():
            soup = BeautifulSoup(reader.read(path), "html.parser")
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


def run(
    output: Path,
    book_dir: Path | None = None,
    min_length_ratio: float | None = None,
) -> AuditResult:
    resolved_ratio = (
        _min_length_ratio_from_book_dir(book_dir)
        if min_length_ratio is None
        else min_length_ratio
    )
    passed, failures = audit(output, min_length_ratio=resolved_ratio)
    return AuditResult(
        name="translation_quality",
        status="pass" if passed else "fail",
        failures=failures,
        details={"output": str(output), "min_length_ratio": resolved_ratio},
    )


def _source_only_exceptions(reader: EPUBReader) -> set[str]:
    for name in reader.namelist():
        if name.endswith("translations/source_only.json"):
            data = json.loads(reader.read(name).decode("utf-8"))
            if isinstance(data, list):
                result = set()
                for item in data:
                    if isinstance(item, str):
                        result.add(item)
                    elif isinstance(item, dict) and isinstance(item.get("src_text"), str):
                        result.add(item["src_text"])
                return result
    return set()


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
