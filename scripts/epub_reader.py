"""Read-only helpers for EPUB zip OPF and spine access."""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ManifestItem:
    href: str
    media_type: str
    properties: str


@dataclass(frozen=True)
class OPFPackage:
    opf_path: str
    manifest: dict[str, ManifestItem]


def find_opf_path(zip_: zipfile.ZipFile) -> str | None:
    """Find the OPF package path in an open EPUB zip."""
    try:
        soup = BeautifulSoup(zip_.read("META-INF/container.xml"), "lxml-xml")
    except KeyError:
        soup = None
    if soup is not None:
        rootfile = soup.find("rootfile", attrs={"media-type": "application/oebps-package+xml"})
        if rootfile and rootfile.get("full-path"):
            return str(rootfile.get("full-path"))
    return next((name for name in zip_.namelist() if name.lower().endswith(".opf")), None)


class EPUBReader:
    """Read-only context manager over an EPUB zip."""

    def __init__(self, epub_path: Path) -> None:
        self.epub_path = epub_path
        self._zip: zipfile.ZipFile | None = None

    def __enter__(self) -> "EPUBReader":
        self._zip = zipfile.ZipFile(self.epub_path)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def opf_package(self) -> OPFPackage | None:
        zip_ = self._require_zip()
        opf_path = find_opf_path(zip_)
        if opf_path is None:
            return None

        soup = BeautifulSoup(zip_.read(opf_path), "lxml-xml")
        opf_dir = posixpath.dirname(opf_path)
        manifest: dict[str, ManifestItem] = {}
        for item in soup.find_all("item"):
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            href = str(item.get("href") or "")
            full_href = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else posixpath.normpath(href)
            manifest[item_id] = ManifestItem(
                href=full_href,
                media_type=str(item.get("media-type") or ""),
                properties=str(item.get("properties") or ""),
            )
        return OPFPackage(opf_path=opf_path, manifest=manifest)

    def spine_xhtml_paths(self, *, include_nav: bool = False) -> list[str]:
        zip_ = self._require_zip()
        package = self.opf_package()
        if package is None:
            return []

        soup = BeautifulSoup(zip_.read(package.opf_path), "lxml-xml")
        names = set(zip_.namelist())
        paths: list[str] = []
        for itemref in soup.find_all("itemref"):
            idref = str(itemref.get("idref") or "")
            item = package.manifest.get(idref)
            if item is None or item.media_type != "application/xhtml+xml":
                continue
            if not include_nav and "nav" in item.properties.split():
                continue
            if item.href in names:
                paths.append(item.href)
        return paths

    def read(self, path: str) -> bytes:
        return self._require_zip().read(path)

    def namelist(self) -> list[str]:
        return self._require_zip().namelist()

    def _require_zip(self) -> zipfile.ZipFile:
        if self._zip is None:
            raise RuntimeError("EPUBReader methods are only valid inside a with block")
        return self._zip
