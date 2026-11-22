"""The shop example application.

Run it with the CLI (from the repository root)::

    corlet -a examples.shop.app:create -c examples/shop/shop.toml list
    corlet -a examples.shop.app:create -c examples/shop/shop.toml graph
    corlet -a examples.shop.app:create -c examples/shop/shop.toml demo alice carol
    corlet -a examples.shop.app:create -c examples/shop/shop.toml run

or directly: ``python -m examples.shop.app``.
"""

from __future__ import annotations

from typing import Any

from corlet import Container, ModuleSource
from corlet.std import (
    AdminComponent,
    HealthComponent,
    LoggingComponent,
    SchedulerComponent,
)


def create(config: Any = None) -> Container:
    return Container(
        ModuleSource("examples.shop.components", recursive=True),
        LoggingComponent,
        SchedulerComponent,
        HealthComponent,
        AdminComponent,
        name="shop",
        config=config,
    )


if __name__ == "__main__":
    from corlet.std import LayeredConfig

    raise SystemExit(create(LayeredConfig(files=["examples/shop/shop.toml"])).run())
