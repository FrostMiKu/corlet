"""In-memory storage provider (swap for a real database component)."""

from __future__ import annotations

from corlet import Component, ComponentContext, component, contribution
from corlet.std import HEALTH_CHECKS, HealthCheck

from ..contracts import Storage


class MemoryStorage:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    async def put(self, key: str, value: dict) -> None:
        self.data[key] = value

    async def get(self, key: str) -> dict | None:
        return self.data.get(key)

    async def all(self, prefix: str) -> list[dict]:
        return [v for k, v in sorted(self.data.items()) if k.startswith(prefix)]


@component(
    "shop.storage.memory",
    version="1.0.0",
    provides=[Storage],
    extensions=[contribution(HEALTH_CHECKS, HealthCheck("storage", lambda: True))],
)
class MemoryStorageComponent(Component):
    """Keeps everything in a dict."""

    async def start(self, ctx: ComponentContext) -> None:
        ctx.services.register(Storage, MemoryStorage(), properties={"kind": "memory"})
