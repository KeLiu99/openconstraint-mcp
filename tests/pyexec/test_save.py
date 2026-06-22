"""Unit tests for pyexec/save.py — executor mocked for speed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.core import VERIFIED_STATUSES, CpsatPythonResult
from openconstraint_mcp.save_target import MANIFEST_FILENAME

_SCRIPT = "print('hi')"
_OPTIMAL_RESULT = CpsatPythonResult(
    status="optimal",
    solution={"x": 3},
    objective=3.0,
    stdout='{"status":"optimal","objective":3,"solution":{"x":3}}',
    stderr="",
    return_code=0,
    timed_out=False,
    truncated=False,
    duration_ms=42,
)
_INFEASIBLE_RESULT = CpsatPythonResult(
    status="infeasible",
    solution=None,
    objective=None,
    stdout='{"status":"infeasible","objective":null,"solution":{}}',
    stderr="",
    return_code=0,
    timed_out=False,
    truncated=False,
    duration_ms=10,
)
_ERROR_RESULT = CpsatPythonResult(
    status="error",
    solution=None,
    objective=None,
    stdout="",
    stderr="NameError: name 'x' is not defined",
    return_code=1,
    timed_out=False,
    truncated=False,
    duration_ms=5,
)


def _patch_executor(monkeypatch: pytest.MonkeyPatch, result: CpsatPythonResult) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: result,
    )


# (a) solving script → saved=True, file on disk, manifest written
def test_save_verified_cpsat_python_optimal_saves_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is True
    assert result.status in VERIFIED_STATUSES
    assert (target / "solution.py").is_file()
    assert (target / MANIFEST_FILENAME).is_file()
    assert (target / "solution.py").read_text() == _SCRIPT


# (a2) manifest has correct structure
def test_save_verified_cpsat_python_manifest_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"
    save_verified_cpsat_python(_SCRIPT, target_dir=target)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["managed_by"] == "openconstraint-mcp"
    assert isinstance(manifest["artifacts"], list)
    artifact_names = [a["path"] for a in manifest["artifacts"]]
    assert "solution.py" in artifact_names


# (a3) problem.txt written when problem supplied
def test_save_verified_cpsat_python_writes_problem_txt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, problem="Assign workers to tasks."
    )

    assert result.saved is True
    assert (target / "problem.txt").is_file()
    assert (target / "problem.txt").read_text() == "Assign workers to tasks."


# (a4) optimal status but no solution dict → saved=False, reason set, nothing written
def test_save_verified_cpsat_python_optimal_no_solution_does_not_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(
        monkeypatch,
        CpsatPythonResult(
            status="optimal",
            solution=None,
            objective=None,
            stdout='{"status":"optimal","objective":null,"solution":null}',
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=7,
        ),
    )
    target = tmp_path / "no_solution"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is False
    assert result.reason is not None
    assert result.target_dir is None
    assert not target.exists()


# (a5) verified status but empty solution dict → saved=False, reason set, nothing written
def test_save_verified_cpsat_python_empty_solution_does_not_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(
        monkeypatch,
        CpsatPythonResult(
            status="optimal",
            solution={},
            objective=None,
            stdout='{"status":"optimal","objective":null,"solution":{}}',
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=7,
        ),
    )
    target = tmp_path / "empty_solution"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is False
    assert result.reason is not None
    assert result.target_dir is None
    assert not target.exists()


# (b) infeasible → saved=False, reason set, nothing written
def test_save_verified_cpsat_python_infeasible_does_not_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    target = tmp_path / "infeas"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is False
    assert result.reason is not None
    assert not target.exists()


# (b2) error result → saved=False, reason set, nothing written
def test_save_verified_cpsat_python_error_does_not_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _ERROR_RESULT)
    target = tmp_path / "err"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is False
    assert result.reason is not None
    assert not target.exists()


# (c) relative target_dir → ValueError before executor runs
def test_save_verified_cpsat_python_relative_path_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    called = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: called.append(True) or _OPTIMAL_RESULT,
    )

    with pytest.raises(ValueError, match="absolute"):
        save_verified_cpsat_python(_SCRIPT, target_dir=Path("relative/path"))

    assert not called, "executor must not be called before path validation"


# (d) non-empty unmanaged dir → ValueError
def test_save_verified_cpsat_python_unmanaged_nonempty_dir_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "some_other_file.txt").write_text("not ours")

    with pytest.raises(ValueError, match="not empty"):
        save_verified_cpsat_python(_SCRIPT, target_dir=target)


# (e) existing managed save without overwrite → refusal
def test_save_verified_cpsat_python_existing_managed_no_overwrite_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "managed"

    # First save
    save_verified_cpsat_python(_SCRIPT, target_dir=target)
    assert target.is_dir()

    # Second save without overwrite
    with pytest.raises(ValueError, match="overwrite"):
        save_verified_cpsat_python(_SCRIPT, target_dir=target, overwrite=False)


# (f) overwrite=True replaces managed directory
def test_save_verified_cpsat_python_overwrite_replaces_managed_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "managed"

    save_verified_cpsat_python(_SCRIPT, target_dir=target)
    new_script = "# updated script"
    save_verified_cpsat_python(new_script, target_dir=target, overwrite=True)

    assert (target / "solution.py").read_text() == new_script
    assert (target / MANIFEST_FILENAME).is_file()


@pytest.mark.integration
def test_save_verified_cpsat_python_integration(tmp_path: Path) -> None:
    """Run a real script end-to-end and verify it saves."""
    from pathlib import Path as _Path

    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    examples = _Path(__file__).parent.parent.parent / "examples" / "cpsat_python"
    source = (examples / "assignment.py").read_text()
    target = tmp_path / "assignment_save"

    result = save_verified_cpsat_python(source, target_dir=target)

    assert result.saved is True
    assert (target / "solution.py").is_file()
    assert (target / MANIFEST_FILENAME).is_file()
