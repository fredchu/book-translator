#!/usr/bin/env python3
"""Build a review queue for book-specific terminology.

The source text decides whether a key exists. An LLM may propose candidates and
translations, but only a user can approve the Chinese wording. The output is a
ranked review queue, not an automatically authoritative ``spec_terms.json``.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable

from bs4 import BeautifulSoup

try:
    from scripts.offline_postprocess import _converter, _simplified_triggers
    from scripts.paragraph_classification import untranslated_reason
    from scripts.term_matching import (
        count_term_occurrences, count_term_occurrences_normalized,
        iter_term_matches_normalized, normalize_match_text,
        normalize_match_text_with_map, term_occurs, term_occurs_normalized,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from offline_postprocess import _converter, _simplified_triggers
    from paragraph_classification import untranslated_reason
    from term_matching import (
        count_term_occurrences, count_term_occurrences_normalized,
        iter_term_matches_normalized, normalize_match_text,
        normalize_match_text_with_map, term_occurs, term_occurs_normalized,
    )

_BODY_EXCLUDED_REASONS = {"bibliography", "index", "notes", "publication_metadata"}
_PERSON_GROUPS = {"people", "person", "persons", "name", "names"}
_WORD_RE = re.compile(
    r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’\-‐‑‒–—][A-Za-zÀ-ÖØ-öø-ÿ]+)*"
)
_CAPITAL_WORD = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]*"
_NAME_RE = re.compile(
    rf"(?<![A-Za-z])(?:Dr\.|Mr\.|Mrs\.|Ms\.|Prof\.)?\s*{_CAPITAL_WORD}"
    rf"(?:(?:\s+(?:(?:of|the|for|and|on|at|in)\s+)?|,\s*)"
    rf"(?:[A-Z]\.|{_CAPITAL_WORD})){{0,7}}"
)
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}(?![A-Za-z0-9])")
_CAPITAL_TOKEN_RE = re.compile(rf"(?<![A-Za-z]){_CAPITAL_WORD}(?![A-Za-z])")
_TITLE_TOKENS = {"dr", "mr", "mrs", "ms", "prof", "professor"}
# Conservative review-only heuristic. A false positive merely asks a user to
# verify the original Han name; it never manufactures a translation.
_EAST_ASIAN_SURNAMES = {
    "chen", "choi", "huang", "kim", "lee", "li", "lin", "liu", "nakamura",
    "park", "sato", "suzuki", "wang", "wu", "yang", "zhang", "zhao", "zhou",
}
_NGRAM_EDGE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by",
    "for", "from", "he", "her", "his", "i", "in", "is", "it", "its", "my",
    "of", "on", "or", "our", "she", "that", "the", "their", "them", "these",
    "this", "those", "to", "use", "was", "we", "were", "will", "with",
    "you", "your", "can", "could", "did", "do", "does", "had", "has", "have",
    "may", "might", "must", "no", "not", "should", "would",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_possessive(text: str) -> str:
    return re.sub(r"(?:['’]s)$", "", text, flags=re.IGNORECASE)


def _html_units(raw: bytes, path: str) -> list[dict]:
    soup = BeautifulSoup(raw, "html.parser")
    units = []
    for node in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]):
        if node.find_parent(["p", "li", "blockquote"]):
            continue
        text = _clean(node.get_text(" ", strip=True))
        if not text:
            continue
        reason = untranslated_reason(path, node, text)
        units.append({"text": text, "path": path, "excluded_reason": reason if reason in _BODY_EXCLUDED_REASONS else None})
    return units


def read_source_units(source: Path) -> list[dict]:
    """Read EPUB, HTML directory, or UTF-8 text into located paragraph units."""
    source = Path(source)
    if source.suffix.casefold() == ".epub":
        units = []
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                if name.casefold().endswith((".xhtml", ".html", ".htm")):
                    units.extend(_html_units(archive.read(name), name))
        return units
    if source.is_dir():
        units = []
        for path in sorted(source.rglob("*")):
            if path.suffix.casefold() in {".xhtml", ".html", ".htm"}:
                units.extend(_html_units(path.read_bytes(), str(path.relative_to(source))))
            elif path.suffix.casefold() in {".txt", ".md"}:
                units.extend({"text": p, "path": str(path.relative_to(source)), "excluded_reason": None} for p in split_paragraphs(path.read_text(encoding="utf-8")))
        return units
    return [{"text": p, "path": source.name, "excluded_reason": None} for p in split_paragraphs(source.read_text(encoding="utf-8"))]


def split_paragraphs(text: str) -> list[str]:
    return [_clean(part) for part in re.split(r"\n\s*\n", text) if _clean(part)]


def body_units(units: Iterable[dict]) -> list[dict]:
    return [unit for unit in units if not unit.get("excluded_reason")]


def load_proposals(paths: Iterable[Path]) -> list[dict]:
    rows = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("proposals"), list):
            data = data["proposals"]
        elif isinstance(data, dict) and isinstance(data.get("terms"), list):
            data = data["terms"]
        elif isinstance(data, dict) and isinstance(data.get("terms"), dict):
            data = [{"en": en, "zh": zh} for en, zh in data["terms"].items()]
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected a JSON list, proposals list, or terms mapping")
        for row in data:
            if isinstance(row, dict) and (row.get("en") or row.get("key")):
                copied = dict(row)
                copied["en"] = str(row.get("en") or row.get("key")).strip()
                copied["zh"] = str(row.get("zh") or row.get("proposed_zh") or "").strip()
                copied["proposal_source"] = str(path)
                rows.append(copied)
    return rows


def _source_spelling(text: str, source_indexes: list[int], match) -> str:
    return text[source_indexes[match.start()]:source_indexes[match.end() - 1] + 1]


def _preferred_spelling(spellings: list[str]) -> str | None:
    if not spellings:
        return None
    counts = Counter(spellings)
    return min(
        counts,
        key=lambda spelling: (
            -counts[spelling],
            sum(char.isupper() for char in spelling),
            spelling.casefold(),
        ),
    )


def _literal_source_occurs(term: str, text: str) -> bool:
    return bool(re.search(
        rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text
    ))


def _actual_forms(
    proposal: dict, text: str, normalized_text: str, source_indexes: list[int]
) -> list[str]:
    hints = [proposal["en"]] + [str(x) for x in proposal.get("all_forms", [])]
    forms = []
    for hint in hints:
        if not hint:
            continue
        # Keep a literal proposal spelling. Otherwise a title encountered first
        # can turn a lowercase key into a case-sensitive key that misses prose.
        if _literal_source_occurs(hint, text):
            if hint not in forms:
                forms.append(hint)
            continue
        matches = list(iter_term_matches_normalized(hint, normalized_text))
        preferred = _preferred_spelling([
            _source_spelling(text, source_indexes, match) for match in matches
        ])
        if preferred and preferred not in forms:
            forms.append(preferred)
    # Proper multiword proposals may differ only in sentence-case styling.
    # Rewrite the key to the literal source spelling rather than dropping it.
    proposal_words = _WORD_RE.findall(proposal["en"])
    if not forms and len(proposal_words) >= 2:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(normalize_match_text(proposal['en']))}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(normalized_text))
        preferred = _preferred_spelling([
            _source_spelling(text, source_indexes, match) for match in matches
        ])
        if preferred:
            forms.append(preferred)
    # Demonym/adjectival place forms are a narrow morphology correction seen
    # in LLM proposals (Okinawa -> Okinawan), not a runtime fuzzy match.
    if not forms and proposal["en"][:1].isupper() and len(proposal_words) == 1:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(normalize_match_text(proposal['en']))}(?:n|an|ian)(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(normalized_text))
        preferred = _preferred_spelling([
            _source_spelling(text, source_indexes, match) for match in matches
        ])
        if preferred:
            forms.append(preferred)

    # LLMs often add or omit a middle initial. First+last must both agree;
    # surname-only evidence would turn a hallucinated George Donner into the
    # real Jacob Donner and is therefore insufficient.
    person_tokens = _person_tokens(proposal["en"])
    if len(person_tokens) >= 2:
        first, last = person_tokens[0], person_tokens[-1]
        variant = re.search(
            rf"(?<![A-Za-z]){re.escape(first)}(?:\s+[A-Z]\.)?\s+{re.escape(last)}(?![A-Za-z])",
            text,
            re.IGNORECASE,
        )
        if variant and variant.group(0) not in forms:
            forms.append(variant.group(0))
    return forms


def _person_tokens(name: str) -> list[str]:
    return [
        token.casefold().rstrip(".")
        for token in _WORD_RE.findall(name)
        if token.casefold().rstrip(".") not in _TITLE_TOKENS and len(token.rstrip(".")) > 1
    ]


def _standalone_name_token(text: str, token: str):
    """Find a token used as its own name form, not merely inside a full name."""
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", re.IGNORECASE)
    for match in pattern.finditer(text):
        if not match.group(0)[:1].isupper():
            continue
        before = text[max(0, match.start() - 40):match.start()]
        after = text[match.end():match.end() + 40]
        previous = re.search(r"(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]*|[A-Z]\.|Dr\.|Mr\.|Mrs\.|Ms\.|Prof\.)[\s-]+$", before)
        following = re.match(r"[\s-]+(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]*|[A-Z]\.)", after)
        if not previous and not following:
            return match
    return None


def add_person_forms(rows: list[dict], text: str) -> tuple[dict[int, list[str]], list[dict], dict[int, int]]:
    """Add safe corpus forms, merging subset name variants into one entity."""
    people = [(i, row) for i, row in enumerate(rows) if str(row.get("group", "")).casefold() in _PERSON_GROUPS]
    parent = {i: i for i, _ in people}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    cores = {i: set(_person_tokens(row["en"])) for i, row in people}
    for pos, (i, _) in enumerate(people):
        for j, _ in people[pos + 1:]:
            if cores[i] and cores[j] and (cores[i] <= cores[j] or cores[j] <= cores[i]):
                union(i, j)
    entity_of = {i: find(i) for i, _ in people}
    token_owners: dict[str, set[int]] = defaultdict(set)
    entity_rows: dict[int, list[int]] = defaultdict(list)
    for i, _ in people:
        entity_rows[entity_of[i]].append(i)
        for token in cores[i]:
            token_owners[token].add(entity_of[i])

    added: dict[int, list[str]] = defaultdict(list)
    collisions = []
    for token, owners in sorted(token_owners.items()):
        spelling_match = _standalone_name_token(text, token)
        if not spelling_match:
            continue
        if len(owners) == 1:
            entity = next(iter(owners))
            i = min(entity_rows[entity])
            actual = spelling_match.group(0)
            if actual.casefold() not in {form.casefold() for form in added[i]}:
                added[i].append(actual)
            surname = _person_tokens(rows[i]["en"])[-1:] or [token]
            for title in ("Dr.", "Mr.", "Mrs.", "Ms.", "Prof."):
                titled = re.search(rf"(?<![A-Za-z]){re.escape(title)}\s+{re.escape(surname[0])}(?![A-Za-z])", text, re.IGNORECASE)
                if titled and titled.group(0) not in added[i]:
                    added[i].append(titled.group(0))
        else:
            names = [rows[i]["en"] for entity in sorted(owners) for i in entity_rows[entity]]
            collisions.append({"token": token, "entities": names, "action": "user_review"})
    return added, collisions, entity_of


def extract_mechanical_candidates(text: str, zipf: Callable[[str], float]) -> dict[str, dict]:
    """Extract names plus uncommon 1–3 grams; uppercase sequences emit subwindows."""
    found: dict[str, dict] = {}
    name_counts = Counter()
    for match in _NAME_RE.finditer(text):
        phrase = _strip_possessive(_clean(match.group(0)))
        tokens = [_strip_possessive(token) for token in phrase.split()]
        phrase = " ".join(tokens)
        # A sentence-initial capitalized word is not mechanically a name.
        if len(tokens) < 2 and not phrase.startswith(("Dr. ", "Mr. ", "Mrs. ", "Ms. ", "Prof. ")):
            continue
        variants = {phrase}
        for size in (2, 3):
            variants.update(" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1))
        name_counts.update(item for item in variants if len(item) >= 3)
    for item, count in name_counts.items():
        found[item] = {"key": item, "mechanical_kind": "proper_name", "count": count}
    for item, count in Counter(_ACRONYM_RE.findall(text)).items():
        found.setdefault(item, {"key": item, "mechanical_kind": "acronym", "count": count})
    capital_tokens = Counter(
        _strip_possessive(item) for item in _CAPITAL_TOKEN_RE.findall(text)
    )
    for item, count in capital_tokens.items():
        if item.casefold() in _NGRAM_EDGE_STOPWORDS:
            continue
        if count >= 2 or zipf(item) < 3.3:
            found.setdefault(item, {"key": item, "mechanical_kind": "proper_token", "count": count})
    counts = Counter()
    first_spelling = {}
    # Never form an n-gram across punctuation or paragraph boundaries: such a
    # candidate could never pass the runtime injection matcher.
    for segment in re.split(r"[^A-Za-zÀ-ÖØ-öø-ÿ'’\-‐‑‒–—\s]+|\n", text):
        words = [_strip_possessive(word) for word in _WORD_RE.findall(segment)]
        for size in (1, 2, 3):
            for i in range(len(words) - size + 1):
                actual = " ".join(words[i:i + size])
                edge_tokens = [
                    normalize_match_text(word).casefold()
                    for word in words[i:i + size]
                ]
                if size > 1 and (
                    edge_tokens[0] in _NGRAM_EDGE_STOPWORDS
                    or edge_tokens[-1] in _NGRAM_EDGE_STOPWORDS
                ):
                    continue
                identity = normalize_match_text(actual).casefold()
                counts[identity] += 1
                first_spelling.setdefault(identity, actual)
    for identity, count in counts.items():
        phrase = first_spelling[identity]
        if count < 3 or len(phrase) < 4 or zipf(phrase) >= 4.3:
            continue
        found.setdefault(phrase, {"key": phrase, "mechanical_kind": "uncommon_ngram", "count": count})
    return found


def _paragraphs_for_comparison(path: Path) -> list[str]:
    return split_paragraphs(Path(path).read_text(encoding="utf-8"))


def _paragraphs_for_comparison_chapter(path: Path) -> list[str]:
    try:
        from scripts.dispatch import html_to_paragraphs
    except ModuleNotFoundError:  # pragma: no cover - direct script execution
        from dispatch import html_to_paragraphs
    return html_to_paragraphs(Path(path).read_text(encoding="utf-8"))


def impact_counts(rows: list[dict], source_paragraphs: list[str], baseline: list[str], termed: list[str]) -> dict[str, int]:
    if not (len(source_paragraphs) == len(baseline) == len(termed)):
        raise ValueError(
            "comparison paragraphs are not aligned: "
            f"source={len(source_paragraphs)}, baseline={len(baseline)}, termed={len(termed)}"
        )
    result = {}
    for row in rows:
        key, zh = row["key"], row.get("proposed_zh", "")
        changed = 0
        if zh:
            for src, before, after in zip(source_paragraphs, baseline, termed):
                occurrences = count_term_occurrences(key, src)
                if occurrences and zh not in before and zh in after:
                    changed += occurrences
        result[key] = changed
    return result


def _zipf_scorer() -> Callable[[str], float]:
    try:
        from wordfreq import zipf_frequency
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("build_terms requires wordfreq; install requirements.txt") from exc
    @functools.lru_cache(maxsize=65536)
    def score(text: str) -> float:
        return float(zipf_frequency(text, "en"))
    return score


def build_review_queue(
    units: list[dict],
    proposals: list[dict],
    *,
    zipf: Callable[[str], float],
    comparison: tuple[list[str], list[str], list[str]] | None = None,
    max_mechanical_references: int = 300,
) -> dict:
    body = body_units(units)
    text = "\n\n".join(unit["text"] for unit in body)
    normalized_text, source_indexes = normalize_match_text_with_map(text)
    normalized_body = [(unit, normalize_match_text(unit["text"])) for unit in body]
    converter = _converter()
    if converter is None:
        raise RuntimeError("opencc is required: cannot validate proposed Chinese safely")
    simplified = _simplified_triggers(converter)
    if not simplified:
        raise RuntimeError("opencc Simplified trigger set is unavailable or empty")
    dropped, conflicts = [], []
    accepted = []
    mechanically_safe_person_forms: set[str] = set()

    person_additions, collisions, person_entities = add_person_forms(proposals, text)
    seen: dict[str, dict] = {}
    for i, proposal in enumerate(proposals):
        forms = _actual_forms(proposal, text, normalized_text, source_indexes)
        if forms:
            forms.extend(form for form in person_additions.get(i, []) if form not in forms)
            if str(proposal.get("group", "")).casefold() in _PERSON_GROUPS:
                mechanically_safe_person_forms.update(
                    normalize_match_text(form).casefold() for form in forms
                )
        if not forms:
            dropped.append({"key": proposal["en"], "proposed_zh": proposal.get("zh", ""), "reason": "not_found_in_body", "proposal_source": proposal.get("proposal_source")})
            continue
        for form in forms:
            identity = normalize_match_text(form).casefold()
            existing = seen.get(identity)
            if existing:
                if proposal.get("zh") and existing.get("proposed_zh") and proposal["zh"] != existing["proposed_zh"]:
                    conflicts.append({"key": form, "zh_values": [existing["proposed_zh"], proposal["zh"]], "action": "user_review"})
                continue
            count = count_term_occurrences_normalized(form, normalized_text)
            contexts = []
            for unit, normalized_unit in normalized_body:
                if term_occurs_normalized(form, normalized_unit):
                    contexts.append({"path": unit["path"], "text": unit["text"][:240]})
                    if len(contexts) == 2:
                        break
            score = zipf(form)
            group = str(proposal.get("group", "unknown")).casefold()
            item = {
                "key": form,
                "proposed_zh": proposal.get("zh", ""),
                "group": group,
                "body_occurrences": count,
                "contexts": contexts,
                "zipf_frequency": round(score, 2),
                "match_normalizations": ["apostrophe", "hyphen", "diacritic", "alphanumeric_boundary"],
                "proposal_source": proposal.get("proposal_source"),
                "proposal_en": proposal["en"],
                "entity_id": proposal.get("entity_id") or (
                    f"person:{person_entities[i]}" if i in person_entities else None
                ),
            }
            if proposal.get("distinguish_from"):
                item["distinguish_from"] = proposal["distinguish_from"]
            if proposal.get("user_decided"):
                item["user_decided"] = True
            person_tokens = set(_person_tokens(proposal["en"]))
            east_asian_hint = (
                proposal.get("east_asian")
                or str(proposal.get("origin", "")).casefold() in {"east_asian", "east-asian"}
                or (group in _PERSON_GROUPS and bool(person_tokens & _EAST_ASIAN_SURNAMES))
            )
            if east_asian_hint:
                item["zh_review"] = "verify_original_hanzi"
            residue = sorted(set(item["proposed_zh"]) & simplified)
            if residue:
                item["zh_simplified_triggers"] = residue
            seen[identity] = item
            accepted.append(item)

    entity_zh: dict[str, set[str]] = defaultdict(set)
    for item in accepted:
        if item.get("entity_id") and item.get("proposed_zh"):
            entity_zh[item["entity_id"]].add(item["proposed_zh"])
    for entity_id, values in entity_zh.items():
        if len(values) > 1:
            conflicts.append({
                "entity_id": entity_id,
                "zh_values": sorted(values),
                "action": "user_review_same_entity_translation",
            })

    mechanical = extract_mechanical_candidates(text, zipf)
    mechanical_keys = {normalize_match_text(key).casefold(): value for key, value in mechanical.items()}
    for item in accepted:
        hit = mechanical_keys.pop(normalize_match_text(item["key"]).casefold(), None)
        item["extraction_sources"] = ["llm_proposal"] + (["mechanical"] if hit else [])

    extra = sorted(mechanical_keys.values(), key=lambda x: (-x["count"], zipf(x["key"])))[:max_mechanical_references]
    for row in extra:
        accepted.append({
            "key": row["key"], "proposed_zh": "", "group": "mechanical_candidate",
            "body_occurrences": row["count"], "contexts": [],
            "zipf_frequency": round(zipf(row["key"]), 2),
            "extraction_sources": ["mechanical"], "mechanical_kind": row["mechanical_kind"],
            "match_normalizations": ["apostrophe", "hyphen", "diacritic", "alphanumeric_boundary"],
        })

    collision_tokens = {
        normalize_match_text(row["token"]).casefold() for row in collisions
    }
    accepted = [
        item for item in accepted
        if normalize_match_text(item["key"]).casefold() not in collision_tokens
    ]

    impacts = impact_counts(accepted, *comparison) if comparison else {}
    for item in accepted:
        reasons = []
        impact = impacts.get(item["key"], 0)
        item["translation_change_count"] = impact
        if impact:
            reasons.append(f"paired_translation_changed:{impact}")
        if item.get("distinguish_from"):
            reasons.append("concept_requires_distinction")
        if item.get("user_decided"):
            reasons.append("user_selected_wording")
        if item.get("zh_review"):
            reasons.append("proper_name_requires_original_hanzi")
        z = item["zipf_frequency"]
        proposal_backed = "llm_proposal" in item.get("extraction_sources", [])
        if proposal_backed and not impact:
            reasons.append("llm_proposal_backed")
        mechanically_proper = item.get("mechanical_kind") in {
            "proper_name", "proper_token", "acronym",
        }
        multiword = len(_WORD_RE.findall(item["key"])) > 1
        middle_needs_review = (
            3.3 <= z < 4.3
            and (proposal_backed or mechanically_proper or multiword)
        )
        if (
            z >= 4.3
            and item["group"] not in _PERSON_GROUPS
            and not item.get("distinguish_from")
            and not item.get("user_decided")
        ):
            item["decision"] = "excluded_common_word"
            reasons.append("wordfreq_high:auto_exclude")
        elif (
            impact
            or item.get("distinguish_from")
            or item.get("user_decided")
            or item.get("zh_review")
            or middle_needs_review
        ):
            item["decision"] = "needs_user_decision"
            if 3.3 <= z < 4.3:
                reasons.append("wordfreq_middle:user_review")
        else:
            item["decision"] = "reference"
            if 3.3 <= z < 4.3 and not proposal_backed and not mechanically_proper and not multiword:
                reasons.append("wordfreq_middle:mechanical_single_reference")
            else:
                reasons.append("wordfreq_low:auto_include" if z < 3.3 else "supporting_candidate")
        if not impact:
            reasons.append("frequency_tiebreak:not_impact_evidence")
        item["ranking_reasons"] = reasons

    decision_rank = {"needs_user_decision": 0, "reference": 1, "excluded_common_word": 2}
    accepted.sort(key=lambda item: (
        decision_rank[item["decision"]],
        -item["translation_change_count"],
        0 if item.get("distinguish_from") else 1,
        0 if item.get("user_decided") else 1,
        0 if "llm_proposal" in item.get("extraction_sources", []) else 1,
        -item["body_occurrences"],
        item["key"].casefold(),
    ))
    for rank, item in enumerate(accepted, 1):
        item["rank"] = rank

    extractor_coverage = {}
    buckets = {
        "people": _PERSON_GROUPS,
        "institutions": {"institution", "institutions", "organization", "organizations", "places"},
        "concepts": {"concept", "concepts", "medical", "term", "terms"},
    }
    for label, groups in buckets.items():
        grouped = defaultdict(list)
        for item in accepted:
            if item["group"] in groups and "llm_proposal" in item.get("extraction_sources", []):
                grouped[(item.get("proposal_source"), item.get("proposal_en", item["key"]))].append(item)
        covered = [items for items in grouped.values() if any("mechanical" in item.get("extraction_sources", []) for item in items)]
        ratio = len(covered) / len(grouped) if grouped else None
        extractor_coverage[label] = {
            "mechanically_extracted": len(covered),
            "llm_proposals_in_body": len(grouped),
            "ratio": round(ratio, 4) if ratio is not None else None,
            "status": "broken" if label != "concepts" and ratio is not None and ratio < 0.75 else "ok",
        }

    emitted_person_forms = {
        normalize_match_text(item["key"]).casefold()
        for item in accepted
        if item["group"] in _PERSON_GROUPS
    }
    covered_person_forms = mechanically_safe_person_forms & emitted_person_forms
    person_form_ratio = (
        len(covered_person_forms) / len(mechanically_safe_person_forms)
        if mechanically_safe_person_forms else 1.0
    )

    return {
        "schema_version": 1,
        "contract": "LLM translations are proposals; only the user approves zh wording.",
        "population": {
            "source_units": len(units), "body_units": len(body),
            "enumeration": "EPUB/HTML paragraph nodes excluding bibliography, index, notes, and publication metadata; injection-equivalent normalized whole-token matching",
        },
        "summary": {
            "ranked": len(accepted),
            "needs_user_decision": sum(x["decision"] == "needs_user_decision" for x in accepted),
            "reference": sum(x["decision"] == "reference" for x in accepted),
            "excluded_common_word": sum(x["decision"] == "excluded_common_word" for x in accepted),
            "hallucinated_or_nonbody": len(dropped),
            "mechanical_extractor_coverage": extractor_coverage,
            "mechanical_person_form_coverage": {
                "covered_keys": len(covered_person_forms),
                "determinable_keys": len(mechanically_safe_person_forms),
                "ratio": round(person_form_ratio, 4),
                "denominator": "unique person spellings mechanically linked by exact/full-name evidence and corpus-unique person tokens; true token collisions excluded and listed separately",
            },
        },
        "ranked_terms": accepted,
        "dropped_proposals": dropped,
        "zh_conflicts": conflicts,
        "person_name_collisions": collisions,
    }


def _comparison_source(units: list[dict], page_fragment: str) -> list[str]:
    matches = [unit["text"] for unit in body_units(units) if page_fragment.casefold() in unit["path"].casefold()]
    if not matches:
        raise ValueError(f"no source page matched --comparison-page {page_fragment!r}")
    return matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a ranked, user-reviewed terminology queue")
    parser.add_argument("--source", type=Path, required=True, help="Source EPUB, HTML/text directory, or text file")
    parser.add_argument("--proposals", type=Path, action="append", default=[], help="LLM proposal JSON; repeatable")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline-translation", type=Path)
    parser.add_argument("--termed-translation", type=Path)
    parser.add_argument("--comparison-page", help="Source page path fragment; its paragraph split must align exactly")
    parser.add_argument("--comparison-source", type=Path, help="Blank-line-separated source paragraphs aligned exactly to both translations")
    parser.add_argument("--comparison-chapter", type=Path, help="Extract the exact translator paragraph array via dispatch.html_to_paragraphs")
    parser.add_argument("--max-mechanical-references", type=int, default=300)
    args = parser.parse_args(argv)
    translations = (args.baseline_translation, args.termed_translation)
    source_choices = (args.comparison_page, args.comparison_source, args.comparison_chapter)
    if any(translations) and not all(translations):
        parser.error("comparison requires both --baseline-translation and --termed-translation")
    if all(translations) and sum(choice is not None for choice in source_choices) != 1:
        parser.error("comparison requires exactly one of --comparison-chapter, --comparison-source, or --comparison-page")
    if not any(translations) and any(source_choices):
        parser.error("comparison source/page requires both translation files")

    units = read_source_units(args.source)
    proposals = load_proposals(args.proposals)
    comparison = None
    if all(translations):
        if args.comparison_chapter:
            source_paragraphs = _paragraphs_for_comparison_chapter(args.comparison_chapter)
        elif args.comparison_source:
            source_paragraphs = _paragraphs_for_comparison(args.comparison_source)
        else:
            source_paragraphs = _comparison_source(units, args.comparison_page)
        comparison = (
            source_paragraphs,
            _paragraphs_for_comparison(args.baseline_translation),
            _paragraphs_for_comparison(args.termed_translation),
        )
    report = build_review_queue(
        units, proposals, zipf=_zipf_scorer(), comparison=comparison,
        max_mechanical_references=args.max_mechanical_references,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
