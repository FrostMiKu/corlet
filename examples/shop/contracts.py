"""Service contracts shared by the shop components.

Contracts are plain Protocols: components depend on these, never on each
other's implementation classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from corlet import Event, ExtensionPoint, Strategy


@runtime_checkable
class Storage(Protocol):
    async def put(self, key: str, value: dict) -> None: ...
    async def get(self, key: str) -> dict | None: ...
    async def all(self, prefix: str) -> list[dict]: ...


@runtime_checkable
class Orders(Protocol):
    async def place(self, customer: str, items: dict[str, int]) -> dict: ...


@dataclass(frozen=True)
class OrderPlaced(Event):
    topic = "shop.order.placed"
    order_id: str
    customer: str
    total: float


#: Pricing rules form a pipeline: each rule receives the running total.
PRICING_RULES: ExtensionPoint = ExtensionPoint(
    "shop.pricing.rules",
    strategy=Strategy.PIPELINE,
    method="apply",
    description="Adjust an order total (discounts, taxes, ...)",
)
