"""Constraint builders for Google OR-Tools CP-SAT solver.

Portions adapted from chuk-mcp-solver (Apache-2.0).
"""

from typing import TYPE_CHECKING

from openconstraint_mcp.cpsat.schemas import (
    ORToolsAllDifferentParams,
    ORToolsCircuitParams,
    ORToolsConstraint,
    ORToolsCumulativeParams,
    ORToolsElementParams,
    ORToolsImplicationParams,
    ORToolsLinearParams,
    ORToolsNoOverlapParams,
    ORToolsReservoirParams,
    ORToolsTableParams,
)

if TYPE_CHECKING:
    from ortools.sat.python.cp_model import CpModel, IntVar  # pragma: no cover


def _build_linear(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsLinearParams)  # type: ignore[unreachable]
    expr = sum(term.coef * var_map[term.var] for term in params.terms)
    if params.sense == "<=":
        model.Add(expr <= params.rhs).WithName(constraint.id)  # type: ignore[attr-defined]
    elif params.sense == ">=":
        model.Add(expr >= params.rhs).WithName(constraint.id)  # type: ignore[attr-defined]
    else:
        model.Add(expr == params.rhs).WithName(constraint.id)  # type: ignore[attr-defined]


def _build_all_different(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsAllDifferentParams)  # type: ignore[unreachable]
    vars_list = [var_map[v] for v in params.vars]
    model.AddAllDifferent(vars_list).WithName(constraint.id)  # type: ignore[attr-defined]


def _build_element(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsElementParams)  # type: ignore[unreachable]
    model.AddElement(  # type: ignore[attr-defined]
        var_map[params.index_var], params.array, var_map[params.target_var]
    ).WithName(constraint.id)


def _build_table(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsTableParams)  # type: ignore[unreachable]
    vars_list = [var_map[v] for v in params.vars]
    model.AddAllowedAssignments(vars_list, params.allowed_tuples).WithName(  # type: ignore[attr-defined]
        constraint.id
    )


def _build_cumulative(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsCumulativeParams)  # type: ignore[unreachable]
    start_vars = [var_map[v] for v in params.start_vars]
    if isinstance(params.duration_vars[0], str):
        dur_vars = [var_map[v] for v in params.duration_vars]  # type: ignore[index]
    else:
        dur_vars = params.duration_vars  # type: ignore[assignment]
    if isinstance(params.demand_vars[0], str):
        dem_vars = [var_map[v] for v in params.demand_vars]  # type: ignore[index]
    else:
        dem_vars = params.demand_vars  # type: ignore[assignment]

    intervals = []
    for i, (start, dur) in enumerate(zip(start_vars, dur_vars, strict=True)):
        interval = model.NewIntervalVar(  # type: ignore[attr-defined]
            start, dur, start + dur, f"{constraint.id}_interval_{i}"
        )
        intervals.append(interval)
    model.AddCumulative(intervals, dem_vars, params.capacity).WithName(  # type: ignore[attr-defined]
        constraint.id
    )


def _build_circuit(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsCircuitParams)  # type: ignore[unreachable]
    arcs = [(f, t, var_map[v]) for f, t, v in params.arcs]
    model.AddCircuit(arcs).WithName(constraint.id)  # type: ignore[attr-defined]


def _build_no_overlap(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsNoOverlapParams)  # type: ignore[unreachable]
    start_vars = [var_map[v] for v in params.start_vars]
    if isinstance(params.duration_vars[0], str):
        dur_vars = [var_map[v] for v in params.duration_vars]  # type: ignore[index]
    else:
        dur_vars = params.duration_vars  # type: ignore[assignment]

    intervals = []
    for i, (start, dur) in enumerate(zip(start_vars, dur_vars, strict=True)):
        interval = model.NewIntervalVar(  # type: ignore[attr-defined]
            start, dur, start + dur, f"{constraint.id}_interval_{i}"
        )
        intervals.append(interval)
    model.AddNoOverlap(intervals).WithName(constraint.id)  # type: ignore[attr-defined]


def _build_implication(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    *,
    constraints_by_id: dict[str, ORToolsConstraint],
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsImplicationParams)  # type: ignore[unreachable]
    if_var = var_map[params.if_var]

    # Belt-and-suspenders: ORToolsSolveRequest's validators already guarantee the
    # target exists and is linear. These checks keep build_constraint safe when
    # called directly with a hand-built constraints_by_id (e.g. in tests).
    then_constraint = constraints_by_id.get(params.then_constraint_id)
    if then_constraint is None:
        raise ValueError(
            f"Implication '{constraint.id}': referenced constraint "
            f"'{params.then_constraint_id}' not found"
        )
    if then_constraint.kind != "linear":
        raise ValueError(
            f"Implication '{constraint.id}': only supports linear constraints, "
            f"got '{then_constraint.kind}'"
        )

    assert isinstance(then_constraint.params, ORToolsLinearParams)  # type: ignore[unreachable]
    expr = sum(t.coef * var_map[t.var] for t in then_constraint.params.terms)
    if then_constraint.params.sense == "<=":
        model.Add(expr <= then_constraint.params.rhs).WithName(constraint.id).OnlyEnforceIf(  # type: ignore[attr-defined]
            if_var
        )
    elif then_constraint.params.sense == ">=":
        model.Add(expr >= then_constraint.params.rhs).WithName(constraint.id).OnlyEnforceIf(  # type: ignore[attr-defined]
            if_var
        )
    else:
        model.Add(expr == then_constraint.params.rhs).WithName(constraint.id).OnlyEnforceIf(  # type: ignore[attr-defined]
            if_var
        )


def _build_reservoir(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    **kwargs: object,
) -> None:
    params = constraint.params
    assert isinstance(params, ORToolsReservoirParams)  # type: ignore[unreachable]
    time_vars = [var_map[v] for v in params.time_vars]
    model.AddReservoirConstraint(  # type: ignore[attr-defined]
        time_vars, params.level_changes, params.min_level, params.max_level
    ).WithName(constraint.id)


_BUILDERS: dict[str, object] = {
    "linear": _build_linear,
    "all_different": _build_all_different,
    "element": _build_element,
    "table": _build_table,
    "cumulative": _build_cumulative,
    "circuit": _build_circuit,
    "no_overlap": _build_no_overlap,
    "implication": _build_implication,
    "reservoir": _build_reservoir,
}


def build_constraint(
    model: CpModel,
    constraint: ORToolsConstraint,
    var_map: dict[str, IntVar],
    *,
    constraints_by_id: dict[str, ORToolsConstraint] | None = None,
) -> None:
    """Build and add ``constraint`` into the CP-SAT ``model``."""
    builder = _BUILDERS.get(constraint.kind)
    if builder is None:
        raise ValueError(f"Unsupported constraint kind: {constraint.kind}")
    if constraint.kind == "implication":
        if constraints_by_id is None:
            raise ValueError("constraints_by_id is required for implication constraints")
        _build_implication(
            model, constraint, var_map, constraints_by_id=constraints_by_id
        )
    else:
        builder(model, constraint, var_map)  # type: ignore[operator]
