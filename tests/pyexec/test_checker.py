"""Unit tests for pyexec/checker.py — runner mocked for speed."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openconstraint_mcp.pyexec.runner import ChildExecutionResult
from openconstraint_mcp.schemas import CpsatCheckerReport, CpsatPythonResult

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

_CHECKER_SOURCE = (
    "import sys, json; payload=json.load(open(sys.argv[1])); "
    "print(json.dumps({'status':'accepted','errors':[]}))"
)


def _make_child_result(
    *,
    stdout: str = '{"status":"accepted","errors":[]}',
    stderr: str = "",
    return_code: int = 0,
    timed_out: bool = False,
    truncated: bool = False,
    duration_ms: int = 10,
) -> ChildExecutionResult:
    """Build a fake ChildExecutionResult for mocking execute_child."""
    return ChildExecutionResult(
        stdout=stdout,
        stderr=stderr,
        return_code=None if timed_out else return_code,
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=duration_ms,
    )


def _patch_runner(monkeypatch: pytest.MonkeyPatch, child_result: ChildExecutionResult) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.checker.execute_child",
        lambda argv, cwd, *, timeout_ms, tracker, **kw: child_result,
    )


class _SpyTracker:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def register(self, proc: Any) -> None:
        self.events.append(("register", proc))

    def unregister(self, proc: Any) -> None:
        self.events.append(("unregister", proc))


# --- Happy path: accepted checker -------------------------------------------


def test_checker_accepted_returns_accepted_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from openconstraint_mcp.pyexec.checker import run_checker

    _patch_runner(monkeypatch, _make_child_result())
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )
    assert report.status == "accepted"
    assert report.errors == []
    assert report.timed_out is False
    assert report.truncated is False


# --- Rejected checker --------------------------------------------------------


def test_checker_rejected_returns_rejected_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from openconstraint_mcp.pyexec.checker import run_checker

    _patch_runner(
        monkeypatch,
        _make_child_result(stdout='{"status":"rejected","errors":["constraint violated"]}'),
    )
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )
    assert report.status == "rejected"
    assert report.errors == ["constraint violated"]


# --- Nonzero exit ------------------------------------------------------------


def test_checker_nonzero_exit_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from openconstraint_mcp.pyexec.checker import run_checker

    _patch_runner(monkeypatch, _make_child_result(return_code=1, stdout=""))
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )
    assert report.status == "error"
    assert any("non-zero" in e for e in report.errors)


# --- Timeout -----------------------------------------------------------------


def test_checker_timeout_returns_timeout_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from openconstraint_mcp.pyexec.checker import run_checker

    _patch_runner(monkeypatch, _make_child_result(timed_out=True, return_code=0))
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=100, tracker=None
    )
    assert report.status == "timeout"
    assert report.timed_out is True


# --- Truncation --------------------------------------------------------------


def test_checker_truncated_output_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from openconstraint_mcp.pyexec.checker import run_checker

    _patch_runner(monkeypatch, _make_child_result(truncated=True, return_code=0))
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )
    assert report.status == "error"
    assert report.truncated is True


# --- Malformed protocol adapter tests ----------------------------------------


def _run_with_stdout(monkeypatch: pytest.MonkeyPatch, stdout: str) -> CpsatCheckerReport:
    from openconstraint_mcp.pyexec.checker import run_checker

    _patch_runner(monkeypatch, _make_child_result(stdout=stdout))
    return run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )


def test_checker_non_json_stdout_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_with_stdout(monkeypatch, "this is not json")
    assert report.status == "error"


def test_checker_no_final_json_object_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_with_stdout(monkeypatch, "[1, 2, 3]")
    assert report.status == "error"


def test_checker_accepted_then_trailing_output_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Verdict JSON must be the final stdout line; trailing non-JSON content
    # must not let an "accepted" object slip past the save gate.
    report = _run_with_stdout(monkeypatch, '{"status":"accepted","errors":[]}\noops trailing line')
    assert report.status == "error"


def test_checker_accepted_with_trailing_blank_line_returns_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A trailing newline / blank line (from print) is benign, not trailing content.
    report = _run_with_stdout(monkeypatch, '{"status":"accepted","errors":[]}\n\n')
    assert report.status == "accepted"


def test_checker_unknown_status_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_with_stdout(monkeypatch, '{"status":"passed","errors":[]}')
    assert report.status == "error"
    assert any("passed" in e for e in report.errors)


def test_checker_errors_missing_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_with_stdout(monkeypatch, '{"status":"accepted"}')
    assert report.status == "error"


def test_checker_errors_not_a_list_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_with_stdout(monkeypatch, '{"status":"rejected","errors":"bad"}')
    assert report.status == "error"


def test_checker_errors_contains_non_string_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_with_stdout(monkeypatch, '{"status":"rejected","errors":[42]}')
    assert report.status == "error"


def test_checker_details_not_dict_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_with_stdout(monkeypatch, '{"status":"rejected","errors":["x"],"details":"bad"}')
    assert report.status == "error"


def test_checker_accepted_with_non_empty_errors_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _run_with_stdout(
        monkeypatch, '{"status":"accepted","errors":["should not have errors"]}'
    )
    assert report.status == "error"
    assert any("self-contradictory" in e for e in report.errors)


# --- Tracker register/unregister wiring --------------------------------------


def test_checker_registers_then_unregisters_subprocess() -> None:
    """Checker subprocess is registered with tracker then unregistered."""
    from openconstraint_mcp.pyexec.checker import run_checker

    tracker = _SpyTracker()
    proc_handle: list[Any] = []

    def _fake_execute_child(
        argv: list[str],
        cwd: Path,
        *,
        timeout_ms: int,
        tracker: Any,
        **kw: Any,
    ) -> ChildExecutionResult:
        # Simulate what execute_child does with the tracker
        fake_proc = object()
        proc_handle.append(fake_proc)
        if tracker is not None:
            tracker.register(fake_proc)
        try:
            pass
        finally:
            if tracker is not None:
                tracker.unregister(fake_proc)
        return _make_child_result()

    with patch("openconstraint_mcp.pyexec.checker.execute_child", side_effect=_fake_execute_child):
        run_checker(
            _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=tracker
        )

    assert [name for name, _ in tracker.events] == ["register", "unregister"]
    assert tracker.events[0][1] is tracker.events[1][1]  # same proc handle both times


# --- on_start passthrough -----------------------------------------------------


def test_checker_on_start_receives_checker_child_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_checker forwards on_start to execute_child, which calls it with the
    checker child's Popen handle (here simulated by the fake runner)."""
    from openconstraint_mcp.pyexec.checker import run_checker

    fake_proc = object()
    received: list[Any] = []

    def _fake_execute_child(
        argv: list[str],
        cwd: Path,
        *,
        timeout_ms: int,
        tracker: Any,
        on_start: Any = None,
        **kw: Any,
    ) -> ChildExecutionResult:
        if on_start is not None:
            on_start(fake_proc)
        return _make_child_result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.checker.execute_child", _fake_execute_child)
    report = run_checker(
        _CHECKER_SOURCE,
        _OPTIMAL_RESULT,
        problem=None,
        timeout_ms=5000,
        tracker=None,
        on_start=received.append,
    )
    assert report.status == "accepted"
    assert received == [fake_proc]


# --- checker.py is dependency-light: no minizinc/runtime imports -------------


def test_checker_module_has_no_minizinc_import() -> None:
    import openconstraint_mcp.pyexec.checker as checker_mod

    import_names = [
        name for name in dir(checker_mod) if "minizinc" in name.lower() or "runtime" in name.lower()
    ]
    assert not import_names, f"checker module has forbidden imports: {import_names}"
