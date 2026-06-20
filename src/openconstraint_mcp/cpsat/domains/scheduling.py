"""Scheduling problem converter.

Portions adapted from chuk-mcp-solver (Apache-2.0).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from openconstraint_mcp.cpsat.core import solve_model
from openconstraint_mcp.cpsat.schemas import (
    ORToolsConstraint,
    ORToolsCumulativeParams,
    ORToolsLinearParams,
    ORToolsLinearTerm,
    ORToolsNoOverlapParams,
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


class Task(BaseModel):
    """A task to be scheduled with duration, dependencies, and resources."""

    id: str = Field(..., min_length=1)
    duration: int = Field(..., ge=0)
    resources_required: dict[str, int] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    earliest_start: int | None = Field(default=None, ge=0)
    deadline: int | None = Field(default=None, ge=0)
    priority: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Resource(BaseModel):
    """A resource with a capacity limit."""

    id: str = Field(..., min_length=1)
    capacity: int = Field(..., ge=0)
    cost_per_unit: float = Field(default=0.0, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SchedulingObjective(StrEnum):
    """Scheduling optimization objectives."""

    MINIMIZE_MAKESPAN = "minimize_makespan"
    MINIMIZE_COST = "minimize_cost"
    MINIMIZE_LATENESS = "minimize_lateness"


class SolveSchedulingProblemRequest(BaseModel):
    """High-level scheduling problem definition."""

    tasks: list[Task] = Field(..., min_length=1)
    resources: list[Resource] = Field(default_factory=list)
    objective: SchedulingObjective = SchedulingObjective.MINIMIZE_MAKESPAN
    max_makespan: int | None = Field(default=None, ge=1)
    no_overlap_tasks: list[list[str]] = Field(default_factory=list)
    timeout_ms: int = Field(default=60_000, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskAssignment(BaseModel):
    """A scheduled task with timing information."""

    task_id: str
    start_time: int = Field(..., ge=0)
    end_time: int = Field(..., ge=0)
    resources_used: dict[str, int] = Field(default_factory=dict)
    on_critical_path: bool = False
    slack: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SchedulingExplanation(BaseModel):
    """Human-readable explanation of the schedule."""

    summary: str
    bottlenecks: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    binding_constraints: list[str] = Field(default_factory=list)


class SolveSchedulingProblemResponse(BaseModel):
    """Scheduling solution with domain-specific details."""

    status: SolverStatus
    makespan: int | None = Field(default=None, ge=0)
    total_cost: float | None = Field(default=None, ge=0.0)
    schedule: list[TaskAssignment] = Field(default_factory=list)
    resource_utilization: list[dict[str, Any]] = Field(default_factory=list)
    critical_path: list[str] = Field(default_factory=list)
    solve_time_ms: int = Field(default=0, ge=0)
    optimality_gap: float | None = None
    explanation: SchedulingExplanation


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------


def convert_scheduling_to_cpsat(
    request: SolveSchedulingProblemRequest,
) -> ORToolsSolveRequest:
    """Compile a scheduling request into a core CP-SAT solve request.

    Currently only ``minimize_makespan`` is supported.
    """
    variables: list[ORToolsVariable] = []
    constraints: list[ORToolsConstraint] = []

    # --- Horizon ------------------------------------------------------------
    if request.max_makespan is not None:
        horizon = request.max_makespan
    else:
        horizon = sum(task.duration for task in request.tasks)
        for task in request.tasks:
            if task.earliest_start is not None:
                horizon = max(horizon, task.earliest_start + task.duration)

    # --- Start/end time variables -------------------------------------------
    for task in request.tasks:
        start_lower = task.earliest_start if task.earliest_start is not None else 0
        start_upper = task.deadline if task.deadline is not None else horizon

        variables.append(
            ORToolsVariable(
                id=f"start_{task.id}",
                domain="integer",
                lower=start_lower,
                upper=start_upper,
                metadata={"task": task.id, "type": "start_time"},
            )
        )

        end_lower = start_lower + task.duration
        variables.append(
            ORToolsVariable(
                id=f"end_{task.id}",
                domain="integer",
                lower=end_lower,
                upper=horizon,
                metadata={"task": task.id, "type": "end_time"},
            )
        )

    # --- Duration: end = start + duration -----------------------------------
    for task in request.tasks:
        constraints.append(
            ORToolsConstraint(
                id=f"duration_{task.id}",
                kind="linear",
                params=ORToolsLinearParams(
                    terms=[
                        ORToolsLinearTerm(var=f"end_{task.id}", coef=1),
                        ORToolsLinearTerm(var=f"start_{task.id}", coef=-1),
                    ],
                    sense="==",
                    rhs=task.duration,
                ),
                metadata={
                    "description": f"Task {task.id} duration = {task.duration}"
                },
            )
        )

    # --- Precedence: start_target >= end_dep --------------------------------
    for task in request.tasks:
        for dep_id in task.dependencies:
            constraints.append(
                ORToolsConstraint(
                    id=f"precedence_{dep_id}_to_{task.id}",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[
                            ORToolsLinearTerm(var=f"start_{task.id}", coef=1),
                            ORToolsLinearTerm(var=f"end_{dep_id}", coef=-1),
                        ],
                        sense=">=",
                        rhs=0,
                    ),
                    metadata={
                        "description": (
                            f"Task {task.id} must start after "
                            f"{dep_id} completes"
                        )
                    },
                )
            )

    # --- Deadlines ----------------------------------------------------------
    for task in request.tasks:
        if task.deadline is not None:
            constraints.append(
                ORToolsConstraint(
                    id=f"deadline_{task.id}",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[ORToolsLinearTerm(var=f"end_{task.id}", coef=1)],
                        sense="<=",
                        rhs=task.deadline,
                    ),
                    metadata={
                        "description": (
                            f"Task {task.id} must complete by "
                            f"{task.deadline}"
                        )
                    },
                )
            )

    # --- Cumulative resource capacity ---------------------------------------
    for resource in request.resources:
        start_vars: list[str] = []
        duration_vals: list[int] = []
        demand_vals: list[int] = []

        for task in request.tasks:
            if resource.id in task.resources_required:
                start_vars.append(f"start_{task.id}")
                duration_vals.append(task.duration)
                demand_vals.append(task.resources_required[resource.id])

        if start_vars:
            constraints.append(
                ORToolsConstraint(
                    id=f"capacity_{resource.id}",
                    kind="cumulative",
                    params=ORToolsCumulativeParams(
                        start_vars=start_vars,
                        duration_vars=duration_vals,
                        demand_vars=demand_vals,
                        capacity=resource.capacity,
                    ),
                    metadata={
                        "description": (
                            f"Resource {resource.id} capacity limit "
                            f"({resource.capacity})"
                        )
                    },
                )
            )

    # --- No-overlap groups --------------------------------------------------
    for group in request.no_overlap_tasks:
        if len(group) >= 2:
            durations = [
                next(t.duration for t in request.tasks if t.id == tid)
                for tid in group
            ]
            constraints.append(
                ORToolsConstraint(
                    id=f"no_overlap_{'_'.join(group)}",
                    kind="no_overlap",
                    params=ORToolsNoOverlapParams(
                        start_vars=[f"start_{tid}" for tid in group],
                        duration_vars=durations,
                    ),
                    metadata={
                        "description": (
                            f"Tasks {', '.join(group)} cannot overlap"
                        )
                    },
                )
            )

    # --- Objective ----------------------------------------------------------
    objective: ORToolsObjective | None = None

    if request.objective == SchedulingObjective.MINIMIZE_MAKESPAN:
        variables.append(
            ORToolsVariable(
                id="makespan",
                domain="integer",
                lower=0,
                upper=horizon,
                metadata={"type": "makespan"},
            )
        )
        for task in request.tasks:
            constraints.append(
                ORToolsConstraint(
                    id=f"makespan_bounds_{task.id}",
                    kind="linear",
                    params=ORToolsLinearParams(
                        terms=[
                            ORToolsLinearTerm(var="makespan", coef=1),
                            ORToolsLinearTerm(var=f"end_{task.id}", coef=-1),
                        ],
                        sense=">=",
                        rhs=0,
                    ),
                    metadata={"description": "Makespan lower bound"},
                )
            )
        objective = ORToolsObjective(
            sense="min",
            terms=[ORToolsLinearTerm(var="makespan", coef=1)],
        )
    elif request.objective == SchedulingObjective.MINIMIZE_COST:
        raise ValueError(
            "scheduling objective 'minimize_cost' is not yet supported"
        )
    elif request.objective == SchedulingObjective.MINIMIZE_LATENESS:
        raise ValueError(
            "scheduling objective 'minimize_lateness' is not yet supported"
        )

    return ORToolsSolveRequest(
        mode="optimize" if objective else "satisfy",
        variables=variables,
        constraints=constraints,
        objective=objective,
        search=ORToolsSearchConfig(timeout_ms=request.timeout_ms),
    )


def convert_cpsat_to_scheduling_response(
    result: ORToolsSolveResult,
    original_request: SolveSchedulingProblemRequest,
) -> SolveSchedulingProblemResponse:
    """Convert a core CP-SAT result back to the scheduling domain."""
    non_solution: tuple[SolverStatus, ...] = (
        "infeasible",
        "timeout_no_solution",
        "error",
    )

    if result.status in non_solution:
        return SolveSchedulingProblemResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=SchedulingExplanation(
                summary=f"Problem is {result.status}"
            ),
        )

    if not result.solutions:
        return SolveSchedulingProblemResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=SchedulingExplanation(
                summary="No solution available"
            ),
        )

    solution = result.solutions[0]
    var_values: dict[str, int] = {v.id: v.value for v in solution.variables}

    schedule: list[TaskAssignment] = []
    for task in original_request.tasks:
        start_time = var_values.get(f"start_{task.id}", 0)
        end_time = var_values.get(
            f"end_{task.id}", start_time + task.duration
        )
        schedule.append(
            TaskAssignment(
                task_id=task.id,
                start_time=start_time,
                end_time=end_time,
                resources_used=task.resources_required,
                metadata=task.metadata,
            )
        )

    makespan_val: int | None = var_values.get("makespan")
    if makespan_val is None:
        makespan_val = max((t.end_time for t in schedule), default=0)

    summary_parts: list[str] = []
    if result.status == "optimal":
        summary_parts.append(
            f"Found optimal schedule completing in {makespan_val} time units"
        )
    elif result.status in ("feasible", "timeout_best"):
        summary_parts.append(
            f"Found feasible schedule completing in {makespan_val} time units"
        )
        if result.optimality_gap is not None:
            summary_parts.append(
                f"(gap: {result.optimality_gap:.2f}%)"
            )
    else:
        summary_parts.append("Schedule found")

    summary_parts.append(f"with {len(schedule)} tasks")
    if original_request.resources:
        summary_parts.append(
            f"using {len(original_request.resources)} resources"
        )

    return SolveSchedulingProblemResponse(
        status=result.status,
        makespan=makespan_val,
        schedule=schedule,
        solve_time_ms=result.solve_time_ms,
        optimality_gap=result.optimality_gap,
        explanation=SchedulingExplanation(
            summary=" ".join(summary_parts)
        ),
    )


def solve_scheduling_problem(
    request: SolveSchedulingProblemRequest,
) -> SolveSchedulingProblemResponse:
    """Solve a scheduling problem.

    Converts the high-level request to CP-SAT, solves, and converts back.
    """
    cpsat_request = convert_scheduling_to_cpsat(request)
    result = solve_model(cpsat_request)
    return convert_cpsat_to_scheduling_response(result, request)
