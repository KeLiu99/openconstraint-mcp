"""Opt-in end-to-end install check for Apple Silicon Macs.

Downloads the real pinned ``.dmg`` from the MiniZinc GitHub release and
installs it, so it runs only under ``just integration`` on darwin/arm64 with
``OPENCONSTRAINT_MCP_RUN_INSTALL_DOWNLOAD_TEST=1`` set explicitly.

The CLI is invoked in-process (``CliRunner``) with
``openconstraint_mcp.runtime._config_path`` monkeypatched into ``tmp_path``:
``install-runtime`` persists its location to a platformdirs config with no
env override, so a subprocess invocation would overwrite the machine's real
install config.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openconstraint_mcp.cli import app
from openconstraint_mcp.minizinc.core import solve_model
from openconstraint_mcp.runtime import read_install_config
from openconstraint_mcp.runtime_install.core import MANAGED_RUNTIME_MARKER

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS"),
    pytest.mark.skipif(platform.machine() != "arm64", reason="requires Apple Silicon"),
    pytest.mark.skipif(
        os.environ.get("OPENCONSTRAINT_MCP_RUN_INSTALL_DOWNLOAD_TEST") != "1",
        reason="set OPENCONSTRAINT_MCP_RUN_INSTALL_DOWNLOAD_TEST=1 to run the real download",
    ),
]


def test_install_runtime_end_to_end_on_apple_silicon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "install.json"
    monkeypatch.setattr("openconstraint_mcp.runtime._config_path", lambda: config_path)
    monkeypatch.delenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", raising=False)
    runner = CliRunner()
    runtime_dir = tmp_path / "runtime"

    install = runner.invoke(app, ["install-runtime", "--runtime-dir", str(runtime_dir), "--yes"])
    assert install.exit_code == 0, install.output

    binary = runtime_dir / "bin" / "minizinc"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)

    version = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    assert version.returncode == 0, version.stderr
    assert "minizinc" in version.stdout.lower()

    assert (runtime_dir / MANAGED_RUNTIME_MARKER).is_file()
    config = read_install_config()
    assert config is not None
    assert Path(config.runtime_dir) == runtime_dir.resolve()

    # Resolution must flow through the persisted config, not the env var.
    check = runner.invoke(app, ["check-runtime"])
    assert check.exit_code == 0, check.output
    solvers = runner.invoke(app, ["list-solvers"])
    assert solvers.exit_code == 0, solvers.output

    # Solve smoke through the installed runtime for every shipped solver:
    # cp-sat (default), chuffed (backs num_solutions), and gecode — whose Qt
    # frameworks are vendored into <runtime>/Frameworks during .dmg extraction
    # so it loads headless.
    model = "var 1..3: x; constraint x > 1; solve satisfy;"
    for solver in ("cp-sat", "org.chuffed.chuffed", "org.gecode.gecode"):
        result = solve_model(model, solver=solver)
        assert result.status == "satisfied", f"{solver}: {result.status}"
