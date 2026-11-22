"""Order handling: uses Storage, applies pricing rules, publishes events."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from corlet import Component, ComponentContext, component

from ..contracts import PRICING_RULES, OrderPlaced, Orders, Storage

PRICES = {"apple": 0.5, "pear": 0.75, "melon": 3.0}


@dataclass
class OrdersConfig:
    currency: str = "EUR"


class OrderService:
    def __init__(self, ctx: ComponentContext, storage: Storage, config: OrdersConfig) -> None:
        self.ctx = ctx
        self.storage = storage
        self.config = config
        self._ids = itertools.count(1)

    async def place(self, customer: str, items: dict[str, int]) -> dict:
        subtotal = sum(PRICES[name] * qty for name, qty in items.items())
        total = await self.ctx.extensions.invoke(PRICING_RULES, subtotal, customer=customer)
        order = {
            "id": f"o{next(self._ids)}",
            "customer": customer,
            "items": items,
            "total": round(total, 2),
            "currency": self.config.currency,
        }
        await self.storage.put(f"order:{order['id']}", order)
        self.ctx.events.publish(OrderPlaced(order["id"], customer, order["total"]))
        return order


@component(
    "shop.orders",
    version="1.0.0",
    provides=[Orders],
    defines=[PRICING_RULES],
    config=OrdersConfig,
)
class OrdersComponent(Component):
    """Places orders."""

    async def start(self, ctx: ComponentContext, storage: Storage, config: OrdersConfig) -> None:
        ctx.services.register(Orders, OrderService(ctx, storage, config))
