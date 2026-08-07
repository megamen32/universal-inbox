"""Hermes-neutral read-only consumer facade for Core search and digest."""

from __future__ import annotations

from typing import Any, Protocol


class CoreSurface(Protocol):
    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def _bounded_limit(value: int) -> int:
    return max(1, min(100, value))


class HermesNeutralConsumer:
    """Calls the Core surface only and exposes no outbound action methods."""

    def __init__(self, surface: CoreSurface) -> None:
        self._surface = surface

    def search(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        text = query.strip()
        if not text:
            raise ValueError("query must not be empty")
        return self._surface.dispatch("inbox.search", {"query": text, "limit": _bounded_limit(limit)})

    def digest_candidates(self, *, limit: int = 20) -> dict[str, Any]:
        return self._surface.dispatch("inbox.digest_candidates", {"limit": _bounded_limit(limit)})
