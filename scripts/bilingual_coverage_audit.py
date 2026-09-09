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
    from .paragraph_classification import (
        HAN_RE,
        LEGACY_ENGLISH_MIN_LENGTH,
        is_english_content,
        reasoned_source_only_entries,
        untranslated_reason,
    )
except ImportError:  # pragma: no cover
    from audit_result import AuditResult
    from content_blocks import walk_text_nodes
    from epub_reader import EPUBReader
    from paragraph_classification import (  # type: ignore
        HAN_RE,
        LEGACY_ENGLISH_MIN_LENGTH,
        is_english_content,
        reasoned_source_only_entries,
        untranslated_reason,
    )


def _audit_with_warnings(
    source: Path | str | None = None,
    output: Path | str | None = None,
    *,
    epub_path: str | None = None,
) -> tuple[bool, list[str]]:
    if epub_path is not None:
        source = epub_path
        output = epub_path
    if output is None:
        raise TypeError("audit() missing required output path")
    del source
    failures: list[str] = []
    warnings: list[str] = []
    with EPUBReader(output) as reader:
        package = reader.opf_package()
        if package is None:
            return False, [f"{output}: missing OPF package"], warnings
        exceptions = _source_only_exceptions(reader)
        for path in reader.spine_xhtml_paths():
            soup = BeautifulSoup(reader.read(path), "html.parser", from_encoding="utf-8")
            if soup.find("div", class_="aup-refused-note"):
                # AUP-refused chapter — source is intentionally preserved; skip
                continue
            for node in _source_nodes_requiring_coverage(soup):
                sibling = _next_tag(node)
                if sibling is not None and HAN_RE.search(sibling.get_text(" ", strip=True)):
                    continue
                text = _clean(node.get_text(" ", strip=True))
                if text in exceptions:
                    continue
                # Short identifiers/non-Latin metadata never entered the old
                # coverage universe. Keep them legal without manufacturing Han;
                # long English exceptions must be persisted with a reason.
                reason = untranslated_reason(path, node, text)
                if not is_english_content(text) and reason is not None:
                    continue
                message = f"{path}: missing adjacent zh after: {text[:120]}"
                # Short English that the old universe (>=50 chars) never looked
                # at: "—Reid", "Begin Reading", "A Note on the Text". These are
                # genuinely untranslated, so they must not vanish — but turning
                # them red would flip existing books from green to red, which is
                # a scope change from "stop faking coverage". They go amber; the
                # decision to promote them is the user's.
                if _is_newly_in_scope(text):
                    warnings.append(message)
                    continue
                failures.append(message)
    return not failures, failures, warnings


def _source_only_exceptions(reader: EPUBReader) -> set[str]:
    """Read the EPUB-internal source_only.json (same contract as translation_quality_audit)."""
    import json as _json

    for name in reader.namelist():
        if name.endswith("translations/source_only.json"):
            try:
                data = _json.loads(reader.read(name).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return set()
            return set(reasoned_source_only_entries(data))
    return set()


def run(source: Path, output: Path) -> AuditResult:
    passed, failures, warnings = _audit_with_warnings(source, output)
    if failures:
        status = "fail"
    elif warnings:
        status = "warn"
    else:
        status = "pass"
    return AuditResult(
        name="bilingual_coverage",
        status=status,
        failures=failures,
        warnings=warnings,
        details={"source": str(source), "output": str(output)},
    )


def _source_nodes_requiring_coverage(soup: BeautifulSoup) -> list:
    """Nodes that must have a Chinese translation somewhere adjacent.

    A node that ALREADY contains Han characters is excluded: the bilingual ToC
    renders its two languages inside one node ("Title Page ｜ 書名頁", built by
    `nav_builder`), so demanding a separate zh sibling for it is asking a node
    that is already bilingual to be translated again. The old universe excluded
    these implicitly by only admitting Han-free text; widening it to "marked
    src and has latin" let them in and turned Superagency's contents page into
    6 failures and The Meaning of Your Life's into 3 — all of them reading
    "missing adjacent zh after: Notes ｜ 註釋".
    """
    nodes = []
    for node in walk_text_nodes(soup):
        text = _clean(node.get_text(" ", strip=True))
        if HAN_RE.search(text):
            continue
        marked_source = "src" in set(node.get("class", []))
        has_latin = bool(re.search(r"[A-Za-z]", text))
        if (
            ((marked_source and has_latin) or is_english_content(text))
            and not _is_target(node)
            and not _covered_by_ancestor(node)
        ):
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


# Backward-compatible private alias used by older callers/tests.
_is_english_content = is_english_content


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


# The pre-2026-09-09 universe: only Han-free English of at least this length was
# ever checked. Anything shorter is newly in scope and reports amber, not red.
# Sourced from paragraph_classification so the two do not drift apart.
_LEGACY_MIN_LENGTH = LEGACY_ENGLISH_MIN_LENGTH


def _is_newly_in_scope(text: str) -> bool:
    return len(text) < _LEGACY_MIN_LENGTH


def audit(*args, **kwargs):
    """Back-compat two-tuple contract: (passed, failures)."""
    passed, failures, _warnings = _audit_with_warnings(*args, **kwargs)
    return passed, failures
