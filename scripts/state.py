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

from dataclasses import dataclass
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:  # pragma: no cover - import mode depends on caller
    from . import manifest as manifest_module
except ImportError:  # pragma: no cover
    import manifest as manifest_module  # type: ignore

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


@dataclass
class ChapterEntry:
    """Represents one chapter row in state.json's `chapters` dict.

    Internal representation only: state.json on disk remains a plain dict.
    Mutations preserve output_strategy, except mark_dropped which explicitly
    replaces it with drop_explicit.
    """

    output_strategy: str
    status: str
    translation_hash: str | None = None
    carryover: str | None = None
    retry_count: int = 0
    error: str | None = None
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ChapterEntry":
        return cls(
            output_strategy=str(data.get("output_strategy") or ""),
            status=str(data.get("status", PENDING)),
            translation_hash=data.get("translation_hash"),
            carryover=data.get("carryover"),
            retry_count=int(data.get("retry_count", 0) or 0),
            error=data.get("error"),
            reason=data.get("reason"),
        )

    def to_dict(self) -> dict:
        entry: dict = {}
        if self.output_strategy:
            entry["output_strategy"] = self.output_strategy
        entry["status"] = self.status
        if self.translation_hash is not None:
            entry["translation_hash"] = self.translation_hash
        if self.carryover is not None:
            entry["carryover"] = self.carryover
        if self.retry_count:
            entry["retry_count"] = self.retry_count
        if self.error is not None:
            entry["error"] = self.error
        if self.reason is not None:
            entry["reason"] = self.reason
        return entry

    def mark_done(self, translation_text: str) -> None:
        self.status = DONE
        self.translation_hash = hashlib.sha256(
            translation_text.encode("utf-8")
        ).hexdigest()[:12]
        self.carryover = (
            translation_text[-200:] if len(translation_text) >= 200 else translation_text
        )
        self.retry_count = 0
        self.error = None
        self.reason = None

    def mark_failed(self, error: str) -> None:
        self.status = FAILED
        self.retry_count += 1
        self.error = error
        self.translation_hash = None
        self.carryover = None
        self.reason = None

    def mark_source_ready(self) -> None:
        self.status = SOURCE_READY
        self.translation_hash = None
        self.carryover = None
        self.retry_count = 0
        self.error = None
        self.reason = None

    def mark_dropped(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("drop_explicit requires a non-empty reason")
        self.output_strategy = DROP_EXPLICIT
        self.status = DROPPED
        self.translation_hash = None
        self.carryover = None
        self.retry_count = 0
        self.error = None
        self.reason = reason


def init_state(book_path: Path, spine_or_ids: list[str] | list[dict], target_lang: str) -> dict:
    chapters: dict[str, dict] = {}
    if spine_or_ids and isinstance(spine_or_ids[0], dict):
        for item in manifest_module.normalize_entries({"spine": spine_or_ids}):  # type: ignore[arg-type]
            item_id = item.id
            strategy = str(item.output_strategy or TRANSLATE)
            entry = {"output_strategy": strategy, "status": PENDING}
            if strategy == DROP_EXPLICIT:
                entry["reason"] = str(item.reason or "")
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
    entry = ChapterEntry.from_dict(state["chapters"].get(chapter_id, {}))
    if entry.output_strategy == "":
        entry.output_strategy = TRANSLATE
    entry.mark_done(translation_text)
    state["chapters"][chapter_id] = entry.to_dict()


def mark_failed(state: dict, chapter_id: str, error: str) -> None:
    entry = ChapterEntry.from_dict(state["chapters"].get(chapter_id, {}))
    entry.mark_failed(error)
    state["chapters"][chapter_id] = entry.to_dict()


def mark_source_ready(state: dict, chapter_id: str) -> None:
    entry = ChapterEntry.from_dict(state["chapters"].get(chapter_id, {}))
    if entry.output_strategy == "":
        entry.output_strategy = SOURCE_ONLY
    entry.mark_source_ready()
    state["chapters"][chapter_id] = entry.to_dict()


def mark_dropped(state: dict, chapter_id: str, reason: str) -> None:
    entry = ChapterEntry.from_dict(state["chapters"].get(chapter_id, {}))
    entry.mark_dropped(reason)
    state["chapters"][chapter_id] = entry.to_dict()


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
