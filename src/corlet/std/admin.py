"""A tiny, dependency-free HTTP admin endpoint (asyncio streams).

Built-in routes::

    GET  /                          route index
    GET  /health                    HealthService report (503 when down)
    GET  /components                component snapshot
    GET  /components/{id}
    GET  /services
    GET  /extensions
    GET  /graph?format=dot|mermaid
    POST /components/{id}/enable    (only with allow_control: true)
    POST /components/{id}/disable
    POST /components/{id}/restart

Other components add routes by contributing `Route` objects to
`ADMIN_ROUTES`.  Set ``token`` to require ``Authorization: Bearer ...``.
This is an operations endpoint, not a general purpose web server: one request
per connection, bodies up to 1 MiB.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, unquote, urlsplit

from ..core import (
    Component,
    ComponentContext,
    ExtensionPoint,
    ServiceRef,
    Strategy,
    component,
    optional,
    to_dot,
    to_mermaid,
)
from .health import HealthService, Status

__all__ = [
    "ADMIN_ROUTES",
    "AdminComponent",
    "AdminConfig",
    "AdminServer",
    "Request",
    "Response",
    "Route",
]

MAX_BODY = 1 << 20
MAX_HEAD = 16 << 10


@dataclass(frozen=True)
class Request:
    """A parsed HTTP request passed to route handlers."""

    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str]
    body: bytes
    params: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """The body parsed as JSON (``None`` if empty)."""
        return json.loads(self.body or b"null")

    def arg(self, name: str, default: str | None = None) -> str | None:
        """First value of query parameter ``name``."""
        values = self.query.get(name)
        return values[0] if values else default


@dataclass
class Response:
    """An HTTP response.

    Handlers may also return plain data (sent as JSON), ``None`` (204)
    or a ``(status, body)`` tuple.
    """

    status: int = 200
    body: bytes | str | Any = b""
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data: Any, status: int = 200) -> Response:
        """JSON response."""
        return cls(status, json.dumps(data, default=str, indent=2), "application/json")

    @classmethod
    def text(cls, text: str, status: int = 200) -> Response:
        """Plain text response."""
        return cls(status, text, "text/plain; charset=utf-8")

    def encode(self) -> bytes:
        """Serialize to HTTP/1.1 bytes."""
        body = self.body
        ctype = self.content_type
        if isinstance(body, str):
            body = body.encode()
            ctype = ctype or "text/plain; charset=utf-8"
        elif not isinstance(body, bytes):
            body = json.dumps(body, default=str, indent=2).encode()
            ctype = ctype or "application/json"
        reason = (
            HTTPStatus(self.status).phrase if self.status in HTTPStatus._value2member_map_ else ""
        )
        head = [f"HTTP/1.1 {self.status} {reason}"]
        hdrs = {"Content-Length": str(len(body)), "Connection": "close", **self.headers}
        if ctype:
            hdrs["Content-Type"] = ctype
        head += [f"{k}: {v}" for k, v in hdrs.items()]
        return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


Handler = Callable[[Request], Awaitable[Any] | Any]


@dataclass(frozen=True)
class Route:
    """An HTTP route; contribute it to `ADMIN_ROUTES`."""

    method: str
    path: str
    """URL path pattern with ``{name}`` placeholders, e.g. ``/orders/{id}``."""
    handler: Handler
    description: str = ""

    def compile(self) -> re.Pattern[str]:
        """Regular expression matching the route's path."""
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path.rstrip("/") or "/")
        return re.compile(f"^{pattern}/?$")


ADMIN_ROUTES: ExtensionPoint[Route] = ExtensionPoint(
    "corlet.admin.routes",
    contract=Route,
    strategy=Strategy.COLLECT,
    description="HTTP routes served by corlet.admin",
)


@runtime_checkable
class AdminServer(Protocol):
    """Service registered by ``corlet.admin``."""

    @property
    def port(self) -> int:
        """The bound TCP port (useful with ``port: 0``)."""
        ...

    @property
    def url(self) -> str:
        """Base URL of the endpoint."""
        ...


@dataclass
class AdminConfig:
    """Configuration of ``corlet.admin``.

    Attributes:
        host: Interface to bind.
        port: TCP port (``0`` = any free port).
        allow_control: Enable the ``POST .../enable|disable|restart`` routes.
        token: If set, require ``Authorization: Bearer <token>``.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    allow_control: bool = False
    token: str | None = None


class _Server:
    def __init__(self, ctx: ComponentContext, cfg: AdminConfig, health: ServiceRef[HealthService]):
        self.ctx = ctx
        self.cfg = cfg
        self.health = health
        self._server: asyncio.base_events.Server | None = None
        self._builtin = self._builtin_routes()
        # control requests currently waiting for a lifecycle operation
        self._control_inflight = 0

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.cfg.host, self.cfg.port)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        # Since Python 3.12 wait_closed() also waits for open connections.  A
        # control request that restarts/disables this very component holds a
        # connection while waiting for the operation that is closing us: do
        # not wait for it (it finishes and closes its connection on its own).
        if self._control_inflight == 0:
            await self._server.wait_closed()

    # -- routing ----------------------------------------------------------
    def routes(self) -> list[tuple[Route, str]]:
        contributed = [(e.impl, e.owner) for e in self.ctx.extensions.extensions(ADMIN_ROUTES)]
        return [(r, self.ctx.id) for r in self._builtin] + contributed

    def _builtin_routes(self) -> list[Route]:
        rs = [
            Route("GET", "/", self.index, "route index"),
            Route("GET", "/health", self.get_health, "aggregated health"),
            Route("GET", "/components", self.get_components, "all components"),
            Route("GET", "/components/{id}", self.get_component, "one component"),
            Route("GET", "/services", self.get_services, "service registry"),
            Route("GET", "/extensions", self.get_extensions, "extension points"),
            Route("GET", "/graph", self.get_graph, "dependency graph (dot|mermaid)"),
        ]
        for action in ("enable", "disable", "restart"):
            rs.append(Route("POST", f"/components/{{id}}/{action}", self._control(action), action))
        return rs

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._process(reader)
        except Exception as exc:
            self.ctx.logger.exception("admin request failed")
            response = Response.json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        try:
            writer.write(response.encode())
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _process(self, reader: asyncio.StreamReader) -> Response:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            return Response.text("bad request", 400)
        if len(head) > MAX_HEAD:
            return Response.text("headers too large", 431)
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            return Response.text("bad request line", 400)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            return Response.text("payload too large", 413)
        body = await reader.readexactly(length) if length else b""
        url = urlsplit(target)
        path = unquote(url.path) or "/"
        if self.cfg.token is not None:
            given = headers.get("authorization", "")
            if not hmac.compare_digest(given, f"Bearer {self.cfg.token}"):
                return Response.json({"error": "unauthorized"}, 401)
        allowed: list[str] = []
        for route, _owner in self.routes():
            m = route.compile().match(path)
            if not m:
                continue
            if route.method.upper() != method.upper():
                allowed.append(route.method.upper())
                continue
            req = Request(method.upper(), path, parse_qs(url.query), headers, body, m.groupdict())
            result = route.handler(req)
            if inspect.isawaitable(result):
                result = await result
            return _to_response(result)
        if allowed:
            return Response(
                405, {"error": "method not allowed"}, headers={"Allow": ", ".join(allowed)}
            )
        return Response.json({"error": f"no route for {path}"}, 404)

    # -- handlers -----------------------------------------------------------
    def index(self, req: Request) -> Any:
        return [
            {"method": r.method, "path": r.path, "owner": o, "description": r.description}
            for r, o in self.routes()
        ]

    async def get_health(self, req: Request) -> Response:
        if self.health.available:
            report = await self.health.resolve().report()
            code = 503 if report.status is Status.DOWN else 200
            return Response.json(report.to_dict(), code)
        snap = self.ctx.container.inspect()
        failed = [c.id for c in snap.components if c.state.value == "failed"]
        return Response.json({"status": "degraded" if failed else "up", "failed": failed}, 200)

    def get_components(self, req: Request) -> Any:
        return self.ctx.container.inspect().to_dict()["components"]

    def get_component(self, req: Request) -> Response:
        snap = self.ctx.container.inspect().to_dict()
        for c in snap["components"]:
            if c["id"] == req.params["id"]:
                return Response.json(c)
        return Response.json({"error": "unknown component"}, 404)

    def get_services(self, req: Request) -> Any:
        return self.ctx.container.inspect().to_dict()["services"]

    def get_extensions(self, req: Request) -> Any:
        d = self.ctx.container.inspect().to_dict()
        return {"points": d["extension_points"], "contributions": d["extensions"]}

    def get_graph(self, req: Request) -> Response:
        snap = self.ctx.container.inspect()
        fmt = req.arg("format", "dot")
        if fmt == "mermaid":
            return Response.text(to_mermaid(snap))
        return Response.text(to_dot(snap))

    def _control(self, action: str) -> Handler:
        async def handler(req: Request) -> Response:
            if not self.cfg.allow_control:
                return Response.json({"error": "control endpoints are disabled"}, 403)
            container = self.ctx.container
            cid = req.params["id"]
            if cid not in container:
                return Response.json({"error": "unknown component"}, 404)
            op = getattr(container, action)
            # Detached: the operation may stop this admin component itself.
            self._control_inflight += 1
            try:
                changes = await asyncio.shield(container.spawn(op(cid)))
            finally:
                self._control_inflight -= 1
            return Response.json(
                {
                    "component": cid,
                    "action": action,
                    "state": container.state_of(cid).value,
                    "stopped": list(changes.stopped),
                    "started": list(changes.started),
                    "failed": list(changes.failed),
                }
            )

        return handler


def _to_response(result: Any) -> Response:
    if isinstance(result, Response):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        status, body = result
        return body if isinstance(body, Response) else Response(status, body)
    if result is None:
        return Response(204)
    return Response(200, result)


@component(
    "corlet.admin",
    version="0.1.0",
    description="HTTP admin endpoint: introspection, health and runtime control",
    config=AdminConfig,
    provides=[AdminServer],
    requires_services=[optional(HealthService)],
    defines=[ADMIN_ROUTES],
    privileged=True,
)
class AdminComponent(Component):
    """``corlet.admin``: HTTP admin endpoint (see the module documentation)."""

    async def start(
        self,
        ctx: ComponentContext,
        config: AdminConfig,
        health: ServiceRef[HealthService],
    ) -> None:
        server = _Server(ctx, config, health)
        await server.start()
        ctx.defer(server.close)
        ctx.services.register(AdminServer, server)
        ctx.logger.info("admin endpoint listening on %s", server.url)
