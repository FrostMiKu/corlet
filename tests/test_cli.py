"""CLI and example application tests."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from corlet.std.cli import main
from corlet.testing import assert_no_leaks

ROOT = Path(__file__).resolve().parent.parent
SHOP = ["-a", "examples.shop.app:create", "-c", str(ROOT / "examples/shop/shop.toml")]


@pytest.fixture(autouse=True)
def _cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.syspath_prepend(str(ROOT))


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(["--log-level", "ERROR", "--no-entry-points", *argv])
    return code, capsys.readouterr().out


def test_list_graph_check_inspect(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_cli(capsys, *SHOP, "list")
    assert code == 0 and "shop.orders" in out and "Storage" in out
    code, out = run_cli(capsys, *SHOP, "list", "--json")
    assert {c["id"] for c in json.loads(out)} >= {"shop.orders", "corlet.admin"}
    code, out = run_cli(capsys, *SHOP, "graph", "--format", "dot")
    assert code == 0 and '"shop.orders" -> "shop.storage.memory"' in out
    code, out = run_cli(capsys, *SHOP, "check")
    assert code == 0 and out.strip().endswith("OK")
    code, out = run_cli(capsys, *SHOP, "inspect")
    assert json.loads(out)["name"] == "shop"


def test_config_command(capsys: pytest.CaptureFixture[str]) -> None:
    _code, out = run_cli(
        capsys,
        *SHOP,
        "-s",
        "components.shop.pricing.discount.percent=50",
        "config",
        "shop.pricing.discount",
    )
    data = json.loads(out)
    assert data["effective"]["percent"] == 50
    assert data["effective"]["vip"] == ["alice"]


def test_contributed_command_runs_app(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_cli(
        capsys, *SHOP, "-s", "components.corlet.admin.port=0", "demo", "alice", "bob"
    )
    assert code == 0
    assert "alice pays 5.1 EUR" in out  # 15% VIP discount, then 20% VAT
    assert "bob pays 6.0 EUR" in out


def test_disable_from_cli(capsys: pytest.CaptureFixture[str]) -> None:
    _code, out = run_cli(
        capsys,
        *SHOP,
        "--disable",
        "shop.pricing.vat",
        "-s",
        "components.corlet.admin.port=0",
        "demo",
        "bob",
    )
    assert "bob pays 5.0 EUR" in out


def test_check_fails_on_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "app.py").write_text(
        textwrap.dedent(
            """
            from corlet import Component, component

            @component("x.needy", requires=["x.missing"])
            class Needy(Component):
                async def start(self, ctx):
                    pass
            """
        )
    )
    code, out = run_cli(capsys, "-a", str(tmp_path), "check")
    assert code == 1 and "missing-component" in out and "FAILED" in out
    code, out = run_cli(capsys, "-a", str(tmp_path / "app.py"), "doctor")
    assert code == 1 and "python" in out


def test_std_components_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_cli(capsys, "-a", "corlet.std.health", "--std", "scheduler", "list")
    assert code == 0 and "corlet.scheduler" in out and "corlet.health" in out
    code = main(["--no-entry-points", "-a", "corlet.std.health", "--std", "nope", "list"])
    assert code == 2
    code = main(["--no-entry-points", "-a", "no.such.module", "list"])
    assert code == 2
    assert "cannot import" in capsys.readouterr().err


def test_pyproject_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.corlet]\napp = ["corlet.std.scheduler"]\nstd = ["health"]\n'
    )
    monkeypatch.chdir(tmp_path)
    code, out = run_cli(capsys, "list")
    assert code == 0 and "corlet.scheduler" in out and "corlet.health" in out


async def test_shop_example_in_process() -> None:
    from examples.shop.app import create
    from examples.shop.contracts import Orders

    cfg = {
        "components": {
            "corlet.admin": {"port": 0},
            "shop": {"pricing": {"discount": {"vip": ["dora"], "percent": 50}}},
        }
    }
    container = create(cfg)
    async with container:
        orders = container.services.instance(Orders)
        assert (await orders.place("dora", {"pear": 4}))["total"] == 1.8  # 3 * .5 * 1.2
        await container.disable("shop.pricing.discount")
        assert (await orders.place("dora", {"pear": 4}))["total"] == 3.6
        await container.disable("shop.storage.memory")
        assert not container.services.has(Orders)  # orders stopped with its storage
    assert_no_leaks(container)


def test_log_level_defaults_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from corlet.std.cli import log_level_for

    monkeypatch.delenv("CORLET_LOG_LEVEL", raising=False)

    def ns(**kw: object) -> argparse.Namespace:
        return argparse.Namespace(**{"log_level": None, "verbose": 0, "quiet": False, **kw})

    assert log_level_for(ns(), "run") == "INFO"
    assert log_level_for(ns(), "list") == "WARNING"
    assert log_level_for(ns(), "my-command") == "WARNING"
    assert log_level_for(ns(verbose=1), "list") == "INFO"
    assert log_level_for(ns(verbose=5), "run") == "DEBUG"
    assert log_level_for(ns(quiet=True), "run") == "ERROR"
    assert log_level_for(ns(log_level="debug", quiet=True), "list") == "DEBUG"
    monkeypatch.setenv("CORLET_LOG_LEVEL", "error")
    assert log_level_for(ns(), "run") == "ERROR"


def test_log_file_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import logging

    log_file = tmp_path / "corlet.log"
    code = main(
        [
            "--no-entry-points",
            "-a",
            "corlet.std.health",
            "--disable",
            "no.such.component",
            "--log-file",
            str(log_file),
            "list",
        ]
    )
    out, err = capsys.readouterr()
    assert code == 0 and "corlet.health" in out
    assert err == ""
    assert "unknown component 'no.such.component'" in log_file.read_text()
    assert logging.getLogger().level == logging.WARNING  # default for non-'run' commands


def test_contributed_command_output_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    # without the app's own corlet.logging component, the CLI default (WARNING) applies
    args = ["--no-entry-points", *SHOP, "--disable", "corlet.logging"]
    code = main([*args, "-s", "components.corlet.admin.port=0", "demo", "bob"])
    out, err = capsys.readouterr()
    assert code == 0 and "bob pays" in out
    assert err == ""
