"""Reacts to events, schedules a report and adds an admin route + CLI command."""

from __future__ import annotations

import argparse
from collections import Counter

from corlet import Component, ComponentContext, Container, component, contribution
from corlet.std import ADMIN_ROUTES, Route, Scheduler
from corlet.std.cli import CLI_COMMANDS, CliCommand

from ..contracts import OrderPlaced, Orders


class Stats:
    def __init__(self) -> None:
        self.revenue = 0.0
        self.by_customer: Counter[str] = Counter()

    def on_order(self, event: OrderPlaced) -> None:
        self.revenue += event.total
        self.by_customer[event.customer] += 1

    def snapshot(self) -> dict:
        return {"revenue": round(self.revenue, 2), "orders": dict(self.by_customer)}


STATS = Stats()


async def demo_command(container: Container, args: argparse.Namespace) -> int:
    orders = container.services.instance(Orders)
    for customer in args.customers:
        order = await orders.place(customer, {"apple": 4, "melon": 1})
        print(f"{order['id']}: {customer} pays {order['total']} {order['currency']}")
    await container.events.drain(1)
    print("stats:", STATS.snapshot())
    return 0


def configure_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("customers", nargs="*", default=["alice", "bob"])


@component(
    "shop.reporting",
    version="1.0.0",
    extensions=[
        contribution(ADMIN_ROUTES, Route("GET", "/shop/stats", lambda req: STATS.snapshot())),
        contribution(
            CLI_COMMANDS,
            CliCommand("demo", "place demo orders", demo_command, configure_demo),
        ),
    ],
)
class ReportingComponent(Component):
    """Order statistics."""

    async def start(self, ctx: ComponentContext, scheduler: Scheduler | None = None) -> None:
        ctx.events.subscribe(OrderPlaced, STATS.on_order)
        if scheduler is not None:  # optional dependency
            scheduler.every(30, self.report, name="report")
        self.ctx = ctx

    def report(self) -> None:
        self.ctx.logger.info("stats: %s", STATS.snapshot())
