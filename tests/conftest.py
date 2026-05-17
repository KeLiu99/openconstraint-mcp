from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest


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
