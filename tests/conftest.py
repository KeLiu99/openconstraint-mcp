from __future__ import annotations

import io
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from openconstraint_mcp.runtime import is_runtime_installed


@pytest.fixture
def require_real_runtime() -> None:
    """Skip a real-binary integration test when no managed runtime is installed."""
    if not is_runtime_installed():
        pytest.skip("managed MiniZinc runtime not installed")


@pytest.fixture
def fake_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_minizinc_binary(fake_runtime_dir: Path) -> Path:
    bin_dir = fake_runtime_dir / "bin"
    bin_dir.mkdir()
    binary_name = "minizinc.exe" if sys.platform == "win32" else "minizinc"
    binary = bin_dir / binary_name
    binary.write_text("")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


@pytest.fixture
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``runtime._config_path`` at a tmp file and clear the env override.

    Tests that exercise config read/write must not pick up the developer's
    real ``~/.config/openconstraint-mcp/install.json`` or shell env var.
    """
    config_path = tmp_path / "install.json"
    monkeypatch.setattr("openconstraint_mcp.runtime._config_path", lambda: config_path)
    monkeypatch.delenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", raising=False)
    return config_path


@pytest.fixture
def fake_minizinc_tarball(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Mirror the real MiniZinc 2.9.7 Linux bundle: one top-level wrapper dir
    containing ``bin/minizinc`` (executable shell stub) and ``share/README``."""
    archive_dir = tmp_path_factory.mktemp("fake-minizinc-tarball")
    archive = archive_dir / "MiniZincIDE-2.9.7-bundle-linux-x86_64.tgz"
    wrapper = "MiniZincIDE-2.9.7-bundle-linux-x86_64"
    binary_payload = b"#!/bin/sh\necho 'minizinc 2.9.7 (fake)'\n"
    readme_payload = b"fake bundle\n"

    with tarfile.open(archive, "w:gz") as tar:
        wrapper_info = tarfile.TarInfo(name=wrapper)
        wrapper_info.type = tarfile.DIRTYPE
        wrapper_info.mode = 0o755
        tar.addfile(wrapper_info)

        bin_info = tarfile.TarInfo(name=f"{wrapper}/bin")
        bin_info.type = tarfile.DIRTYPE
        bin_info.mode = 0o755
        tar.addfile(bin_info)

        binary_info = tarfile.TarInfo(name=f"{wrapper}/bin/minizinc")
        binary_info.size = len(binary_payload)
        binary_info.mode = 0o755
        tar.addfile(binary_info, io.BytesIO(binary_payload))

        share_info = tarfile.TarInfo(name=f"{wrapper}/share")
        share_info.type = tarfile.DIRTYPE
        share_info.mode = 0o755
        tar.addfile(share_info)

        readme_info = tarfile.TarInfo(name=f"{wrapper}/share/README")
        readme_info.size = len(readme_payload)
        readme_info.mode = 0o644
        tar.addfile(readme_info, io.BytesIO(readme_payload))

    return archive
