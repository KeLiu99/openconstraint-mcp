from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas import (
    CheckerReport,
    CheckResult,
    CpsatCheckerReport,
    CpsatExpectation,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonJobStatus,
    CpsatPythonResult,
    CpsatStatus,
    PortfolioAttempt,
    PortfolioJobStatus,
    PortfolioSolveControls,
    PortfolioSolveResult,
    SavedModelArtifact,
    SaveVerifiedModelResult,
    SaveVerifiedPythonResult,
    SolutionCheck,
    SolveJobStatus,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolveStatus,
    UnsatCoreConstraint,
    UnsatCoreResult,
    cpsat_job_state_for_result,
    job_state_for_result,
)
from openconstraint_mcp.shared.save_target import text_sha256


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


def _passing_check() -> CheckResult:
    return CheckResult(status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=5)


def _satisfied_solve() -> SolveResult:
    return SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=1\n",
        stderr="",
        elapsed_ms=9,
        solution={"x": 1},
        solutions=[{"x": 1}],
        objective=None,
    )


def test_save_verified_model_result_round_trips_as_json() -> None:
    # A saved result serializes with model_dump(mode="json") — the same mode the
    # server uses for structuredContent — with bare-filename artifact paths.
    result = SaveVerifiedModelResult(
        status="saved",
        message="Verified model saved.",
        target_dir="/home/user/projects/knapsack",
        files=[
            SavedModelArtifact(role="model", path="model.mzn", sha256="ab" * 32),
            SavedModelArtifact(role="solve_result", path="solve-result.json", sha256="cd" * 32),
        ],
        check=_passing_check(),
        solve=_satisfied_solve(),
    )

    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "saved"
    assert dumped["target_dir"] == "/home/user/projects/knapsack"
    assert dumped["files"] == [
        {"role": "model", "path": "model.mzn", "sha256": "ab" * 32},
        {"role": "solve_result", "path": "solve-result.json", "sha256": "cd" * 32},
    ]
    assert dumped["check"]["status"] == "ok"
    assert dumped["solve"]["status"] == "satisfied"


def test_save_verified_model_result_files_default_to_empty_list() -> None:
    result = SaveVerifiedModelResult(
        status="not_verified",
        message="Solve did not verify.",
        target_dir="/home/user/projects/knapsack",
        check=_passing_check(),
        solve=_satisfied_solve(),
    )
    assert result.files == []


def test_save_verified_model_result_check_gate_serializes_with_null_solve() -> None:
    # The check-gate outcome: the compile failed, so no solve ran — `solve` is
    # None and the result still serializes cleanly.
    result = SaveVerifiedModelResult(
        status="not_verified",
        message="Model failed the compile check.",
        target_dir="/home/user/projects/knapsack",
        check=CheckResult(status="error", solver="cp-sat", stdout="", stderr="boom", elapsed_ms=3),
    )

    dumped = result.model_dump(mode="json")
    assert dumped["solve"] is None
    assert dumped["files"] == []
    assert dumped["check"]["status"] == "error"


def test_save_verified_model_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SaveVerifiedModelResult(
            status="written",  # type: ignore[arg-type]
            message="",
            target_dir="/tmp/x",
            check=_passing_check(),
        )


def test_saved_model_artifact_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        SavedModelArtifact(
            role="readme",  # type: ignore[arg-type]
            path="README.md",
            sha256="ef" * 32,
        )


def _job_solve_result(status: SolveStatus, *, timed_out: bool = False) -> SolveResult:
    return SolveResult(
        status=status,
        solver="cp-sat",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        stdout="",
        stderr="",
        elapsed_ms=5,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("satisfied", "succeeded"),
        ("optimal", "succeeded"),
        ("unsatisfiable", "succeeded"),
        ("unknown", "succeeded"),
        ("unbounded", "succeeded"),
        ("unsat_or_unbounded", "succeeded"),
        # The load-bearing case: a structured solver/driver `error` verdict is a
        # SUCCEEDED job (a result was produced), not a job-machinery failure (D1.9).
        ("error", "succeeded"),
        ("timeout", "timeout"),
    ],
)
def test_job_state_for_result_maps_every_solve_status(status: SolveStatus, expected: str) -> None:
    # The total D1.9 mapping over all eight SolveStatus values for the
    # result-present paths: only a `timeout` verdict is `timeout`; everything else
    # (including `error`, `unbounded`, `unsat_or_unbounded`) is `succeeded`.
    assert job_state_for_result(_job_solve_result(status)) == expected


def test_job_state_for_result_timed_out_flag_overrides_status_to_timeout() -> None:
    # SolveResult.timed_out is a separate bool from status; a hard subprocess
    # timeout maps to `timeout` even when the partial stream's status is `unknown`.
    assert job_state_for_result(_job_solve_result("unknown", timed_out=True)) == "timeout"


def test_solve_job_status_succeeded_round_trips_with_result() -> None:
    status = SolveJobStatus(
        job_id="abc123",
        state="succeeded",
        solver="cp-sat",
        timeout_ms=30000,
        submitted_at_ms=1000,
        started_at_ms=1001,
        finished_at_ms=1050,
        elapsed_ms=49,
        result=_job_solve_result("optimal"),
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "succeeded"
    assert dumped["result"]["status"] == "optimal"
    assert dumped["message"] is None


def test_solve_job_status_queued_serializes_without_result() -> None:
    status = SolveJobStatus(
        job_id="q1", state="queued", solver="cp-sat", timeout_ms=30000, submitted_at_ms=5
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "queued"
    assert dumped["result"] is None
    assert dumped["started_at_ms"] is None


def test_solve_job_status_cancelled_serializes_with_message_and_no_result() -> None:
    status = SolveJobStatus(
        job_id="c1",
        state="cancelled",
        solver="cp-sat",
        timeout_ms=30000,
        submitted_at_ms=5,
        started_at_ms=6,
        finished_at_ms=7,
        elapsed_ms=1,
        message="cancelled by client",
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "cancelled"
    assert dumped["result"] is None
    assert dumped["message"] == "cancelled by client"


def test_solve_job_status_failed_has_none_result() -> None:
    # A runner exception → failed with result is None (failed ⇒ result is None; the
    # converse fails — queued/running/cancelled are result-less too).
    status = SolveJobStatus(
        job_id="f1",
        state="failed",
        solver="cp-sat",
        timeout_ms=30000,
        submitted_at_ms=5,
        message="boom",
    )
    assert status.result is None


def test_solve_job_status_rejects_unknown_state() -> None:
    with pytest.raises(ValidationError):
        SolveJobStatus(
            job_id="x",
            state="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            timeout_ms=30000,
            submitted_at_ms=1,
        )


def test_solve_job_status_rejects_failed_carrying_a_result() -> None:
    # The enforced invariant: a non-result-bearing state must not carry a result.
    with pytest.raises(ValidationError):
        SolveJobStatus(
            job_id="f",
            state="failed",
            solver="cp-sat",
            timeout_ms=30000,
            submitted_at_ms=1,
            result=_job_solve_result("error"),
        )


def test_solve_job_status_rejects_succeeded_without_a_result() -> None:
    # The enforced invariant: a result-bearing state must carry a result.
    with pytest.raises(ValidationError):
        SolveJobStatus(
            job_id="s", state="succeeded", solver="cp-sat", timeout_ms=30000, submitted_at_ms=1
        )


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


# --- portfolio result models -----------------------------------------------


def _portfolio_winner_result() -> SolveResult:
    return SolveResult(
        status="optimal",
        solver="org.chuffed.chuffed",
        return_code=0,
        timed_out=False,
        stdout="x=2 total=22\n",
        stderr="",
        elapsed_ms=120,
        statistics={"failures": "0"},
        solution={"x": 2},
        solutions=[{"x": 2}],
        objective=22,
    )


_PORTFOLIO_CONTROLS = PortfolioSolveControls(
    free_search=False, parallel=None, all_solutions=False, num_solutions=None
)


def test_portfolio_attempt_round_trips() -> None:
    attempt = PortfolioAttempt(
        index=1,
        model_index=3,
        solver="org.gecode.gecode",
        seed=2,
        timeout_ms=5000,
        state="succeeded",
        job_id="job-2",
        job_state="succeeded",
        result_status="satisfied",
        objective=None,
        elapsed_ms=80,
        message=None,
    )
    assert attempt.model_dump(mode="json") == {
        "index": 1,
        "model_index": 3,
        "solver": "org.gecode.gecode",
        "seed": 2,
        "timeout_ms": 5000,
        "state": "succeeded",
        "job_id": "job-2",
        "job_state": "succeeded",
        "result_status": "satisfied",
        "objective": None,
        "elapsed_ms": 80,
        "message": None,
        "checker_status": None,
    }


def test_portfolio_solve_result_winner_with_cancelled_loser_round_trips() -> None:
    # The common case: a decisive winner plus a sibling cancelled when the winner
    # was selected. The winner attempt's result_status mirrors the winning result.
    winner = _portfolio_winner_result()
    result = PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=winner,
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="org.chuffed.chuffed",
                seed=None,
                timeout_ms=5000,
                state="succeeded",
                job_id="job-0",
                job_state="succeeded",
                result_status="optimal",
                objective=22,
                elapsed_ms=120,
            ),
            PortfolioAttempt(
                index=1,
                model_index=0,
                solver="org.gecode.gecode",
                seed=None,
                timeout_ms=5000,
                state="cancelled",
                job_id="job-1",
                job_state="cancelled",
                message="Cancelled by client",
            ),
        ],
        elapsed_ms=130,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "winner"
    assert dumped["winner_index"] == 0
    assert dumped["winner"]["status"] == "optimal"
    assert [a["state"] for a in dumped["attempts"]] == ["succeeded", "cancelled"]
    assert dumped["attempts"][1]["result_status"] is None
    assert dumped["selection_policy"] == "first-decisive-result"


def test_portfolio_solve_result_timeout_with_incumbent_is_a_winner() -> None:
    # A timeout attempt is result-bearing: when it is the best available, it is a
    # valid winner carrying its partial SolveResult.
    incumbent = SolveResult(
        status="timeout",
        solver="cp-sat",
        return_code=None,
        timed_out=True,
        stdout="x=1\n",
        stderr="",
        elapsed_ms=5000,
        solution={"x": 1},
        solutions=[{"x": 1}],
    )
    result = PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=incumbent,
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="cp-sat",
                seed=None,
                timeout_ms=5000,
                state="timeout",
                job_id="job-0",
                job_state="timeout",
                result_status="timeout",
                elapsed_ms=5000,
            )
        ],
        elapsed_ms=5010,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )
    assert result.winner is not None
    assert result.winner.status == "timeout"
    assert result.winner.solution == {"x": 1}


def test_portfolio_solve_result_all_failed_has_no_winner() -> None:
    result = PortfolioSolveResult(
        status="no_winner",
        winner_index=None,
        winner=None,
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="cp-sat",
                seed=None,
                timeout_ms=5000,
                state="failed",
                job_id="job-0",
                job_state="failed",
                message="MiniZincExecutionError: boom",
            )
        ],
        elapsed_ms=42,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "no_winner"
    assert dumped["winner_index"] is None
    assert dumped["winner"] is None


def test_portfolio_solve_result_rejects_winner_status_without_a_winner() -> None:
    with pytest.raises(ValidationError):
        PortfolioSolveResult(
            status="winner",
            winner_index=None,
            winner=None,
            attempts=[],
            elapsed_ms=1,
            selection_policy="first-decisive-result",
            models_sha256=[],
            data_sha256=None,
            checker_sha256=None,
            solve_controls=_PORTFOLIO_CONTROLS,
        )


def test_portfolio_solve_result_rejects_no_winner_carrying_a_winner() -> None:
    with pytest.raises(ValidationError):
        PortfolioSolveResult(
            status="no_winner",
            winner_index=0,
            winner=_portfolio_winner_result(),
            attempts=[],
            elapsed_ms=1,
            selection_policy="first-decisive-result",
            models_sha256=[],
            data_sha256=None,
            checker_sha256=None,
            solve_controls=_PORTFOLIO_CONTROLS,
        )


def test_portfolio_solve_result_rejects_attempt_model_index_out_of_range() -> None:
    # The winner's attempt is one of `self.attempts`, so validating every attempt
    # automatically covers the winner too. Here the sole attempt (and winner)
    # claims model_index=1, but models_sha256 has only one entry (valid index 0).
    with pytest.raises(ValidationError, match="model_index"):
        PortfolioSolveResult(
            status="winner",
            winner_index=0,
            winner=_portfolio_winner_result(),
            attempts=[
                PortfolioAttempt(
                    index=0,
                    model_index=1,
                    solver="org.chuffed.chuffed",
                    timeout_ms=5000,
                    state="succeeded",
                    job_id="job-0",
                    job_state="succeeded",
                    result_status="optimal",
                    objective=22,
                )
            ],
            elapsed_ms=130,
            selection_policy="first-decisive-result",
            models_sha256=["m0-hash"],
            data_sha256=None,
            checker_sha256=None,
            solve_controls=_PORTFOLIO_CONTROLS,
        )


def test_empty_string_data_hashes_distinctly_from_none() -> None:
    # PortfolioSolveResult.data_sha256/checker_sha256 are None iff the race ran
    # with no data/checker supplied. An empty-string input, if the solve path ever
    # accepts one, must still hash to sha256("") — a real digest, never collapsing
    # to the None sentinel used for "not supplied".
    empty_hash = text_sha256("")
    assert empty_hash == hashlib.sha256(b"").hexdigest()
    assert empty_hash is not None


def _portfolio_job_result() -> PortfolioSolveResult:
    return PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=_portfolio_winner_result(),
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="org.chuffed.chuffed",
                timeout_ms=5000,
                state="succeeded",
                job_id="job-0",
                job_state="succeeded",
                result_status="optimal",
                objective=22,
            )
        ],
        elapsed_ms=130,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )


def test_portfolio_job_status_succeeded_round_trips_with_result() -> None:
    status = PortfolioJobStatus(
        job_id="pj1",
        state="succeeded",
        per_attempt_timeout_ms=5000,
        submitted_at_ms=1000,
        started_at_ms=1001,
        finished_at_ms=1140,
        elapsed_ms=139,
        result=_portfolio_job_result(),
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "succeeded"
    assert dumped["result"]["winner"]["status"] == "optimal"
    assert dumped["message"] is None


def test_portfolio_job_status_running_serializes_without_result() -> None:
    status = PortfolioJobStatus(
        job_id="pj-run",
        state="running",
        per_attempt_timeout_ms=5000,
        submitted_at_ms=5,
        started_at_ms=6,
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "running"
    assert dumped["result"] is None


def test_portfolio_job_status_cancelled_has_message_and_no_result() -> None:
    status = PortfolioJobStatus(
        job_id="pj-c",
        state="cancelled",
        per_attempt_timeout_ms=5000,
        submitted_at_ms=5,
        started_at_ms=6,
        finished_at_ms=7,
        elapsed_ms=1,
        message="Cancelled by client",
    )
    assert status.result is None
    assert status.message == "Cancelled by client"


def test_portfolio_job_status_rejects_succeeded_without_a_result() -> None:
    # Enforced invariant: result is present exactly when state == "succeeded".
    with pytest.raises(ValidationError):
        PortfolioJobStatus(
            job_id="pj-bad",
            state="succeeded",
            per_attempt_timeout_ms=5000,
            submitted_at_ms=1,
        )


def test_portfolio_job_status_rejects_cancelled_carrying_a_result() -> None:
    # A non-result-bearing state must not carry a result (only `succeeded` does).
    with pytest.raises(ValidationError):
        PortfolioJobStatus(
            job_id="pj-bad2",
            state="cancelled",
            per_attempt_timeout_ms=5000,
            submitted_at_ms=1,
            result=_portfolio_job_result(),
        )


def test_portfolio_job_status_rejects_unknown_state() -> None:
    # `failed` is not a portfolio job state — winner-selection cannot itself fail.
    with pytest.raises(ValidationError):
        PortfolioJobStatus(
            job_id="pj-bad3",
            state="failed",  # type: ignore[arg-type]
            per_attempt_timeout_ms=5000,
            submitted_at_ms=1,
        )


# --- CpsatPythonJobStatus + cpsat_job_state_for_result ----------------------


def _cpsat_result(status: CpsatStatus, *, timed_out: bool = False) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,
        solution={"x": 1} if status not in ("error", "infeasible", "unknown", "timeout") else None,
        objective=None,
        stdout="",
        stderr="",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=False,
        duration_ms=10,
    )


@pytest.mark.parametrize(
    ("status", "timed_out", "expected"),
    [
        ("optimal", False, "succeeded"),
        ("feasible", False, "succeeded"),
        ("infeasible", False, "succeeded"),
        ("unknown", False, "succeeded"),
        # The load-bearing case: error → succeeded (a structured verdict, not a
        # job-machinery failure — D3 / schemas.py:144-159 analogue).
        ("error", False, "succeeded"),
        ("timeout", False, "timeout"),
        # timed_out flag overrides status → always timeout
        ("unknown", True, "timeout"),
    ],
)
def test_cpsat_job_state_for_result_maps_every_status(
    status: CpsatStatus, timed_out: bool, expected: str
) -> None:
    assert cpsat_job_state_for_result(_cpsat_result(status, timed_out=timed_out)) == expected


def test_cpsat_job_state_for_result_error_maps_to_succeeded_not_failed() -> None:
    # Explicit assertion for the D3 semantic: error → succeeded.
    result = _cpsat_result("error")
    assert cpsat_job_state_for_result(result) == "succeeded"


def test_cpsat_job_state_for_result_timeout_maps_to_timeout() -> None:
    result = _cpsat_result("timeout")
    assert cpsat_job_state_for_result(result) == "timeout"


def test_cpsat_python_job_status_succeeded_round_trips_with_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj1",
        state="succeeded",
        timeout_ms=30000,
        submitted_at_ms=1000,
        started_at_ms=1001,
        finished_at_ms=1050,
        elapsed_ms=49,
        result=_cpsat_result("optimal"),
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "succeeded"
    assert dumped["result"]["status"] == "optimal"
    assert dumped["message"] is None


def test_cpsat_python_job_status_queued_serializes_without_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj-q", state="queued", timeout_ms=30000, submitted_at_ms=5
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "queued"
    assert dumped["result"] is None


def test_cpsat_python_job_status_rejects_running_carrying_a_result() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonJobStatus(
            job_id="cj-bad",
            state="running",
            timeout_ms=30000,
            submitted_at_ms=1,
            result=_cpsat_result("optimal"),
        )


def test_cpsat_python_job_status_rejects_succeeded_without_a_result() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonJobStatus(
            job_id="cj-bad2",
            state="succeeded",
            timeout_ms=30000,
            submitted_at_ms=1,
        )


def test_cpsat_python_job_status_timeout_carries_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj-to",
        state="timeout",
        timeout_ms=5000,
        submitted_at_ms=1,
        result=_cpsat_result("timeout", timed_out=True),
    )
    assert status.result is not None
    assert status.result.timed_out is True


def test_cpsat_python_job_status_cancelled_has_no_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj-c",
        state="cancelled",
        timeout_ms=5000,
        submitted_at_ms=1,
        message="Cancelled by client",
    )
    assert status.result is None
    assert status.message == "Cancelled by client"


# --- CpsatExpectation schemas -----------------------------------------------


def test_cpsat_expectation_maximize_with_int_threshold() -> None:
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100)
    assert exp.objective_sense == "maximize"
    assert exp.objective_threshold == 100.0


def test_cpsat_expectation_minimize_with_float_threshold() -> None:
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=3.14)
    assert exp.objective_sense == "minimize"
    assert exp.objective_threshold == 3.14


def test_cpsat_expectation_accepts_zero_threshold() -> None:
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=0)
    assert exp.objective_threshold == 0.0


def test_cpsat_expectation_accepts_negative_threshold() -> None:
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=-50)
    assert exp.objective_threshold == -50.0


def test_cpsat_expectation_rejects_true() -> None:
    with pytest.raises(ValidationError, match="bool"):
        CpsatExpectation(objective_sense="maximize", objective_threshold=True)  # type: ignore[arg-type]


def test_cpsat_expectation_rejects_false() -> None:
    with pytest.raises(ValidationError, match="bool"):
        CpsatExpectation(objective_sense="minimize", objective_threshold=False)  # type: ignore[arg-type]


def test_cpsat_expectation_rejects_nan() -> None:
    import math

    with pytest.raises(ValidationError, match="finite"):
        CpsatExpectation(objective_sense="maximize", objective_threshold=math.nan)


def test_cpsat_expectation_rejects_positive_inf() -> None:
    import math

    with pytest.raises(ValidationError, match="finite"):
        CpsatExpectation(objective_sense="maximize", objective_threshold=math.inf)


def test_cpsat_expectation_rejects_negative_inf() -> None:
    import math

    with pytest.raises(ValidationError, match="finite"):
        CpsatExpectation(objective_sense="minimize", objective_threshold=-math.inf)


def test_cpsat_expectation_rejects_unknown_sense() -> None:
    with pytest.raises(ValidationError):
        CpsatExpectation(objective_sense="unknown", objective_threshold=10.0)  # type: ignore[arg-type]


def test_cpsat_expectation_rejects_missing_threshold() -> None:
    with pytest.raises(ValidationError):
        CpsatExpectation(objective_sense="maximize")  # type: ignore[call-arg]


def test_cpsat_expectation_rejects_null_threshold() -> None:
    with pytest.raises(ValidationError):
        CpsatExpectation(objective_sense="maximize", objective_threshold=None)  # type: ignore[arg-type]


# --- CpsatCheckerReport schemas ----------------------------------------------


def _make_checker_report(**overrides: object) -> CpsatCheckerReport:
    defaults: dict = {
        "status": "accepted",
        "errors": [],
        "details": None,
        "stdout": "",
        "stderr": "",
        "duration_ms": 42,
        "timed_out": False,
        "truncated": False,
    }
    defaults.update(overrides)
    return CpsatCheckerReport(**defaults)  # type: ignore[arg-type]


def test_cpsat_checker_report_accepted_round_trips() -> None:
    report = _make_checker_report(status="accepted", errors=[])
    dumped = report.model_dump()
    assert dumped["status"] == "accepted"
    assert dumped["errors"] == []
    assert dumped["timed_out"] is False
    assert dumped["truncated"] is False


def test_cpsat_checker_report_rejected_with_errors_round_trips() -> None:
    report = _make_checker_report(
        status="rejected",
        errors=["golfer 3 appears twice in week 1"],
        duration_ms=15,
    )
    dumped = report.model_dump()
    assert dumped["status"] == "rejected"
    assert dumped["errors"] == ["golfer 3 appears twice in week 1"]


def test_cpsat_checker_report_error_round_trips() -> None:
    report = _make_checker_report(status="error", errors=["malformed checker output"])
    assert report.status == "error"


def test_cpsat_checker_report_timeout_round_trips() -> None:
    report = _make_checker_report(status="timeout", errors=[], timed_out=True)
    assert report.status == "timeout"
    assert report.timed_out is True


def test_cpsat_checker_report_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _make_checker_report(status="passed")  # type: ignore[arg-type]


def test_cpsat_checker_report_with_details_round_trips() -> None:
    report = _make_checker_report(
        status="rejected",
        errors=["constraint violated"],
        details={"week": 1, "pair": [1, 2]},
    )
    assert report.details == {"week": 1, "pair": [1, 2]}


# --- SaveVerifiedPythonResult schemas ----------------------------------------


def _make_save_python_result(**overrides: object) -> SaveVerifiedPythonResult:
    defaults: dict = {
        "status": "optimal",
        "target_dir": None,
        "reason": "status=infeasible",
        "solution": None,
        "objective": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "truncated": False,
        "duration_ms": 10,
    }
    defaults.update(overrides)
    return SaveVerifiedPythonResult(**defaults)  # type: ignore[arg-type]


def test_save_verified_python_result_defaults_to_none_verification() -> None:
    result = _make_save_python_result()
    assert result.verification_level == "none"
    assert result.reported_passed is False
    assert result.expectation is None
    assert result.expectation_passed is None
    assert result.checker is None


def test_save_verified_python_result_saved_computed_from_reason_and_dir() -> None:
    saved = _make_save_python_result(
        target_dir="/tmp/x",
        reason=None,
        verification_level="reported",
        reported_passed=True,
    )
    not_saved = _make_save_python_result(target_dir=None, reason="status=infeasible")
    assert saved.saved is True
    assert not_saved.saved is False


def test_save_verified_python_result_with_expectation_echoed() -> None:
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100.0)
    result = _make_save_python_result(
        target_dir="/tmp/x",
        reason=None,
        verification_level="expectation",
        reported_passed=True,
        expectation=exp,
        expectation_passed=True,
    )
    assert result.expectation is not None
    assert result.expectation.objective_sense == "maximize"
    assert result.expectation_passed is True


# --- CpsatPythonExperimentResult ---------------------------------------------


def _experiment_winner_result() -> CpsatPythonResult:
    return CpsatPythonResult(
        status="optimal",
        solution={"x": 3},
        objective=3.0,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=5,
    )


def _experiment_attempt_row(**overrides: object) -> CpsatPythonExperimentAttemptResult:
    defaults: dict[str, object] = {
        "index": 0,
        "name": "attempt-0",
        "seed": None,
        "config_sha256": None,
        "source_sha256": "hash0",
        "timeout_ms": 5000,
        "status": "optimal",
        "objective": 3.0,
        "accepted": True,
        "checker_status": None,
        "message": None,
        "timed_out": False,
        "truncated": False,
        "duration_ms": 5,
    }
    defaults.update(overrides)
    return CpsatPythonExperimentAttemptResult(**defaults)  # type: ignore[arg-type]


def test_experiment_attempt_result_stderr_tail_defaults_to_none() -> None:
    row = _experiment_attempt_row()
    assert row.stderr_tail is None


def test_experiment_attempt_result_stderr_tail_round_trips() -> None:
    row = _experiment_attempt_row(status="error", accepted=False, stderr_tail="Traceback: boom")
    dumped = row.model_dump()
    assert dumped["stderr_tail"] == "Traceback: boom"


def test_cpsat_python_experiment_result_winner_round_trips() -> None:
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="attempt-0",
        winner=_experiment_winner_result(),
        attempts=[_experiment_attempt_row()],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "winner"
    assert dumped["winner_name"] == "attempt-0"
    assert dumped["winner_index"] == 0


def test_cpsat_python_experiment_result_warnings_defaults_to_empty_list() -> None:
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="attempt-0",
        winner=_experiment_winner_result(),
        attempts=[_experiment_attempt_row()],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
    )
    assert result.warnings == []


def test_cpsat_python_experiment_result_warnings_round_trips() -> None:
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="attempt-0",
        winner=_experiment_winner_result(),
        attempts=[_experiment_attempt_row()],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
        warnings=["some warning"],
    )
    dumped = result.model_dump(mode="json")
    assert dumped["warnings"] == ["some warning"]


def test_cpsat_python_experiment_result_no_winner_round_trips() -> None:
    result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[_experiment_attempt_row(accepted=False, status="infeasible", objective=None)],
        elapsed_ms=10,
        objective_sense="minimize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "no_winner"
    assert dumped["winner_index"] is None
    assert dumped["winner_name"] is None
    assert dumped["winner"] is None


def test_cpsat_python_experiment_result_rejects_winner_status_without_a_winner() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonExperimentResult(
            status="winner",
            winner_index=None,
            winner_name=None,
            winner=None,
            attempts=[],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=[],
            checker_sha256=None,
            problem_sha256=None,
        )


def test_cpsat_python_experiment_result_rejects_no_winner_carrying_a_winner() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonExperimentResult(
            status="no_winner",
            winner_index=0,
            winner_name="attempt-0",
            winner=_experiment_winner_result(),
            attempts=[_experiment_attempt_row()],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=["hash0"],
            checker_sha256=None,
            problem_sha256=None,
        )


def test_cpsat_python_experiment_result_rejects_out_of_range_winner_index() -> None:
    with pytest.raises(ValidationError, match="winner_index"):
        CpsatPythonExperimentResult(
            status="winner",
            winner_index=1,
            winner_name="attempt-0",
            winner=_experiment_winner_result(),
            attempts=[_experiment_attempt_row()],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=["hash0"],
            checker_sha256=None,
            problem_sha256=None,
        )


def test_cpsat_python_experiment_result_rejects_winner_name_mismatch() -> None:
    with pytest.raises(ValidationError, match="winner_name"):
        CpsatPythonExperimentResult(
            status="winner",
            winner_index=0,
            winner_name="not-the-right-name",
            winner=_experiment_winner_result(),
            attempts=[_experiment_attempt_row(name="attempt-0")],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=["hash0"],
            checker_sha256=None,
            problem_sha256=None,
        )
