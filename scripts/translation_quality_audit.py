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
    from scripts.dispatch import contains_model_token_leak
    from scripts.epub_reader import EPUBReader
    from scripts.glossary import resolve_register
    from scripts.paragraph_classification import reasoned_source_only_entries
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from audit_result import AuditResult
    from dispatch import contains_model_token_leak
    from epub_reader import EPUBReader
    from glossary import resolve_register
    from paragraph_classification import reasoned_source_only_entries

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
    "譯文：",
    "<｜",
    "<|",
]

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SIMPLIFIED_CHINESE_CHARS = frozenset(
    "学国应该时这们个经历实来体现进觉党报见远边长万与业东严两临卫厂厅县发为过还样种从会动问开关车书无语气"
)
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_SOURCE_SENTENCE_END_RE = re.compile(r"[.!?…\"”’)\]:;。！？；：」』）】]$")
_INDEX_PAGE_REF_RE = re.compile(r",\s*\d")
_SENTENCE_PUNCTUATION_RE = re.compile(r"[.!?…。！？]+")
_CLOSING_PUNCTUATION = "\"”’）)]}」』】"
_SHORT_SOURCE_MAX = 150
_TITLE_RATIO_MARGIN = 0.04
_SHORT_SENTENCE_RATIO_MARGIN = 0.02


def contains_simplified_chinese(text: str) -> bool:
    """Return True when text has enough Han content plus high-confidence Simplified chars."""
    return bool(_offending_simplified_chinese_chars(text))


def _contains_han(text: str) -> bool:
    """True when text has any CJK Han character (i.e. it is already (inline-)translated)."""
    return any("一" <= ch <= "鿿" for ch in text)


def audit(
    output: Path | str | None = None,
    min_length_ratio: float = 0.22,
    *,
    epub_path: str | None = None,
) -> tuple[bool, list[str]]:
    """Backward-compatible red-light audit result.

    Call :func:`audit_with_warnings` or :func:`run` to also receive yellow-light
    findings. Warnings never change the returned pass/fail gate result.
    """
    failures, _warnings = _audit_findings(output, min_length_ratio, epub_path=epub_path)
    return not failures, failures


def audit_with_warnings(
    output: Path | str | None = None,
    min_length_ratio: float = 0.22,
    *,
    epub_path: str | None = None,
) -> tuple[bool, list[str], list[str]]:
    failures, warnings = _audit_findings(output, min_length_ratio, epub_path=epub_path)
    return not failures, failures, warnings


def _audit_findings(
    output: Path | str | None,
    min_length_ratio: float,
    *,
    epub_path: str | None,
) -> tuple[list[str], list[str]]:
    if epub_path is not None:
        output = epub_path
    if output is None:
        raise TypeError("audit() missing required output path")
    failures: list[str] = []
    warnings: list[str] = []
    with EPUBReader(output) as reader:
        exceptions = _source_only_exceptions(reader)
        package = reader.opf_package()
        if package is None:
            return [f"{output}: missing OPF package"], warnings
        for path in reader.spine_xhtml_paths():
            soup = BeautifulSoup(reader.read(path), "html.parser", from_encoding="utf-8")
            if soup.find("div", class_="aup-refused-note"):
                continue
            for src in soup.find_all(class_=_has_src_class):
                src_text = _clean(src.get_text(" ", strip=True))
                if not src_text:
                    continue
                tgt = _next_tag(src)
                if tgt is None or not _has_tgt_class(tgt):
                    # Inline-bilingual paragraphs (e.g. ToC links rendered
                    # "English ｜ 中文") carry their translation in the same node.
                    if src_text not in exceptions and not _contains_han(src_text):
                        failures.append(f"{path}: source-only paragraph not in exceptions: {src_text[:120]}")
                    continue
                tgt_text = _clean(tgt.get_text(" ", strip=True))
                hard_target_failure = False
                simplified_chars = _offending_simplified_chinese_chars(tgt_text)
                if simplified_chars:
                    hard_target_failure = True
                    failures.append(
                        f"{path}: error: target contains Simplified Chinese characters "
                        f"{simplified_chars!r}: {tgt_text[:120]}"
                    )
                if contains_model_token_leak(tgt_text):
                    hard_target_failure = True
                    failures.append(f"{path}: model control token leaked: {src_text[:120]}")
                for pattern in BANNED_PATTERNS:
                    matched = (
                        tgt_text.startswith(pattern)
                        if pattern == "譯文："
                        else pattern in tgt_text
                    )
                    if matched:
                        hard_target_failure = True
                        failures.append(f"{path}: banned pattern {pattern!r}: {src_text[:120]}")
                if (
                    src.name not in HEADING_TAGS
                    and len(src_text) >= 50
                    and len(tgt_text) < min_length_ratio * len(src_text)
                ):
                    finding = (
                        f"{path}: target too short ({len(tgt_text)}/{len(src_text)}): "
                        f"{src_text[:120]}"
                    )
                    if not hard_target_failure and _looks_reviewable_short_target(
                        src, src_text, tgt_text, min_length_ratio
                    ):
                        warnings.append(finding)
                    else:
                        failures.append(finding)
    return failures, warnings


def _looks_reviewable_short_target(
    src,
    src_text: str,
    tgt_text: str,
    min_length_ratio: float,
) -> bool:
    """Whether a too-short pair is plausible enough for human review.

    Title-like source nodes are short and lack terminal punctuation. Short prose
    must be close to the ratio threshold and preserve both terminal-punctuation
    kind and sentence count. Structured glossary definitions and index entries
    stay red because corpus evidence shows they are frequent alignment failures.
    """
    if len(src_text) > _SHORT_SOURCE_MAX or _looks_like_index_entry(src, src_text):
        return False
    ratio = len(tgt_text) / len(src_text)
    if not _SOURCE_SENTENCE_END_RE.search(src_text):
        # The measured real titles bottom out near 0.19. Far shorter output is
        # truncation even when the source happens to look title-like.
        return ratio >= max(0.0, min_length_ratio - _TITLE_RATIO_MARGIN)
    if ratio < max(0.0, min_length_ratio - _SHORT_SENTENCE_RATIO_MARGIN):
        return False
    if src.name in {"dd", "dt"}:
        return False
    return (
        _terminal_kind(src_text) == _terminal_kind(tgt_text) != ""
        and _sentence_punctuation_count(src_text)
        == _sentence_punctuation_count(tgt_text)
    )


def _looks_like_index_entry(src, src_text: str) -> bool:
    return src.name == "li" and bool(_INDEX_PAGE_REF_RE.search(src_text))


def _terminal_kind(text: str) -> str:
    stripped = text.rstrip(_CLOSING_PUNCTUATION)
    if not stripped:
        return ""
    final = stripped[-1]
    if final in ".…。":
        return "declarative"
    if final in "?？":
        return "question"
    if final in "!！":
        return "exclamation"
    if final in ":：":
        return "colon"
    if final in ";；":
        return "semicolon"
    return ""


def _sentence_punctuation_count(text: str) -> int:
    return len(_SENTENCE_PUNCTUATION_RE.findall(text))


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
    passed, failures, warnings = audit_with_warnings(
        output, min_length_ratio=resolved_ratio
    )
    status = "fail" if not passed else "warn" if warnings else "pass"
    return AuditResult(
        name="translation_quality",
        status=status,
        failures=failures,
        warnings=warnings,
        details={"output": str(output), "min_length_ratio": resolved_ratio},
    )


def _source_only_exceptions(reader: EPUBReader) -> set[str]:
    for name in reader.namelist():
        if name.endswith("translations/source_only.json"):
            data = json.loads(reader.read(name).decode("utf-8"))
            return set(reasoned_source_only_entries(data))
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


def _offending_simplified_chinese_chars(text: str) -> str:
    if len(_HAN_RE.findall(text or "")) < 8:
        return ""
    seen: set[str] = set()
    chars: list[str] = []
    for char in text:
        if char in SIMPLIFIED_CHINESE_CHARS and char not in seen:
            seen.add(char)
            chars.append(char)
    return "".join(chars)


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
    passed, failures, warnings = audit_with_warnings(
        args.output, min_length_ratio=min_length_ratio
    )
    verdict = "FAIL" if not passed else "WARN" if warnings else "PASS"
    print(f"translation_quality_audit: {verdict}")
    for failure in failures:
        print(f"  - {failure}")
    for warning in warnings:
        print(f"  - warning: {warning}")
    if not failures and not warnings:
        print("all src/tgt paragraph pairs meet length and banned-pattern checks")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
