#!/usr/bin/env python3
"""Build adaptive-concurrency probe prompts from the book about to be translated."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from ebooklib import epub

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch  # noqa: E402
from adaptive_concurrency_probe import REQUIRED  # noqa: E402  single source with the probe
from chunker import chunk_paragraphs  # noqa: E402
from content_blocks import extract_paragraphs  # noqa: E402

LARGE_CHARS = 2500
SEED = 20260910
LOAD_MODES = ("full", "fast")


def prompts_from_epub(book_path: Path) -> list[dict[str, object]]:
    book = epub.read_epub(str(book_path), options={"ignore_ncx": True})
    by_id = {item.get_id(): item for item in book.get_items()}
    prompts: list[dict[str, object]] = []
    for idref, _linear in book.spine:
        item = by_id.get(idref)
        if item is None:
            continue
        try:
            html = item.get_content().decode("utf-8", errors="replace")
        except AttributeError:
            continue
        paragraphs = extract_paragraphs(html)
        if not paragraphs:
            continue
        for index, chunk in enumerate(chunk_paragraphs(paragraphs).chunks):
            system, user = dispatch.build_ollama_chunk_prompt(
                chunk_paragraphs=chunk.paragraphs,
                carryover="",
                fixed_terms={},
            )
            prompts.append(
                {
                    "kind": "chunk",
                    "chapter": str(idref),
                    "idx": index,
                    "system": system,
                    "user": user,
                    "source_chars": sum(len(p) for p in chunk.paragraphs),
                }
            )
    return prompts


def _resolve_required_count(mode: str, requested_count: int | None) -> int:
    """Resolve the caller's load mode without copying the probe's count formula.

    ``full`` is intentionally tied to the REQUIRED exported by the probe.  The
    ``fast`` mode is an explicit mini-sweep contract: its caller must provide a
    positive count (currently spec-10 uses 5 x N=32 = 160).  Keeping that count
    at the call site prevents this builder from growing a second candidate or
    REQUIRED definition that can drift from the probe.
    """
    if mode not in LOAD_MODES:
        choices = ", ".join(LOAD_MODES)
        raise ValueError(f"mode must be one of {choices}, got {mode!r}")
    if requested_count is not None:
        if isinstance(requested_count, bool) or not isinstance(requested_count, int):
            raise ValueError("required_count must be a positive integer")
        if requested_count <= 0:
            raise ValueError("required_count must be a positive integer")
    if mode == "full":
        if requested_count is not None:
            raise ValueError(
                "full mode uses adaptive_concurrency_probe.REQUIRED; "
                "do not override required_count"
            )
        if isinstance(REQUIRED, bool) or not isinstance(REQUIRED, int) or REQUIRED <= 0:
            raise ValueError("adaptive_concurrency_probe.REQUIRED must be a positive integer")
        return REQUIRED
    if requested_count is None:
        raise ValueError("fast mode requires an explicit required_count")
    return requested_count


def _prompt_fingerprint(row: dict[str, object]) -> tuple[object, object]:
    """Return the prompt identity used by prefix-cache implementations."""
    return row["system"], row["user"]


def _unique_prompts(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate exact prompts while preserving source order.

    A repeated source row is not a distinct cold prompt for the probe: both
    vLLM prefix caching and a radix cache identify it by the prompt payload,
    not by the chapter/index metadata around it.  Treating such rows as
    distinct would under-report repetition and make the load look safer than
    it is.
    """
    seen: set[tuple[object, object]] = set()
    unique: list[dict[str, object]] = []
    for row in rows:
        fingerprint = _prompt_fingerprint(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(row)
    return unique


def _select_pool(
    all_prompts: list[dict[str, object]],
    large: list[dict[str, object]],
    required_count: int,
) -> tuple[list[dict[str, object]], str]:
    """Choose a deterministic pool, preferring production-sized chunks.

    The new full load is about 230 prompts.  Mind-Gut has enough *total* real
    chunks, but not necessarily enough chunks above LARGE_CHARS; silently
    repeating the large subset would incorrectly label that book short.  Use
    the largest remaining real chunks when the large subset is insufficient,
    and only repeat after all distinct source material is exhausted.
    """
    unique_all = _unique_prompts(all_prompts)
    unique_large = _unique_prompts(large)
    if len(unique_large) >= required_count:
        return unique_large, "large"
    if not unique_all:
        raise ValueError("no translatable chunks found")
    if len(unique_all) > len(unique_large):
        # Keep the load representative when smaller chunks must be included:
        # larger source chunks are chosen first, with the source order as a
        # deterministic tie-breaker before the seeded shuffle below.
        unique_all = sorted(
            unique_all,
            key=lambda row: int(row["source_chars"]),
            reverse=True,
        )
    # The caller trims a sufficient pool to the requested count after this
    # function returns. If the source is short, retain every row so the caller
    # can disclose and fill the deficit by repetition.
    return unique_all, "all"


def build_load(
    book_path: Path,
    *,
    mode: str = "full",
    required_count: int | None = None,
) -> dict[str, object]:
    effective_required = _resolve_required_count(mode, required_count)
    all_prompts = prompts_from_epub(book_path)
    if not all_prompts:
        raise ValueError(f"no translatable chunks found in {book_path}")
    large = [row for row in all_prompts if int(row["source_chars"]) >= LARGE_CHARS]
    source_pool, pool_kind = _select_pool(all_prompts, large, effective_required)
    available_distinct = len(source_pool)
    # A sufficient pool is trimmed only after ranking it, so a mixed-size book
    # uses its largest real chunks first. The seeded shuffle still prevents
    # source order from becoming a tier-specific bias.
    pool = source_pool[:effective_required]
    rng = random.Random(SEED)
    rng.shuffle(pool)

    repeated = available_distinct < effective_required
    selected: list[dict[str, object]] = [
        dict(pool[index % len(pool)]) for index in range(effective_required)
    ]
    repeated_count = max(0, effective_required - available_distinct)
    repetition_note = (
        "Repeated prompts are not distinct cold chunks: with radix cache disabled "
        "they receive no prefix-cache savings, while vLLM with prefix caching enabled "
        "may reuse them."
        if repeated
        else "All selected prompts are distinct by system/user payload."
    )
    return {
        "book": str(book_path),
        "seed": SEED,
        "mode": mode,
        "requested_count": required_count,
        "required": effective_required,
        "large_source_chars": LARGE_CHARS,
        "available_chunks": len(all_prompts),
        "available_large_chunks": len(large),
        "available_distinct_chunks": len(_unique_prompts(all_prompts)),
        "available_distinct_large_chunks": len(_unique_prompts(large)),
        "pool_kind": pool_kind,
        # Keep this legacy field: adaptive_concurrency_probe.py uses it to
        # permit disclosed duplicate prompts rather than rejecting the load.
        "repeated_for_short_book": repeated,
        "repetition": {
            "status": "repeated" if repeated else "distinct",
            "available_distinct_chunks": available_distinct,
            "required_count": effective_required,
            "repeated_request_count": repeated_count,
            "note": repetition_note,
        },
        "requests": selected,
    }


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic adaptive-concurrency probe load from an EPUB.",
        epilog=(
            "Fast mini-sweep (5 x N=32): python3 scripts/build_adaptive_probe_load.py "
            "--book BOOK.epub --out load.json --mode fast --required-count 160"
        ),
    )
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=LOAD_MODES, default="full")
    parser.add_argument(
        "--required-count",
        type=_positive_int,
        default=None,
        help="explicit request count for --mode fast (for example 160 for 5 x N=32)",
    )
    args = parser.parse_args()
    payload = build_load(args.book, mode=args.mode, required_count=args.required_count)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if payload["pool_kind"] != "large":
        print(
            f"[adaptive-probe] using {payload['available_distinct_chunks']} distinct real chunks "
            f"from the full source pool (large={payload['available_distinct_large_chunks']}); "
            "the large-chunk subset was insufficient for this load",
            file=sys.stderr,
        )
    if payload["repeated_for_short_book"]:
        print(
            f"[adaptive-probe] load mode={payload['mode']} requires "
            f"{payload['required']} requests, "
            f"but only {payload['repetition']['available_distinct_chunks']} distinct chunks "
            "are available; status=repeated. Repeated prompts receive no prefix-cache "
            "savings with radix cache disabled, but vLLM with prefix caching enabled may "
            "reuse them. This is recorded in load.json.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
