"""Unit tests for pyexec/script_path.py — the shared script-path validator.

Every rejection must name the caller-facing parameter, because that string is
what reaches the MCP client through `@_as_mcp_error(ValueError)`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.pyexec.script_path import validate_script_path


def test_valid_script_returns_the_resolved_absolute_path(tmp_path: Path) -> None:
    script = tmp_path / "sub" / "model.py"
    script.parent.mkdir()
    script.write_text("print('x')", encoding="utf-8")

    resolved = validate_script_path(Path(script.parent) / ".." / "sub" / "model.py")

    assert resolved == script.resolve()


def test_missing_path_names_the_parameter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"checker_path does not exist"):
        validate_script_path(tmp_path / "nope.py", parameter="checker_path")


def test_directory_names_the_parameter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"checker_path is not a file"):
        validate_script_path(tmp_path, parameter="checker_path")


def test_non_utf8_script_names_the_parameter(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(ValueError, match=r"checker_path is not valid UTF-8"):
        validate_script_path(script, parameter="checker_path")


def test_blank_script_names_the_parameter(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("   \n\t\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"checker_path file is empty"):
        validate_script_path(script, parameter="checker_path")


def test_unreadable_script_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError (e.g. a mode-000 file) is translated to ValueError, not leaked raw.

    So the tool's @_as_mcp_error(ValueError, ...) wrapper turns an unreadable
    script into an actionable client message instead of an opaque traceback.
    """
    script = tmp_path / "secret.py"
    script.write_text("print('x')", encoding="utf-8")

    def _boom(*_a: Any, **_k: Any) -> str:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(ValueError, match=r"checker_path is not readable"):
        validate_script_path(script, parameter="checker_path")


def test_parameter_defaults_to_script_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"script_path does not exist"):
        validate_script_path(tmp_path / "nope.py")
