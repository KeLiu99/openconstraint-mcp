"""Core CP-SAT solver — build, solve, collect results.

Portions adapted from chuk-mcp-solver (Apache-2.0).
"""

import importlib
import time
from typing import Any

from openconstraint_mcp.cpsat.constraints import build_constraint
from openconstraint_mcp.cpsat.objectives import (
    apply_warm_start,
    build_objective_expr,
    configure_solver,
    effective_timeout_ms,
    order_objective_groups,
    set_objective,
)
from openconstraint_mcp.cpsat.schemas import (
    ORToolsObjective,
    ORToolsSolution,
    ORToolsSolutionVariable,
    ORToolsSolveRequest,
    ORToolsSolveResult,
)


def _require_cp_model() -> Any:
    """Lazily import ``ortools.sat.python.cp_model``.

    This is the only runtime ``ortools`` import site — the rest of ``cpsat``
    imports it only under ``TYPE_CHECKING``.  On ``ImportError`` a clear
    ``ValueError`` tells the user to reinstall.
    """
    try:
        return importlib.import_module("ortools.sat.python.cp_model")
    except ImportError:
        raise ValueError(
            "OR-Tools (ortools) is not available; "
            "reinstall openconstraint-mcp — ortools is a required dependency"
        ) from None


def solve_model(request: ORToolsSolveRequest) -> ORToolsSolveResult:
    """Build a CP-SAT model from ``request``, solve it, and return the result."""
    cp = _require_cp_model()

    model = cp.CpModel()
    solver = cp.CpSolver()

    # ------------------------------------------------------------------
    # Build variables
    # ------------------------------------------------------------------
    var_map: dict[str, Any] = {}
    for v in request.variables:
        if v.domain == "bool":
            var_map[v.id] = model.NewIntVar(0, 1, v.id)
        else:
            var_map[v.id] = model.NewIntVar(v.lower, v.upper, v.id)

    # ------------------------------------------------------------------
    # Index constraints; identify implication template ids
    # ------------------------------------------------------------------
    constr_by_id = {c.id: c for c in request.constraints}
    template_ids: set[str] = set()
    for c in request.constraints:
        if c.kind == "implication":
            template_ids.add(c.params.then_constraint_id)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Build constraints (skip template-only ones)
    # ------------------------------------------------------------------
    for c in request.constraints:
        if c.id in template_ids:
            continue
        build_constraint(model, c, var_map, constraints_by_id=constr_by_id)

    # ------------------------------------------------------------------
    # Warm-start hints & solver config
    # ------------------------------------------------------------------
    if request.search is not None:
        apply_warm_start(model, request.search.warm_start, var_map)
    configure_solver(solver, request.search)

    # ------------------------------------------------------------------
    # Objective & solve
    # ------------------------------------------------------------------
    obj_spec = request.objective
    is_multi = isinstance(obj_spec, list) and len(obj_spec) > 0
    max_solutions = request.search.max_solutions if request.search else 1
    callback: Any = None
    lex_values: list[int] = []

    start_time = time.monotonic()

    if request.mode == "satisfy":
        if max_solutions > 1:
            solver.parameters.enumerate_all_solutions = True
            callback = _make_solution_collector(cp, max_solutions, var_map)
            status_code = solver.Solve(model, callback)
        else:
            status_code = solver.Solve(model)
    elif not is_multi:
        set_objective(model, obj_spec, var_map)  # type: ignore[arg-type]
        status_code = solver.Solve(model)
    else:
        assert isinstance(obj_spec, list)
        status_code, lex_values = _run_lexicographic(
            cp,
            solver,
            model,
            obj_spec,
            var_map,  # type: ignore[arg-type]
        )

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # ------------------------------------------------------------------
    # Status mapping
    # ------------------------------------------------------------------
    # Wall-clock heuristic, only consulted on the FEASIBLE branch below to tell
    # "feasible" from "timeout_best". In lexicographic mode the limit applies to
    # each group solve, so total elapsed can be a multiple of the single limit —
    # a long final group then always reads as timed out. Acceptable for v0.
    timed_out = elapsed_ms >= effective_timeout_ms(request.search)

    status: str
    if status_code == cp.OPTIMAL:
        status = "optimal" if request.mode == "optimize" else "satisfied"
    elif status_code == cp.FEASIBLE:
        if request.mode == "satisfy":
            status = "satisfied"
        elif timed_out:
            status = "timeout_best"
        else:
            status = "feasible"
    elif status_code == cp.INFEASIBLE:
        status = "infeasible"
    elif status_code == cp.UNKNOWN:
        status = "timeout_no_solution"
    else:
        status = "error"

    # ------------------------------------------------------------------
    # Collect solutions
    # ------------------------------------------------------------------
    solutions = _collect_solutions(solver, var_map, request, callback, cp, status_code)

    # ------------------------------------------------------------------
    # Objective values
    # ------------------------------------------------------------------
    objective_values: list[int] = []
    non_error = ("infeasible", "timeout_no_solution", "error")
    if status not in non_error:
        if is_multi:
            objective_values = lex_values
        elif request.mode == "optimize":
            objective_values = [int(round(solver.ObjectiveValue()))]

    objective_value = objective_values[0] if objective_values else None

    # ------------------------------------------------------------------
    # Optimality gap
    # ------------------------------------------------------------------
    gap: float | None = None
    non_terminal = ("optimal", "infeasible", "error", "timeout_no_solution")
    if request.mode == "optimize" and status not in non_terminal:
        best_bound = solver.BestObjectiveBound()
        obj_val = solver.ObjectiveValue()
        if obj_val != 0:
            gap = abs(obj_val - best_bound) / abs(obj_val) * 100.0

    return ORToolsSolveResult(
        status=status,  # type: ignore[arg-type]
        objective_value=objective_value,
        objective_values=objective_values,
        optimality_gap=gap,
        solve_time_ms=elapsed_ms,
        solutions=solutions,
        message=None,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _make_solution_collector(cp: Any, limit: int, var_map: dict[str, Any]) -> Any:
    """Create a solution callback that stops after ``limit`` solutions.

    Defined inside a function so the module import does not require ortools.
    """

    class _SolutionCollector(cp.CpSolverSolutionCallback):
        def __init__(self, limit: int, var_map: dict[str, Any]):
            super().__init__()
            self._limit = limit
            self._var_map = var_map
            self.solutions: list[dict[str, int]] = []

        def on_solution_callback(self) -> None:
            sol = {vid: self.Value(cp_var) for vid, cp_var in self._var_map.items()}
            self.solutions.append(sol)
            if len(self.solutions) >= self._limit:
                self.StopSearch()

    return _SolutionCollector(limit, var_map)


def _run_lexicographic(
    cp: Any,
    solver: Any,
    model: Any,
    objectives: list[ORToolsObjective],
    var_map: dict[str, Any],
) -> tuple[int, list[int]]:
    """Run lexicographic multi-objective sequential solve.

    Groups objectives by priority (ascending — priority 1 first).  For each
    group, sets a weighted-sum objective, solves, and locks the optimum with
    an equality constraint before moving to the next group.

    Returns ``(status_code, locked_values)`` — the **last** solve's status and
    each completed group's optimum in solve order (primary first).  Stops early
    on the first non-optimal group, returning the values locked so far.
    """
    groups = order_objective_groups(objectives)
    locked_values: list[int] = []
    last_status = cp.UNKNOWN

    for group in groups:
        expr = build_objective_expr(group, var_map)
        if group[0].sense == "min":
            model.Minimize(expr)  # type: ignore[attr-defined]
        else:
            model.Maximize(expr)  # type: ignore[attr-defined]
        last_status = solver.Solve(model)
        if last_status != cp.OPTIMAL:
            return last_status, locked_values
        optimum = int(round(solver.ObjectiveValue()))
        locked_values.append(optimum)
        model.Add(expr == optimum)  # type: ignore[attr-defined]

    return last_status, locked_values


def _collect_solutions(
    solver: Any,
    var_map: dict[str, Any],
    request: ORToolsSolveRequest,
    callback: Any | None,
    cp: Any,
    status_code: int,
) -> list[ORToolsSolution]:
    """Extract solutions from the solver."""
    # No solution available
    if status_code in (cp.INFEASIBLE, cp.UNKNOWN, cp.MODEL_INVALID):
        return []

    if callback is not None and hasattr(callback, "solutions"):
        sols: list[ORToolsSolution] = []
        for raw in callback.solutions:
            sol_vars = [ORToolsSolutionVariable(id=vid, value=val) for vid, val in raw.items()]
            sols.append(ORToolsSolution(variables=sol_vars))
        return sols

    # Single solution
    sol_vars = [
        ORToolsSolutionVariable(id=vid, value=solver.Value(cp_var))
        for vid, cp_var in var_map.items()
    ]
    return [ORToolsSolution(variables=sol_vars)]
