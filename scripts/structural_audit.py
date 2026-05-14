"""Audit that a generated EPUB represents the original OPF spine."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub

sys.path.insert(0, str(Path(__file__).parent))
import extract_epub  # type: ignore  # noqa: E402
import state as state_schema  # type: ignore  # noqa: E402

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")


@dataclass
class Check:
    name: str
    status: str
    details: list[str]


def audit(source: Path, output: Path, book_dir: Path | None = None) -> dict:
    source_book = epub.read_epub(str(source), options={"ignore_ncx": True})
    output_book = epub.read_epub(str(output), options={"ignore_ncx": True})
    source_spine = _source_spine(source, source_book)
    output_idrefs = _spine_idrefs(output_book)
    output_items = _spine_items(output_book)
    output_images = _zip_image_basenames(output)
    manifest = _load_manifest(book_dir)
    manifest_entries = _manifest_entries(manifest)
    state_data = _load_state(book_dir)

    checks: list[Check] = []
    warnings: list[str] = []

    expected_source_count = _expected_source_count(source_spine, manifest_entries)
    checks.append(_check(
        "source_spine_count_vs_output_spine_count",
        len(output_idrefs) >= expected_source_count,
        [
            f"source_spine={len(source_spine)}",
            f"expected_represented_or_dropped_source_items={expected_source_count}",
            f"output_spine={len(output_idrefs)}",
        ],
    ))

    missing = _missing_source_items(source_spine, manifest_entries, output_idrefs)
    checks.append(_check(
        "every_source_spine_item_represented_or_dropped",
        not missing,
        missing or ["all source spine items represented or explicitly dropped"],
    ))

    missing_translations = _missing_translations(book_dir, manifest_entries)
    checks.append(_check(
        "translate_items_have_translation_files",
        not missing_translations,
        missing_translations or ["all translate items have item-id translation files"],
    ))

    invalid_statuses = _invalid_state_statuses(state_data)
    checks.append(_check(
        "state_statuses_match_schema",
        not invalid_statuses,
        invalid_statuses or ["state missing or all statuses valid"],
    ))

    source_has_cover = any(item["role"] == "cover" for item in source_spine)
    output_has_cover = _output_has_cover(manifest_entries, output_idrefs, output_items)
    checks.append(_check(
        "output_cover_spine_item_exists",
        (not source_has_cover) or output_has_cover,
        ["source has no cover spine item" if not source_has_cover else f"output_cover_spine={output_has_cover}"],
    ))

    missing_images = _missing_source_only_images(book_dir, manifest_entries, output_images)
    checks.append(_check(
        "source_only_images_present_in_output",
        not missing_images,
        missing_images or ["all represented source-only image basenames are present"],
    ))

    has_body = _output_has_body_chapter(manifest_entries, output_idrefs, output_items)
    checks.append(_check(
        "output_spine_has_body_chapter",
        has_body,
        [f"body_chapter_present={has_body}"],
    ))

    for entry in manifest_entries:
        if entry.get("output_strategy") == "source_only" and entry.get("role") == "notes":
            warnings.append(f"{entry['id']}: notes kept source-only")
        if entry.get("output_strategy") == "drop_explicit" and entry.get("role") == "promo":
            warnings.append(f"{entry['id']}: promotional page dropped explicitly")

    passed = all(c.status == "PASS" for c in checks)
    return {
        "passed": passed,
        "source_spine_count": len(source_spine),
        "output_spine_count": len(output_idrefs),
        "checks": [asdict(c) for c in checks],
        "warnings": warnings,
    }


def _check(name: str, passed: bool, details: list[str]) -> Check:
    return Check(name=name, status="PASS" if passed else "FAIL", details=details)


def _source_spine(source: Path, book: epub.EpubBook) -> list[dict]:
    _opf_path, opf_manifest = extract_epub._read_opf_manifest(source)
    items: list[dict] = []
    for src_idref, linear in extract_epub._spine_idrefs(book):
        item = book.get_item_with_id(src_idref)
        opf_item = opf_manifest.get(src_idref, {})
        if item is None:
            continue
        html = item.get_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        first_heading = extract_epub._first_heading(soup) or extract_epub._first_text(text) or "(untitled)"
        src_href = str(opf_item.get("href") or item.get_name())
        role = extract_epub.infer_role(
            src_idref=src_idref,
            src_href=src_href,
            first_heading=first_heading,
            properties=str(opf_item.get("properties") or ""),
        )
        items.append({
            "src_idref": src_idref,
            "src_href": src_href,
            "linear": linear,
            "role": role,
            "first_heading": first_heading,
        })
    return items


def _spine_idrefs(book: epub.EpubBook) -> list[str]:
    result = []
    for raw in book.spine:
        result.append(str(raw[0] if isinstance(raw, tuple) else raw))
    return result


def _spine_items(book: epub.EpubBook) -> dict[str, str]:
    items: dict[str, str] = {}
    for idref in _spine_idrefs(book):
        item = book.get_item_with_id(idref)
        items[idref] = item.get_name() if item is not None else ""
    return items


def _load_manifest(book_dir: Path | None) -> dict | None:
    if not book_dir:
        return None
    path = book_dir / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_state(book_dir: Path | None) -> dict | None:
    if not book_dir:
        return None
    path = book_dir / "state.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_entries(manifest: dict | None) -> list[dict]:
    if not manifest:
        return []
    if "spine" in manifest:
        return list(manifest["spine"])
    entries: list[dict] = []
    for entry in manifest.get("chapters", []):
        copied = dict(entry)
        copied.setdefault("src_idref", copied.get("id", ""))
        copied.setdefault("role", extract_epub.infer_role(
            src_idref=str(copied.get("id", "")),
            src_href=str(copied.get("src_href", "")),
            first_heading=str(copied.get("first_heading", "")),
        ))
        copied.setdefault("output_strategy", "translate")
        entries.append(copied)
    return entries


def _entry_for_source(source_item: dict, manifest_entries: list[dict]) -> dict | None:
    for entry in manifest_entries:
        if entry.get("src_idref") == source_item["src_idref"]:
            return entry
        entry_href = str(entry.get("src_href", ""))
        source_href = str(source_item["src_href"])
        if entry_href == source_href or source_href.endswith(entry_href) or entry_href.endswith(source_href):
            return entry
    return None


def _entry_represented(entry: dict, output_idrefs: list[str]) -> bool:
    strategy = entry.get("output_strategy")
    if strategy == "drop_explicit":
        return bool(str(entry.get("reason", "")).strip())
    if strategy == "nav_generated":
        return "nav" in output_idrefs
    return str(entry.get("id")) in output_idrefs


def _expected_source_count(source_spine: list[dict], manifest_entries: list[dict]) -> int:
    if not manifest_entries:
        return len(source_spine)
    count = 0
    for source_item in source_spine:
        entry = _entry_for_source(source_item, manifest_entries)
        if entry and entry.get("output_strategy") == "drop_explicit" and str(entry.get("reason", "")).strip():
            continue
        count += 1
    return count


def _missing_source_items(source_spine: list[dict], manifest_entries: list[dict], output_idrefs: list[str]) -> list[str]:
    if not manifest_entries:
        return ["book-dir/manifest.json missing; cannot prove source spine representation"]
    missing: list[str] = []
    for source_item in source_spine:
        entry = _entry_for_source(source_item, manifest_entries)
        label = f"{source_item['src_href']} — {source_item['first_heading']} ({source_item['role']})"
        if entry is None:
            missing.append(f"missing manifest entry for {label}")
        elif not _entry_represented(entry, output_idrefs):
            missing.append(f"not represented in output: {label}")
    return missing


def _missing_translations(book_dir: Path | None, manifest_entries: list[dict]) -> list[str]:
    if not book_dir:
        return []
    missing: list[str] = []
    for entry in manifest_entries:
        if entry.get("output_strategy") != "translate":
            continue
        path = book_dir / "chapters" / f"{entry['id']}_translation.txt"
        if not path.is_file():
            missing.append(f"{entry['id']}: missing {path.name}")
    return missing


def _invalid_state_statuses(state_data: dict | None) -> list[str]:
    if state_data is None:
        return []
    invalid: list[str] = []
    for item_id, entry in state_data.get("chapters", {}).items():
        status = entry.get("status")
        strategy = entry.get("output_strategy")
        if status not in state_schema.VALID_STATUSES:
            invalid.append(f"{item_id}: unknown status {status!r}")
        if strategy is not None and strategy not in state_schema.VALID_OUTPUT_STRATEGIES:
            invalid.append(f"{item_id}: unknown output_strategy {strategy!r}")
        if strategy == state_schema.DROP_EXPLICIT and not str(entry.get("reason", "")).strip():
            invalid.append(f"{item_id}: drop_explicit missing reason")
    return invalid


def _output_has_cover(manifest_entries: list[dict], output_idrefs: list[str], output_items: dict[str, str]) -> bool:
    for entry in manifest_entries:
        if entry.get("role") == "cover" and entry.get("id") in output_idrefs:
            return True
    for idref, href in output_items.items():
        if "cover" in f"{idref} {href}".lower():
            return True
    return False


def _missing_source_only_images(book_dir: Path | None, manifest_entries: list[dict], output_images: set[str]) -> list[str]:
    if not book_dir:
        return []
    missing: list[str] = []
    for entry in manifest_entries:
        if entry.get("output_strategy") != "source_only":
            continue
        html_path = book_dir / str(entry.get("href", ""))
        if not html_path.is_file():
            continue
        for image in sorted(_referenced_images(html_path.read_text(encoding="utf-8"))):
            if image not in output_images:
                missing.append(f"{entry['id']}: source-only image missing from output EPUB: {image}")
    return missing


def _referenced_images(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    refs: set[str] = set()
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if src and not src.startswith("data:"):
            refs.add(Path(src).name)
    return refs


def _zip_image_basenames(epub_path: Path) -> set[str]:
    with zipfile.ZipFile(epub_path) as z:
        return {
            Path(name).name
            for name in z.namelist()
            if name.lower().endswith(IMAGE_SUFFIXES)
        }


def _output_has_body_chapter(manifest_entries: list[dict], output_idrefs: list[str], output_items: dict[str, str]) -> bool:
    for entry in manifest_entries:
        if entry.get("role") in {"body", "epilogue"} and entry.get("id") in output_idrefs:
            return True
    return any(idref != "nav" and "nav" not in href.lower() for idref, href in output_items.items())


def _print_human(report: dict) -> None:
    print(f"structural_audit: {'PASS' if report['passed'] else 'FAIL'}")
    print(f"source_spine_count={report['source_spine_count']}")
    print(f"output_spine_count={report['output_spine_count']}")
    for check in report["checks"]:
        print(f"{check['status']} {check['name']}")
        for detail in check["details"]:
            print(f"  - {detail}")
    if report["warnings"]:
        print("WARNINGS")
        for warning in report["warnings"]:
            print(f"  - {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--book-dir", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    for path in (args.source, args.output):
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            return 2
    if args.book_dir and not args.book_dir.is_dir():
        print(f"not a directory: {args.book_dir}", file=sys.stderr)
        return 2

    report = audit(args.source, args.output, args.book_dir)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
