from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from openconstraint_mcp.cli import app
from openconstraint_mcp.minizinc import MiniZincExecutionError

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


def test_list_solvers_handles_execution_failure_cleanly(
    fake_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_execution_error() -> None:
        raise MiniZincExecutionError(
            "Managed MiniZinc binary failed to list solvers: bad config. "
            "Try `openconstraint-mcp install-runtime`."
        )

    monkeypatch.setattr("openconstraint_mcp.cli.list_solvers", _raise_execution_error)

    result = runner.invoke(app, ["list-solvers"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, MiniZincExecutionError)
    assert "install-runtime" in result.stdout
