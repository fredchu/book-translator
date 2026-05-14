"""Audit that internal XHTML href targets in an EPUB resolve to zip entries."""

from __future__ import annotations

import argparse
import posixpath
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup


def audit(output: Path) -> tuple[bool, list[str]]:
    broken: list[str] = []
    with zipfile.ZipFile(output) as z:
        names = set(z.namelist())
        for name in sorted(n for n in names if n.lower().endswith((".xhtml", ".html"))):
            soup = BeautifulSoup(z.read(name), "html.parser")
            for link in soup.find_all("a"):
                href = str(link.get("href") or "")
                target = _resolve_href(name, href)
                if target and target not in names:
                    broken.append(f"{name}: {href} -> {target}")
    return not broken, broken


def _resolve_href(source_name: str, href: str) -> str | None:
    if not href:
        return None
    parsed = urlsplit(href)
    if parsed.scheme in {"http", "https", "mailto", "tel", "urn", "doi"}:
        return None
    if href.startswith("#"):
        return None
    without_fragment = href.split("#", 1)[0]
    if not without_fragment:
        return None
    without_fragment = unquote(without_fragment)
    base = posixpath.dirname(source_name)
    return posixpath.normpath(posixpath.join(base, without_fragment))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.output.is_file():
        print(f"not a file: {args.output}", file=sys.stderr)
        return 2
    passed, broken = audit(args.output)
    print(f"href_resolve_audit: {'PASS' if passed else 'FAIL'}")
    if broken:
        for item in broken:
            print(f"  - {item}")
    else:
        print("all internal href targets resolve")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
