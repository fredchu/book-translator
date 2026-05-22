#!/usr/bin/env python3
"""Cross-model translation benchmark CLI."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch  # noqa: E402
from epub_reader import EPUBReader  # noqa: E402
from providers import OllamaProvider, ProviderError, ProviderResult  # noqa: E402
from translate_chapter_cli import _build_prompt, _read_chapter_html  # noqa: E402


@dataclass(frozen=True)
class ChapterSource:
    chapter: int
    html: str
    spine_item: str
    source_paragraphs: list[str]
    source_path: Path


@dataclass(frozen=True)
class BenchmarkRecord:
    chapter: int
    model: str
    request_id: str
    result: ProviderResult | None
    elapsed_s_observed: float | None
    error: str | None = None


def _parse_csv(value: str, *, label: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError(f"{label} must contain at least one value")
    return items


def _parse_models(value: str) -> list[str]:
    return _parse_csv(value, label="--models")


def _parse_chapters(value: str) -> list[int]:
    chapters: list[int] = []
    for item in _parse_csv(value, label="--chapters"):
        try:
            chapter = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("--chapters must be comma-separated integers") from exc
        if chapter < 1:
            raise argparse.ArgumentTypeError("--chapters values must be 1-indexed positive integers")
        chapters.append(chapter)
    return chapters


def _model_slug(model: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in model)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Ollama translation benchmarks across models and chapters.")
    parser.add_argument("--book", required=True, type=Path, help="Source EPUB file to benchmark.")
    parser.add_argument("--models", required=True, type=_parse_models, help="Comma-separated Ollama model names.")
    parser.add_argument("--chapters", type=_parse_chapters, default=[5], help="Comma-separated 1-indexed spine XHTML positions.")
    parser.add_argument("--baseline-bocky", type=Path, default=None, help="Optional Bocky bilingual EPUB baseline.")
    parser.add_argument("--baseline-fred", type=Path, default=None, help="Optional Fred bilingual EPUB baseline.")
    parser.add_argument("--out", required=True, type=Path, help="Output directory for benchmark artifacts.")
    parser.add_argument("--book-title", default=None, help="Book title to inject into prompts and comparison output.")
    parser.add_argument("--ollama-host", default="http://localhost:11434", help="Ollama server URL.")
    parser.add_argument("--timeout", type=int, default=1200, help="Per-request Ollama timeout in seconds.")
    parser.add_argument("--num-ctx", type=int, default=32768, help="Ollama context window size.")
    parser.add_argument("--num-predict", type=int, default=8192, help="Ollama maximum generated tokens.")
    parser.add_argument("--temperature", type=float, default=0.3, help="Ollama sampling temperature.")
    return parser


def _read_sources(book: Path, chapters: Sequence[int], out_dir: Path) -> dict[int, ChapterSource]:
    sources: dict[int, ChapterSource] = {}
    for chapter in chapters:
        html, spine_item = _read_chapter_html(book, chapter)
        source_paragraphs = dispatch.html_to_paragraphs(html)
        source_path = out_dir / f"ch{chapter:02d}_source.txt"
        source_path.write_text("\n\n".join(source_paragraphs), encoding="utf-8")
        sources[chapter] = ChapterSource(
            chapter=chapter,
            html=html,
            spine_item=spine_item,
            source_paragraphs=source_paragraphs,
            source_path=source_path,
        )
    return sources


def _extract_baseline_for_chapter(
    baseline_epub: Path,
    *,
    source_spine_item: str,
    chapter: int,
) -> str:
    with EPUBReader(baseline_epub) as reader:
        paths = reader.spine_xhtml_paths()
        if source_spine_item in paths:
            chosen = source_spine_item
        elif 1 <= chapter <= len(paths):
            chosen = paths[chapter - 1]
        else:
            return "# (no Chinese translation in bilingual epub for this spine item)"
        html = reader.read(chosen).decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "lxml")
    paragraphs: list[str] = []
    for p_tag in soup.find_all("p"):
        classes = p_tag.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        if any("tgt" in class_name for class_name in classes):
            text = p_tag.get_text(" ", strip=True)
            if text:
                paragraphs.append(text)
    if not paragraphs:
        return "# (no Chinese translation in bilingual epub for this spine item)"
    return "\n\n".join(paragraphs)


def _write_baselines(
    baselines: dict[str, Path | None],
    *,
    sources: dict[int, ChapterSource],
    out_dir: Path,
) -> dict[tuple[int, str], str]:
    outputs: dict[tuple[int, str], str] = {}
    for label, baseline_epub in baselines.items():
        if baseline_epub is None:
            continue
        for chapter, source in sources.items():
            text = _extract_baseline_for_chapter(
                baseline_epub,
                source_spine_item=source.spine_item,
                chapter=chapter,
            )
            (out_dir / f"ch{chapter:02d}_{label}.txt").write_text(text, encoding="utf-8")
            outputs[(chapter, label)] = text
    return outputs


def _write_translation_outputs(
    *,
    out_dir: Path,
    book: Path,
    book_title: str,
    source: ChapterSource,
    request_id: str,
    result: ProviderResult,
    elapsed_s_observed: float,
) -> None:
    (out_dir / f"{request_id}.txt").write_text(result.raw_text, encoding="utf-8")
    (out_dir / f"{request_id}.meta.json").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "engine": "ollama",
                "model": result.model,
                "book": str(book),
                "book_title": book_title,
                "chapter": source.chapter,
                "spine_item": source.spine_item,
                "source_paragraphs": len(source.source_paragraphs),
                "latency_ms": result.latency_ms,
                "elapsed_s_observed": round(elapsed_s_observed, 2),
                "retries": result.retries,
                "metadata": result.metadata,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _first_paragraph(text: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    return paragraphs[0] if paragraphs else ""


def _paragraph_count(text: str) -> int:
    return len([paragraph for paragraph in text.split("\n\n") if paragraph.strip()])


def _manual_eval_table() -> str:
    return "\n".join(
        [
            "| Dimension | 4b | 12b | 27b | bocky | fred |",
            "|-----------|----|----|----|------|------|",
            "| 段對齊 |  |  |  |  |  |",
            "| 商管術語 |  |  |  |  |  |",
            "| 第一人稱 |  |  |  |  |  |",
            "| 語氣對齊 |  |  |  |  |  |",
            "| 流暢度 |  |  |  |  |  |",
        ]
    )


def _render_comparison(
    *,
    book_title: str,
    chapters: Sequence[int],
    models: Sequence[str],
    sources: dict[int, ChapterSource],
    records: list[BenchmarkRecord],
    baselines: dict[tuple[int, str], str],
    timestamp_iso: str,
) -> str:
    by_pair = {(record.chapter, record.model): record for record in records}
    lines = [
        f"# Benchmark comparison: {book_title}",
        "",
        f"- Book title: {book_title}",
        f"- Chapters: {', '.join(f'ch{chapter:02d}' for chapter in chapters)}",
        f"- Models: {', '.join(models)}",
        f"- ISO timestamp: {timestamp_iso}",
        "",
    ]

    for chapter in chapters:
        source = sources[chapter]
        lines.extend(
            [
                f"## ch{chapter:02d}",
                "",
                f"- Spine item: `{source.spine_item}`",
                f"- Source first paragraph: {source.source_paragraphs[0] if source.source_paragraphs else ''}",
                "",
                "### Model outputs",
                "",
            ]
        )
        for model in models:
            record = by_pair.get((chapter, model))
            if record is None:
                lines.append(f"- {model}: FAILED missing record")
            elif record.error is not None:
                lines.append(f"- {model}: FAILED {record.error}")
            elif record.result is not None:
                lines.append(
                    f"- {model}: first paragraph: {_first_paragraph(record.result.raw_text)}; "
                    f"paragraphs: {_paragraph_count(record.result.raw_text)}; "
                    f"latency_ms: {record.result.latency_ms}"
                )
        for label in ["bocky", "fred"]:
            baseline = baselines.get((chapter, label))
            if baseline is not None:
                lines.append(f"- {label}: first paragraph: {_first_paragraph(baseline)}")
        lines.extend(["", "### Manual eval TODO", "", _manual_eval_table(), ""])

    chapter_headers = [f"ch{chapter:02d}" for chapter in chapters]
    lines.extend(
        [
            "## Latency summary",
            "",
            "| Model | " + " | ".join(chapter_headers) + " | avg source_paragraphs |",
            "|-------|" + "|".join(["---"] * len(chapter_headers)) + "|-----------------------|",
        ]
    )
    avg_source_paragraphs = (
        sum(len(sources[chapter].source_paragraphs) for chapter in chapters) / len(chapters)
        if chapters
        else 0.0
    )
    for model in models:
        cells: list[str] = []
        for chapter in chapters:
            record = by_pair.get((chapter, model))
            if record is None or record.error is not None or record.result is None:
                cells.append("FAILED")
            else:
                cells.append(str(record.result.latency_ms))
        lines.append(f"| {model} | " + " | ".join(cells) + f" | {avg_source_paragraphs:.1f} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    first_provider = OllamaProvider(
        model=args.models[0],
        host=args.ollama_host,
        timeout=args.timeout,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        temperature=args.temperature,
    )
    if not first_provider.ping():
        print(f"ERROR: ollama server unreachable at {args.ollama_host}", file=sys.stderr)
        raise SystemExit(2)

    args.out.mkdir(parents=True, exist_ok=True)
    book_title = args.book_title or args.book.stem
    sources = _read_sources(args.book, args.chapters, args.out)
    baseline_outputs = _write_baselines(
        {"bocky": args.baseline_bocky, "fred": args.baseline_fred},
        sources=sources,
        out_dir=args.out,
    )

    records: list[BenchmarkRecord] = []
    for chapter in args.chapters:
        source = sources[chapter]
        prompt = _build_prompt(source.html, chapter_label=str(chapter), book_title=book_title)
        for model in args.models:
            request_id = f"ch{chapter:02d}_{_model_slug(model)}"
            print(f"[start] chapter={chapter} model={model} request_id={request_id}", file=sys.stderr)
            provider = OllamaProvider(
                model=model,
                host=args.ollama_host,
                timeout=args.timeout,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                temperature=args.temperature,
            )
            started = time.monotonic()
            try:
                result = provider.translate(prompt, request_id=request_id, log_dir=args.out / "_logs")
            except ProviderError as exc:
                elapsed_s = time.monotonic() - started
                records.append(
                    BenchmarkRecord(
                        chapter=chapter,
                        model=model,
                        request_id=request_id,
                        result=None,
                        elapsed_s_observed=elapsed_s,
                        error=str(exc),
                    )
                )
                print(f"[finish] chapter={chapter} model={model} FAILED elapsed_s={elapsed_s:.1f}", file=sys.stderr)
                continue

            elapsed_s = time.monotonic() - started
            _write_translation_outputs(
                out_dir=args.out,
                book=args.book,
                book_title=book_title,
                source=source,
                request_id=request_id,
                result=result,
                elapsed_s_observed=elapsed_s,
            )
            records.append(
                BenchmarkRecord(
                    chapter=chapter,
                    model=model,
                    request_id=request_id,
                    result=result,
                    elapsed_s_observed=elapsed_s,
                )
            )
            print(f"[finish] chapter={chapter} model={model} latency_ms={result.latency_ms}", file=sys.stderr)

    comparison = _render_comparison(
        book_title=book_title,
        chapters=args.chapters,
        models=args.models,
        sources=sources,
        records=records,
        baselines=baseline_outputs,
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
    )
    (args.out / "comparison.md").write_text(comparison, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
