from __future__ import annotations

import importlib
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openconstraint_mcp.cli import app
from openconstraint_mcp.minizinc.core import MiniZincExecutionError
from openconstraint_mcp.runtime import read_install_config
from openconstraint_mcp.runtime_install.core import _write_runtime_marker
from openconstraint_mcp.runtime_install.errors import RuntimeInstallError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_supported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the platform check to a no-op so these tests work on macOS, Windows,
    and Linux-ARM dev machines. Tests that need a different behaviour re-patch."""
    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.core.check_supported_platform",
        lambda: None,
    )


def _fake_install_success(
    target: Path,
) -> Path:
    target = target.expanduser().resolve()
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "minizinc"
    binary.write_text("#!/bin/sh\necho 'minizinc fake'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    _write_runtime_marker(target)
    return target


def test_check_runtime_reports_missing(fake_runtime_dir: Path) -> None:
    result = runner.invoke(app, ["check-runtime"])
    assert result.exit_code == 1
    assert "not installed" in result.stdout.lower()


def test_list_solvers_reports_missing(fake_runtime_dir: Path) -> None:
    result = runner.invoke(app, ["list-solvers"])
    assert result.exit_code == 1
    assert "install-runtime" in result.stdout


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "stdio",
        "install-runtime",
        "configure-runtime",
        "check-runtime",
        "list-solvers",
    ):
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


def test_check_runtime_warns_on_corrupt_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Runtime is deterministically missing (env override at an empty dir), so the
    # command exits 1; a present-but-corrupt config must still be surfaced.
    monkeypatch.setenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", str(tmp_path / "empty"))
    config_path = tmp_path / "install.json"
    config_path.write_text("{ not valid json")
    monkeypatch.setattr("openconstraint_mcp.runtime._config_path", lambda: config_path)

    result = runner.invoke(app, ["check-runtime"])
    assert result.exit_code == 1
    assert "corrupt" in result.stderr.lower()


# ---------------------------------------------------------------------------
# install-runtime CLI


def test_install_runtime_succeeds_when_config_write_fails(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_install(target: Path, *, yes: bool, console: object) -> Path:
        return _fake_install_success(target)

    def _fail_write(_runtime_dir: Path) -> None:
        raise PermissionError(13, "Permission denied", str(isolated_config_dir))

    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake_install
    )
    monkeypatch.setattr("openconstraint_mcp.runtime.write_install_config", _fail_write)

    target = tmp_path / "runtime"
    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target), "--yes"])
    # The runtime is on disk, so the command exits 0 with a yellow warning
    # pointing the user at the env-var workaround.
    assert result.exit_code == 0, result.output
    # rich may wrap the long tmp path across lines; normalise before matching.
    flat = result.stdout.replace("\n", "")
    assert "Permission denied" in flat
    assert "OPENCONSTRAINT_MCP_RUNTIME_DIR" in flat
    assert str(target.resolve()) in flat


def test_install_runtime_warns_on_corrupt_config(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_config_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_config_dir.write_text("{ not valid json")

    def _fake(target: Path, *, yes: bool, console: object) -> Path:
        return _fake_install_success(target)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    target = tmp_path / "runtime"
    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target), "--yes"])
    assert result.exit_code == 0, result.output
    assert "corrupt" in result.stderr.lower()


def test_install_runtime_with_explicit_dir_and_yes(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, bool]] = []

    def _fake(target: Path, *, yes: bool, console: object) -> Path:
        calls.append((target, yes))
        return _fake_install_success(target)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    target = tmp_path / "runtime"
    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target), "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == [(target.expanduser().resolve(), True)]

    config = read_install_config()
    assert config is not None
    assert Path(config.runtime_dir) == target.resolve()


def test_install_runtime_without_runtime_dir_uses_default(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_target = tmp_path / "default-runtime"
    monkeypatch.setattr("openconstraint_mcp.runtime.get_runtime_dir", lambda: default_target)

    received: list[Path] = []

    def _fake(target: Path, *, yes: bool, console: object) -> Path:
        received.append(target)
        return _fake_install_success(target)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    result = runner.invoke(app, ["install-runtime", "--yes"])
    assert result.exit_code == 0, result.output
    assert received == [default_target.expanduser().resolve()]

    # The default path is persisted to install.json, not just handed to the installer.
    config = read_install_config()
    assert config is not None
    assert Path(config.runtime_dir) == default_target.resolve()


def test_install_runtime_prompts_for_path_when_tty(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_target = tmp_path / "custom"
    default_target = tmp_path / "default"
    monkeypatch.setattr("openconstraint_mcp.runtime.get_runtime_dir", lambda: default_target)
    monkeypatch.setattr("openconstraint_mcp.cli._stdin_is_tty", lambda: True)

    received: list[Path] = []

    def _fake(target: Path, *, yes: bool, console: object) -> Path:
        received.append(target)
        return _fake_install_success(target)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    result = runner.invoke(app, ["install-runtime"], input=f"{custom_target}\n")
    assert result.exit_code == 0, result.output
    assert received == [custom_target.resolve()]


def test_install_runtime_overwrite_confirm_accepted(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    _write_runtime_marker(target)

    monkeypatch.setattr("openconstraint_mcp.cli._stdin_is_tty", lambda: True)

    received_yes: list[bool] = []

    def _fake(t: Path, *, yes: bool, console: object) -> Path:
        received_yes.append(yes)
        return _fake_install_success(t)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target)], input="y\n")
    assert result.exit_code == 0, result.output
    assert received_yes == [True]


def test_install_runtime_overwrite_confirm_declined(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    _write_runtime_marker(target)
    (target / "prior-evidence").write_text("from prior install")

    monkeypatch.setattr("openconstraint_mcp.cli._stdin_is_tty", lambda: True)

    called = {"n": 0}

    def _fake(*args: object, **kwargs: object) -> Path:
        called["n"] += 1
        raise AssertionError("installer should not be called when user declines")

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target)], input="n\n")
    assert result.exit_code == 0, result.output
    assert called["n"] == 0
    assert "aborted" in result.stdout.lower()
    assert (target / "prior-evidence").read_text() == "from prior install"


def test_install_runtime_overwrite_refused_in_non_tty(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    _write_runtime_marker(target)

    monkeypatch.setattr("openconstraint_mcp.cli._stdin_is_tty", lambda: False)

    called = {"n": 0}

    def _fake(*args: object, **kwargs: object) -> Path:
        called["n"] += 1
        return target

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target)])
    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert called["n"] == 0


def test_install_runtime_unmanaged_nonempty_refused_with_yes(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "important.txt").write_text("user data")

    prompt_calls = {"n": 0}
    install_calls = {"n": 0}

    def _prompt(*args: object, **kwargs: object) -> str:
        prompt_calls["n"] += 1
        raise AssertionError("must not prompt for unmanaged directory")

    def _fake(*args: object, **kwargs: object) -> Path:
        install_calls["n"] += 1
        return target

    monkeypatch.setattr("click.termui.visible_prompt_func", _prompt)
    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target), "--yes"])
    assert result.exit_code == 1
    assert prompt_calls["n"] == 0
    assert install_calls["n"] == 0
    assert (target / "important.txt").read_text() == "user data"


def test_install_runtime_refuses_target_that_is_a_regular_file(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime-file"
    target.write_text("not a directory")

    install_calls = {"n": 0}
    prompt_calls = {"n": 0}

    def _fake(*args: object, **kwargs: object) -> Path:
        install_calls["n"] += 1
        return target

    def _prompt(*args: object, **kwargs: object) -> str:
        prompt_calls["n"] += 1
        raise AssertionError("must not prompt when target is a regular file")

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)
    monkeypatch.setattr("click.termui.visible_prompt_func", _prompt)

    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target), "--yes"])
    assert result.exit_code == 1
    assert "not a directory" in result.stdout.lower()
    assert install_calls["n"] == 0
    assert prompt_calls["n"] == 0


def test_install_runtime_unsupported_platform_rejected_before_prompt(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> None:
        raise RuntimeInstallError(
            "openconstraint-mcp install-runtime currently supports Linux x86_64 only."
        )

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.check_supported_platform", _raise)

    install_calls = {"n": 0}
    prompt_calls = {"n": 0}

    def _fake(*args: object, **kwargs: object) -> Path:
        install_calls["n"] += 1
        return tmp_path

    def _prompt(*args: object, **kwargs: object) -> str:
        prompt_calls["n"] += 1
        return ""

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)
    monkeypatch.setattr("click.termui.visible_prompt_func", _prompt)

    result = runner.invoke(app, ["install-runtime"])
    assert result.exit_code == 1
    assert "Linux x86_64" in result.stdout
    assert install_calls["n"] == 0
    assert prompt_calls["n"] == 0


def test_install_runtime_installer_error_surfaces(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*args: object, **kwargs: object) -> Path:
        raise RuntimeInstallError("simulated failure")

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    target = tmp_path / "runtime"
    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target), "--yes"])
    assert result.exit_code == 1
    assert "simulated failure" in result.stdout


def test_install_runtime_filesystem_error_surfaces_cleanly(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare OSError (e.g. PermissionError on `parent.mkdir` for a privileged
    # --runtime-dir) must produce the clean red message, not a raw traceback.
    def _fake(*args: object, **kwargs: object) -> Path:
        raise PermissionError(13, "Permission denied", str(tmp_path / "runtime"))

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.install_managed_runtime", _fake)

    target = tmp_path / "runtime"
    result = runner.invoke(app, ["install-runtime", "--runtime-dir", str(target), "--yes"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, OSError)
    assert "Permission denied" in result.stdout


# ---------------------------------------------------------------------------
# configure-runtime CLI


def _fake_external_minizinc(target: Path) -> Path:
    """Build a fake external MiniZinc install — bin/minizinc only, no marker.

    Mirrors what configure-runtime expects to find on disk: a user-owned
    MiniZinc directory that this package did not install and does not manage.
    """
    target = target.expanduser().resolve()
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "minizinc"
    binary.write_text("#!/bin/sh\necho 'minizinc fake'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return target


def test_configure_runtime_persists_path(
    tmp_path: Path,
    isolated_config_dir: Path,
) -> None:
    target = _fake_external_minizinc(tmp_path / "existing-minizinc")
    result = runner.invoke(app, ["configure-runtime", "--runtime-dir", str(target)])
    assert result.exit_code == 0, result.output

    config = read_install_config()
    assert config is not None
    assert Path(config.runtime_dir) == target.resolve()


def test_configure_runtime_rejects_missing_dir(
    tmp_path: Path,
    isolated_config_dir: Path,
) -> None:
    target = tmp_path / "does-not-exist"
    result = runner.invoke(app, ["configure-runtime", "--runtime-dir", str(target)])
    assert result.exit_code == 1
    assert "Not a directory" in result.stdout
    assert read_install_config() is None


def test_configure_runtime_rejects_dir_without_minizinc_binary(
    tmp_path: Path,
    isolated_config_dir: Path,
) -> None:
    target = tmp_path / "empty-dir"
    target.mkdir()
    result = runner.invoke(app, ["configure-runtime", "--runtime-dir", str(target)])
    assert result.exit_code == 1
    # rich may wrap long tmp paths, including inside words around line breaks.
    assert "does not look like a MiniZinc install" in " ".join(result.stdout.split())
    assert read_install_config() is None


def test_configure_runtime_rejects_non_executable_binary(
    tmp_path: Path,
    isolated_config_dir: Path,
) -> None:
    if sys.platform == "win32":
        pytest.skip("Executable bit semantics differ on Windows")
    target = tmp_path / "broken-minizinc"
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "minizinc"
    binary.write_text("#!/bin/sh\n")
    # Deliberately no chmod +x.

    result = runner.invoke(app, ["configure-runtime", "--runtime-dir", str(target)])
    assert result.exit_code == 1
    assert "not executable" in result.stdout
    assert read_install_config() is None


def test_configure_runtime_config_write_failure_surfaces_cleanly(
    tmp_path: Path,
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _fake_external_minizinc(tmp_path / "existing-minizinc")

    def _fail_write(_runtime_dir: Path) -> None:
        raise PermissionError(13, "Permission denied", str(isolated_config_dir))

    monkeypatch.setattr("openconstraint_mcp.runtime.write_install_config", _fail_write)

    result = runner.invoke(app, ["configure-runtime", "--runtime-dir", str(target)])
    assert result.exit_code == 1
    flat = result.stdout.replace("\n", "")
    assert "Permission denied" in flat
    assert "OPENCONSTRAINT_MCP_RUNTIME_DIR" in flat
    assert str(target.resolve()) in flat


def test_cli_module_does_not_import_httpx_eagerly() -> None:
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "httpx" or name.startswith("openconstraint_mcp")
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        importlib.import_module("openconstraint_mcp.cli")
        assert "httpx" not in sys.modules
    finally:
        for name in list(sys.modules):
            if name == "httpx" or name.startswith("openconstraint_mcp"):
                sys.modules.pop(name, None)
        sys.modules.update(saved)
