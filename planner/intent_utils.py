#!/usr/bin/env python3
"""Domain-neutral text matching helpers for the planner and router."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def detect_alias_groups(
    text: str,
    aliases: Mapping[str, Iterable[str]],
) -> tuple[str, ...]:
    normalized = normalize_text(text)
    detected: list[str] = []
    for group, synonyms in aliases.items():
        if any(
            normalized_alias
            and normalized_alias in normalized
            for synonym in synonyms
            for normalized_alias in [normalize_text(str(synonym))]
        ):
            detected.append(str(group))
    return tuple(detected)
