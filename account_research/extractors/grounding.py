"""
Grounding checks.

Pydantic validates that LLM output has the right SHAPE. It says nothing
about whether the content is TRUE — a well-formed, schema-valid stakeholder
name, unit volume, or news URL can still be entirely invented.

These functions are deliberately cheap and literal: they check whether a
specific extracted value actually appears (or a close variant of it) in the
source text/URLs that were given to the model. This is NOT a semantic
entailment check and won't catch subtle misreadings — it only catches
outright invention, which is the failure mode that matters most for
fields like stakeholder names, disclosed unit volumes, and news citations.

Every place this module is used, "not grounded" means "discard or flag the
value" — never "try harder to keep it."
"""

from __future__ import annotations

import re


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def text_contains_claim(claim: str, source_text: str, min_overlap: float = 0.6) -> bool:
    """
    Checks whether `claim` (e.g. a person's name) is actually present in
    `source_text`, tolerating minor formatting differences.

    Strategy: exact substring match first (cheap, catches the common case);
    if that fails, fall back to token overlap — what fraction of the
    claim's significant words (3+ letters, alphabetic) also appear
    somewhere in the source text. A real name pulled from a real snippet
    should score at or near 1.0; a fabricated name typically scores 0.
    """
    claim_norm  = _normalize(claim)
    source_norm = _normalize(source_text)
    if not claim_norm or not source_norm:
        return False

    if claim_norm in source_norm:
        return True

    words = [w for w in re.findall(r"[a-z]+", claim_norm) if len(w) >= 3]
    if not words:
        return False

    hits = sum(1 for w in words if w in source_norm)
    return (hits / len(words)) >= min_overlap


def number_grounded(value, source_text: str) -> bool:
    """
    Checks whether a numeric claim (e.g. an annual unit volume) appears
    in the source text in any common formatting — with or without comma
    separators. Returns False for None/0 (nothing to ground).
    """
    if value is None:
        return False
    try:
        digits = str(int(round(float(value))))
    except (TypeError, ValueError):
        return False
    if not digits:
        return False

    source_digits_only = re.sub(r"[,\s]", "", source_text or "")
    return digits in source_digits_only


def _normalize_url(url: str) -> str:
    return (url or "").strip().lower().rstrip("/")


def is_url_grounded(url: str, known_urls: set[str]) -> bool:
    """
    Checks whether `url` matches one of the URLs actually returned by
    search (`known_urls`) — used to catch a model citing a news source
    that was never in the retrieved snippets at all.
    """
    return _normalize_url(url) in {_normalize_url(u) for u in known_urls}