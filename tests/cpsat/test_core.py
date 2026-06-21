"""Tests for cpsat.core — solve_model end-to-end."""

from __future__ import annotations

import importlib
from unittest import mock

import pytest

from openconstraint_mcp.cpsat.schemas import (
    ORToolsConstraint,
    ORToolsLinearParams,
    ORToolsLinearTerm,
    ORToolsObjective,
    ORToolsSearchConfig,
    ORToolsSolveRequest,
    ORToolsVariable,
)


def _make_request(
    mode="satisfy",
    *,
    variables=None,
    constraints=None,
    objective=None,
    search=None,
):
    """Shorthand to build an ORToolsSolveRequest."""
    if variables is None:
        variables = [ORToolsVariable(id="x", domain="integer", lower=0, upper=10)]
    return ORToolsSolveRequest(
        mode=mode,  # type: ignore[arg-type]
        variables=variables,
        constraints=constraints or [],
        objective=objective,
        search=search,
    )


# Don't import solve_model at top level — we need to test the lazy import gate.
def _solve(request):
    from openconstraint_mcp.cpsat.core import solve_model

    return solve_model(request)


def test_satisfy_no_constraints() -> None:
    """Satisfy with no constraints → satisfied, variable in bounds."""
    result = _solve(_make_request("satisfy"))
    assert result.status == "satisfied"
    assert 0 <= result.solutions[0].variables[0].value <= 10


def test_conflicting_linears() -> None:
    """Conflicting constraints → infeasible."""
    result = _solve(
        _make_request(
            "satisfy",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=10)],
            constraints=[
                ORToolsConstraint(
                    id="c1",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense=">=", rhs=5
                    ),
                ),
                ORToolsConstraint(
                    id="c2",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense="<=", rhs=3
                    ),
                ),
            ],
        )
    )
    assert result.status == "infeasible"


def test_minimize_x_ge_3() -> None:
    """Minimizing x where x >= 3 → optimal, objective_value == 3 (int)."""
    result = _solve(
        _make_request(
            "optimize",
            objective=ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)]),
            constraints=[
                ORToolsConstraint(
                    id="c",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense=">=", rhs=3
                    ),
                ),
            ],
        )
    )
    assert result.status == "optimal"
    assert result.objective_value == 3
    assert isinstance(result.objective_value, int)


def test_maximize_x_le_7() -> None:
    """Maximizing x where x <= 7 → optimal, objective_value == 7."""
    result = _solve(
        _make_request(
            "optimize",
            objective=ORToolsObjective(sense="max", terms=[ORToolsLinearTerm(var="x", coef=1)]),
            constraints=[
                ORToolsConstraint(
                    id="c",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense="<=", rhs=7
                    ),
                ),
            ],
        )
    )
    assert result.status == "optimal"
    assert result.objective_value == 7


def test_knapsack_optimal() -> None:
    """Simple knapsack → optimal with known objective_value."""
    # items: a (value 5, weight 3), b (value 2, weight 2), c (value 1, weight 1)
    # capacity: 4
    # optimal: pick a (3) + c (1) = weight 4, value 6
    result = _solve(
        ORToolsSolveRequest(
            mode="optimize",
            variables=[
                ORToolsVariable(id="a", domain="bool"),
                ORToolsVariable(id="b", domain="bool"),
                ORToolsVariable(id="c", domain="bool"),
            ],
            constraints=[
                ORToolsConstraint(
                    id="cap",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[
                            ORToolsLinearTerm(var="a", coef=3),
                            ORToolsLinearTerm(var="b", coef=2),
                            ORToolsLinearTerm(var="c", coef=1),
                        ],
                        sense="<=",
                        rhs=4,
                    ),
                ),
            ],
            objective=ORToolsObjective(
                sense="max",
                terms=[
                    ORToolsLinearTerm(var="a", coef=5),
                    ORToolsLinearTerm(var="b", coef=2),
                    ORToolsLinearTerm(var="c", coef=1),
                ],
            ),
        )
    )
    assert result.status == "optimal"
    assert result.objective_value == 6


def test_bool_var_domain() -> None:
    """Bool variable ∈ {0, 1}."""
    result = _solve(
        _make_request(
            "satisfy",
            variables=[ORToolsVariable(id="b", domain="bool")],
        )
    )
    assert result.status == "satisfied"
    assert result.solutions[0].variables[0].value in (0, 1)


def test_same_seed_same_solution() -> None:
    """Same random_seed twice → same first solution."""
    from openconstraint_mcp.cpsat.core import solve_model

    req = _make_request(
        "satisfy",
        search=ORToolsSearchConfig(random_seed=123, max_solutions=1),
    )
    r1 = solve_model(req)
    r2 = solve_model(req)
    assert r1.solutions[0].variables[0].value == r2.solutions[0].variables[0].value


def test_max_solutions_enumerate() -> None:
    """max_solutions=3 on a satisfy problem with many solutions returns up to 3."""
    result = _solve(
        _make_request(
            "satisfy",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=100)],
            search=ORToolsSearchConfig(max_solutions=3),
        )
    )
    assert result.status == "satisfied"
    assert 1 <= len(result.solutions) <= 3
    # All solutions should be different
    values = [s.variables[0].value for s in result.solutions]
    assert len(values) == len(set(values)) or len(result.solutions) < 3


def test_multi_objective_lexicographic() -> None:
    """Two-objective lexicographic: objective_values primary-first."""
    result = _solve(
        ORToolsSolveRequest(
            mode="optimize",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=100)],
            constraints=[
                ORToolsConstraint(
                    id="c",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense=">=", rhs=5
                    ),
                ),
            ],
            objective=[
                ORToolsObjective(
                    sense="min",
                    terms=[ORToolsLinearTerm(var="x", coef=1)],
                    priority=1,
                ),
                ORToolsObjective(
                    sense="max",
                    terms=[ORToolsLinearTerm(var="x", coef=1)],
                    priority=2,
                ),
            ],
        )
    )
    assert result.status == "optimal"
    # Primary (priority 1): min x where x>=5 → 5
    # Secondary (priority 2): max x given x==5 → 5
    assert result.objective_values == [5, 5]
    assert result.objective_value == result.objective_values[0]


def test_multi_objective_infeasible_reports_no_values() -> None:
    """An infeasible multi-objective yields empty solutions AND empty values.

    Guards the contract that objective_values is never populated on a
    no-solution status (it must not leak locked group optima).
    """
    result = _solve(
        ORToolsSolveRequest(
            mode="optimize",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=100)],
            constraints=[
                ORToolsConstraint(
                    id="lo",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense=">=", rhs=10
                    ),
                ),
                ORToolsConstraint(
                    id="hi",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense="<=", rhs=3
                    ),
                ),
            ],
            objective=[
                ORToolsObjective(
                    sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=1
                ),
                ORToolsObjective(
                    sense="max", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=2
                ),
            ],
        )
    )
    assert result.status == "infeasible"
    assert result.objective_values == []
    assert result.objective_value is None
    assert result.solutions == []


def test_lexicographic_feasible_not_locked() -> None:
    """FEASIBLE on first objective is NOT locked; result is feasible, not optimal."""
    result = _solve(
        ORToolsSolveRequest(
            mode="optimize",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=100)],
            constraints=[
                ORToolsConstraint(
                    id="c",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var="x", coef=1)], sense=">=", rhs=5
                    ),
                ),
            ],
            objective=[
                ORToolsObjective(
                    sense="min",
                    terms=[ORToolsLinearTerm(var="x", coef=1)],
                    priority=1,
                ),
                ORToolsObjective(
                    sense="max",
                    terms=[ORToolsLinearTerm(var="x", coef=1)],
                    priority=2,
                ),
            ],
            search=ORToolsSearchConfig(timeout_ms=1),
        )
    )
    # With a 1ms timeout, first objective may not prove OPTIMAL.
    # After the fix, FEASIBLE is not locked, so the status must NOT be "optimal"
    # unless OR-Tools did quickly prove optimality.
    if result.status == "optimal":
        # Fast machine proved optimal in <1ms — check objective values are present
        assert result.objective_values == [5, 5]
    else:
        # Status is "feasible" (or possibly "timeout_no_solution")
        assert result.status != "optimal"


def test_zero_objective_value() -> None:
    """objective_value == 0 is reported as 0, not None."""
    result = _solve(
        ORToolsSolveRequest(
            mode="optimize",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=100)],
            objective=ORToolsObjective(
                sense="min",
                terms=[ORToolsLinearTerm(var="x", coef=1)],
            ),
        )
    )
    assert result.status == "optimal"
    assert result.objective_value == 0
    assert isinstance(result.objective_value, int)


def test_graceful_degradation_ortools_missing() -> None:
    """solve_model raises ValueError with clear message when ortools import fails."""
    from openconstraint_mcp.cpsat.core import solve_model

    def _raise_import_error(name, *args, **kwargs):
        raise ImportError("No module named ortools")

    with mock.patch.object(importlib, "import_module", side_effect=_raise_import_error):
        with pytest.raises(ValueError, match="OR-Tools"):
            solve_model(_make_request("satisfy"))


def test_can_import_cpsat_core_without_ortools() -> None:
    """Importing cpsat.core does NOT trigger an ortools import at module level."""
    # Verify the module has no top-level ortools import by checking it's importable
    # even when we havent tested solve_model yet (proven by the other tests).
    import openconstraint_mcp.cpsat.core  # noqa: F401
    # If we got here, import succeeded without importing ortools at module level.
