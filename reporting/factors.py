from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from reporting.config import DICTIONARY_VERSION, PACKAGE_ROOT
from reporting.source import canonical_json, clean


SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+")
WORD_RE = re.compile(r"[a-z][a-z0-9'-]{2,}")
EXPLICIT_AGE_RE = re.compile(
    r"\b(?:i(?:'m| am)\s+)?(1[89]|[2-9]\d|100)\s*(?:years?|yrs?)\s*old\b",
    re.IGNORECASE,
)
AGE_RANGE_RE = re.compile(r"\b(1[89]|[2-9]\d)\s*[-–]\s*([2-9]\d|100)\b")
AGE_SOURCE_RANGE_RE = re.compile(r"^(1[89]|[2-9]\d)to([2-9]\d|100)$", re.I)
STOPWORDS = {
    "the",
    "and",
    "that",
    "this",
    "with",
    "for",
    "was",
    "have",
    "has",
    "had",
    "but",
    "not",
    "are",
    "from",
    "you",
    "they",
    "its",
    "would",
    "could",
    "should",
    "very",
    "just",
    "all",
    "can",
    "will",
    "one",
    "out",
    "use",
    "used",
    "using",
    "mower",
    "lawn",
    "product",
    "really",
    "much",
    "more",
    "than",
    "when",
    "what",
    "after",
    "about",
    "because",
    "only",
    "been",
    "were",
    "also",
    "get",
    "got",
    "did",
    "does",
    "didn't",
    "doesn't",
}


@dataclass(frozen=True)
class FactorSpec:
    namespace: str
    factor_id: str
    label: str
    terms: tuple[str, ...]
    framework: str | None = None


def load_dictionary(path: Path | None = None) -> dict[str, Any]:
    dictionary_path = path or PACKAGE_ROOT / "dictionaries" / "mower-v1.json"
    return json.loads(dictionary_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> re.Pattern[str]:
    term = term.casefold()
    if term.replace("-", "").replace("'", "").replace(" ", "").isalnum():
        return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")
    return re.compile(re.escape(term))


def _contains_term(text: str, term: str) -> bool:
    # Every pattern contains the escaped literal. Reject absent literals before
    # scanning the whole review for word boundaries, preserving exact matches.
    return term.casefold() in text and _term_pattern(term).search(text) is not None


def _is_negated(text: str, terms: Iterable[str]) -> bool:
    for term in terms:
        match = _term_pattern(term).search(text)
        if not match:
            continue
        prefix = text[max(0, match.start() - 48) : match.start()]
        if re.search(
            r"\b(?:no|not|never|without|hardly|isn't|wasn't|doesn't|didn't|won't|cannot|can't)\b[^.!?]{0,36}$",
            prefix,
        ):
            return True
    return False


def _first_snippet(text: str, terms: Iterable[str], limit: int = 300) -> str:
    sentences = [clean(item) for item in SENTENCE_SPLIT.split(text) if clean(item)]
    for sentence in sentences:
        folded = sentence.casefold()
        if any(_contains_term(folded, term) for term in terms):
            return sentence[:limit]
    return clean(text)[:limit]


def _rating_polarity(rating: int | None) -> str:
    if rating is None:
        return "unknown"
    if rating >= 4:
        return "positive"
    if rating <= 2:
        return "negative"
    return "neutral"


def _evidence_id(fact: dict[str, Any], namespace: str, factor_id: str) -> str:
    material = {
        "occurrence_id": fact["review_occurrence_id"],
        "namespace": namespace,
        "factor_id": factor_id,
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"ev_{digest[:24]}"


def factor_specs(dictionary: dict[str, Any]) -> list[FactorSpec]:
    groups = (
        ("attribute", "attributes"),
        ("pain", "pain_points"),
        ("use_case", "use_cases"),
        ("user_trait", "user_traits"),
        ("smart_signal", "smart_signals"),
    )
    specs: list[FactorSpec] = []
    for namespace, key in groups:
        for raw in dictionary.get(key, []):
            specs.append(
                FactorSpec(
                    namespace=namespace,
                    factor_id=raw["id"],
                    label=raw["label"],
                    terms=tuple(raw.get("terms") or ()),
                    framework=raw.get("framework"),
                )
            )
    return specs


def extract_atoms(
    facts: list[dict[str, Any]],
    dictionary: dict[str, Any] | None = None,
    evidence_per_factor: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    dictionary = dictionary or load_dictionary()
    specs = factor_specs(dictionary)
    atoms: list[dict[str, Any]] = []
    candidates_by_factor: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    covered_entities: set[str] = set()

    for fact in facts:
        if not fact["has_text"]:
            continue
        text = clean(f"{fact['review_title']}. {fact['review_text']}")
        folded = text.casefold()
        entity_ref = fact["review_entity_id"] or fact["review_occurrence_id"]
        matched_attribute_ids: set[str] = set()
        for spec in specs:
            matched_terms = [term for term in spec.terms if _contains_term(folded, term)]
            if not matched_terms:
                continue
            negated = _is_negated(folded, matched_terms)
            # Explicit failure phrases preceded by a local negation are not
            # counted as pain occurrences. The negation is still retained for
            # attributes because "not noisy" is valid attribute evidence.
            if spec.namespace == "pain" and negated:
                continue
            evidence_id = _evidence_id(fact, spec.namespace, spec.factor_id)
            atom_material = {
                "entity_ref": entity_ref,
                "namespace": spec.namespace,
                "factor_id": spec.factor_id,
                "evidence_id": evidence_id,
            }
            atom_id = (
                "atom_"
                + hashlib.sha256(canonical_json(atom_material).encode("utf-8"))
                .hexdigest()[:24]
            )
            polarity = _rating_polarity(fact["rating"])
            atom = {
                "atom_id": atom_id,
                "review_entity_id": fact["review_entity_id"],
                "review_occurrence_id": fact["review_occurrence_id"],
                "entity_ref": entity_ref,
                "factor_namespace": spec.namespace,
                "factor_id": spec.factor_id,
                "factor_label": spec.label,
                "framework": spec.framework,
                "polarity": polarity,
                "intensity": (
                    "high"
                    if fact["rating"] in {1, 5}
                    else "medium" if fact["rating"] in {2, 4} else "unknown"
                ),
                "negated": negated,
                "mixed": False,
                "evidence_id": evidence_id,
                "extractor_version": DICTIONARY_VERSION,
                "confidence": "rule-exact",
            }
            atoms.append(atom)
            covered_entities.add(entity_ref)
            if spec.namespace == "attribute":
                matched_attribute_ids.add(spec.factor_id)
            evidence = {
                "evidence_id": evidence_id,
                "review_entity_id": fact["review_entity_id"],
                "review_occurrence_id": fact["review_occurrence_id"],
                "factor_namespace": spec.namespace,
                "factor_id": spec.factor_id,
                "factor_label": spec.label,
                "snippet": _first_snippet(text, matched_terms),
                "channel": fact["channel"],
                "brand": fact["brand"],
                "model": fact["model"],
                **(
                    {
                        "market_region": fact.get("market_region"),
                        "market_region_evidence": fact.get(
                            "market_region_evidence"
                        ),
                        "display_site": fact.get("display_site"),
                        "source_site": fact.get(
                            "source_site_normalized"
                        ),
                    }
                    if fact.get("market_region")
                    else {}
                ),
                "rating": fact["rating"],
                "submission_date": fact["submission_date"],
                "method": "versioned exact phrase dictionary",
                "extractor_version": DICTIONARY_VERSION,
            }
            candidates_by_factor[(spec.namespace, spec.factor_id)].append(evidence)

        if any(_contains_term(folded, cue) for cue in dictionary.get("need_cues", [])):
            for factor_id in sorted(matched_attribute_ids):
                evidence_id = _evidence_id(fact, "need", factor_id)
                atom = {
                    "atom_id": f"atom_{hashlib.sha256(evidence_id.encode()).hexdigest()[:24]}",
                    "review_entity_id": fact["review_entity_id"],
                    "review_occurrence_id": fact["review_occurrence_id"],
                    "entity_ref": entity_ref,
                    "factor_namespace": "need",
                    "factor_id": factor_id,
                    "factor_label": next(
                        (
                            spec.label
                            for spec in specs
                            if spec.namespace == "attribute"
                            and spec.factor_id == factor_id
                        ),
                        factor_id,
                    )
                    + "明确需求",
                    "framework": None,
                    "polarity": _rating_polarity(fact["rating"]),
                    "intensity": "explicit-request",
                    "negated": False,
                    "mixed": False,
                    "evidence_id": evidence_id,
                    "extractor_version": DICTIONARY_VERSION,
                    "confidence": "rule-cooccurrence",
                }
                atoms.append(atom)
                evidence = {
                    "evidence_id": evidence_id,
                    "review_entity_id": fact["review_entity_id"],
                    "review_occurrence_id": fact["review_occurrence_id"],
                    "factor_namespace": "need",
                    "factor_id": factor_id,
                    "factor_label": next(
                        (
                            spec.label
                            for spec in specs
                            if spec.namespace == "attribute"
                            and spec.factor_id == factor_id
                        ),
                        factor_id,
                    )
                    + "明确需求",
                    "snippet": _first_snippet(
                        text, dictionary.get("need_cues", [])
                    ),
                    "channel": fact["channel"],
                    "brand": fact["brand"],
                    "model": fact["model"],
                    **(
                        {
                            "market_region": fact.get("market_region"),
                            "market_region_evidence": fact.get(
                                "market_region_evidence"
                            ),
                            "display_site": fact.get("display_site"),
                            "source_site": fact.get(
                                "source_site_normalized"
                            ),
                        }
                        if fact.get("market_region")
                        else {}
                    ),
                    "rating": fact["rating"],
                    "submission_date": fact["submission_date"],
                    "method": "need cue and attribute co-occurrence",
                    "extractor_version": DICTIONARY_VERSION,
                }
                candidates_by_factor[("need", factor_id)].append(evidence)

    atoms.sort(
        key=lambda item: (
            item["factor_namespace"],
            item["factor_id"],
            item["entity_ref"],
            item["atom_id"],
        )
    )
    evidence: list[dict[str, Any]] = []
    for key in sorted(candidates_by_factor):
        values = candidates_by_factor[key]
        values.sort(
            key=lambda item: (
                item["rating"] is None,
                item["rating"] if item["rating"] is not None else 9,
                item["submission_date"] or "",
                item["evidence_id"],
            )
        )
        selected = []
        seen_snippets: set[str] = set()
        for item in values:
            snippet_key = clean(item["snippet"]).casefold()
            if not snippet_key or snippet_key in seen_snippets:
                continue
            seen_snippets.add(snippet_key)
            selected.append(item)
            if len(selected) >= evidence_per_factor:
                break
        evidence.extend(selected)
    evidence.sort(key=lambda item: item["evidence_id"])
    written_entities = {
        fact["review_entity_id"] or fact["review_occurrence_id"]
        for fact in facts
        if fact["has_text"]
    }
    return atoms, evidence, {
        "written_entities": len(written_entities),
        "covered_entities": len(covered_entities),
        "coverage_rate": (
            len(covered_entities) / len(written_entities) if written_entities else None
        ),
        "atoms": len(atoms),
        "evidence_snippets": len(evidence),
        "dictionary_version": dictionary.get("version"),
    }


def explicit_age_group(fact: dict[str, Any]) -> tuple[str | None, str | None]:
    raw_age = clean(fact.get("age"))
    text = clean(f"{fact.get('review_title')}. {fact.get('review_text')}")
    if raw_age:
        source_range_match = AGE_SOURCE_RANGE_RE.fullmatch(raw_age)
        if source_range_match:
            midpoint = (
                int(source_range_match.group(1)) + int(source_range_match.group(2))
            ) // 2
            return age_bucket(midpoint), "structured-field"
        if raw_age.casefold() == "17orunder":
            return "17岁及以下", "structured-field"
        range_match = AGE_RANGE_RE.search(raw_age)
        if range_match:
            midpoint = (int(range_match.group(1)) + int(range_match.group(2))) // 2
            return age_bucket(midpoint), "structured-field"
        number_match = re.search(r"\b(1[89]|[2-9]\d|100)\b", raw_age)
        if number_match:
            return age_bucket(int(number_match.group(1))), "structured-field"
        lowered = raw_age.casefold()
        if "65" in lowered and ("over" in lowered or "+" in lowered):
            return "65岁及以上", "structured-field"
        return raw_age, "structured-field-verbatim"
    text_match = EXPLICIT_AGE_RE.search(text)
    if text_match:
        return age_bucket(int(text_match.group(1))), "explicit-text"
    return None, None


def age_bucket(age: int) -> str:
    if age < 25:
        return "18–24岁"
    if age < 35:
        return "25–34岁"
    if age < 45:
        return "35–44岁"
    if age < 55:
        return "45–54岁"
    if age < 65:
        return "55–64岁"
    return "65岁及以上"


def keyword_document_frequency(
    facts: list[dict[str, Any]], limit: int = 24
) -> list[dict[str, Any]]:
    unigram_docs: Counter[str] = Counter()
    bigram_docs: Counter[str] = Counter()
    written_count = 0
    for fact in facts:
        if not fact["has_text"]:
            continue
        written_count += 1
        tokens = [
            token
            for token in WORD_RE.findall(fact["review_text"].casefold())
            if token not in STOPWORDS
        ]
        unigram_docs.update(set(tokens))
        bigram_docs.update(
            set(
                f"{left} {right}"
                for left, right in zip(tokens, tokens[1:])
                if left != right
            )
        )
    candidates = [
        (term, count, "unigram") for term, count in unigram_docs.items() if count >= 3
    ]
    candidates.extend(
        (term, count, "bigram") for term, count in bigram_docs.items() if count >= 3
    )
    candidates.sort(key=lambda item: (-item[1], item[0], item[2]))
    return [
        {
            "term": term,
            "document_count": count,
            "document_rate": count / written_count if written_count else None,
            "ngram": kind,
        }
        for term, count, kind in candidates[:limit]
    ]


def factor_cooccurrence(
    atoms: list[dict[str, Any]],
    *,
    namespaces: tuple[str, ...] = ("attribute", "pain", "use_case"),
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Count factor pairs once per review entity.

    This is document-level co-occurrence. It intentionally does not claim that
    two factors occur in the same sentence or have a causal relationship.
    """

    factors_by_entity: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for atom in atoms:
        if atom["factor_namespace"] not in namespaces:
            continue
        factors_by_entity[atom["entity_ref"]].add(
            (
                atom["factor_namespace"],
                atom["factor_id"],
                atom["factor_label"],
            )
        )

    counts: Counter[tuple[tuple[str, str, str], tuple[str, str, str]]] = Counter()
    for values in factors_by_entity.values():
        ordered = sorted(values)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                counts[(left, right)] += 1

    rows = []
    for (left, right), count in sorted(
        counts.items(), key=lambda item: (-item[1], item[0])
    )[:limit]:
        rows.append(
            {
                "left_namespace": left[0],
                "left_factor_id": left[1],
                "left_label": left[2],
                "right_namespace": right[0],
                "right_factor_id": right[1],
                "right_label": right[2],
                "document_count": count,
            }
        )
    return rows
