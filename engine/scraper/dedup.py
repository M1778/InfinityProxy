"""First-seen dedup of candidates. Stable order, later duplicates dropped."""

from __future__ import annotations

from engine.models import NodeCandidate


def dedup(candidates: list[NodeCandidate]) -> list[NodeCandidate]:
    seen: set[str] = set()
    unique: list[NodeCandidate] = []
    for candidate in candidates:
        key = candidate.identity_key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
