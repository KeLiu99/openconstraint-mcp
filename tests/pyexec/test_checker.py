"""Unit tests for pyexec/checker.py — runner mocked for speed."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import openconstraint_mcp.pyexec.checker
from openconstraint_mcp.pyexec.checker import (
    checker_infrastructure_report,
    run_checker,
    run_checker_file,
)
from openconstraint_mcp.pyexec.env_vars import CPSAT_CONFIG_ENV_VAR, CPSAT_SEED_ENV_VAR
from openconstraint_mcp.schemas.cpsat import CpsatCheckerReport, CpsatPythonResult
from openconstraint_mcp.shared.childrun import ChildExecutionResult

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
    _patch_runner(monkeypatch, _make_child_result())
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )
    assert report.status == "accepted"
    assert report.errors == []
    assert report.timed_out is False
    assert report.truncated is False


# --- Payload construction ----------------------------------------------------


@pytest.mark.parametrize(
    ("solution", "expected_solution"),
    [({"x": 3}, {"x": 3}), (None, {}), ({}, {})],
    ids=["non_empty", "none", "empty"],
)
def test_checker_payload_carries_solution_objective_and_status(
    monkeypatch: pytest.MonkeyPatch,
    solution: dict | None,
    expected_solution: dict,
) -> None:
    """Pin which result fields cross into the checker protocol.

    ``objective`` and ``best_objective_bound`` are adjacent, same-typed fields on
    ``CpsatPythonResult``, so reading the bound here would be type-correct and
    silent; the fixture keeps them distinct. Comparing the whole capture list
    also pins that the checker child is invoked exactly once.
    """
    result = CpsatPythonResult(
        status="optimal",
        solution=solution,
        objective=3.0,
        best_objective_bound=99.0,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=42,
    )
    payloads: list[Any] = []

    def _capture(argv: list[str], **kwargs: Any) -> ChildExecutionResult:
        # Read while the TemporaryDirectory still exists; argv[-1] is payload.json.
        payloads.append(json.loads(Path(argv[-1]).read_text(encoding="utf-8")))
        return _make_child_result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.checker.execute_child", _capture)

    run_checker(_CHECKER_SOURCE, result, problem="ship 3 units", timeout_ms=5000, tracker=None)

    assert payloads == [
        {
            "problem": "ship 3 units",
            "solution": expected_solution,
            "objective": 3.0,
            "solver_status": "optimal",
        }
    ]


# --- Child environment overlay -----------------------------------------------


def test_checker_strips_seed_and_config_env_from_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # The checker takes its inputs from the payload JSON, never from env vars, so
    # both CP-SAT protocol vars must be deleted (value None) from the child's
    # environment rather than inherited from the server process.
    captured: dict[str, Any] = {}

    def _capture(argv: list[str], **kwargs: Any) -> ChildExecutionResult:
        captured.update(kwargs)
        return _make_child_result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.checker.execute_child", _capture)

    run_checker(_CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None)

    assert captured["env"] == {CPSAT_SEED_ENV_VAR: None, CPSAT_CONFIG_ENV_VAR: None}


# --- Rejected checker --------------------------------------------------------


def test_checker_rejected_returns_rejected_report(monkeypatch: pytest.MonkeyPatch) -> None:
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
    _patch_runner(monkeypatch, _make_child_result(return_code=1, stdout=""))
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )
    assert report.status == "error"
    assert any("non-zero" in e for e in report.errors)


# --- Timeout -----------------------------------------------------------------


def test_checker_timeout_returns_timeout_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, _make_child_result(timed_out=True, return_code=0))
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=100, tracker=None
    )
    assert report.status == "timeout"
    assert report.timed_out is True


# --- Truncation --------------------------------------------------------------


def test_checker_truncated_output_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, _make_child_result(truncated=True, return_code=0))
    report = run_checker(
        _CHECKER_SOURCE, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )
    assert report.status == "error"
    assert report.truncated is True


# --- Malformed protocol adapter tests ----------------------------------------


def _run_with_stdout(monkeypatch: pytest.MonkeyPatch, stdout: str) -> CpsatCheckerReport:
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


# --- run_checker_file: an on-disk checker, executed in place ------------------


def _write_checker(tmp_path: Path, body: str) -> Path:
    checker = tmp_path / "checker.py"
    checker.write_text(body, encoding="utf-8")
    return checker


def _run_file_with_stdout(
    monkeypatch: pytest.MonkeyPatch, checker_path: Path, stdout: str
) -> CpsatCheckerReport:
    _patch_runner(monkeypatch, _make_child_result(stdout=stdout))
    return run_checker_file(
        checker_path, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None
    )


def test_run_checker_file_accepted_returns_accepted_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _write_checker(tmp_path, _CHECKER_SOURCE)
    report = _run_file_with_stdout(monkeypatch, checker, '{"status":"accepted","errors":[]}')
    assert report.status == "accepted"


def test_run_checker_file_rejected_returns_rejected_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _write_checker(tmp_path, _CHECKER_SOURCE)
    report = _run_file_with_stdout(monkeypatch, checker, '{"status":"rejected","errors":["bad"]}')
    assert report.status == "rejected"


def test_run_checker_file_timeout_returns_timeout_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _write_checker(tmp_path, _CHECKER_SOURCE)
    _patch_runner(monkeypatch, _make_child_result(stdout="", timed_out=True))
    report = run_checker_file(checker, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None)
    assert report.status == "timeout"


def test_run_checker_file_nonzero_exit_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _write_checker(tmp_path, _CHECKER_SOURCE)
    _patch_runner(monkeypatch, _make_child_result(stdout="", return_code=3))
    report = run_checker_file(checker, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None)
    assert report.status == "error"


def test_run_checker_file_malformed_final_line_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _write_checker(tmp_path, _CHECKER_SOURCE)
    report = _run_file_with_stdout(monkeypatch, checker, "not json at all")
    assert report.status == "error"


def test_run_checker_file_invalid_path_names_checker_path(tmp_path: Path) -> None:
    with patch("openconstraint_mcp.pyexec.checker.execute_child") as fake_execute:
        with pytest.raises(ValueError, match=r"checker_path does not exist"):
            run_checker_file(
                tmp_path / "nope.py",
                _OPTIMAL_RESULT,
                problem=None,
                timeout_ms=5000,
                tracker=None,
            )
    fake_execute.assert_not_called()


def test_run_checker_file_executes_in_the_checker_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cwd, not sys.path: a sibling *import* would resolve regardless, because
    # Python puts the script's own directory on sys.path[0]. Only a relative
    # data-file read proves the working directory moved.
    checker_dir = tmp_path / "verify"
    checker_dir.mkdir()
    checker = _write_checker(checker_dir, _CHECKER_SOURCE)
    captured: dict[str, Any] = {}

    def _fake_execute_child(argv: list[str], cwd: Path, **kw: Any) -> ChildExecutionResult:
        captured["argv"] = argv
        captured["cwd"] = cwd
        return _make_child_result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.checker.execute_child", _fake_execute_child)
    run_checker_file(checker, _OPTIMAL_RESULT, problem=None, timeout_ms=5000, tracker=None)

    assert captured["cwd"] == checker_dir.resolve()
    assert captured["argv"][2] == str(checker.resolve())
    assert Path(captured["argv"][2]).is_absolute()
    assert Path(captured["argv"][3]).is_absolute()


@pytest.mark.integration
def test_run_checker_file_reads_a_sibling_data_file(tmp_path: Path) -> None:
    """A real checker child resolves a RELATIVE sibling data read via cwd."""
    checker_dir = tmp_path / "verify"
    checker_dir.mkdir()
    (checker_dir / "reference.json").write_text(json.dumps({"expected": 3}), encoding="utf-8")
    checker = _write_checker(
        checker_dir,
        "import sys, json\n"
        "from pathlib import Path\n"
        "payload = json.loads(Path(sys.argv[1]).read_text())\n"
        'reference = json.loads(Path("reference.json").read_text())\n'
        'ok = payload["solution"]["x"] == reference["expected"]\n'
        'print(json.dumps({"status": "accepted" if ok else "rejected", '
        '"errors": [] if ok else ["mismatch"]}))\n',
    )

    report = run_checker_file(
        checker, _OPTIMAL_RESULT, problem=None, timeout_ms=20_000, tracker=None
    )

    assert report.status == "accepted"


# --- the shared infrastructure-failure report --------------------------------


def test_checker_infrastructure_report_summarizes_the_exception() -> None:
    report = checker_infrastructure_report(OSError("no space left"))

    assert report.status == "error"
    assert report.errors == ["checker infrastructure error: OSError: no space left"]


def test_checker_infrastructure_report_is_diagnosed() -> None:
    """Both call sites rely on the helper attaching the nested diagnostic."""
    report = checker_infrastructure_report(RuntimeError("spawn failed"))

    assert report.diagnostic is not None
    assert report.diagnostic.category == "checker_failed"


# --- checker.py is dependency-light: no core/minizinc/runtime imports --------


def _imported_modules(module_path: Path) -> set[str]:
    """Every module name ``module_path`` imports, absolute and relative alike.

    Parses the source rather than inspecting ``dir(module)``: ``from .core
    import X`` binds a symbol named ``X``, so a name grep over the module
    namespace cannot see which module a symbol came from. Relative levels are
    reduced to the bare module name (``.core`` and ``..minizinc.core`` both
    contribute ``core``/``minizinc``), which is what the layering rule is
    stated over.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(part for alias in node.names for part in alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
    return names - {""}


def test_checker_module_imports_no_core_minizinc_or_runtime() -> None:
    """Acceptance criterion 7 — checker.py must stay free of those dependencies.

    ``core`` matters specifically: ``core.py`` now imports ``checker.py``, so a
    back-edge would be a genuine import cycle.
    """
    source = Path(openconstraint_mcp.pyexec.checker.__file__)

    forbidden = _imported_modules(source) & {"core", "minizinc", "runtime"}

    assert not forbidden, f"checker module has forbidden imports: {sorted(forbidden)}"
