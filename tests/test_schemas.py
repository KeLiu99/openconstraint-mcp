from __future__ import annotations

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas import (
    CheckerReport,
    CheckResult,
    SolutionCheck,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    UnsatCoreConstraint,
    UnsatCoreResult,
)


def test_solve_result_round_trips() -> None:
    # A multi-solution optimization result: `solutions` holds the improving
    # sequence in order, `solution` is its last (best) element, `objective` is
    # the best `_objective`, and `statistics` are bare stringified stream values
    # (no raw-token quotes, unlike the old %%%mzn-stat: scrape).
    result = SolveResult(
        status="optimal",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=0 y=1 total=2\nx=2 y=10 total=22\n",
        stderr="",
        elapsed_ms=42,
        statistics={"failures": "0", "method": "maximize"},
        solution={"x": 2, "y": 10},
        solutions=[{"x": 0, "y": 1}, {"x": 2, "y": 10}],
        objective=22,
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "optimal",
        "solver": "cp-sat",
        "return_code": 0,
        "timed_out": False,
        "stdout": "x=0 y=1 total=2\nx=2 y=10 total=22\n",
        "stderr": "",
        "elapsed_ms": 42,
        "statistics": {"failures": "0", "method": "maximize"},
        "solution": {"x": 2, "y": 10},
        "solutions": [{"x": 0, "y": 1}, {"x": 2, "y": 10}],
        "objective": 22,
        # An ordinary solve carries no checker; the additive field renders as null,
        # consistent with the other always-emitted nullable fields.
        "checker": None,
    }


def test_solve_result_round_trips_satisfaction_has_null_objective() -> None:
    # A satisfaction result carries a solution but no objective: `_objective` is
    # absent from a satisfy model's json section, so `objective` stays None while
    # `solution`/`solutions` still round-trip.
    result = SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=1 y=2\n",
        stderr="",
        elapsed_ms=10,
        solution={"x": 1, "y": 2},
        solutions=[{"x": 1, "y": 2}],
        objective=None,
    )
    dumped = result.model_dump()
    assert dumped["objective"] is None
    assert dumped["solution"] == {"x": 1, "y": 2}
    assert dumped["solutions"] == [{"x": 1, "y": 2}]
    assert dumped["status"] == "satisfied"


def test_solve_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SolveResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            return_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_check_result_round_trips() -> None:
    result = CheckResult(
        status="ok",
        solver="cp-sat",
        stdout="",
        stderr="",
        elapsed_ms=12,
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "ok",
        "solver": "cp-sat",
        "stdout": "",
        "stderr": "",
        "elapsed_ms": 12,
    }


def test_check_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        CheckResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_unsat_core_result_round_trips() -> None:
    result = UnsatCoreResult(
        status="mus_found",
        core=[
            UnsatCoreConstraint(
                line=4,
                column=12,
                end_line=4,
                end_column=20,
                source="x + y > 5",
            )
        ],
        message="findMUS reported a minimal unsatisfiable subset.",
        stdout="MUS: 1 2\n",
        stderr="",
        elapsed_ms=7,
    )

    assert result.model_dump() == {
        "status": "mus_found",
        "core": [
            {
                "line": 4,
                "column": 12,
                "end_line": 4,
                "end_column": 20,
                "source": "x + y > 5",
            }
        ],
        "message": "findMUS reported a minimal unsatisfiable subset.",
        "stdout": "MUS: 1 2\n",
        "stderr": "",
        "elapsed_ms": 7,
    }


def test_unsat_core_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        UnsatCoreResult(
            status="bogus",  # type: ignore[arg-type]
            message="bad status",
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_solve_result_with_checker_round_trips() -> None:
    # A violation solve nests a CheckerReport on the SolveResult: `solutions` still
    # INCLUDES the checker-rejected solution (fact 5), `checker.checks` is
    # index-aligned with it, and `checker.transcript` preserves the raw
    # `--solution-checker` transcript verbatim — the authoritative checker record,
    # while `stdout` is the solution-only text.
    result = SolveResult(
        status="satisfied",
        solver="org.gecode.gecode",
        return_code=0,
        timed_out=False,
        stdout="x=1 y=2\n",
        stderr="",
        elapsed_ms=15,
        solution={"x": 1, "y": 2},
        solutions=[{"x": 1, "y": 2}],
        objective=None,
        checker=CheckerReport(
            status="violation",
            checks=[SolutionCheck(violation=True, output="model inconsistency detected")],
            transcript='{"type":"checker"}\n{"type":"solution"}\n',
        ),
    )

    dumped = result.model_dump()
    assert dumped["checker"] == {
        "status": "violation",
        "checks": [{"violation": True, "output": "model inconsistency detected"}],
        "transcript": '{"type":"checker"}\n{"type":"solution"}\n',
    }
    # The rejected solution stays in `solutions` (a violation does not suppress it).
    assert dumped["solutions"] == [{"x": 1, "y": 2}]
    assert dumped["status"] == "satisfied"


def test_solution_check_round_trips() -> None:
    # The per-solution check: `violation` is the one server-asserted verdict;
    # `output` carries the author CORRECT/INCORRECT text verbatim, unadjudicated.
    check = SolutionCheck(violation=False, output="CORRECT\n")
    assert check.model_dump() == {"violation": False, "output": "CORRECT\n"}


def test_checker_report_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        CheckerReport(
            status="checked",  # type: ignore[arg-type]
            checks=[],
            transcript="",
        )


def test_solver_capabilities_round_trips() -> None:
    caps = SolverCapabilities(
        supports_all_solutions=True,
        supports_free_search=True,
        supports_parallel=True,
        supports_random_seed=True,
        supports_num_solutions=True,
        std_flags=["-a", "-f", "-n", "-p", "-r"],
    )
    assert caps.model_dump() == {
        "supports_all_solutions": True,
        "supports_free_search": True,
        "supports_parallel": True,
        "supports_random_seed": True,
        "supports_num_solutions": True,
        "std_flags": ["-a", "-f", "-n", "-p", "-r"],
    }


def test_solver_info_round_trips_with_capabilities() -> None:
    info = SolverInfo(
        id="org.gecode.gecode",
        name="Gecode",
        version="6.3.0",
        tags=["cp", "int"],
        capabilities=SolverCapabilities(
            supports_all_solutions=True,
            supports_free_search=True,
            supports_parallel=True,
            supports_random_seed=True,
            supports_num_solutions=True,
            std_flags=["-a", "-f", "-n", "-p", "-r"],
        ),
    )
    assert info.model_dump() == {
        "id": "org.gecode.gecode",
        "name": "Gecode",
        "version": "6.3.0",
        "tags": ["cp", "int"],
        "capabilities": {
            "supports_all_solutions": True,
            "supports_free_search": True,
            "supports_parallel": True,
            "supports_random_seed": True,
            "supports_num_solutions": True,
            "std_flags": ["-a", "-f", "-n", "-p", "-r"],
        },
    }


def test_solver_info_capabilities_default_is_conservative() -> None:
    # A bare SolverInfo defaults capabilities to all-False booleans and an empty
    # std_flags — the conservative default that keeps Pydantic construction
    # compatible and the missing-config case default-deny.
    info = SolverInfo(id="com.example.unknown", name="Unknown")
    assert info.capabilities.model_dump() == {
        "supports_all_solutions": False,
        "supports_free_search": False,
        "supports_parallel": False,
        "supports_random_seed": False,
        "supports_num_solutions": False,
        "std_flags": [],
    }
