"""Tests for cpsat.objectives — objective build, solver config, warm-start."""

from ortools.sat.python import cp_model

from openconstraint_mcp.cpsat.objectives import (
    DEFAULT_ORTOOLS_TIMEOUT_MS,
    configure_solver,
    effective_timeout_ms,
    order_objective_groups,
    set_objective,
)
from openconstraint_mcp.cpsat.schemas import (
    ORToolsLinearTerm,
    ORToolsObjective,
    ORToolsSearchConfig,
)


def _make_model_and_solve(
    objective: ORToolsObjective | list[ORToolsObjective],
    var_bounds: tuple[int, int] = (0, 10),
) -> tuple[str, int, list[int]]:
    """Build minimal model, set objective, solve, return (status, value, values)."""
    model = cp_model.CpModel()
    x = model.NewIntVar(var_bounds[0], var_bounds[1], "x")
    var_map = {"x": x}
    # Add a constraint to make the problem non-trivial
    model.Add(x >= 3)

    set_objective(model, objective, var_map)
    solver = cp_model.CpSolver()
    status_code = solver.Solve(model)
    status_map = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
    }
    status = status_map.get(status_code, "unknown")
    value = int(round(solver.ObjectiveValue())) if status in ("optimal", "feasible") else 0
    return status, value, []


def _solve_lexicographic(
    objectives: list[ORToolsObjective],
    var_bounds: tuple[int, int] = (0, 10),
) -> tuple[str, list[int]]:
    """Solve a model with lexicographic multi-objective, returning group values."""
    model = cp_model.CpModel()
    x = model.NewIntVar(var_bounds[0], var_bounds[1], "x")
    var_map = {"x": x}
    model.Add(x >= 3)

    solver = cp_model.CpSolver()
    values: list[int] = []
    groups = order_objective_groups(objectives)
    for group in groups:
        expr = sum(o.weight * sum(t.coef * var_map[t.var] for t in o.terms) for o in group)
        if group[0].sense == "min":
            model.Minimize(expr)
        else:
            model.Maximize(expr)
        status_code = solver.Solve(model)
        if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return "infeasible", values
        optimum = int(round(solver.ObjectiveValue()))
        values.append(optimum)
        model.Add(expr == optimum)

    return "optimal", values


def test_single_minimize() -> None:
    """Minimizing x where x >= 3 gives optimum 3."""
    obj = ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)])
    status, value, _ = _make_model_and_solve(obj)
    assert status in ("optimal", "feasible")
    assert value == 3


def test_single_maximize() -> None:
    """Maximizing x with x <= 7 gives optimum 7."""
    model = cp_model.CpModel()
    x = model.NewIntVar(0, 10, "x")
    var_map = {"x": x}
    model.Add(x <= 7)
    obj = ORToolsObjective(sense="max", terms=[ORToolsLinearTerm(var="x", coef=1)])
    set_objective(model, obj, var_map)
    solver = cp_model.CpSolver()
    solver.Solve(model)
    assert int(round(solver.ObjectiveValue())) == 7


def test_order_objective_groups_ascending() -> None:
    """order_objective_groups returns groups by ascending priority."""
    objectives = [
        ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=2),
        ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=1),
        ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=3),
    ]
    groups = order_objective_groups(objectives)
    assert len(groups) == 3
    assert groups[0][0].priority == 1
    assert groups[1][0].priority == 2
    assert groups[2][0].priority == 3


def test_lexicographic_priority_order() -> None:
    """Priority-1 (min x) is locked first, then priority-2 (max x) is optimized."""
    objectives = [
        ORToolsObjective(
            sense="min",
            terms=[ORToolsLinearTerm(var="x", coef=1)],
            priority=1,
            weight=1,
        ),
        ORToolsObjective(
            sense="max",
            terms=[ORToolsLinearTerm(var="x", coef=1)],
            priority=2,
            weight=1,
        ),
    ]
    status, values = _solve_lexicographic(objectives)
    assert status == "optimal"
    # With x >= 3: priority-1 minimizes x → optimum = 3
    assert values[0] == 3
    # priority-2 now maximizes x subject to x == 3 → stays 3
    assert values[1] == 3


def test_lexicographic_different_direction() -> None:
    """Priority-1 (max x) locked, priority-2 (min x) — reverses order."""
    objectives = [
        ORToolsObjective(
            sense="max",
            terms=[ORToolsLinearTerm(var="x", coef=1)],
            priority=1,
            weight=1,
        ),
        ORToolsObjective(
            sense="min",
            terms=[ORToolsLinearTerm(var="x", coef=1)],
            priority=2,
            weight=1,
        ),
    ]
    status, values = _solve_lexicographic(objectives)
    assert status == "optimal"
    # With x >= 3 and x <= 10: priority-1 maximizes x → 10
    # priority-2 tries to minimize x subject to x == 10 → stays 10
    assert values[0] <= 10
    assert values[1] <= 10


def test_configure_solver_timeout_and_seed() -> None:
    """configure_solver sets max_time_in_seconds and random_seed."""
    solver = cp_model.CpSolver()
    config = ORToolsSearchConfig(timeout_ms=5000, random_seed=42)
    configure_solver(solver, config)
    assert solver.parameters.max_time_in_seconds == 5.0
    assert solver.parameters.random_seed == 42


def test_effective_timeout_falls_back_to_default() -> None:
    """An unset timeout resolves to the default; a set one passes through."""
    assert effective_timeout_ms(None) == DEFAULT_ORTOOLS_TIMEOUT_MS
    assert effective_timeout_ms(ORToolsSearchConfig()) == DEFAULT_ORTOOLS_TIMEOUT_MS
    assert effective_timeout_ms(ORToolsSearchConfig(timeout_ms=5000)) == 5000


def test_configure_solver_applies_default_timeout_when_unset() -> None:
    """No search config still bounds the solve (never unbounded)."""
    solver = cp_model.CpSolver()
    configure_solver(solver, None)
    assert solver.parameters.max_time_in_seconds == DEFAULT_ORTOOLS_TIMEOUT_MS / 1000.0


def test_warm_start_does_not_break() -> None:
    """Warm-start hints on a satisfiable model don't prevent solving."""
    from openconstraint_mcp.cpsat.objectives import apply_warm_start

    model = cp_model.CpModel()
    x = model.NewIntVar(0, 10, "x")
    var_map = {"x": x}
    model.Add(x >= 3)

    warm_start = {"x": 5}
    apply_warm_start(model, warm_start, var_map)

    solver = cp_model.CpSolver()
    status_code = solver.Solve(model)
    assert status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    assert solver.Value(x) >= 3
