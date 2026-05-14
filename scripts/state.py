"""Resume state for book-translator.

state.json schema:
    {
        "book": "animal_farm.epub",
        "started": "2026-05-14T03:00:00Z",
        "target_lang": "zh-tw",
        "glossary_built": true,
        "style_confirmed": false,
        "chapters": {
            "item_001": {"output_strategy": "source_only", "status": "source_ready"},
            "item_002": {"output_strategy": "translate", "status": "done",
                         "translation_hash": "abc123", "carryover": "..."},
            "item_003": {"output_strategy": "drop_explicit", "status": "dropped",
                         "reason": "promotional page"}
        }
    }

Output strategy values: "translate" | "source_only" | "nav_generated" | "drop_explicit".
Status values: "pending" | "in_progress" | "done" | "failed" | "source_ready" | "dropped".
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"
SOURCE_READY = "source_ready"
DROPPED = "dropped"

TRANSLATE = "translate"
SOURCE_ONLY = "source_only"
NAV_GENERATED = "nav_generated"
DROP_EXPLICIT = "drop_explicit"

VALID_STATUSES = {PENDING, IN_PROGRESS, DONE, FAILED, SOURCE_READY, DROPPED}
VALID_OUTPUT_STRATEGIES = {TRANSLATE, SOURCE_ONLY, NAV_GENERATED, DROP_EXPLICIT}


def init_state(book_path: Path, spine_or_ids: list[str] | list[dict], target_lang: str) -> dict:
    chapters: dict[str, dict] = {}
    if spine_or_ids and isinstance(spine_or_ids[0], dict):
        for item in spine_or_ids:  # type: ignore[union-attr]
            item_id = str(item["id"])
            strategy = str(item.get("output_strategy", TRANSLATE))
            entry = {"output_strategy": strategy, "status": PENDING}
            if strategy == DROP_EXPLICIT:
                entry["reason"] = str(item.get("reason", ""))
            chapters[item_id] = entry
    else:
        chapters = {str(cid): {"status": PENDING} for cid in spine_or_ids}  # type: ignore[arg-type]

    state = {
        "book": book_path.name,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_lang": target_lang,
        "glossary_built": False,
        "style_confirmed": False,
        "chapters": chapters,
    }
    validate_state(state, require_strategy=bool(spine_or_ids and isinstance(spine_or_ids[0], dict)))
    return state


def load(state_path: Path) -> dict | None:
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    validate_state(state, require_strategy=False)
    return state


def save(state_path: Path, state: dict) -> None:
    validate_state(state, require_strategy=False)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def validate_state(state: dict, require_strategy: bool = False) -> None:
    chapters = state.get("chapters", {})
    if not isinstance(chapters, dict):
        raise ValueError("state.chapters must be a dict")
    for chapter_id, entry in chapters.items():
        status = entry.get("status")
        if status not in VALID_STATUSES:
            raise ValueError(f"{chapter_id}: unknown status {status!r}")
        strategy = entry.get("output_strategy")
        if strategy is None:
            if require_strategy:
                raise ValueError(f"{chapter_id}: missing output_strategy")
            continue
        if strategy not in VALID_OUTPUT_STRATEGIES:
            raise ValueError(f"{chapter_id}: unknown output_strategy {strategy!r}")
        if strategy == DROP_EXPLICIT and not str(entry.get("reason", "")).strip():
            raise ValueError(f"{chapter_id}: drop_explicit requires a non-empty reason")


def mark_done(state: dict, chapter_id: str, translation_text: str) -> None:
    last200 = translation_text[-200:] if len(translation_text) >= 200 else translation_text
    previous = state["chapters"].get(chapter_id, {})
    state["chapters"][chapter_id] = {
        "output_strategy": previous.get("output_strategy", TRANSLATE),
        "status": DONE,
        "translation_hash": hashlib.sha256(translation_text.encode("utf-8")).hexdigest()[:12],
        "carryover": last200,
    }


def mark_failed(state: dict, chapter_id: str, error: str) -> None:
    entry = state["chapters"].get(chapter_id, {})
    retry = int(entry.get("retry_count", 0)) + 1
    updated = {
        "status": FAILED,
        "retry_count": retry,
        "error": error,
    }
    if "output_strategy" in entry:
        updated["output_strategy"] = entry["output_strategy"]
    state["chapters"][chapter_id] = updated


def mark_source_ready(state: dict, chapter_id: str) -> None:
    entry = state["chapters"].get(chapter_id, {})
    state["chapters"][chapter_id] = {
        "output_strategy": entry.get("output_strategy", SOURCE_ONLY),
        "status": SOURCE_READY,
    }


def mark_dropped(state: dict, chapter_id: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("drop_explicit requires a non-empty reason")
    state["chapters"][chapter_id] = {
        "output_strategy": DROP_EXPLICIT,
        "status": DROPPED,
        "reason": reason,
    }


def chapters_by_status(state: dict, status: str) -> list[str]:
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    return [
        cid for cid, entry in state["chapters"].items() if entry.get("status") == status
    ]


def carryover_for(state: dict, chapter_id: str) -> str:
    """Return the previous done chapter's last-200 carryover, or empty string."""
    keys = sorted(state["chapters"].keys())
    if chapter_id not in keys:
        return ""
    idx = keys.index(chapter_id)
    for prev_key in reversed(keys[:idx]):
        prev = state["chapters"].get(prev_key, {})
        if prev.get("status") == DONE:
            return prev.get("carryover", "")
    return ""
