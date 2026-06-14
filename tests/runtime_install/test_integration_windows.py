"""Opt-in install checks for native Windows x86_64.

Two halves, because the silent NSIS install cannot run on a headless CI runner —
``setup.exe`` re-launches itself elevated for UAC and the consent prompt has no
answerer, so it hangs (this was observed burning a runner for hours, leaving
orphan ``…setup-win64.tmp`` processes):

* ``test_windows_bundle_downloads_and_verifies`` — the headless-verifiable half.
  Downloads the real pinned NSIS bundle and checks its sha256 on Windows. Runs
  under ``just integration`` on win32/AMD64 with
  ``OPENCONSTRAINT_MCP_RUN_INSTALL_DOWNLOAD_TEST=1`` (what CI sets).
* ``test_install_runtime_end_to_end_on_windows`` — the full silent
  install + solve. Needs interactive UAC, so it is additionally gated behind
  ``OPENCONSTRAINT_MCP_RUN_WINDOWS_INSTALL_TEST=1`` and skipped in CI; a
  maintainer runs it on a real, interactive Windows host (clicking the UAC
  prompt). The install target is a user-writable directory under ``tmp_path`` —
  no ``Program Files``, no admin.

The end-to-end CLI is invoked in-process (``CliRunner``) with
``openconstraint_mcp.runtime._config_path`` monkeypatched into ``tmp_path``:
``install-runtime`` persists its location to a platformdirs config with no env
override, so a subprocess invocation would overwrite the machine's real install
config.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from openconstraint_mcp.cli import app
from openconstraint_mcp.minizinc.core import solve_model
from openconstraint_mcp.runtime import read_install_config
from openconstraint_mcp.runtime_install.core import MANAGED_RUNTIME_MARKER
from openconstraint_mcp.runtime_install.download import _download_archive, select_bundle

# The silent NSIS install can't be verified on a headless runner: setup.exe
# re-launches itself elevated for UAC, which nobody can answer (it hangs, leaving
# orphan `…setup-win64.tmp` processes). So CI verifies the part that *is* headless
# — the bundle downloads and its sha256 matches on Windows — and the full
# install+solve end-to-end test is opt-in behind a second flag for a maintainer
# running it on a real, interactive Windows box.
_RUN_WINDOWS_INSTALL_TEST = os.environ.get("OPENCONSTRAINT_MCP_RUN_WINDOWS_INSTALL_TEST") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="requires Windows"),
    pytest.mark.skipif(platform.machine() != "AMD64", reason="requires Windows x86_64"),
    pytest.mark.skipif(
        os.environ.get("OPENCONSTRAINT_MCP_RUN_INSTALL_DOWNLOAD_TEST") != "1",
        reason="set OPENCONSTRAINT_MCP_RUN_INSTALL_DOWNLOAD_TEST=1 to run the real download",
    ),
]


def test_windows_bundle_downloads_and_verifies(tmp_path: Path) -> None:
    """The pinned NSIS bundle downloads and its sha256 matches on Windows.

    This is the headless-verifiable half: it exercises the Windows download path
    and confirms the pinned asset's integrity without running the installer (which
    needs interactive UAC). ``_download_archive`` raises on a sha256 mismatch.
    """
    bundle = select_bundle()
    assert bundle.kind == "nsis"
    archive_path = tmp_path / bundle.filename
    _download_archive(bundle.url, archive_path, bundle.sha256, Console(quiet=True))
    assert archive_path.is_file()
    assert archive_path.stat().st_size > 0


@pytest.mark.skipif(
    not _RUN_WINDOWS_INSTALL_TEST,
    reason=(
        "silent NSIS install needs interactive UAC; not verifiable on a headless "
        "runner — set OPENCONSTRAINT_MCP_RUN_WINDOWS_INSTALL_TEST=1 to run it on a "
        "real interactive Windows host"
    ),
)
def test_install_runtime_end_to_end_on_windows(
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

    binary = runtime_dir / "bin" / "minizinc.exe"
    assert binary.is_file()

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
    # cp-sat (default), chuffed and gecode (both back num_solutions). The NSIS
    # installer lays the bundle down in place — no tree reshaping — so gecode's
    # Qt DLLs sit in bin\ beside fzn-gecode.exe; this asserts it loads headless.
    # The solve loop runs after the install/version/check-runtime assertions, so
    # a gecode-only failure still leaves the primary elevation gate provably green.
    model = "var 1..3: x; constraint x > 1; solve satisfy;"
    for solver in ("cp-sat", "org.chuffed.chuffed", "org.gecode.gecode"):
        result = solve_model(model, solver=solver)
        assert result.status == "satisfied", f"{solver}: {result.status}"
