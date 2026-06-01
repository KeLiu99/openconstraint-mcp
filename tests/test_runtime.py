from __future__ import annotations

import json
from pathlib import Path

import pytest

from openconstraint_mcp.runtime import (
    get_minizinc_binary,
    get_runtime_dir,
    get_runtime_status,
    install_config_warning,
    is_runtime_installed,
    read_install_config,
    write_install_config,
)


def test_runtime_dir_uses_env_override(fake_runtime_dir: Path) -> None:
    assert get_runtime_dir() == fake_runtime_dir


def test_minizinc_binary_path_under_bin(fake_runtime_dir: Path) -> None:
    binary = get_minizinc_binary()
    assert binary.parent == fake_runtime_dir / "bin"
    assert binary.name in {"minizinc", "minizinc.exe"}


def test_runtime_not_installed_when_binary_missing(fake_runtime_dir: Path) -> None:
    assert is_runtime_installed() is False
    status = get_runtime_status()
    assert status.installed is False
    assert status.minizinc_binary is None
    assert status.runtime_dir == str(fake_runtime_dir)


def test_runtime_installed_when_executable_present(fake_minizinc_binary: Path) -> None:
    assert is_runtime_installed() is True
    status = get_runtime_status()
    assert status.installed is True
    assert status.minizinc_binary == str(fake_minizinc_binary)


def test_read_install_config_missing_file(isolated_config_dir: Path) -> None:
    assert read_install_config() is None


def test_read_install_config_non_json(isolated_config_dir: Path) -> None:
    isolated_config_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_config_dir.write_text("not json at all")
    assert read_install_config() is None


def test_read_install_config_schema_mismatch(isolated_config_dir: Path) -> None:
    isolated_config_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_config_dir.write_text(json.dumps({"some_other_field": 1}))
    assert read_install_config() is None


def test_read_install_config_rejects_empty_runtime_dir(
    isolated_config_dir: Path,
) -> None:
    isolated_config_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_config_dir.write_text(json.dumps({"runtime_dir": ""}))
    assert read_install_config() is None


def test_read_install_config_rejects_relative_path(
    isolated_config_dir: Path,
) -> None:
    isolated_config_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_config_dir.write_text(json.dumps({"runtime_dir": "./runtime"}))
    assert read_install_config() is None


def test_install_config_warning_none_when_missing(isolated_config_dir: Path) -> None:
    assert install_config_warning() is None


def test_install_config_warning_flags_non_json(isolated_config_dir: Path) -> None:
    isolated_config_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_config_dir.write_text("not json at all")
    warning = install_config_warning()
    assert warning is not None
    assert "invalid json" in warning.lower()


def test_install_config_warning_flags_schema_mismatch(isolated_config_dir: Path) -> None:
    isolated_config_dir.parent.mkdir(parents=True, exist_ok=True)
    isolated_config_dir.write_text(json.dumps({"some_other_field": 1}))
    warning = install_config_warning()
    assert warning is not None
    assert "validation" in warning.lower()


def test_install_config_warning_none_when_valid(
    isolated_config_dir: Path,
    tmp_path: Path,
) -> None:
    write_install_config(tmp_path / "runtime")
    assert install_config_warning() is None


def test_write_then_read_round_trips(
    isolated_config_dir: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "install-target"
    write_install_config(target)
    config = read_install_config()
    assert config is not None
    assert Path(config.runtime_dir) == target.resolve()


def test_get_runtime_dir_env_wins_over_config(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_target = tmp_path / "from-config"
    write_install_config(config_target)
    env_target = tmp_path / "from-env"
    monkeypatch.setenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", str(env_target))
    assert get_runtime_dir() == env_target


def test_get_runtime_dir_returns_config_when_env_unset(
    isolated_config_dir: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "from-config"
    write_install_config(target)
    assert get_runtime_dir() == target.resolve()


def test_get_runtime_dir_falls_back_to_platformdirs_default(
    isolated_config_dir: Path,
) -> None:
    result = get_runtime_dir()
    assert result.name == "minizinc"
    assert "openconstraint-mcp" in str(result)


def test_get_runtime_dir_expands_tilde_in_env(
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", "~/managed-minizinc")
    result = get_runtime_dir()
    assert "~" not in str(result)
    assert result == Path("~/managed-minizinc").expanduser()
