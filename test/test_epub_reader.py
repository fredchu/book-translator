from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from epub_reader import EPUBReader, ManifestItem, OPFPackage, find_opf_path  # type: ignore  # noqa: E402


ANIMAL_FARM = Path.home() / "ghkb/interested/bilingual_book_maker/test_books/animal_farm.epub"


def test_animal_farm_fixture_reads_opf_manifest_and_spine():
    if not ANIMAL_FARM.is_file():
        pytest.skip(f"fixture missing: {ANIMAL_FARM}")

    with EPUBReader(ANIMAL_FARM) as reader:
        package = reader.opf_package()
        paths = reader.spine_xhtml_paths(include_nav=True)

    assert isinstance(package, OPFPackage)
    assert package.opf_path.endswith(".opf")
    assert package.manifest
    assert all(isinstance(item, ManifestItem) for item in package.manifest.values())
    assert paths
    assert all(path.endswith((".xhtml", ".html")) for path in paths)


def test_missing_container_uses_opf_fallback(tmp_path: Path):
    epub_path = tmp_path / "fallback.epub"
    _write_epub(epub_path, include_container=False, opf_path="content.opf")

    with zipfile.ZipFile(epub_path) as z:
        assert find_opf_path(z) == "content.opf"

    with EPUBReader(epub_path) as reader:
        package = reader.opf_package()
        assert package is not None
        assert package.opf_path == "content.opf"
        assert reader.spine_xhtml_paths() == ["chapter.xhtml"]


def test_missing_container_and_missing_opf_returns_none(tmp_path: Path):
    epub_path = tmp_path / "missing.epub"
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("chapter.xhtml", "<html><body>chapter</body></html>")

    with EPUBReader(epub_path) as reader:
        assert reader.opf_package() is None
        assert reader.spine_xhtml_paths() == []


def test_nav_in_spine_include_nav_toggle(tmp_path: Path):
    epub_path = tmp_path / "nav.epub"
    _write_epub(
        epub_path,
        manifest_items=[
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="chap" href="chapter.xhtml" media-type="application/xhtml+xml"/>',
        ],
        spine_items=['<itemref idref="nav"/>', '<itemref idref="chap"/>'],
        files={
            "nav.xhtml": "<html><body>nav</body></html>",
            "chapter.xhtml": "<html><body>chapter</body></html>",
        },
    )

    with EPUBReader(epub_path) as reader:
        assert reader.spine_xhtml_paths() == ["chapter.xhtml"]
        assert reader.spine_xhtml_paths(include_nav=True) == ["nav.xhtml", "chapter.xhtml"]


def test_opf_in_subdirectory_normalizes_manifest_paths(tmp_path: Path):
    epub_path = tmp_path / "subdir.epub"
    _write_epub(
        epub_path,
        opf_path="OEBPS/package/content.opf",
        manifest_items=[
            '<item id="chap" href="../xhtml/./chapter.xhtml" media-type="application/xhtml+xml"/>',
        ],
        files={
            "OEBPS/xhtml/chapter.xhtml": "<html><body>chapter</body></html>",
        },
    )

    with EPUBReader(epub_path) as reader:
        package = reader.opf_package()
        assert package is not None
        assert package.manifest["chap"].href == "OEBPS/xhtml/chapter.xhtml"
        assert reader.spine_xhtml_paths() == ["OEBPS/xhtml/chapter.xhtml"]


def _write_epub(
    path: Path,
    *,
    include_container: bool = True,
    opf_path: str = "content.opf",
    manifest_items: list[str] | None = None,
    spine_items: list[str] | None = None,
    files: dict[str, str] | None = None,
) -> None:
    manifest_items = manifest_items or [
        '<item id="chap" href="chapter.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items = spine_items or ['<itemref idref="chap"/>']
    files = files or {"chapter.xhtml": "<html><body>chapter</body></html>"}

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        if include_container:
            z.writestr(
                "META-INF/container.xml",
                f"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
            )
        z.writestr(
            opf_path,
            f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    {chr(10).join(manifest_items)}
  </manifest>
  <spine>{chr(10).join(spine_items)}</spine>
</package>
""",
        )
        for name, content in files.items():
            z.writestr(name, content)
