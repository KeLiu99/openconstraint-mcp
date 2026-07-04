"""Unit tests for pyexec/save.py — executor mocked for speed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.core import VERIFIED_STATUSES
from openconstraint_mcp.save_target import MANIFEST_FILENAME, text_sha256
from openconstraint_mcp.schemas import (
    CpsatCheckerReport,
    CpsatExpectation,
    CpsatPythonResult,
    CpsatPythonSweepAttempt,
    CpsatPythonSweepResult,
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


def _sweep_attempt(
    *,
    index: int = 0,
    seed: int = 7,
    status: str = "optimal",
    objective: float | int | None = 3,
    accepted: bool = True,
) -> CpsatPythonSweepAttempt:
    return CpsatPythonSweepAttempt(
        index=index,
        seed=seed,
        status=status,
        objective=objective,
        accepted=accepted,
        checker_status=None,
        message=None,
        timed_out=False,
        truncated=False,
        duration_ms=5,
    )


def _sweep_result(
    *,
    source: str = _SCRIPT,
    seed: int = 7,
    winner_status: str = "optimal",
    checker_sha256: str | None = None,
    problem_sha256: str | None = None,
    source_sha256: str | None = None,
    extra_attempts: list[CpsatPythonSweepAttempt] | None = None,
) -> CpsatPythonSweepResult:
    """Build a minimal, self-consistent winning ``CpsatPythonSweepResult``."""
    winner = CpsatPythonResult(
        status=winner_status,
        solution={"x": 3},
        objective=3.0,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=winner_status == "timeout",
        truncated=False,
        duration_ms=5,
    )
    return CpsatPythonSweepResult(
        status="winner",
        winner_index=0,
        winner_seed=seed,
        winner=winner,
        attempts=[_sweep_attempt(seed=seed, status=winner_status), *(extra_attempts or [])],
        elapsed_ms=10,
        objective_sense="minimize",
        selection_policy="best_objective_then_status_then_seed",
        distinct_accepted_objectives=1,
        source_sha256=source_sha256 if source_sha256 is not None else text_sha256(source),
        per_run_timeout_ms=5000,
        checker_sha256=checker_sha256,
        problem_sha256=problem_sha256,
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

    # A timeout sweep winner is unproven: replaying its seed does not make it savable.
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


# --- sweep_result --------------------------------------------------------


def test_save_with_sweep_result_writes_experiment_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python
    from openconstraint_mcp.save_target import EXPERIMENT_LOG_FILENAME

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "swept"
    sweep = _sweep_result(seed=7)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7, sweep_result=sweep)

    assert result.saved is True
    log_path = target / EXPERIMENT_LOG_FILENAME
    assert log_path.is_file()

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    artifact_roles = {a["role"]: a["path"] for a in manifest["artifacts"]}
    assert artifact_roles["experiment_log"] == EXPERIMENT_LOG_FILENAME

    summary = manifest["verification"]["experiment_log"]
    assert summary["exploration_type"] == "cpsat_python_sweep"
    assert summary["winner_index"] == 0
    assert summary["winner_seed"] == 7
    assert summary["attempt_count"] == 1
    assert summary["accepted_attempt_count"] == 1
    assert summary["statuses_seen"] == ["optimal"]
    assert summary["selection_policy"] == "best_objective_then_status_then_seed"


def test_cpsat_sweep_manifest_statuses_seen_are_solver_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "swept_status_summary"
    sweep = _sweep_result(seed=7, extra_attempts=[_sweep_attempt(seed=8, status="timeout")])

    save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7, sweep_result=sweep)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    summary = manifest["verification"]["experiment_log"]
    assert summary["statuses_seen"] == ["optimal", "timeout"]


def test_save_with_sweep_result_log_content_matches_source_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python
    from openconstraint_mcp.save_target import EXPERIMENT_LOG_FILENAME

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "swept"
    sweep = _sweep_result(seed=7, checker_sha256="c" * 64, problem_sha256="p" * 64)

    save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7, sweep_result=sweep)

    log = json.loads((target / EXPERIMENT_LOG_FILENAME).read_text())
    assert log["managed_by"] == "openconstraint-mcp"
    assert log["exploration_type"] == "cpsat_python_sweep"
    assert log["source_sha256"] == text_sha256(_SCRIPT)
    assert log["checker_sha256"] == "c" * 64
    assert log["problem_sha256"] == "p" * 64
    assert log["winner_index"] == 0
    assert log["winner_seed"] == 7
    assert log["objective_sense"] == "minimize"
    assert log["selection_policy"] == "best_objective_then_status_then_seed"
    assert len(log["attempts"]) == 1
    attempt = log["attempts"][0]
    assert attempt == {
        "index": 0,
        "seed": 7,
        "status": "optimal",
        "objective": 3,
        "accepted": True,
        "checker_status": None,
        "message": None,
        "timed_out": False,
        "truncated": False,
        "duration_ms": 5,
    }


def test_save_failure_with_sweep_result_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh re-run that fails the reported gate writes nothing, sweep_result or not."""
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    target = tmp_path / "swept_fail"
    sweep = _sweep_result(seed=7)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7, sweep_result=sweep)

    assert result.saved is False
    assert not target.exists()


def test_save_sweep_result_no_winner_raises_before_executor_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    calls = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    sweep = CpsatPythonSweepResult(
        status="no_winner",
        attempts=[_sweep_attempt(accepted=False)],
        elapsed_ms=10,
        objective_sense="minimize",
        selection_policy="best_objective_then_status_then_seed",
        distinct_accepted_objectives=0,
        source_sha256=text_sha256(_SCRIPT),
        per_run_timeout_ms=5000,
    )

    with pytest.raises(ValueError, match="no_winner"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=7, sweep_result=sweep)

    assert not calls, "executor must not be called before sweep_result validation"


def test_save_sweep_result_with_seed_none_raises_before_executor_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    calls = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    sweep = _sweep_result(seed=7)

    with pytest.raises(ValueError, match="seed"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", sweep_result=sweep)

    assert not calls, "executor must not be called before sweep_result validation"


def test_save_sweep_result_seed_mismatch_raises_before_executor_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    calls = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    sweep = _sweep_result(seed=7)

    with pytest.raises(ValueError, match="winner_seed"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=8, sweep_result=sweep)

    assert not calls, "executor must not be called before sweep_result validation"


def test_save_sweep_result_source_hash_mismatch_raises_before_executor_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    calls = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    sweep = _sweep_result(seed=7, source_sha256="deadbeef" * 8)

    with pytest.raises(ValueError, match="source_sha256"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=7, sweep_result=sweep)

    assert not calls, "executor must not be called before sweep_result validation"


def test_save_sweep_result_timeout_winner_does_not_bypass_reported_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout sweep winner is eagerly consistent (correct seed, correct source hash)
    but the fresh re-run reports timeout too, so the save must still fail — proving
    the gate decision never consults sweep_result.winner.status."""
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _TIMEOUT_RESULT)
    target = tmp_path / "swept_timeout"
    sweep = _sweep_result(seed=7, winner_status="timeout")

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7, sweep_result=sweep)

    assert result.saved is False
    assert result.verification_level == "none"
    assert not target.exists()


def test_save_sweep_result_optimal_winner_does_not_bypass_fresh_timeout_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sweep_result.winner.status is 'optimal' (and eagerly self-consistent: matching
    seed and source hash), but the fresh re-run this call triggers reports timeout.
    If the gate ever read sweep_result.winner.status instead of the fresh run_result,
    this save would incorrectly succeed — so this is the case that actually proves
    the gate reads the fresh result, unlike the timeout/timeout case above where
    both sources agree and either implementation would pass."""
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _TIMEOUT_RESULT)
    target = tmp_path / "swept_optimal_winner_fresh_timeout"
    sweep = _sweep_result(seed=7, winner_status="optimal")

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7, sweep_result=sweep)

    assert result.saved is False
    assert result.verification_level == "none"
    assert not target.exists()


def test_save_sweep_result_checker_and_problem_hash_mismatch_is_not_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checker_sha256/problem_sha256 mismatches are informational-only: the fresh
    checker gate — not sweep_result — decides, so a save can still succeed."""
    from openconstraint_mcp.pyexec.save import save_verified_cpsat_python

    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())
    target = tmp_path / "swept_checked"
    sweep = _sweep_result(
        seed=7,
        checker_sha256="mismatched-checker-hash".ljust(64, "0"),
        problem_sha256="mismatched-problem-hash".ljust(64, "0"),
    )

    result = save_verified_cpsat_python(
        _SCRIPT,
        target_dir=target,
        seed=7,
        sweep_result=sweep,
        checker=_CHECKER_SOURCE,
        problem="a different problem than the sweep saw",
    )

    assert result.saved is True
    assert result.verification_level == "checked"
