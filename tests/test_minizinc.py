from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from openconstraint_mcp.minizinc import list_solvers
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas import SolverList


def test_list_solvers_raises_clear_error_when_runtime_missing(
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        list_solvers()
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


def _materialise_fake_binary(fake_runtime_dir: Path) -> Path:
    bin_dir = fake_runtime_dir / "bin"
    bin_dir.mkdir()
    binary_name = "minizinc.exe" if sys.platform == "win32" else "minizinc"
    binary = bin_dir / binary_name
    binary.write_text("")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def test_list_solvers_parses_solvers_json(
    fake_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _materialise_fake_binary(fake_runtime_dir)

    payload = json.dumps(
        [
            {
                "id": "org.gecode.gecode",
                "name": "Gecode",
                "version": "6.3.0",
                "tags": ["cp", "int"],
            },
            {
                "id": "com.google.or-tools.cpsat",
                "name": "OR-Tools CP-SAT",
                "version": "9.10",
            },
        ]
    )

    class _FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

    def _fake_run(*args: object, **kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(payload)

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    result = list_solvers()
    assert isinstance(result, SolverList)
    assert [solver.id for solver in result.solvers] == [
        "org.gecode.gecode",
        "com.google.or-tools.cpsat",
    ]
    assert result.solvers[0].tags == ["cp", "int"]
    assert result.solvers[1].version == "9.10"
    assert result.solvers[1].tags == []
