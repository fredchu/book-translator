"""Per-chapter translation log — full prompt + raw response + parsed output.

One JSON file per chapter under `<book_dir>/translation_log/<chapter_id>.json`.
Lets the main session replay a chapter's translation offline (without re-
dispatching the subagent) when an audit catches a regression or the user
wants to inspect what the LLM actually saw vs returned.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_MARKER_COUNT_RE = re.compile(r"\[\[\s*PARA[\s_]*\d+\s*\]\]", re.IGNORECASE)


def _log_dir(book_dir: Path) -> Path:
    return Path(book_dir) / "translation_log"


def write_log_entry(
    *,
    book_dir: Path,
    chapter_id: str,
    prompt: str,
    raw_response: str,
    parsed_translation: str,
    validation_warnings: list[str],
    model: str,
    source_paragraph_count: int | None = None,
) -> Path:
    """Write a per-chapter log entry. Overwrites any prior entry for the same id."""
    log_dir = _log_dir(book_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "chapter_id": chapter_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model,
        "prompt": prompt,
        "raw_response": raw_response,
        "parsed_translation": parsed_translation,
        "validation_warnings": validation_warnings,
    }
    if source_paragraph_count is not None:
        entry["source_paragraph_count"] = source_paragraph_count
        entry["response_marker_count"] = len(_MARKER_COUNT_RE.findall(raw_response or ""))
    path = log_dir / f"{chapter_id}.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_log_entry(*, book_dir: Path, chapter_id: str) -> dict | None:
    path = _log_dir(book_dir) / f"{chapter_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_logged_chapters(book_dir: Path) -> list[str]:
    log_dir = _log_dir(book_dir)
    if not log_dir.is_dir():
        return []
    return sorted(p.stem for p in log_dir.glob("*.json"))
