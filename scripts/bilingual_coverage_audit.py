"""Audit adjacent zh-TW coverage for English paragraphs in an EPUB."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

try:  # pragma: no cover - import mode depends on caller
    from .audit_result import AuditResult
    from .content_blocks import walk_text_nodes
    from .epub_reader import EPUBReader
except ImportError:  # pragma: no cover
    from audit_result import AuditResult
    from content_blocks import walk_text_nodes
    from epub_reader import EPUBReader

HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def audit(source: Path, output: Path) -> tuple[bool, list[str]]:
    del source
    failures: list[str] = []
    with EPUBReader(output) as reader:
        package = reader.opf_package()
        if package is None:
            return False, [f"{output}: missing OPF package"]
        for path in reader.spine_xhtml_paths():
            soup = BeautifulSoup(reader.read(path), "html.parser")
            for node in _english_nodes(soup):
                sibling = _next_tag(node)
                if sibling is None or not HAN_RE.search(sibling.get_text(" ", strip=True)):
                    text = _clean(node.get_text(" ", strip=True))
                    failures.append(f"{path}: missing adjacent zh after: {text[:120]}")
    return not failures, failures


def run(source: Path, output: Path) -> AuditResult:
    passed, failures = audit(source, output)
    return AuditResult(
        name="bilingual_coverage",
        status="pass" if passed else "fail",
        failures=failures,
        details={"source": str(source), "output": str(output)},
    )


def _english_nodes(soup: BeautifulSoup) -> list:
    nodes = []
    for node in walk_text_nodes(soup):
        text = _clean(node.get_text(" ", strip=True))
        if _is_english_content(text) and not _is_target(node) and not _covered_by_ancestor(node):
            nodes.append(node)
    return nodes


def _is_target(node) -> bool:
    classes = set(node.get("class", []))
    return bool(classes & {"tgt", "tgt-zh"})


def _covered_by_ancestor(node) -> bool:
    for ancestor in node.parents:
        if not getattr(ancestor, "name", None):
            continue
        classes = set(ancestor.get("class", []))
        if "src" not in classes:
            continue
        sibling = _next_tag(ancestor)
        if sibling is not None and HAN_RE.search(sibling.get_text(" ", strip=True)):
            return True
    return False


def _is_english_content(text: str) -> bool:
    if len(text) < 50 or HAN_RE.search(text):
        return False
    letters = sum(1 for char in text if "A" <= char <= "Z" or "a" <= char <= "z")
    return letters >= max(20, int(len(text) * 0.35))


def _next_tag(node):
    sibling = node.next_sibling
    while sibling is not None:
        if getattr(sibling, "name", None):
            return sibling
        sibling = sibling.next_sibling
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    for path in (args.source, args.output):
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            return 2
    passed, failures = audit(args.source, args.output)
    print(f"bilingual_coverage_audit: {'PASS' if passed else 'FAIL'}")
    if failures:
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("all English content paragraphs have adjacent Han translations")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
