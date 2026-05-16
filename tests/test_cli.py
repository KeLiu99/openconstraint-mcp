from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from openconstraint_mcp.cli import app

runner = CliRunner()


def test_check_runtime_reports_missing(fake_runtime_dir: Path) -> None:
    result = runner.invoke(app, ["check-runtime"])
    assert result.exit_code == 1
    assert "not installed" in result.stdout.lower()


def test_list_solvers_reports_missing(fake_runtime_dir: Path) -> None:
    result = runner.invoke(app, ["list-solvers"])
    assert result.exit_code == 1
    assert "install-runtime" in result.stdout


def test_install_runtime_is_placeholder(fake_runtime_dir: Path) -> None:
    result = runner.invoke(app, ["install-runtime"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.stdout.lower()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("stdio", "install-runtime", "check-runtime", "list-solvers"):
        assert cmd in result.stdout
