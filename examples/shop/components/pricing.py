"""Pricing rules contributed to the orders pipeline.

Each rule is an independent component: enable/disable them at runtime (e.g.
``POST /components/shop.pricing.vat/disable`` on the admin endpoint) and the
next order is priced without it.
"""

from __future__ import annotations

from dataclasses import dataclass

from corlet import Component, ComponentContext, component, contribution

from ..contracts import PRICING_RULES


@dataclass
class DiscountConfig:
    percent: float = 10.0
    vip: list[str] | None = None


class VipDiscount:
    def __init__(self, config: DiscountConfig) -> None:
        self.config = config

    def apply(self, total: float, *, customer: str) -> float:
        if customer in (self.config.vip or []):
            return total * (1 - self.config.percent / 100)
        return total


@component("shop.pricing.discount", version="1.0.0", config=DiscountConfig)
class DiscountComponent(Component):
    """VIP discount (runs before VAT)."""

    async def start(self, ctx: ComponentContext, config: DiscountConfig) -> None:
        self.rule = VipDiscount(config)
        ctx.extensions.contribute(PRICING_RULES, self.rule, name="discount", before=["vat"])

    async def reconfigure(self, ctx: ComponentContext, config: DiscountConfig) -> bool:
        self.rule.config = config  # applied in place, no restart needed
        return True


class Vat:
    rate = 0.2

    def apply(self, total: float, *, customer: str) -> float:
        return total * (1 + self.rate)


@component(
    "shop.pricing.vat",
    version="1.0.0",
    extensions=[contribution(PRICING_RULES, Vat(), name="vat")],
)
class VatComponent(Component):
    """Adds 20% VAT (declarative contribution, no code in start)."""

    async def start(self, ctx: ComponentContext) -> None:
        pass
