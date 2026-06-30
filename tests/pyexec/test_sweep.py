"""Unit tests for pyexec/sweep.py — run_cpsat_python and run_checker mocked."""

from __future__ import annotations

import math
from typing import Any

import pytest

from openconstraint_mcp.pyexec import sweep
from openconstraint_mcp.pyexec.sweep import (
    MAX_SWEEP_SEEDS,
    MAX_SWEEP_WALL_CLOCK_MS,
    run_cpsat_python_sweep,
)
from openconstraint_mcp.schemas import CpsatCheckerReport, CpsatPythonResult


def _result(
    *,
    status: str = "optimal",
    solution: dict | None = None,
    objective: float | int | None = 1.0,
    timed_out: bool = False,
    truncated: bool = False,
) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,  # type: ignore[arg-type]
        solution=solution if solution is not None else {"x": 1},
        objective=objective,
        stdout="",
        stderr="",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=5,
    )


def _checker_report(status: str = "accepted") -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[],
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=False,
        truncated=False,
    )


def _patch_runner(
    monkeypatch: pytest.MonkeyPatch,
    results_by_seed: dict[int, CpsatPythonResult],
    calls: list[dict[str, Any]] | None = None,
) -> None:
    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        seed = int(env["OPENCONSTRAINT_MCP_CPSAT_SEED"])
        if calls is not None:
            calls.append({"seed": seed, "env": env, "tracker": tracker, "timeout_ms": timeout_ms})
        return results_by_seed[seed]

    monkeypatch.setattr("openconstraint_mcp.pyexec.sweep.run_cpsat_python", _fake)


def _patch_checker(
    monkeypatch: pytest.MonkeyPatch,
    status_by_seed: dict[int, str] | str = "accepted",
    calls: list[dict[str, Any]] | None = None,
) -> None:
    def _fake(
        *, checker: str, run_result: CpsatPythonResult, problem: Any, timeout_ms: int, tracker: Any
    ) -> CpsatCheckerReport:
        if calls is not None:
            calls.append(
                {
                    "checker": checker,
                    "run_result": run_result,
                    "problem": problem,
                    "timeout_ms": timeout_ms,
                    "tracker": tracker,
                }
            )
        if isinstance(status_by_seed, str):
            return _checker_report(status_by_seed)
        # Key the verdict by the result's solution value, since the seed is not on
        # the run_result; tests that need per-seed verdicts encode it in solution.
        marker = run_result.solution.get("seed") if run_result.solution else None
        return _checker_report(status_by_seed.get(marker, "accepted"))  # type: ignore[arg-type]

    monkeypatch.setattr("openconstraint_mcp.pyexec.sweep.run_checker", _fake)


# --- seed env propagation ----------------------------------------------------


def test_each_attempt_receives_its_seed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {1: _result(), 2: _result(), 3: _result()}, calls=calls)

    run_cpsat_python_sweep("src", seeds=[1, 2, 3], objective_sense="minimize")

    assert [c["env"]["OPENCONSTRAINT_MCP_CPSAT_SEED"] for c in calls] == ["1", "2", "3"]


# --- winner selection --------------------------------------------------------


def test_minimize_picks_smallest_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {1: _result(objective=10), 2: _result(objective=3), 3: _result(objective=7)},
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2, 3], objective_sense="minimize")

    assert result.status == "winner"
    assert result.winner_seed == 2
    assert result.winner.objective == 3  # type: ignore[union-attr]


def test_maximize_picks_largest_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {1: _result(objective=10), 2: _result(objective=3), 3: _result(objective=7)},
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2, 3], objective_sense="maximize")

    assert result.winner_seed == 1
    assert result.winner.objective == 10  # type: ignore[union-attr]


def test_winner_index_points_at_winning_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {5: _result(objective=10), 6: _result(objective=2), 7: _result(objective=8)},
    )

    result = run_cpsat_python_sweep("src", seeds=[5, 6, 7], objective_sense="minimize")

    assert result.winner_index == 1
    assert result.attempts[result.winner_index].seed == result.winner_seed == 6


def test_tie_break_prefers_stronger_status_then_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    # All share objective 5: optimal beats feasible beats timeout; seed order last.
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="timeout", objective=5, timed_out=True),
            2: _result(status="optimal", objective=5),
            3: _result(status="feasible", objective=5),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2, 3], objective_sense="minimize")

    assert result.winner_seed == 2  # the optimal one, despite later seed order


def test_tie_break_by_seed_order_when_status_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {1: _result(status="feasible", objective=5), 2: _result(status="feasible", objective=5)},
    )

    result = run_cpsat_python_sweep("src", seeds=[2, 1], objective_sense="minimize")

    # seed 2 comes first in the list (index 0), so it wins the tie.
    assert result.winner_seed == 2
    assert result.winner_index == 0


# --- partial failures --------------------------------------------------------


def test_failed_attempts_recorded_but_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="error", solution=None, objective=None),
            2: _result(status="optimal", solution={}, objective=4),  # empty solution
            3: _result(status="optimal", objective=9),
        },
    )

    result = run_cpsat_python_sweep(
        "src", seeds=[1, 2, 3], objective_sense="minimize", per_run_timeout_ms=1000
    )

    assert result.winner_seed == 3
    assert len(result.attempts) == 3
    rejected = {a.seed for a in result.attempts if not a.accepted}
    assert rejected == {1, 2}


def test_missing_objective_rejected_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {1: _result(status="optimal", solution={"x": 1}, objective=None)},
    )

    result = run_cpsat_python_sweep("src", seeds=[1], objective_sense="minimize")

    assert result.status == "no_winner"
    attempt = result.attempts[0]
    assert attempt.accepted is False
    assert attempt.message == "objective is missing or non-numeric"


def test_timeout_with_recovered_solution_can_win(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {1: _result(status="timeout", objective=2, timed_out=True)},
    )

    result = run_cpsat_python_sweep("src", seeds=[1], objective_sense="minimize")

    assert result.status == "winner"
    assert result.winner_seed == 1
    assert result.winner.status == "timeout"  # type: ignore[union-attr]


def test_all_rejected_yields_no_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {1: _result(status="infeasible", solution=None, objective=None)},
    )

    result = run_cpsat_python_sweep("src", seeds=[1], objective_sense="minimize")

    assert result.status == "no_winner"
    assert result.winner_index is None
    assert result.winner_seed is None
    assert result.winner is None


# --- distinct accepted objectives -------------------------------------------


def test_distinct_accepted_objectives_counts_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="feasible", objective=5),
            2: _result(status="feasible", objective=5),
            3: _result(status="feasible", objective=8),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2, 3], objective_sense="minimize")

    assert result.distinct_accepted_objectives == 2


def test_distinct_accepted_objectives_one_when_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {1: _result(status="feasible", objective=5), 2: _result(status="feasible", objective=5)},
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2], objective_sense="minimize")

    assert result.distinct_accepted_objectives == 1


def test_seed_variation_hint_not_set_for_equal_objective_different_solutions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="feasible", solution={"x": 1}, objective=5),
            2: _result(status="feasible", solution={"x": 2}, objective=5),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2], objective_sense="minimize")

    assert result.distinct_accepted_objectives == 1
    assert result.seed_variation_hint is None


def test_seed_variation_hint_set_for_identical_feasible_incumbents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="feasible", solution={"x": 1}, objective=5),
            2: _result(status="feasible", solution={"x": 1}, objective=5),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2], objective_sense="minimize")

    assert result.seed_variation_hint is not None
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" in result.seed_variation_hint


def test_seed_variation_hint_treats_matching_solution_nan_as_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="feasible", solution={"x": float("nan")}, objective=5),
            2: _result(status="feasible", solution={"x": float("nan")}, objective=5),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2], objective_sense="minimize")

    assert result.winner is not None
    assert result.winner.solution is not None
    assert math.isnan(result.winner.solution["x"])
    assert result.seed_variation_hint is not None


def test_seed_variation_hint_not_set_for_identical_timeout_partials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="timeout", solution={"x": 1}, objective=5, timed_out=True),
            2: _result(status="timeout", solution={"x": 1}, objective=5, timed_out=True),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2], objective_sense="minimize")

    assert result.status == "winner"
    assert result.seed_variation_hint is None


def test_seed_variation_hint_set_for_identical_optimal_incumbents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script that ignores the seed on a model with a unique optimum reports
    'optimal' every time, not 'feasible' — the hint must catch this common case too."""
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="optimal", solution={"x": 1}, objective=5),
            2: _result(status="optimal", solution={"x": 1}, objective=5),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2], objective_sense="minimize")

    assert result.seed_variation_hint is not None
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" in result.seed_variation_hint


def test_seed_variation_hint_set_for_identical_incumbents_mixed_optimal_and_feasible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout attempt alongside matching optimal/feasible attempts must not
    suppress the hint — only the completed (optimal/feasible) attempts are compared."""
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="optimal", solution={"x": 1}, objective=5),
            2: _result(status="feasible", solution={"x": 1}, objective=5),
            3: _result(status="timeout", solution={"x": 9}, objective=9, timed_out=True),
        },
    )

    result = run_cpsat_python_sweep("src", seeds=[1, 2, 3], objective_sense="minimize")

    assert result.seed_variation_hint is not None


# --- checker gate ------------------------------------------------------------


def test_checker_rejection_removes_best_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="optimal", solution={"seed": 1}, objective=2),
            2: _result(status="optimal", solution={"seed": 2}, objective=9),
        },
    )
    _patch_checker(monkeypatch, status_by_seed={1: "rejected", 2: "accepted"})

    result = run_cpsat_python_sweep(
        "src",
        seeds=[1, 2],
        objective_sense="minimize",
        checker="print('x')",
        per_run_timeout_ms=1000,
    )

    # seed 1 has the better objective but is checker-rejected, so seed 2 wins.
    assert result.winner_seed == 2
    rejected_attempt = next(a for a in result.attempts if a.seed == 1)
    assert rejected_attempt.accepted is False
    assert rejected_attempt.checker_status == "rejected"


def test_checker_runs_only_on_base_eligible_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            1: _result(status="error", solution=None, objective=None),  # not base-eligible
            2: _result(status="optimal", objective=3),
        },
    )
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    result = run_cpsat_python_sweep(
        "src",
        seeds=[1, 2],
        objective_sense="minimize",
        checker="print('x')",
        per_run_timeout_ms=1000,
    )

    # Only the base-eligible seed 2 reaches the checker.
    assert len(calls) == 1
    assert result.winner_seed == 2
    seed1 = next(a for a in result.attempts if a.seed == 1)
    assert seed1.checker_status is None


def test_problem_reaches_checker_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {1: _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_sweep(
        "src",
        seeds=[1],
        objective_sense="minimize",
        checker="print('x')",
        problem="my problem",
    )

    assert calls[0]["problem"] == "my problem"


def test_unsupplied_checker_timeout_resolves_to_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {1: _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_sweep(
        "src", seeds=[1], objective_sense="minimize", checker="print('x')", per_run_timeout_ms=1234
    )

    assert calls[0]["timeout_ms"] == 1234


def test_supplied_checker_timeout_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {1: _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_sweep(
        "src",
        seeds=[1],
        objective_sense="minimize",
        checker="print('x')",
        per_run_timeout_ms=1234,
        checker_timeout_ms=999,
    )

    assert calls[0]["timeout_ms"] == 999


# --- tracker forwarding ------------------------------------------------------


def test_tracker_forwarded_to_runner_and_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    run_calls: list[dict[str, Any]] = []
    checker_calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {1: _result(objective=3), 2: _result(objective=4)}, calls=run_calls)
    _patch_checker(monkeypatch, "accepted", calls=checker_calls)

    run_cpsat_python_sweep(
        "src",
        seeds=[1, 2],
        objective_sense="minimize",
        checker="print('x')",
        per_run_timeout_ms=1000,
        tracker=sentinel,  # type: ignore[arg-type]
    )

    assert all(c["tracker"] is sentinel for c in run_calls)
    assert all(c["tracker"] is sentinel for c in checker_calls)


# --- validation --------------------------------------------------------------


def test_empty_seeds_rejected() -> None:
    with pytest.raises(ValueError, match="seeds must not be empty"):
        run_cpsat_python_sweep("src", seeds=[], objective_sense="minimize")


def test_duplicate_seeds_rejected() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        run_cpsat_python_sweep("src", seeds=[1, 1], objective_sense="minimize")


def test_bool_seed_rejected() -> None:
    with pytest.raises(ValueError, match="non-bool integer"):
        run_cpsat_python_sweep("src", seeds=[True], objective_sense="minimize")


def test_negative_seed_is_accepted_and_passed_to_child(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {-1: _result()}, calls=calls)

    result = run_cpsat_python_sweep("src", seeds=[-1], objective_sense="minimize")

    assert result.winner_seed == -1
    assert calls[0]["env"]["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "-1"


def test_seed_above_int32_rejected() -> None:
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        run_cpsat_python_sweep("src", seeds=[2_147_483_648], objective_sense="minimize")


def test_seed_below_int32_rejected() -> None:
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        run_cpsat_python_sweep("src", seeds=[-2_147_483_649], objective_sense="minimize")


def test_max_sweep_seeds_fits_small_timeout_budget() -> None:
    sweep._validate_sweep_request(
        seeds=list(range(MAX_SWEEP_SEEDS)),
        objective_sense="minimize",
        per_run_timeout_ms=3000,
        checker=None,
        checker_timeout_ms=None,
    )


def test_max_sweep_seeds_with_default_timeout_rejected_by_budget() -> None:
    with pytest.raises(ValueError, match="MAX_SWEEP_WALL_CLOCK_MS"):
        run_cpsat_python_sweep(
            "src",
            seeds=list(range(MAX_SWEEP_SEEDS)),
            objective_sense="minimize",
        )


def test_too_many_seeds_rejected() -> None:
    seeds = list(range(MAX_SWEEP_SEEDS + 1))
    with pytest.raises(ValueError, match=f"MAX_SWEEP_SEEDS={MAX_SWEEP_SEEDS}"):
        run_cpsat_python_sweep("src", seeds=seeds, objective_sense="minimize")


def test_projected_budget_over_cap_rejected() -> None:
    # Few seeds, but a per-run timeout large enough to blow the wall-clock budget.
    with pytest.raises(ValueError, match="MAX_SWEEP_WALL_CLOCK_MS"):
        run_cpsat_python_sweep(
            "src",
            seeds=[1, 2, 3],
            objective_sense="minimize",
            per_run_timeout_ms=MAX_SWEEP_WALL_CLOCK_MS,
        )


def test_checker_fallback_pushes_otherwise_fitting_sweep_over_budget() -> None:
    # Without a checker this fits; the checker timeout fallback (= per_run) plus its
    # own per-child overhead roughly doubles per-seed cost and tips it over budget.
    overhead = sweep._child_timeout_overhead_ms()
    # Pick per_run so that no-checker projection fits but checked projection does not.
    seeds = [1, 2]
    per_run = (MAX_SWEEP_WALL_CLOCK_MS // len(seeds)) - overhead - 1
    # Sanity: the no-checker sweep is admissible.
    sweep._validate_sweep_request(
        seeds=seeds,
        objective_sense="minimize",
        per_run_timeout_ms=per_run,
        checker=None,
        checker_timeout_ms=None,
    )
    with pytest.raises(ValueError, match="MAX_SWEEP_WALL_CLOCK_MS"):
        sweep._validate_sweep_request(
            seeds=seeds,
            objective_sense="minimize",
            per_run_timeout_ms=per_run,
            checker="print('x')",
            checker_timeout_ms=None,
        )


def test_non_positive_per_run_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="per_run_timeout_ms must be positive"):
        run_cpsat_python_sweep("src", seeds=[1], objective_sense="minimize", per_run_timeout_ms=0)


def test_non_positive_checker_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms must be positive"):
        run_cpsat_python_sweep(
            "src",
            seeds=[1],
            objective_sense="minimize",
            checker="print('x')",
            checker_timeout_ms=0,
        )


def test_checker_timeout_without_checker_rejected() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms supplied without checker"):
        run_cpsat_python_sweep("src", seeds=[1], objective_sense="minimize", checker_timeout_ms=100)


def test_whitespace_only_checker_rejected() -> None:
    with pytest.raises(ValueError, match="checker must be non-empty"):
        run_cpsat_python_sweep("src", seeds=[1], objective_sense="minimize", checker="   ")


def test_invalid_objective_sense_rejected_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.sweep.run_cpsat_python", _fake)

    with pytest.raises(ValueError, match="objective_sense"):
        run_cpsat_python_sweep("src", seeds=[1], objective_sense="max")  # type: ignore[arg-type]

    assert not called
