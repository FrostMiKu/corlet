"""Tests for corlet.std components."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from corlet import (
    Component,
    ComponentContext,
    ComponentState,
    ConfigError,
    Container,
    component,
    contribution,
)
from corlet.std import (
    ADMIN_ROUTES,
    HEALTH_CHECKS,
    JOBS,
    AdminComponent,
    AdminServer,
    CheckResult,
    ConfigService,
    ConfigServiceComponent,
    ConfigWatcher,
    HealthCheck,
    HealthComponent,
    HealthService,
    Job,
    JsonFormatter,
    LayeredConfig,
    LoggingComponent,
    Response,
    Route,
    Scheduler,
    SchedulerComponent,
    Status,
)
from corlet.std.logging import ComponentFilter, configure_logging
from corlet.testing import running, wait_for_state

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_layered_config(tmp_path: Path) -> None:
    (tmp_path / "base.toml").write_text(
        '[components."acme.db"]\nhost = "base"\nport = 1\n[container]\ndisabled = ["x"]\n'
    )
    (tmp_path / "override.json").write_text(json.dumps({"components": {"acme.db": {"port": 2}}}))
    (tmp_path / "extra.yaml").write_text("components:\n  acme.db:\n    user: yaml\n")
    cfg = LayeredConfig(
        defaults={"components": {"acme.db": {"pool": 5}}},
        files=[
            tmp_path / "base.toml",
            tmp_path / "override.json",
            tmp_path / "extra.yaml",
            f"?{tmp_path / 'missing.toml'}",
        ],
        env_prefix="APP",
        environ={"APP__COMPONENTS__ACME__DB__PORT": "3", "APP__FEATURE__FLAGS": '["a","b"]'},
        overrides=["components.acme.db.debug=true"],
    )
    tree = cfg.load()
    from corlet import config_section

    section = config_section(tree, "acme.db")
    assert section == {"pool": 5, "host": "base", "port": 3, "user": "yaml", "debug": True}
    assert tree["feature"]["flags"] == ["a", "b"]
    assert tree["container"]["disabled"] == ["x"]
    assert len(cfg.fingerprint()) == 4


def test_parent_and_child_component_sections() -> None:
    from corlet import config_section
    from corlet.core.config import canonicalize

    tree = canonicalize({"components": {"acme": {"debug": True}, "acme.db": {"port": 1}}})
    assert tree == {"components": {"acme": {"debug": True, "db": {"port": 1}}}}
    ids = ["acme", "acme.db"]
    assert config_section(tree, "acme", ids) == {"debug": True}
    assert config_section(tree, "acme.db", ids) == {"port": 1}


def test_layered_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        LayeredConfig(files=[tmp_path / "nope.toml"]).load()
    (tmp_path / "bad.toml").write_text("= nope")
    with pytest.raises(ConfigError, match="cannot read"):
        LayeredConfig(files=[tmp_path / "bad.toml"]).load()
    with pytest.raises(ConfigError, match="invalid override"):
        LayeredConfig(overrides=["novalue"])


async def test_config_service() -> None:
    async with running(ConfigServiceComponent, config={"app": {"title": "Shop"}}) as c:
        svc = c.services.instance(ConfigService)
        assert svc.get("app.title") == "Shop"
        assert svc.get("app.missing", 42) == 42


async def test_config_watcher_reloads_on_file_change(tmp_path: Path) -> None:
    f = tmp_path / "app.toml"
    f.write_text('[components."t.watched"]\nvalue = 1\n')
    seen: list[int] = []

    @component("t.watched")
    class Watched(Component):
        async def start(self, ctx: ComponentContext) -> None:
            seen.append(ctx.config["value"])

    cfg = LayeredConfig(
        files=[f],
        env_prefix=None,
        defaults={"components": {"corlet.config.watcher": {"interval": 0.02}}},
    )
    # without a watchable source the watcher idles harmlessly
    async with running(ConfigWatcher) as idle:
        assert idle.state_of("corlet.config.watcher") is ComponentState.ACTIVE
    c = Container(Watched, ConfigWatcher, config=cfg)
    async with c:
        await asyncio.sleep(0.05)
        import os

        f.write_text('[components."t.watched"]\nvalue = 2\n')
        st = f.stat()
        os.utime(f, (st.st_atime, st.st_mtime + 5))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if seen == [1, 2]:
                break
        assert seen == [1, 2]


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


async def test_logging_component_filter_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    @component("t.chatty")
    class Chatty(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.logger.info("hello from start")

            async def bg() -> None:
                logging.getLogger("some.lib").warning("from background")

            await ctx.spawn(bg())

    cfg = {"components": {"corlet.logging": {"level": "INFO", "format": "json"}}}
    async with running(LoggingComponent, config=cfg) as c:
        assert c.state_of("corlet.logging") is ComponentState.ACTIVE
        c2 = Container(Chatty)
        async with c2:
            pass
    lines = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    by_msg = {entry["message"]: entry for entry in lines}
    assert by_msg["hello from start"]["component"] == "t.chatty"
    assert by_msg["from background"]["component"] == "t.chatty"  # via contextvar


SILENT_APP = """
import asyncio
from corlet import Component, Container, component

@component("app.ok", provides=["thing"])  # declared but not registered -> warning
class Ok(Component):
    async def start(self, ctx):
        ctx.logger.warning("component warning")

@component("app.broken")
class Broken(Component):
    async def start(self, ctx):
        raise RuntimeError("boom")

async def main(container):
    print("app output")

Container(Ok, Broken, fail_fast=False).run(main, install_signal_handlers=False)
"""


def test_no_output_without_logging_configuration(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = tmp_path / "app.py"
    script.write_text(SILENT_APP)
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=30, check=True
    )
    assert proc.stdout == "app output\n"
    assert proc.stderr == ""


def test_configure_logging_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = logging.getLogger("corlet.test")

    configure_logging("INFO", output="stdout")
    log.info("to stdout")
    out = capsys.readouterr()
    assert "to stdout" in out.out and out.err == ""

    path = tmp_path / "logs" / "app.log"  # parent directory is created
    file_handler = configure_logging("INFO", output="file", file=path, format="json")
    log.warning("to file")
    assert json.loads(path.read_text().splitlines()[-1])["message"] == "to file"
    assert capsys.readouterr().out == ""

    assert configure_logging("INFO", output="none") is None
    assert file_handler is not None and file_handler.stream is None  # replaced -> closed
    log.warning("nowhere")
    assert "nowhere" not in path.read_text()
    assert capsys.readouterr() == ("", "")

    class Collect(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    custom = Collect()
    custom.setFormatter(logging.Formatter("%(message)s"))
    assert configure_logging("DEBUG", handler=custom) is custom
    log.debug("to handler")
    assert custom.records[-1].component == "-"  # type: ignore[attr-defined]
    assert custom.format(custom.records[-1]) == "to handler"  # own formatter kept
    configure_logging("INFO", output="none")
    assert logging.getLogger().handlers.count(custom) == 0


def test_configure_logging_rejects_bad_arguments(tmp_path: Path) -> None:
    installed = configure_logging("INFO", output="none", levels={})
    assert installed is None
    with pytest.raises(ValueError, match="requires a file"):
        configure_logging("INFO", output="file")
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("LOUD")
    with pytest.raises(ValueError, match="unknown log output"):
        configure_logging("INFO", output="syslog")  # type: ignore[arg-type]


async def test_logging_component_writes_to_file(tmp_path: Path) -> None:
    path = tmp_path / "app.log"

    @component("t.writer")
    class Writer(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.logger.info("hello file")

    cfg = {"components": {"corlet.logging": {"output": "file", "file": str(path)}}}
    async with running(LoggingComponent, config=cfg), running(Writer):
        pass
    assert "[t.writer]" in path.read_text() and "hello file" in path.read_text()

    bad = {"components": {"corlet.logging": {"output": "file"}}}
    async with running(LoggingComponent, config=bad, fail_fast=False) as c:
        assert c.state_of("corlet.logging") is ComponentState.FAILED
        assert "file: required" in str(c.error_of("corlet.logging"))


def test_component_filter_outside_components() -> None:
    record = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    ComponentFilter().filter(record)
    assert record.component == "-"  # type: ignore[attr-defined]
    payload = json.loads(JsonFormatter(static={"app": "a"}).format(record))
    assert payload["app"] == "a" and payload["component"] == "-"


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------


async def test_scheduler_service_and_declarative_jobs() -> None:
    ticks: list[str] = []

    @component(
        "t.jobs",
        extensions=[
            contribution(JOBS, Job("tick", 0.01, lambda: ticks.append("decl"), run_on_start=True))
        ],
    )
    class Declarative(Component):
        async def start(self, ctx: ComponentContext) -> None:
            pass

    @component("t.imperative")
    class Imperative(Component):
        async def start(self, ctx: ComponentContext, scheduler: Scheduler) -> None:
            async def work() -> None:
                ticks.append("imp")

            scheduler.every(0.01, work, name="work", run_now=True)
            scheduler.after(0.0, lambda: ticks.append("once"))

    async with running(SchedulerComponent, Declarative, Imperative) as c:
        await asyncio.sleep(0.05)
        assert {"decl", "imp", "once"} <= set(ticks)
        sched = c.services.instance(Scheduler)
        owners = {j.owner for j in sched.jobs()}
        assert owners == {"t.jobs", "t.imperative"}
        # stopping the owners cancels their jobs
        await c.disable("t.jobs")
        await c.disable("t.imperative")
        await asyncio.sleep(0.02)
        assert sched.jobs() == []
        n = len(ticks)
        await asyncio.sleep(0.03)
        assert len(ticks) == n


async def test_scheduler_job_failures_are_isolated() -> None:
    async with running(SchedulerComponent) as c:
        sched = c.services.instance(Scheduler)
        calls: list[int] = []

        def flaky() -> None:
            calls.append(1)
            raise RuntimeError("x")

        job = sched.every(0.005, flaky, run_now=True)
        await asyncio.sleep(0.03)
        assert job.failures >= 2 and job.active
        job.cancel()


# ---------------------------------------------------------------------------
# health + admin
# ---------------------------------------------------------------------------


def ok_check() -> CheckResult:
    return CheckResult.up("fine", latency=1)


async def slow_check() -> bool:
    await asyncio.sleep(1)
    return True


def failing_check() -> bool:
    raise RuntimeError("db unreachable")


@component(
    "t.checks",
    extensions=[
        contribution(HEALTH_CHECKS, HealthCheck("ok", ok_check)),
        contribution(HEALTH_CHECKS, HealthCheck("slow", slow_check, timeout=0.01, critical=False)),
    ],
)
class Checks(Component):
    async def start(self, ctx: ComponentContext) -> None:
        pass


@component("t.badcheck", extensions=[contribution(HEALTH_CHECKS, HealthCheck("db", failing_check))])
class BadCheck(Component):
    async def start(self, ctx: ComponentContext) -> None:
        pass


async def test_health_report() -> None:
    async with running(HealthComponent, Checks) as c:
        report = await c.services.instance(HealthService).report()
        assert report.status is Status.DEGRADED  # slow, non-critical check timed out
        by_name = {r.name: r for r in report.checks}
        assert by_name["ok"].status is Status.UP and by_name["ok"].owner == "t.checks"
        assert "timed out" in (by_name["slow"].detail or "")
        await c.disable("t.checks")
        assert (await c.services.instance(HealthService).report()).status is Status.UP
    async with running(HealthComponent, BadCheck) as c:
        report = await c.services.instance(HealthService).report()
        assert report.status is Status.DOWN
        assert "db unreachable" in (report.checks[0].detail or "")


async def http(port: int, method: str, path: str, *, token: str | None = None) -> tuple[int, Any]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    headers = f"Authorization: Bearer {token}\r\n" if token else ""
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: x\r\n{headers}Content-Length: 0\r\n\r\n".encode()
    )
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    ctype = next(
        (
            line.split(b":", 1)[1].strip()
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-type")
        ),
        b"",
    )
    return status, json.loads(body) if b"json" in ctype else body.decode()


@component(
    "t.routes",
    extensions=[
        contribution(
            ADMIN_ROUTES, Route("GET", "/hello/{name}", lambda req: {"hi": req.params["name"]})
        ),
        contribution(
            ADMIN_ROUTES, Route("GET", "/teapot", lambda req: Response.text("short", 418))
        ),
    ],
)
class Routes(Component):
    async def start(self, ctx: ComponentContext) -> None:
        pass


async def test_admin_endpoint() -> None:
    cfg = {"components": {"corlet.admin": {"port": 0, "allow_control": True}}}
    async with running(AdminComponent, HealthComponent, Checks, Routes, config=cfg) as c:
        port = c.services.instance(AdminServer).port
        status, comps = await http(port, "GET", "/components")
        assert status == 200 and {x["id"] for x in comps} >= {"corlet.admin", "t.routes"}
        status, one = await http(port, "GET", "/components/t.routes")
        assert one["state"] == "active"
        assert (await http(port, "GET", "/components/nope"))[0] == 404
        status, health = await http(port, "GET", "/health")
        assert status == 200 and health["status"] == "degraded"
        assert (await http(port, "GET", "/hello/bob"))[1] == {"hi": "bob"}
        assert (await http(port, "GET", "/teapot")) == (418, "short")
        status, graph = await http(port, "GET", "/graph?format=mermaid")
        assert graph.startswith("flowchart")
        assert (await http(port, "GET", "/services"))[0] == 200
        assert (await http(port, "DELETE", "/services"))[0] == 405
        assert (await http(port, "GET", "/missing"))[0] == 404
        index = (await http(port, "GET", "/"))[1]
        assert any(r["path"] == "/hello/{name}" and r["owner"] == "t.routes" for r in index)

        status, result = await http(port, "POST", "/components/t.routes/disable")
        assert status == 200 and result["state"] == "disabled"
        assert (await http(port, "GET", "/hello/bob"))[0] == 404  # route withdrawn
        await http(port, "POST", "/components/t.routes/enable")
        assert (await http(port, "GET", "/hello/bob"))[0] == 200

        # restarting the admin component itself through its own endpoint
        status, result = await http(port, "POST", "/components/corlet.admin/restart")
        assert status == 200 and "corlet.admin" in result["started"]
        await wait_for_state(c, "corlet.admin", ComponentState.ACTIVE)


async def test_admin_token_and_control_disabled() -> None:
    cfg = {"components": {"corlet.admin": {"port": 0, "token": "s3cret"}}}
    async with running(AdminComponent, config=cfg) as c:
        port = c.services.instance(AdminServer).port
        assert (await http(port, "GET", "/components"))[0] == 401
        assert (await http(port, "GET", "/components", token="s3cret"))[0] == 200
        status, _body = await http(port, "POST", "/components/corlet.admin/restart", token="s3cret")
        assert status == 403
        status, health = await http(port, "GET", "/health", token="s3cret")  # no HealthService
        assert status == 200 and health["status"] == "up"


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------

RELOADABLE = """
from corlet import Component, component

@component("hot.greeter", provides=["greeting"])
class Greeter(Component):
    async def start(self, ctx):
        ctx.services.register("greeting", "{text}")
"""


async def test_hot_reload(tmp_path: Path) -> None:
    import os

    from corlet import DirectorySource
    from corlet.std import ReloadComponent

    mod = tmp_path / "greeter.py"
    mod.write_text(RELOADABLE.format(text="v1"))
    cfg = {"components": {"corlet.reload": {"interval": 0.02}}}
    async with running(DirectorySource(tmp_path), ReloadComponent, config=cfg) as c:
        assert c.services.instance("greeting") == "v1"
        await asyncio.sleep(0.05)
        mod.write_text(RELOADABLE.format(text="v2"))
        st = mod.stat()
        os.utime(mod, (st.st_atime, st.st_mtime + 5))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if c.services.has("greeting") and c.services.instance("greeting") == "v2":
                break
        assert c.services.instance("greeting") == "v2"
        # a syntax error keeps the running version
        mod.write_text("this is not python")
        st = mod.stat()
        os.utime(mod, (st.st_atime, st.st_mtime + 10))
        await asyncio.sleep(0.1)
        assert c.services.instance("greeting") == "v2"
        assert c.state_of("hot.greeter") is ComponentState.ACTIVE
