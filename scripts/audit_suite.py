"""Python-callable orchestrator for the EPUB audit scripts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audit_result import AuditResult
from . import bilingual_coverage_audit
from . import href_resolve_audit
from . import structural_audit
from . import translation_quality_audit


def run_all(source: Path, output: Path, book_dir: Path) -> list[AuditResult]:
    """Run all audits in canonical order."""

    return [
        structural_audit.run(source, output, book_dir),
        bilingual_coverage_audit.run(source, output),
        translation_quality_audit.run(output, book_dir=book_dir),
        href_resolve_audit.run(output),
    ]


def all_passed(results: list[AuditResult]) -> bool:
    return all(result.passed for result in results)


def format_summary(results: list[AuditResult]) -> str:
    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    lines = [f"{passed} PASS / {failed} FAIL"]
    for result in results:
        lines.extend(result.format_lines())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--book-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    for path in (args.source, args.output):
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            return 2
    if not args.book_dir.is_dir():
        print(f"not a directory: {args.book_dir}", file=sys.stderr)
        return 2

    results = run_all(args.source, args.output, args.book_dir)
    print(format_summary(results))
    return 0 if all_passed(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
