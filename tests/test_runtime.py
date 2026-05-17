from __future__ import annotations

from pathlib import Path

from openconstraint_mcp.runtime import (
    get_minizinc_binary,
    get_runtime_dir,
    get_runtime_status,
    is_runtime_installed,
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
