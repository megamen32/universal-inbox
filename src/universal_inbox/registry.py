"""Adapter registry for the Universal Inbox polling core."""

from __future__ import annotations

from collections.abc import Iterable

from .adapter import InboxAdapter


class AdapterRegistry:
    def __init__(self, adapters: Iterable[InboxAdapter] = ()) -> None:
        self._adapters: dict[str, InboxAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: InboxAdapter) -> InboxAdapter:
        adapter_id = adapter.manifest.adapter_id
        if adapter_id in self._adapters:
            raise ValueError(f"duplicate adapter_id: {adapter_id}")
        self._adapters[adapter_id] = adapter
        return adapter

    def get(self, adapter_id: str) -> InboxAdapter:
        return self._adapters[adapter_id.strip()]

    def manifests(self) -> tuple[object, ...]:
        return tuple(adapter.manifest for adapter in self._adapters.values())

    def adapters(self) -> tuple[InboxAdapter, ...]:
        return tuple(self._adapters.values())

    def __contains__(self, adapter_id: str) -> bool:
        return adapter_id.strip() in self._adapters
