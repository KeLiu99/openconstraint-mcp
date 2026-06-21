"""Objective build, solver configuration and warm-start for CP-SAT.

Portions adapted from chuk-mcp-solver (Apache-2.0).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openconstraint_mcp.cpsat.schemas import ORToolsObjective, ORToolsSearchConfig

if TYPE_CHECKING:
    from ortools.sat.python.cp_model import CpModel, CpSolver, IntVar  # pragma: no cover

DEFAULT_ORTOOLS_TIMEOUT_MS = 30_000


def effective_timeout_ms(search: ORToolsSearchConfig | None) -> int:
    """The wall-clock time limit actually applied to a solve.

    Falls back to ``DEFAULT_ORTOOLS_TIMEOUT_MS`` when the caller leaves
    ``timeout_ms`` unset, so a hard instance can never run unbounded.  The
    core's ``timed_out`` heuristic reads the same value, so a hit limit is
    labelled ``timeout_best`` consistently.
    """
    if search is not None and search.timeout_ms is not None:
        return search.timeout_ms
    return DEFAULT_ORTOOLS_TIMEOUT_MS


def set_objective(
    model: CpModel,
    objective: ORToolsObjective | list[ORToolsObjective],
    var_map: dict[str, IntVar],
) -> None:
    """Set a single CP-SAT objective: a lone ``ORToolsObjective`` or a single-group
    weighted sum (all same priority/weight, or a one-element list).
    """
    if isinstance(objective, list):
        if len(objective) == 0:
            return
        expr = sum(o.weight * sum(t.coef * var_map[t.var] for t in o.terms) for o in objective)
        sense = objective[0].sense
    else:
        expr = sum(t.coef * var_map[t.var] for t in objective.terms)
        sense = objective.sense

    if sense == "min":
        model.Minimize(expr)  # type: ignore[attr-defined]
    else:
        model.Maximize(expr)  # type: ignore[attr-defined]


def order_objective_groups(
    objectives: list[ORToolsObjective],
) -> list[list[ORToolsObjective]]:
    """Group objectives by priority, ordered ascending (priority 1 first).

    Within equal priority, objectives are combined by integer ``weight``.
    """
    by_priority: dict[int, list[ORToolsObjective]] = {}
    for obj in objectives:
        by_priority.setdefault(obj.priority, []).append(obj)
    return [by_priority[p] for p in sorted(by_priority)]


def build_objective_expr(  # type: ignore[no-untyped-def]
    group: list[ORToolsObjective],
    var_map: dict[str, IntVar],
):
    """Build the weighted-sum expression for one priority group.

    The group's sense must be uniform (enforced by schema validation).
    """
    total = 0  # type: ignore[assignment]
    for obj in group:
        total += obj.weight * sum(t.coef * var_map[t.var] for t in obj.terms)  # type: ignore[assignment]
    return total


def configure_solver(
    solver: CpSolver,
    search: ORToolsSearchConfig | None,
) -> None:
    """Apply time-limit, workers, seed from ``search`` to the CP-SAT ``solver``.

    A time limit is *always* set (defaulting to ``DEFAULT_ORTOOLS_TIMEOUT_MS``)
    so a hard instance can never run unbounded.
    """
    solver.parameters.max_time_in_seconds = effective_timeout_ms(search) / 1000.0
    if search is None:
        return
    if search.num_workers is not None:
        solver.parameters.num_search_workers = search.num_workers
    if search.random_seed is not None:
        solver.parameters.random_seed = search.random_seed


def apply_warm_start(
    model: CpModel,
    warm_start: dict[str, int] | None,
    var_map: dict[str, IntVar],
) -> None:
    """Add hint values to the model for a warm start."""
    if warm_start is None:
        return
    for var_id, value in warm_start.items():
        cp_var = var_map.get(var_id)
        if cp_var is not None:
            model.AddHint(cp_var, value)  # type: ignore[attr-defined]
