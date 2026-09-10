from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILDER_PATH = REPO / "scripts" / "build_adaptive_probe_load.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_adaptive_probe_load_test", BUILDER_PATH)
    assert spec and spec.loader
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder


def _rows(count: int, *, source_chars: int = 3000) -> list[dict[str, object]]:
    return [
        {
            "kind": "chunk",
            "chapter": "chapter",
            "idx": index,
            "system": "system",
            "user": f"unique source chunk {index}",
            "source_chars": source_chars,
        }
        for index in range(count)
    ]


def test_required_is_imported_from_probe_without_local_definition() -> None:
    tree = ast.parse(BUILDER_PATH.read_text(encoding="utf-8"))
    imports_required = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "adaptive_concurrency_probe"
        and any(alias.name == "REQUIRED" and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imports_required
    local_required_assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "REQUIRED" for target in targets):
            local_required_assignments.append(node)
    assert local_required_assignments == []


def test_full_mode_uses_probe_required_and_distinct_real_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    rows = _rows(263)
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: rows)

    load = builder.build_load(Path("book.epub"))

    assert load["mode"] == "full"
    assert load["requested_count"] is None
    assert load["required"] == builder.REQUIRED == 230
    assert len(load["requests"]) == 230
    assert load["repeated_for_short_book"] is False
    assert load["repetition"]["status"] == "distinct"
    assert load["repetition"]["repeated_request_count"] == 0
    assert len({(row["system"], row["user"]) for row in load["requests"]}) == 230


def test_fast_mode_accepts_explicit_160_request_mini_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    rows = _rows(160)
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: rows)

    load = builder.build_load(Path("book.epub"), mode="fast", required_count=160)

    assert load["mode"] == "fast"
    assert load["requested_count"] == 160
    assert load["required"] == 160
    assert load["repeated_for_short_book"] is False
    assert load["repetition"]["status"] == "distinct"
    assert len({(row["system"], row["user"]) for row in load["requests"]}) == 160
    serialized = json.loads(json.dumps(load, ensure_ascii=False))
    assert serialized["mode"] == "fast"
    assert serialized["requested_count"] == 160
    assert serialized["required"] == 160
    assert serialized["repetition"]["status"] == "distinct"


def test_mixed_size_fallback_uses_unique_large_then_largest_smaller_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    large_rows = _rows(100, source_chars=3000)
    smaller_rows = _rows(100, source_chars=2000)
    for index, row in enumerate(smaller_rows):
        row["user"] = f"unique smaller source chunk {index}"
        row["source_chars"] = 2000 - index
    rows = large_rows + smaller_rows
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: rows)

    load = builder.build_load(Path("book.epub"), mode="fast", required_count=160)

    selected_users = {row["user"] for row in load["requests"]}
    expected_large = {f"unique source chunk {index}" for index in range(100)}
    expected_smaller = {
        f"unique smaller source chunk {index}" for index in range(60)
    }
    assert len(load["requests"]) == 160
    assert len({(row["system"], row["user"]) for row in load["requests"]}) == 160
    assert load["repeated_for_short_book"] is False
    assert load["repetition"]["status"] == "distinct"
    assert load["pool_kind"] == "all"
    assert expected_large <= selected_users
    assert selected_users - expected_large == expected_smaller


def test_short_corpus_repeats_deterministically_and_discloses_cache_difference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    rows = _rows(3)
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: rows)

    first = builder.build_load(Path("book.epub"), mode="fast", required_count=160)
    second = builder.build_load(Path("book.epub"), mode="fast", required_count=160)

    assert first == second
    assert len(first["requests"]) == 160
    assert first["repeated_for_short_book"] is True
    assert first["repetition"]["status"] == "repeated"
    assert first["repetition"]["available_distinct_chunks"] == 3
    assert first["repetition"]["required_count"] == 160
    assert first["repetition"]["repeated_request_count"] == 157
    users = [(row["system"], row["user"]) for row in first["requests"]]
    assert len(set(users)) == 3
    assert len(users) - len(set(users)) == 157
    note = str(first["repetition"]["note"])
    assert "radix cache disabled" in note
    assert "vLLM" in note

    serialized = json.loads(json.dumps(first, ensure_ascii=False))
    assert serialized["repeated_for_short_book"] is True
    assert serialized["repetition"]["status"] == "repeated"
    assert serialized["repetition"]["repeated_request_count"] == 157
    assert len({(row["system"], row["user"]) for row in serialized["requests"]}) == 3


def test_cli_fast_mode_records_count_and_warns_for_repetition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    builder = _load_builder()
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: _rows(2))
    output = tmp_path / "load.json"
    monkeypatch.setattr(
        builder.sys,
        "argv",
        [
            "build_adaptive_probe_load.py",
            "--book",
            "book.epub",
            "--out",
            str(output),
            "--mode",
            "fast",
            "--required-count",
            "160",
        ],
    )

    assert builder.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    stderr = capsys.readouterr().err
    assert payload["mode"] == "fast"
    assert payload["requested_count"] == 160
    assert payload["required"] == 160
    assert payload["repetition"]["status"] == "repeated"
    assert "status=repeated" in stderr
    assert "radix cache" in stderr
    assert "vLLM" in stderr


@pytest.mark.parametrize(
    ("mode", "required_count", "message"),
    [
        ("unknown", None, "mode must be one of"),
        ("full", 160, "full mode uses"),
        ("fast", None, "fast mode requires"),
        ("fast", 0, "positive integer"),
        ("fast", -1, "positive integer"),
        ("fast", 1.5, "positive integer"),
        ("fast", True, "positive integer"),
    ],
)
def test_mode_and_required_count_contract_rejects_invalid_combinations(
    mode: str,
    required_count: object,
    message: str,
) -> None:
    builder = _load_builder()
    with pytest.raises(ValueError, match=message):
        builder._resolve_required_count(mode, required_count)  # type: ignore[arg-type]


def test_empty_source_still_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = _load_builder()
    monkeypatch.setattr(builder, "prompts_from_epub", lambda _path: [])

    with pytest.raises(ValueError, match="no translatable chunks"):
        builder.build_load(Path("empty.epub"), mode="fast", required_count=160)


def test_malformed_source_row_still_fails_instead_of_silently_filling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    monkeypatch.setattr(
        builder,
        "prompts_from_epub",
        lambda _path: [{"kind": "chunk", "system": "s", "user": "u", "source_chars": "bad"}],
    )

    with pytest.raises(ValueError, match="invalid literal"):
        builder.build_load(Path("malformed.epub"), mode="fast", required_count=160)
