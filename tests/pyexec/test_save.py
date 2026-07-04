"""Unit tests for pyexec/save.py — executor mocked for speed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.core import VERIFIED_STATUSES
from openconstraint_mcp.save_target import MANIFEST_FILENAME
from openconstraint_mcp.schemas import (
    CpsatCheckerReport,
    CpsatExpectation,
    CpsatPythonResult,
)

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


def _patch_executor_counting(
    monkeypatch: pytest.MonkeyPatch, result: CpsatPythonResult
) -> list[bool]:
    """Like ``_patch_executor``, but returns a list that grows on every call."""
    calls: list[bool] = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: calls.append(True) or result,
    )
    return calls


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


# --- Expectation gate tests -------------------------------------------------


def test_save_no_expectation_saves_with_reported_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is True
    assert result.verification_level == "reported"
    assert result.reported_passed is True
    assert result.expectation_passed is None
    assert result.checker is None


def test_save_maximize_expectation_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=2.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is True
    assert result.verification_level == "expectation"
    assert result.expectation_passed is True
    assert result.expectation is not None
    assert result.expectation.objective_sense == "maximize"


def test_save_maximize_expectation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.reported_passed is True
    assert result.expectation_passed is False
    assert result.checker is None
    assert not target.exists()


def test_save_minimize_expectation_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=100.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is True
    assert result.verification_level == "expectation"
    assert result.expectation_passed is True


def test_save_minimize_expectation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=1.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.expectation_passed is False
    assert not target.exists()


def test_save_expectation_fails_when_objective_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    no_obj = CpsatPythonResult(
        status="optimal",
        solution={"x": 1},
        objective=None,
        stdout='{"status":"optimal","objective":null,"solution":{"x":1}}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=5,
    )
    _patch_executor(monkeypatch, no_obj)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=10.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.expectation_passed is False
    assert result.reason is not None
    assert "objective" in result.reason


def test_save_reported_gate_fails_with_expectation_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=10.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "none"
    assert result.reported_passed is False
    assert result.expectation_passed is None
    assert result.checker is None
    assert not target.exists()


def test_save_reported_gate_fails_with_expectation_and_checker_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=10.0)

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, expectation=exp, checker="import sys; sys.exit(0)"
    )

    assert result.saved is False
    assert result.verification_level == "none"
    assert result.reported_passed is False
    assert result.expectation_passed is None
    assert result.checker is None
    assert not target.exists()


def test_save_checker_timeout_ms_without_checker_raises() -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    with pytest.raises(ValueError, match="checker_timeout_ms"):
        save_verified_cpsat_python(
            _SCRIPT,
            target_dir=Path("/tmp/x"),
            checker_timeout_ms=5000,
        )


def test_save_checker_timeout_ms_zero_raises() -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    with pytest.raises(ValueError, match="positive"):
        save_verified_cpsat_python(
            _SCRIPT,
            target_dir=Path("/tmp/x"),
            checker="print('ok')",
            checker_timeout_ms=0,
        )


def test_save_empty_checker_raises() -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    with pytest.raises(ValueError, match="non-empty"):
        save_verified_cpsat_python(_SCRIPT, target_dir=Path("/tmp/x"), checker="")


def test_save_whitespace_only_checker_raises() -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    with pytest.raises(ValueError, match="non-empty"):
        save_verified_cpsat_python(_SCRIPT, target_dir=Path("/tmp/x"), checker="  \n")


def test_save_expectation_gate_fails_with_checker_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100.0)

    result = save_verified_cpsat_python(
        _SCRIPT,
        target_dir=target,
        expectation=exp,
        checker="import sys; sys.exit(0)",
    )

    # Expectation failed, so checker never ran — checker must be None, not an error report.
    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.expectation_passed is False
    assert result.checker is None
    assert not target.exists()


# --- Checker gate tests -----------------------------------------------------

_CHECKER_SOURCE = "# checker"


def _patch_checker(
    monkeypatch: pytest.MonkeyPatch,
    report: CpsatCheckerReport,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_checker",
        lambda checker, run_result, *, problem, timeout_ms, tracker: report,
    )


def _accepted_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="accepted",
        errors=[],
        stdout="",
        stderr="",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


def _rejected_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="rejected",
        errors=["golfer 3 appears twice"],
        stdout="",
        stderr="",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


def _error_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="error",
        errors=["checker crashed"],
        stdout="",
        stderr="error output",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


def _timeout_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="timeout",
        errors=["checker timed out"],
        stdout="",
        stderr="",
        duration_ms=100,
        timed_out=True,
        truncated=False,
    )


def test_save_accepted_checker_saves_with_checked_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import (
        CHECKER_FILENAME,
        SOLUTION_FILENAME,
        save_verified_cpsat_python,
    )
    from openconstraint_mcp.save_target import MANIFEST_FILENAME

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())
    target = tmp_path / "s"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    assert result.saved is True
    assert result.verification_level == "checked"
    assert result.checker is not None
    assert result.checker.status == "accepted"
    assert (target / CHECKER_FILENAME).is_file()
    assert (target / CHECKER_FILENAME).read_text() == _CHECKER_SOURCE
    assert (target / MANIFEST_FILENAME).is_file()
    assert (target / SOLUTION_FILENAME).is_file()
    assert json.loads((target / SOLUTION_FILENAME).read_text()) == _OPTIMAL_RESULT.solution


def test_save_accepted_checker_manifest_has_scalar_summary_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted checker: manifest carries only scalar summary, never free-text fields."""
    from openconstraint_mcp.schemas import CpsatCheckerReport

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    sensitive_report = CpsatCheckerReport(
        status="accepted",
        errors=[],
        stdout="/tmp/secret_path/output.txt: done",
        stderr="/tmp/secret_path/err.txt: ok",
        details={"path": "/tmp/secret_path/details.json"},
        duration_ms=42,
        timed_out=False,
        truncated=False,
    )
    _patch_checker(monkeypatch, sensitive_report)
    target = tmp_path / "s"

    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python
    from openconstraint_mcp.save_target import MANIFEST_FILENAME

    save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    manifest_text = (target / MANIFEST_FILENAME).read_text()
    manifest = json.loads(manifest_text)
    checker_summary = manifest["verification"]["checker"]

    assert set(checker_summary.keys()) == {
        "status",
        "error_count",
        "duration_ms",
        "timed_out",
        "truncated",
    }
    assert checker_summary["status"] == "accepted"
    assert checker_summary["error_count"] == 0
    assert checker_summary["duration_ms"] == 42
    assert checker_summary["timed_out"] is False
    assert checker_summary["truncated"] is False
    # No free-text leakage
    assert "secret_path" not in manifest_text
    assert "stdout" not in str(checker_summary)
    assert "stderr" not in str(checker_summary)
    assert "details" not in str(checker_summary)
    assert "errors" not in str(checker_summary)


@pytest.mark.parametrize(
    ("report_fn", "expected_checker_status"),
    [
        ("_rejected_report", "rejected"),
        ("_error_report", "error"),
        ("_timeout_report", "timeout"),
    ],
)
def test_save_non_accepted_checker_does_not_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_fn: str,
    expected_checker_status: str,
) -> None:
    import tests.pyexec.test_save as _mod

    report = getattr(_mod, report_fn)()
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, report)
    target = tmp_path / "s"

    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.checker is not None
    assert result.checker.status == expected_checker_status
    assert not target.exists()


def test_save_non_accepted_checker_with_expectation_reports_expectation_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _rejected_report())
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=2.0)

    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, expectation=exp, checker=_CHECKER_SOURCE
    )

    assert result.saved is False
    assert result.verification_level == "expectation"
    assert result.checker is not None
    assert result.checker.status == "rejected"
    assert not target.exists()


def test_save_checker_not_run_when_reported_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When reported gate fails, checker is skipped (checker=None, not an error report)."""
    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    checker_called = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_checker",
        lambda *a, **kw: checker_called.append(True) or _accepted_report(),
    )
    target = tmp_path / "s"

    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    assert result.saved is False
    assert result.verification_level == "none"
    assert result.checker is None
    assert not checker_called


def test_save_checker_not_run_when_expectation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When expectation gate fails, checker is skipped (checker=None)."""
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    checker_called = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_checker",
        lambda *a, **kw: checker_called.append(True) or _accepted_report(),
    )
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=1000.0)

    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, expectation=exp, checker=_CHECKER_SOURCE
    )

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.checker is None
    assert not checker_called


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


# --- seed replay -------------------------------------------------------------


_TIMEOUT_RESULT = CpsatPythonResult(
    status="timeout",
    solution={"x": 3},
    objective=3.0,
    stdout="",
    stderr="",
    return_code=None,
    timed_out=True,
    truncated=False,
    duration_ms=99,
)


def test_save_with_seed_reruns_with_seed_env_and_records_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "seeded"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7)

    assert result.saved is True
    assert captured["env"] == {"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"}

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    verification = manifest["verification"]
    assert verification["replay_seed"] == 7
    # The reproducibility note tells a manual re-runner to set the env var.
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" in verification["reproducibility_note"]


def test_save_without_seed_passes_no_env_and_omits_seed_from_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "unseeded"

    save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert captured["env"] is None
    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert "replay_seed" not in manifest["verification"]


def test_save_with_seed_timeout_winner_still_fails_reported_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _TIMEOUT_RESULT)
    target = tmp_path / "timeout_seeded"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7)

    # A timeout replay is unproven: replaying its seed does not make it savable.
    assert result.saved is False
    assert result.verification_level == "none"
    assert not target.exists()


def test_save_with_bool_seed_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    with pytest.raises(ValueError, match="seed must be a non-bool integer"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=True)


def test_save_with_negative_seed_reruns_with_seed_env_and_records_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "negative_seeded"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=-1)

    assert result.saved is True
    assert captured["env"] == {"OPENCONSTRAINT_MCP_CPSAT_SEED": "-1"}

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["verification"]["replay_seed"] == -1


def test_save_with_seed_below_int32_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=-2_147_483_649)


def test_save_with_seed_above_int32_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=2_147_483_648)
