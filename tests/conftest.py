from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fake_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", str(tmp_path))
    return tmp_path
