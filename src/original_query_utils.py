from __future__ import annotations

import re


BUILTIN_ALIASES: dict[str, list[str]] = {}


def parse_aliases(values: list[str] | None) -> list[str]:
    if not values:
        return []
    aliases: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                aliases.append(part)
    return aliases


def query_terms(value: str, extra_aliases: list[str] | None = None) -> list[str]:
    terms = [value]
    terms.extend(BUILTIN_ALIASES.get(value.lower(), []))
    terms.extend(parse_aliases(extra_aliases))

    seen = set()
    unique = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return sorted(unique, key=len, reverse=True)


def contains_term(text: str, term: str) -> bool:
    if not term:
        return False
    if len(term) <= 4 and re.fullmatch(r"[A-Za-z0-9-]+", term):
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE) is not None
    return term.lower() in text.lower()


def contains_any_term(text: str, terms: list[str]) -> bool:
    return any(contains_term(text, term) for term in terms)
