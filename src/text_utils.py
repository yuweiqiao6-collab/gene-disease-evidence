from __future__ import annotations

import re
from collections.abc import Iterable


TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+")
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def split_sentences(text: str) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    sentences = SENTENCE_BOUNDARY_RE.split(text)
    cleaned = [sentence.strip() for sentence in sentences if sentence.strip()]
    return [sentence for sentence in cleaned if len(sentence) >= 20]


def contains_any(text: str, terms: Iterable[str]) -> bool:
    lower_text = text.lower()
    return any(term.lower() in lower_text for term in terms)


def compact_for_prompt(text: str, max_chars: int = 3500) -> str:
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + " ... [truncated]"

