"""Unit tests for pyexec/experiment.py — run_cpsat_python and run_checker mocked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from openconstraint_mcp.pyexec import experiment
from openconstraint_mcp.pyexec.experiment import (
    MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS,
    run_cpsat_python_experiment,
)
from openconstraint_mcp.schemas import (
    CpsatCheckerReport,
    CpsatPythonExperimentAttempt,
    CpsatPythonResult,
)


def _result(
    *,
    status: str = "optimal",
    solution: dict | None = None,
    objective: float | int | None = 1.0,
    timed_out: bool = False,
    truncated: bool = False,
    stderr: str = "",
) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,  # type: ignore[arg-type]
        solution=solution if solution is not None else {"x": 1},
        objective=objective,
        stdout="",
        stderr=stderr,
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=5,
    )


def _result_without_solution() -> CpsatPythonResult:
    return _result(objective=None).model_copy(update={"solution": None})


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
    results_by_source: dict[str, CpsatPythonResult],
    calls: list[dict[str, Any]] | None = None,
) -> None:
    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        if calls is not None:
            calls.append(
                {"source": source, "env": env, "tracker": tracker, "timeout_ms": timeout_ms}
            )
        return results_by_source[source]

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)


def _patch_checker(
    monkeypatch: pytest.MonkeyPatch,
    status_by_source: dict[str, str] | str = "accepted",
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
        if isinstance(status_by_source, str):
            return _checker_report(status_by_source)
        marker = run_result.solution.get("marker") if run_result.solution else None
        return _checker_report(status_by_source.get(marker, "accepted"))  # type: ignore[arg-type]

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_checker", _fake)


def _attempt(source: str, **kwargs: Any) -> CpsatPythonExperimentAttempt:
    return CpsatPythonExperimentAttempt(source=source, **kwargs)


# --- winner selection --------------------------------------------------------


def test_minimize_picks_smallest_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {"a": _result(objective=10), "b": _result(objective=3), "c": _result(objective=7)},
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")], objective_sense="minimize"
    )

    assert result.status == "winner"
    assert result.winner_index == 1
    assert result.winner_name == "attempt-1"
    assert result.winner.objective == 3  # type: ignore[union-attr]


def test_maximize_picks_largest_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {"a": _result(objective=10), "b": _result(objective=3), "c": _result(objective=7)},
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")], objective_sense="maximize"
    )

    assert result.winner_index == 0
    assert result.winner.objective == 10  # type: ignore[union-attr]


def test_feasibility_accepts_solution_without_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=None)})

    result = run_cpsat_python_experiment([_attempt("a")])

    assert result.status == "winner"
    assert result.objective_sense is None
    assert result.winner.objective is None  # type: ignore[union-attr]
    assert result.attempts[0].accepted is True


@pytest.mark.parametrize(
    "run_result",
    [_result_without_solution(), _result(solution={}, objective=None)],
    ids=["missing", "empty"],
)
def test_feasibility_rejects_missing_or_empty_solution(
    monkeypatch: pytest.MonkeyPatch, run_result: CpsatPythonResult
) -> None:
    _patch_runner(monkeypatch, {"a": run_result})

    result = run_cpsat_python_experiment([_attempt("a")])

    assert result.status == "no_winner"
    assert result.attempts[0].accepted is False
    assert result.attempts[0].message == "solution is missing or empty"


def test_feasibility_tie_break_prefers_status_then_attempt_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="feasible", objective=None),
            "b": _result(status="optimal", objective=None),
            "c": _result(status="optimal", objective=None),
        },
    )

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b"), _attempt("c")])

    assert result.winner_index == 1
    assert result.selection_policy == "accepted_status_then_attempt_order"


def test_tie_break_prefers_stronger_status_then_attempt_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="timeout", objective=5, timed_out=True),
            "b": _result(status="optimal", objective=5),
            "c": _result(status="feasible", objective=5),
        },
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")], objective_sense="minimize"
    )

    assert result.winner_index == 1  # the optimal one, despite later attempt order


def test_tie_break_by_attempt_order_when_status_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="feasible", objective=5),
            "b": _result(status="feasible", objective=5),
        },
    )

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert result.winner_index == 0


def test_winner_name_uses_explicit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})

    result = run_cpsat_python_experiment(
        [_attempt("a", name="baseline")], objective_sense="minimize"
    )

    assert result.winner_name == "baseline"
    assert result.attempts[0].name == "baseline"


def test_unnamed_attempt_defaults_to_attempt_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3), "b": _result(objective=9)})

    result = run_cpsat_python_experiment(
        [_attempt("a", name="baseline"), _attempt("b")], objective_sense="minimize"
    )

    assert result.attempts[1].name == "attempt-1"


# --- name uniqueness ----------------------------------------------------------


def test_duplicate_explicit_names_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate attempt name"):
        run_cpsat_python_experiment(
            [_attempt("a", name="x"), _attempt("b", name="x")], objective_sense="minimize"
        )


def test_explicit_name_colliding_with_defaulted_label_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate attempt name"):
        run_cpsat_python_experiment(
            [_attempt("a", name="attempt-1"), _attempt("b")], objective_sense="minimize"
        )


# --- partial failures ----------------------------------------------------------


def test_failed_attempts_recorded_but_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="error", solution=None, objective=None),
            "b": _result(status="optimal", solution={}, objective=4),  # empty solution
            "c": _result(status="optimal", objective=9),
        },
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")],
        objective_sense="minimize",
        default_timeout_ms=1000,
    )

    assert result.winner_index == 2
    assert len(result.attempts) == 3
    rejected = {a.name for a in result.attempts if not a.accepted}
    assert rejected == {"attempt-0", "attempt-1"}


def test_errored_attempt_message_includes_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    traceback = 'Traceback (most recent call last):\n  File "script.py", line 1\nValueError: boom'
    _patch_runner(
        monkeypatch,
        {"a": _result(status="error", solution=None, objective=None, stderr=traceback)},
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].message is not None
    assert result.attempts[0].message.startswith("status='error':")
    assert "ValueError: boom" in result.attempts[0].message


def test_errored_attempt_with_empty_stderr_keeps_bare_status_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch, {"a": _result(status="error", solution=None, objective=None, stderr="")}
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].message == "status='error'"


def test_optimization_rejects_missing_objective_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(monkeypatch, {"a": _result(status="optimal", solution={"x": 1}, objective=None)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "no_winner"
    assert result.attempts[0].accepted is False
    assert result.attempts[0].message == "objective is missing or non-numeric"


def test_timeout_with_recovered_solution_can_win(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(status="timeout", objective=2, timed_out=True)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "winner"
    assert result.winner.status == "timeout"  # type: ignore[union-attr]


def test_all_rejected_yields_no_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(status="infeasible", solution=None, objective=None)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "no_winner"
    assert result.winner_index is None
    assert result.winner_name is None
    assert result.winner is None


# --- seed env propagation ------------------------------------------------------


def test_seed_forwarded_as_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment([_attempt("a", seed=7)], objective_sense="minimize")

    assert calls[0]["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "7",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


def test_no_seed_no_config_explicitly_clears_both_protocol_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    # Both keys are present but explicitly None, not omitted — this is what tells
    # execute_child to clear any stale value the server process inherited, rather
    # than passing env=None (full, unfiltered inheritance).
    assert calls[0]["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


def test_bool_seed_rejected_at_attempt_construction() -> None:
    # CpsatPythonExperimentAttempt.seed is StrictInt, so a bool is rejected by
    # pydantic before it ever reaches the orchestrator's own range check.
    with pytest.raises(ValidationError, match="int_type"):
        _attempt("a", seed=True)


def test_seed_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        run_cpsat_python_experiment([_attempt("a", seed=2_147_483_648)], objective_sense="minimize")


# --- config env + temp-file injection -----------------------------------------


def test_config_written_to_temp_file_and_env_points_at_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a", config={"num_workers": 2})], objective_sense="minimize"
    )

    env = calls[0]["env"]
    config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])
    # The file existed while run_cpsat_python was invoked; by the time this
    # assertion runs (after the call returns) the TemporaryDirectory is already
    # cleaned up, proving the config file does not outlive its attempt.
    assert not config_path.exists()
    # Key is present but explicitly None (clears any stale inherited value),
    # not omitted from the overlay.
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] is None


def test_config_file_contents_are_the_supplied_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])
        captured["contents"] = json.loads(config_path.read_text())
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment(
        [_attempt("a", config={"restart_strategy": "luby"})], objective_sense="minimize"
    )

    assert captured["contents"] == {"restart_strategy": "luby"}


def test_config_temp_file_cleaned_up_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])
        captured["existed_during_call"] = config_path.exists()
        captured["path"] = config_path
        return _result(status="timeout", timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment([_attempt("a", config={"x": 1})], objective_sense="minimize")

    assert captured["existed_during_call"] is True
    assert not captured["path"].exists()


def test_empty_config_dict_normalizes_to_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    result = run_cpsat_python_experiment([_attempt("a", config={})], objective_sense="minimize")

    assert calls[0]["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }
    assert result.attempts[0].config_sha256 is None


def test_seed_and_config_combine_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a", seed=3, config={"k": "v"})], objective_sense="minimize"
    )

    env = calls[0]["env"]
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "3"
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" in env


# --- canonical config hashing --------------------------------------------------


def test_config_hash_independent_of_key_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(), "b": _result()})

    result = run_cpsat_python_experiment(
        [
            _attempt("a", config={"a": 1, "b": 2}),
            _attempt("b", config={"b": 2, "a": 1}),
        ],
        objective_sense="minimize",
    )

    hash_a = result.attempts[0].config_sha256
    hash_b = result.attempts[1].config_sha256
    assert hash_a is not None
    assert hash_a == hash_b


def test_different_config_values_hash_differently(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(), "b": _result()})

    result = run_cpsat_python_experiment(
        [_attempt("a", config={"k": 1}), _attempt("b", config={"k": 2})],
        objective_sense="minimize",
    )

    assert result.attempts[0].config_sha256 != result.attempts[1].config_sha256


def test_oversized_config_rejected() -> None:
    huge_config = {"blob": "x" * (experiment.MAX_EXPERIMENT_CONFIG_BYTES + 1)}
    with pytest.raises(ValueError, match="MAX_EXPERIMENT_CONFIG_BYTES"):
        run_cpsat_python_experiment([_attempt("a", config=huge_config)], objective_sense="minimize")


# --- checker gate ---------------------------------------------------------------


def test_checker_rejection_removes_best_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="optimal", solution={"marker": "a"}, objective=2),
            "b": _result(status="optimal", solution={"marker": "b"}, objective=9),
        },
    )
    _patch_checker(monkeypatch, status_by_source={"a": "rejected", "b": "accepted"})

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b")],
        objective_sense="minimize",
        checker="print('x')",
        default_timeout_ms=1000,
    )

    assert result.winner_index == 1
    rejected_attempt = result.attempts[0]
    assert rejected_attempt.accepted is False
    assert rejected_attempt.checker_status == "rejected"


def test_checker_runs_only_on_base_eligible_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="error", solution=None, objective=None),
            "b": _result(status="optimal", objective=3),
        },
    )
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b")],
        objective_sense="minimize",
        checker="print('x')",
        default_timeout_ms=1000,
    )

    assert len(calls) == 1
    assert result.winner_index == 1
    assert result.attempts[0].checker_status is None


def test_problem_reaches_checker_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        problem="my problem",
    )

    assert calls[0]["problem"] == "my problem"


def test_unsupplied_checker_timeout_resolves_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        default_timeout_ms=1234,
    )

    assert calls[0]["timeout_ms"] == 1234


def test_unsupplied_checker_timeout_uses_attempt_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {"a": _result(objective=3), "b": _result(objective=4)},
    )
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b", timeout_ms=321)],
        objective_sense="minimize",
        checker="print('x')",
        default_timeout_ms=1234,
    )

    assert [call["timeout_ms"] for call in calls] == [1234, 321]


def test_supplied_checker_timeout_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        default_timeout_ms=1234,
        checker_timeout_ms=999,
    )

    assert calls[0]["timeout_ms"] == 999


# --- per-attempt timeout override -----------------------------------------------


def test_per_attempt_timeout_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    result = run_cpsat_python_experiment(
        [_attempt("a", timeout_ms=1500)], objective_sense="minimize", default_timeout_ms=5000
    )

    assert calls[0]["timeout_ms"] == 1500
    assert result.attempts[0].timeout_ms == 1500


# --- parallel scheduling ---------------------------------------------------------


def test_results_are_ordered_by_attempt_order_not_completion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        order.append(source)
        # "slow" finishes after "fast" despite running first, to prove that
        # result ordering follows attempt (input) order, not completion order.
        if source == "slow":
            return _result(objective=1)
        return _result(objective=2)

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    result = run_cpsat_python_experiment(
        [_attempt("slow"), _attempt("fast")],
        objective_sense="minimize",
        max_parallel_attempts=2,
    )

    assert [a.name for a in result.attempts] == ["attempt-0", "attempt-1"]
    assert result.attempts[0].objective == 1
    assert result.attempts[1].objective == 2


def test_max_parallel_attempts_runs_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    concurrent_count = 0
    max_seen = 0
    lock = threading.Lock()

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal concurrent_count, max_seen
        with lock:
            concurrent_count += 1
            max_seen = max(max_seen, concurrent_count)
        time.sleep(0.05)
        with lock:
            concurrent_count -= 1
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")],
        objective_sense="minimize",
        max_parallel_attempts=3,
    )

    assert max_seen >= 2


def test_max_parallel_attempts_defaults_to_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    concurrent_count = 0
    max_seen = 0
    lock = threading.Lock()

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal concurrent_count, max_seen
        with lock:
            concurrent_count += 1
            max_seen = max(max_seen, concurrent_count)
        time.sleep(0.02)
        with lock:
            concurrent_count -= 1
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert max_seen == 1


def test_max_parallel_attempts_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    with pytest.raises(ValueError, match="exceeds the server cap"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", max_parallel_attempts=5
        )


def test_max_parallel_attempts_non_positive_rejected() -> None:
    with pytest.raises(ValueError, match="max_parallel_attempts must be >= 1"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", max_parallel_attempts=0
        )


def test_max_parallel_attempts_bool_rejected() -> None:
    with pytest.raises(ValueError, match="non-bool positive integer"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", max_parallel_attempts=True
        )


# --- admission wall-clock budget ------------------------------------------------


def test_projected_budget_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS") as exc_info:
        run_cpsat_python_experiment(
            [_attempt("a", timeout_ms=MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS)],
            objective_sense="minimize",
        )

    message = str(exc_info.value)
    for field in (
        "attempt_count=1",
        "max_parallel_attempts=",
        "batches=",
        "per_attempt_timeout_ms=",
        "checker_timeout_ms=",
        "attempt_budget_ms=",
        "checker_budget_ms=",
        "overhead_ms=",
        "total_budget_ms=",
        "max_budget_ms=",
    ):
        assert field in message, f"missing {field!r} in: {message}"
    # A single attempt this slow already exceeds the cap by itself; no
    # attempt-count/parallelism change can fit it.
    assert "use run_cpsat_python instead" in message


def test_budget_rejection_from_batching_hints_at_attempt_count_not_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only the batching (not any single attempt) is over budget, the hint
    should point at attempt count/parallelism, not at swapping tools."""
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    per_attempt_timeout = (MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // 2) - 1000
    attempts = [_attempt("a", timeout_ms=per_attempt_timeout) for _ in range(3)]

    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS") as exc_info:
        experiment._check_wall_clock_budget(
            attempts,
            default_timeout_ms=30_000,
            max_parallel_attempts=1,
            checker_present=False,
            checker_timeout_ms=None,
        )

    message = str(exc_info.value)
    assert "attempt_count=3" in message
    assert "reduce attempt count or per-attempt timeout_ms" in message
    assert "use run_cpsat_python instead" not in message


def test_budget_gate_fires_before_any_child_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)

    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS"):
        run_cpsat_python_experiment(
            [_attempt("a", timeout_ms=MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS)],
            objective_sense="minimize",
        )

    assert called is False


def test_higher_max_parallel_attempts_can_fit_a_budget_that_serial_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    per_attempt_timeout = (MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // 2) - 1000
    attempts = [_attempt("a", timeout_ms=per_attempt_timeout) for _ in range(3)]

    # Serial (implicit batches=3) is over budget...
    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS"):
        experiment._check_wall_clock_budget(
            attempts,
            default_timeout_ms=30_000,
            max_parallel_attempts=1,
            checker_present=False,
            checker_timeout_ms=None,
        )
    # ...but with 3-way parallelism (batches=1) it fits.
    experiment._check_wall_clock_budget(
        attempts,
        default_timeout_ms=30_000,
        max_parallel_attempts=3,
        checker_present=False,
        checker_timeout_ms=None,
    )


# --- other validation ------------------------------------------------------------


def test_empty_attempts_rejected() -> None:
    with pytest.raises(ValueError, match="attempts must not be empty"):
        run_cpsat_python_experiment([], objective_sense="minimize")


def test_empty_source_rejected() -> None:
    with pytest.raises(ValueError, match="source must be non-empty"):
        run_cpsat_python_experiment([_attempt("   ")], objective_sense="minimize")


def test_non_positive_attempt_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="timeout_ms must be positive"):
        run_cpsat_python_experiment([_attempt("a", timeout_ms=0)], objective_sense="minimize")


def test_non_positive_default_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="default_timeout_ms must be positive"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", default_timeout_ms=0
        )


def test_checker_timeout_without_checker_rejected() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms supplied without checker"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", checker_timeout_ms=100
        )


def test_whitespace_only_checker_rejected() -> None:
    with pytest.raises(ValueError, match="checker must be non-empty"):
        run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize", checker="   ")


def test_invalid_objective_sense_rejected_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fake(source: str, *, timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    with pytest.raises(ValueError, match="objective_sense"):
        run_cpsat_python_experiment(
            [_attempt("a")],
            objective_sense="max",  # type: ignore[arg-type]
        )

    assert not called


# --- source hashing ---------------------------------------------------------------


def test_source_sha256_index_aligned_with_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"aaa": _result(), "bbb": _result()})

    result = run_cpsat_python_experiment(
        [_attempt("aaa"), _attempt("bbb")], objective_sense="minimize"
    )

    assert result.source_sha256 == [
        result.attempts[0].source_sha256,
        result.attempts[1].source_sha256,
    ]
    assert result.attempts[0].source_sha256 != result.attempts[1].source_sha256


def test_checker_and_problem_hashes_present_when_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    _patch_checker(monkeypatch, "accepted")

    result = run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        problem="my problem",
    )

    assert result.checker_sha256 is not None
    assert result.problem_sha256 is not None


def test_checker_and_problem_hashes_none_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.checker_sha256 is None
    assert result.problem_sha256 is None


# --- tracker forwarding -----------------------------------------------------------


def test_tracker_forwarded_to_runner_and_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    run_calls: list[dict[str, Any]] = []
    checker_calls: list[dict[str, Any]] = []
    _patch_runner(
        monkeypatch, {"a": _result(objective=3), "b": _result(objective=4)}, calls=run_calls
    )
    _patch_checker(monkeypatch, "accepted", calls=checker_calls)

    run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b")],
        objective_sense="minimize",
        checker="print('x')",
        default_timeout_ms=1000,
        tracker=sentinel,  # type: ignore[arg-type]
    )

    assert all(c["tracker"] is sentinel for c in run_calls)
    assert all(c["tracker"] is sentinel for c in checker_calls)
