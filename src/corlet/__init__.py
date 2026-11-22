"""corlet: a microkernel framework for Python.

Components declare what they need and provide; the `Container`
resolves them into a dependency graph, starts them in order, wires services,
extension points and events between them, and cleans up after them.

Quick start::

    from corlet import Container, Component, component

    class Greeter(Protocol):
        def greet(self, name: str) -> str: ...

    @component("demo.greeter", provides=[Greeter])
    class GreeterComponent(Component):
        async def start(self, ctx):
            ctx.services.register(Greeter, EnglishGreeter())

    @component("demo.app")
    class App(Component):
        async def start(self, ctx, greeter: Greeter):
            print(greeter.greet("world"))

    Container(GreeterComponent, App).run()
"""

from .core import *  # noqa: F403
from .core import __all__ as _core_all

__version__ = "0.1.0"
__all__ = [*_core_all, "__version__"]
