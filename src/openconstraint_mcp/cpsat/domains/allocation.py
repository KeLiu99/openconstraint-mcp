"""Budget allocation / knapsack problem converter.

Portions adapted from chuk-mcp-solver (Apache-2.0).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from openconstraint_mcp.cpsat.core import solve_model
from openconstraint_mcp.cpsat.schemas import (
    ORToolsConstraint,
    ORToolsImplicationParams,
    ORToolsLinearParams,
    ORToolsLinearTerm,
    ORToolsObjective,
    ORToolsSearchConfig,
    ORToolsSolveRequest,
    ORToolsSolveResult,
    ORToolsVariable,
    SolverStatus,
)

# ---------------------------------------------------------------------------
# User-facing models
# ---------------------------------------------------------------------------


class Item(BaseModel):
    """An item that may be selected."""

    id: str = Field(..., min_length=1)
    cost: float = Field(..., ge=0.0)
    value: float = Field(..., ge=0.0)
    resources_required: dict[str, float] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BudgetConstraint(BaseModel):
    """A budget or resource limit."""

    resource: str = Field(..., min_length=1)
    limit: float = Field(..., ge=0.0)
    penalty_per_unit_over: float = Field(default=0.0, ge=0.0)


class AllocationObjective(StrEnum):
    """Budget allocation optimization objectives."""

    MAXIMIZE_VALUE = "maximize_value"
    MAXIMIZE_COUNT = "maximize_count"
    MINIMIZE_COST = "minimize_cost"


class SolveBudgetAllocationRequest(BaseModel):
    """High-level budget allocation / knapsack problem definition."""

    items: list[Item] = Field(..., min_length=1)
    budgets: list[BudgetConstraint] = Field(..., min_length=1)
    objective: AllocationObjective = AllocationObjective.MAXIMIZE_VALUE
    min_value_threshold: float | None = Field(default=None, ge=0.0)
    max_cost_threshold: float | None = Field(default=None, ge=0.0)
    min_items: int | None = Field(default=None, ge=0)
    max_items: int | None = Field(default=None, ge=0)
    timeout_ms: int = Field(default=60_000, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AllocationExplanation(BaseModel):
    """Human-readable explanation of the allocation solution."""

    summary: str
    binding_constraints: list[str] = Field(default_factory=list)
    marginal_items: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class SolveBudgetAllocationResponse(BaseModel):
    """Budget allocation solution with domain-specific details.

    The ``status`` field uses the same SolverStatus literal as the core solver:
    ``optimal``, ``feasible``, ``satisfied``, ``infeasible``, ``timeout_best``,
    ``timeout_no_solution``, ``error``.  Float fields (``total_cost``,
    ``total_value``, ``resource_usage``) are recomputed from the original request's
    floats over the selected items, not unscaled from the integer core, so no
    scaling round-trip error reaches the response.
    """

    status: SolverStatus
    selected_items: list[str] = Field(default_factory=list)
    total_cost: float = Field(default=0.0, ge=0.0)
    total_value: float = Field(default=0.0, ge=0.0)
    resource_usage: dict[str, float] = Field(default_factory=dict)
    resource_slack: dict[str, float] = Field(default_factory=dict)
    solve_time_ms: int = Field(default=0, ge=0)
    optimality_gap: float | None = None
    explanation: AllocationExplanation


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

_MIN_SCALE = 100


def _compute_scale(request: SolveBudgetAllocationRequest) -> int:
    """Compute an integer scale factor that preserves the precision of every
    positive float value in the request.

    The returned scale is the smallest power-of-10 multiplier that makes all
    positive input values representable as integers without truncation, never
    lower than ``_MIN_SCALE``.
    """

    def _decimal_places(value: float) -> int:
        if value <= 0:
            return 0
        try:
            exp = Decimal(str(value)).as_tuple().exponent
        except (InvalidOperation, ValueError):
            return 0
        # ``exp`` is negative for fractional values (e.g. -3 for 1.125); the
        # fractional-place count is ``-exp``.  Integers have ``exp >= 0`` and
        # need no extra scale.
        return -exp if isinstance(exp, int) and exp < 0 else 0

    max_places = 0
    for item in request.items:
        max_places = max(max_places, _decimal_places(item.cost))
        max_places = max(max_places, _decimal_places(item.value))
        for amount in item.resources_required.values():
            max_places = max(max_places, _decimal_places(amount))
    for budget in request.budgets:
        max_places = max(max_places, _decimal_places(budget.limit))
    if request.min_value_threshold is not None:
        max_places = max(max_places, _decimal_places(request.min_value_threshold))
    if request.max_cost_threshold is not None:
        max_places = max(max_places, _decimal_places(request.max_cost_threshold))

    return max(_MIN_SCALE, 10**max_places)


def _scaled(value: float, scale: int) -> int:
    """Scale a float to an integer using the precomputed power-of-ten ``scale``.

    Rounds rather than truncates so float representation error (e.g.
    ``19.99 * 100 == 1998.9999…``) cannot drop a unit.  ``scale`` is chosen by
    :func:`_compute_scale` to cover every input's decimal precision, so the
    rounded result equals the exact scaled value.
    """
    return int(round(value * scale))


def convert_allocation_to_cpsat(
    request: SolveBudgetAllocationRequest,
) -> ORToolsSolveRequest:
    """Compile a budget allocation request into a core CP-SAT solve request.

    Scales floats → ints with a factor computed from the input precision so
    the core sees only integers without truncation loss.
    """
    _scale = _compute_scale(request)
    variables: list[ORToolsVariable] = []
    constraints: list[ORToolsConstraint] = []

    # --- Binary selection variables ----------------------------------------
    for item in request.items:
        variables.append(
            ORToolsVariable(
                id=f"select_{item.id}",
                domain="bool",
                metadata={
                    "item_id": item.id,
                    "cost": item.cost,
                    "value": item.value,
                },
            )
        )

    # --- Budget constraints ------------------------------------------------
    for budget in request.budgets:
        terms = []
        for item in request.items:
            if budget.resource in ("money", "cost"):
                item_cost = item.cost
            elif budget.resource in item.resources_required:
                item_cost = item.resources_required[budget.resource]
            else:
                item_cost = 0

            if item_cost > 0:
                coef = _scaled(item_cost, _scale)
                terms.append(ORToolsLinearTerm(var=f"select_{item.id}", coef=coef))

        if terms:
            limit = _scaled(budget.limit, _scale)
            constraints.append(
                ORToolsConstraint(
                    id=f"budget_{budget.resource}",
                    kind="linear",
                    params=ORToolsLinearParams(terms=terms, sense="<=", rhs=limit),
                    metadata={
                        "description": (
                            f"Budget constraint for {budget.resource} "
                            f"(limit: {budget.limit})"
                        )
                    },
                )
            )

    # --- Dependency constraints (implication) -------------------------------
    for item in request.items:
        for dep_id in item.dependencies:
            then_id = f"then_select_{dep_id}_for_{item.id}"
            constraints.append(
                ORToolsConstraint(
                    id=then_id,
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var=f"select_{dep_id}", coef=1)],
                        sense="==",
                        rhs=1,
                    ),
                    metadata={"description": f"Dependency target: select {dep_id}"},
                )
            )
            constraints.append(
                ORToolsConstraint(
                    id=f"dependency_{item.id}_requires_{dep_id}",
                    kind="implication",
                    params=ORToolsImplicationParams(
                        if_var=f"select_{item.id}",
                        then_constraint_id=then_id,
                    ),
                    metadata={"description": f"Item {item.id} requires {dep_id}"},
                )
            )

    # --- Conflict constraints -----------------------------------------------
    seen: set[tuple[str, str]] = set()
    for item in request.items:
        for conflict_id in item.conflicts:
            pair: tuple[str, str] = (
                (item.id, conflict_id)
                if item.id < conflict_id
                else (conflict_id, item.id)
            )
            if pair in seen:
                continue
            seen.add(pair)
            constraints.append(
                ORToolsConstraint(
                    id=f"conflict_{item.id}_vs_{conflict_id}",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[
                            ORToolsLinearTerm(var=f"select_{item.id}", coef=1),
                            ORToolsLinearTerm(var=f"select_{conflict_id}", coef=1),
                        ],
                        sense="<=",
                        rhs=1,
                    ),
                    metadata={"description": f"Items {item.id} and {conflict_id} conflict"},
                )
            )

    # --- Min/max value thresholds -------------------------------------------
    if request.min_value_threshold is not None:
        value_terms = [
            ORToolsLinearTerm(var=f"select_{item.id}", coef=_scaled(item.value, _scale))
            for item in request.items
        ]
        constraints.append(
            ORToolsConstraint(
                id="min_value_threshold",
                kind="linear",
                params=ORToolsLinearParams(
                    terms=value_terms,
                    sense=">=",
                    rhs=_scaled(request.min_value_threshold, _scale),
                ),
                metadata={"description": f"Minimum value threshold: {request.min_value_threshold}"},
            )
        )

    if request.max_cost_threshold is not None:
        cost_terms = [
            ORToolsLinearTerm(var=f"select_{item.id}", coef=_scaled(item.cost, _scale))
            for item in request.items
        ]
        constraints.append(
            ORToolsConstraint(
                id="max_cost_threshold",
                kind="linear",
                params=ORToolsLinearParams(
                    terms=cost_terms,
                    sense="<=",
                    rhs=_scaled(request.max_cost_threshold, _scale),
                ),
                metadata={"description": f"Maximum cost threshold: {request.max_cost_threshold}"},
            )
        )

    # --- Min/max item count constraints -------------------------------------
    if request.min_items is not None:
        count_terms = [ORToolsLinearTerm(var=f"select_{item.id}", coef=1) for item in request.items]
        constraints.append(
            ORToolsConstraint(
                id="min_items",
                kind="linear",
                params=ORToolsLinearParams(terms=count_terms, sense=">=", rhs=request.min_items),
                metadata={"description": f"Minimum items: {request.min_items}"},
            )
        )

    if request.max_items is not None:
        count_terms = [ORToolsLinearTerm(var=f"select_{item.id}", coef=1) for item in request.items]
        constraints.append(
            ORToolsConstraint(
                id="max_items",
                kind="linear",
                params=ORToolsLinearParams(terms=count_terms, sense="<=", rhs=request.max_items),
                metadata={"description": f"Maximum items: {request.max_items}"},
            )
        )

    # --- Objective ----------------------------------------------------------
    objective: ORToolsObjective | None = None
    if request.objective == AllocationObjective.MAXIMIZE_VALUE:
        value_terms = [
            ORToolsLinearTerm(var=f"select_{item.id}", coef=_scaled(item.value, _scale))
            for item in request.items
        ]
        objective = ORToolsObjective(sense="max", terms=value_terms)
    elif request.objective == AllocationObjective.MAXIMIZE_COUNT:
        count_terms = [ORToolsLinearTerm(var=f"select_{item.id}", coef=1) for item in request.items]
        objective = ORToolsObjective(sense="max", terms=count_terms)
    elif request.objective == AllocationObjective.MINIMIZE_COST:
        cost_terms = [
            ORToolsLinearTerm(var=f"select_{item.id}", coef=_scaled(item.cost, _scale))
            for item in request.items
        ]
        objective = ORToolsObjective(sense="min", terms=cost_terms)

    return ORToolsSolveRequest(
        mode="optimize",
        variables=variables,
        constraints=constraints,
        objective=objective,
        search=ORToolsSearchConfig(timeout_ms=request.timeout_ms),
    )


def convert_cpsat_to_allocation_response(
    result: ORToolsSolveResult,
    original_request: SolveBudgetAllocationRequest,
) -> SolveBudgetAllocationResponse:
    """Convert a core CP-SAT result back to the allocation domain."""
    non_solution: tuple[SolverStatus, ...] = ("infeasible", "timeout_no_solution", "error")

    if result.status in non_solution:
        return SolveBudgetAllocationResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=AllocationExplanation(summary=f"Problem is {result.status}"),
        )

    if not result.solutions:
        return SolveBudgetAllocationResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=AllocationExplanation(summary="No solution available"),
        )

    solution = result.solutions[0]
    var_values: dict[str, int] = {v.id: v.value for v in solution.variables}

    selected_items: list[str] = []
    for var_id, val in var_values.items():
        if val == 1 and var_id.startswith("select_"):
            selected_items.append(var_id[len("select_"):])

    item_map: dict[str, Item] = {item.id: item for item in original_request.items}

    total_cost = sum(item_map[iid].cost for iid in selected_items)
    total_value = sum(item_map[iid].value for iid in selected_items)

    resource_usage: dict[str, float] = {}
    for iid in selected_items:
        item = item_map[iid]
        for resource, amount in item.resources_required.items():
            resource_usage[resource] = resource_usage.get(resource, 0.0) + amount

    primary_resource: str | None = None
    for budget in original_request.budgets:
        if budget.resource in ("money", "cost"):
            primary_resource = budget.resource
            resource_usage[primary_resource] = total_cost
            break

    resource_slack: dict[str, float] = {}
    for budget in original_request.budgets:
        used = resource_usage.get(budget.resource, 0.0)
        resource_slack[budget.resource] = budget.limit - used

    # Build explanation
    summary_parts: list[str] = []
    if result.status == "optimal":
        summary_parts.append(
            f"Optimal selection: {len(selected_items)} items with total value {total_value:.2f}"
        )
    elif result.status in ("feasible", "timeout_best"):
        summary_parts.append(
            f"Feasible selection: {len(selected_items)} items with total value {total_value:.2f}"
        )
        if result.optimality_gap is not None:
            summary_parts.append(f"(gap: {result.optimality_gap:.2f}%)")
    else:
        summary_parts.append(f"Selection: {len(selected_items)} items")

    summary_parts.append(f"under budget of {total_cost:.2f}")

    binding_constraints: list[str] = []
    for budget in original_request.budgets:
        slack = resource_slack.get(budget.resource, 0.0)
        if slack < 0.01:
            binding_constraints.append(f"Budget '{budget.resource}' fully utilized")
        elif slack > budget.limit * 0.2:
            binding_constraints.append(
                f"Budget '{budget.resource}' has {slack:.2f} slack "
                f"({slack / budget.limit * 100:.1f}%)"
            )

    return SolveBudgetAllocationResponse(
        status=result.status,
        selected_items=selected_items,
        total_cost=total_cost,
        total_value=total_value,
        resource_usage=resource_usage,
        resource_slack=resource_slack,
        solve_time_ms=result.solve_time_ms,
        optimality_gap=result.optimality_gap,
        explanation=AllocationExplanation(
            summary=" ".join(summary_parts),
            binding_constraints=binding_constraints,
        ),
    )


def solve_budget_allocation(
    request: SolveBudgetAllocationRequest,
) -> SolveBudgetAllocationResponse:
    """Solve a budget allocation problem.

    Converts the high-level request to CP-SAT, solves, and converts back.
    """
    cpsat_request = convert_allocation_to_cpsat(request)
    result = solve_model(cpsat_request)
    return convert_cpsat_to_allocation_response(result, request)
