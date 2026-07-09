"""Real-runtime smoke tests for the shipped MiniZinc examples in ``examples/``.

These prove the README's example inventory claims are actually true against
the managed binary, not just plausible-sounding prose. Marked ``integration``
and excluded from ``just check``; run with ``just integration`` on a machine
where ``install-runtime`` has placed a runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openconstraint_mcp.minizinc.core import find_unsat_core_path, solve_model_path

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_real_runtime")]

_EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def test_knapsack_files_solve_to_a_feasible_selection() -> None:
    example = _EXAMPLES_DIR / "knapsack"
    result = solve_model_path(
        example / "model.mzn", data_path=example / "data.dzn", solver="cp-sat"
    )

    assert result.status == "optimal"
    assert result.solution is not None


def test_balanced_assignment_files_solve_to_a_feasible_assignment() -> None:
    example = _EXAMPLES_DIR / "balanced_assignment"
    result = solve_model_path(
        example / "model.mzn", data_path=example / "data.dzn", solver="cp-sat"
    )

    assert result.status == "optimal"
    assert result.solution is not None


def test_australia_map_coloring_with_shipped_checker_completes_correct() -> None:
    example = _EXAMPLES_DIR / "australia_map_coloring"
    result = solve_model_path(
        example / "model.mzn", checker_path=example / "model.mzc.mzn", solver="cp-sat"
    )

    assert result.status in ("optimal", "satisfied")
    assert result.checker is not None
    assert result.checker.status == "completed"
    assert result.checker.checks
    assert result.checker.checks[0].violation is False
    assert "CORRECT" in result.checker.checks[0].output


def test_golomb_ruler_files_reproduce_the_saved_optimum() -> None:
    example = _EXAMPLES_DIR / "golomb_ruler"
    result = solve_model_path(example / "model.mzn", data_path=example / "data.dzn")

    assert result.status == "optimal"
    assert result.objective == 11


def test_social_golfers_shipped_instance_is_satisfied() -> None:
    example = _EXAMPLES_DIR / "social_golfers"
    result = solve_model_path(example / "model.mzn", data_path=example / "data.dzn")

    assert result.status == "satisfied"
    assert result.solution is not None


def test_social_golfers_diagnose_and_repair_infeasibility(tmp_path: Path) -> None:
    """The README's "Diagnosing and repairing infeasibility" walkthrough, for real.

    The shipped 5-3-7 instance uses every one of C(15,2)=105 golfer pairs
    exactly once, so an 8th week cannot avoid a repeat. This model's plain
    search-based `solve` is not guaranteed to *prove* that quickly under a
    short budget -- it may report `"unknown"` rather than a clean
    `"unsatisfiable"`, which is exactly the case this walkthrough documents:
    findMUS is a dedicated diagnostic (a different algorithm from cp-sat's
    search), not a fallback only reached after a clean unsat proof. Either
    outcome (`"unknown"` or a proven `"unsatisfiable"`) is an acceptable
    trigger for reaching for `find_unsat_core`, so both are accepted here
    rather than pinning the test to current solver-performance timing.
    findMUS's own outcome is typically the "conservative no_core" case (see
    the `find_unsat_core` docs above), but `"timeout"` under the default
    budget is an equally legitimate, documented outcome.
    """
    model_path = _EXAMPLES_DIR / "social_golfers" / "model.mzn"
    shipped_data_path = _EXAMPLES_DIR / "social_golfers" / "data.dzn"

    over_capacity_data = tmp_path / "n_weeks_8.dzn"
    over_capacity_data.write_text(
        "n_groups   = 5;\ngroup_size = 3;\nn_weeks    = 8;\n", encoding="utf-8"
    )

    # A short budget is enough to show cp-sat's search does not easily find a
    # schedule; whether it also proves unsat within that budget depends on
    # solver performance, not on this walkthrough's premise.
    unresolved_result = solve_model_path(model_path, data_path=over_capacity_data, timeout_ms=5000)
    assert unresolved_result.status in ("unknown", "unsatisfiable")

    diagnosis = find_unsat_core_path(model_path, data_path=over_capacity_data)
    assert diagnosis.status in ("mus_found", "no_core", "timeout")
    assert diagnosis.message

    repaired_result = solve_model_path(model_path, data_path=shipped_data_path)
    assert repaired_result.status == "satisfied"
    assert repaired_result.solution is not None
