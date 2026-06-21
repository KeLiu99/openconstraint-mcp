"""Assignment problem converter.

Portions adapted from chuk-mcp-solver (Apache-2.0).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from openconstraint_mcp.cpsat.core import solve_model
from openconstraint_mcp.cpsat.schemas import (
    ORToolsConstraint,
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


class Agent(BaseModel):
    """An agent that can be assigned tasks."""

    id: str = Field(..., min_length=1)
    capacity: int = Field(default=1, ge=0)
    skills: list[str] = Field(default_factory=list)
    cost_multiplier: float = Field(default=1.0, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssignmentTask(BaseModel):
    """A task to be assigned to an agent."""

    id: str = Field(..., min_length=1)
    required_skills: list[str] = Field(default_factory=list)
    duration: int = Field(default=1, ge=0)
    priority: int = Field(default=1, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssignmentObjective(StrEnum):
    """Assignment optimization objectives."""

    MINIMIZE_COST = "minimize_cost"
    MAXIMIZE_ASSIGNMENTS = "maximize_assignments"
    BALANCE_LOAD = "balance_load"


class SolveAssignmentProblemRequest(BaseModel):
    """High-level assignment problem definition."""

    agents: list[Agent] = Field(..., min_length=1)
    tasks: list[AssignmentTask] = Field(..., min_length=1)
    cost_matrix: list[list[float]] | None = None
    objective: AssignmentObjective = AssignmentObjective.MINIMIZE_COST
    force_assign_all: bool = True
    timeout_ms: int = Field(default=60_000, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Assignment(BaseModel):
    """A task assigned to an agent."""

    task_id: str
    agent_id: str
    cost: float = Field(..., ge=0.0)


class AssignmentExplanation(BaseModel):
    """Human-readable explanation of the assignment solution."""

    summary: str
    overloaded_agents: list[str] = Field(default_factory=list)
    underutilized_agents: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class SolveAssignmentProblemResponse(BaseModel):
    """Assignment solution with domain-specific details."""

    status: SolverStatus
    assignments: list[Assignment] = Field(default_factory=list)
    unassigned_tasks: list[str] = Field(default_factory=list)
    agent_load: dict[str, int] = Field(default_factory=dict)
    total_cost: float = Field(default=0.0, ge=0.0)
    solve_time_ms: int = Field(default=0, ge=0)
    optimality_gap: float | None = None
    explanation: AssignmentExplanation


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

_SCALE = 100


def _build_cost_matrix(
    request: SolveAssignmentProblemRequest,
) -> tuple[list[list[float]], set[tuple[int, int]]]:
    """Build cost matrix and identify incompatible (task, agent) pairs.

    Skill incompatibility is always checked, even when a custom ``cost_matrix``
    is supplied.
    """
    n_tasks = len(request.tasks)
    n_agents = len(request.agents)

    incompatible: set[tuple[int, int]] = set()
    for task_idx, task in enumerate(request.tasks):
        if not task.required_skills:
            continue
        for agent_idx, agent in enumerate(request.agents):
            has_all = all(s in agent.skills for s in task.required_skills)
            if not has_all:
                incompatible.add((task_idx, agent_idx))

    if request.cost_matrix is not None:
        return request.cost_matrix, incompatible

    matrix: list[list[float]] = [[0.0] * n_agents for _ in range(n_tasks)]

    for task_idx, task in enumerate(request.tasks):
        for agent_idx, agent in enumerate(request.agents):
            if (task_idx, agent_idx) in incompatible:
                continue
            matrix[task_idx][agent_idx] = agent.cost_multiplier * task.duration

    return matrix, incompatible


def convert_assignment_to_cpsat(
    request: SolveAssignmentProblemRequest,
) -> ORToolsSolveRequest:
    """Compile an assignment request into a core CP-SAT solve request."""
    n_tasks = len(request.tasks)
    n_agents = len(request.agents)
    cost_matrix, incompatible = _build_cost_matrix(request)

    variables: list[ORToolsVariable] = []
    constraints: list[ORToolsConstraint] = []

    # --- Binary assignment variables ---------------------------------------
    for task_idx, task in enumerate(request.tasks):
        for agent_idx, agent in enumerate(request.agents):
            var_id = f"assign_t{task_idx}_a{agent_idx}"
            variables.append(
                ORToolsVariable(
                    id=var_id,
                    domain="bool",
                    metadata={
                        "task_id": task.id,
                        "agent_id": agent.id,
                        "cost": cost_matrix[task_idx][agent_idx],
                    },
                )
            )

    # --- Forbid incompatible pairs -----------------------------------------
    for task_idx, agent_idx in incompatible:
        task_id = request.tasks[task_idx].id
        agent_id = request.agents[agent_idx].id
        var_id = f"assign_t{task_idx}_a{agent_idx}"
        constraints.append(
            ORToolsConstraint(
                id=f"forbid_{task_id}_to_{agent_id}",
                kind="linear",
                params=ORToolsLinearParams(
                    terms=[ORToolsLinearTerm(var=var_id, coef=1)],
                    sense="==",
                    rhs=0,
                ),
                metadata={
                    "description": (
                        f"Task {task_id} incompatible with agent {agent_id} (missing skills)"
                    )
                },
            )
        )

    # --- Per-task constraints (each task to at most/exactly one agent) -----
    for task_idx, task in enumerate(request.tasks):
        terms = [
            ORToolsLinearTerm(var=f"assign_t{task_idx}_a{agent_idx}", coef=1)
            for agent_idx in range(n_agents)
        ]
        sense: Literal["==", "<="] = "==" if request.force_assign_all else "<="
        constraints.append(
            ORToolsConstraint(
                id=f"task_{task.id}_assignment",
                kind="linear",
                params=ORToolsLinearParams(terms=terms, sense=sense, rhs=1),
                metadata={"description": f"Task {task.id} assigned to at most one agent"},
            )
        )

    # --- Per-agent capacity constraints ------------------------------------
    for agent_idx, agent in enumerate(request.agents):
        terms = [
            ORToolsLinearTerm(var=f"assign_t{task_idx}_a{agent_idx}", coef=1)
            for task_idx in range(n_tasks)
        ]
        constraints.append(
            ORToolsConstraint(
                id=f"agent_{agent.id}_capacity",
                kind="linear",
                params=ORToolsLinearParams(terms=terms, sense="<=", rhs=agent.capacity),
                metadata={
                    "description": (f"Agent {agent.id} can handle at most {agent.capacity} tasks")
                },
            )
        )

    # --- Objective ----------------------------------------------------------
    objective: ORToolsObjective | None = None

    if request.objective == AssignmentObjective.MINIMIZE_COST:
        cost_terms = []
        for task_idx in range(n_tasks):
            for agent_idx in range(n_agents):
                if (task_idx, agent_idx) not in incompatible:
                    # round (not truncate) so float error like 4.35 * 100 ==
                    # 434.999… cannot drop a unit from the cost coefficient.
                    cost = int(round(cost_matrix[task_idx][agent_idx] * _SCALE))
                    cost_terms.append(
                        ORToolsLinearTerm(
                            var=f"assign_t{task_idx}_a{agent_idx}",
                            coef=cost,
                        )
                    )
        # When every pair is incompatible, use a dummy term so the
        # objective is syntactically valid (the model is infeasible anyway).
        if not cost_terms:
            cost_terms.append(ORToolsLinearTerm(var="assign_t0_a0", coef=0))
        objective = ORToolsObjective(sense="min", terms=cost_terms)

    elif request.objective == AssignmentObjective.MAXIMIZE_ASSIGNMENTS:
        assign_terms = [
            ORToolsLinearTerm(var=f"assign_t{task_idx}_a{agent_idx}", coef=1)
            for task_idx in range(n_tasks)
            for agent_idx in range(n_agents)
        ]
        objective = ORToolsObjective(sense="max", terms=assign_terms)

    elif request.objective == AssignmentObjective.BALANCE_LOAD:
        variables.append(
            ORToolsVariable(
                id="max_load",
                domain="integer",
                lower=0,
                upper=n_tasks,
                metadata={"description": "Maximum load across all agents"},
            )
        )
        for agent_idx, agent in enumerate(request.agents):
            load_terms = [
                ORToolsLinearTerm(var=f"assign_t{task_idx}_a{agent_idx}", coef=1)
                for task_idx in range(n_tasks)
            ]
            load_terms.append(ORToolsLinearTerm(var="max_load", coef=-1))
            constraints.append(
                ORToolsConstraint(
                    id=f"balance_agent_{agent.id}",
                    kind="linear",
                    params=ORToolsLinearParams(terms=load_terms, sense="<=", rhs=0),
                    metadata={"description": f"Agent {agent.id} load <= max_load"},
                )
            )
        objective = ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="max_load", coef=1)])

    return ORToolsSolveRequest(
        mode="optimize",
        variables=variables,
        constraints=constraints,
        objective=objective,
        search=ORToolsSearchConfig(timeout_ms=request.timeout_ms),
    )


def convert_cpsat_to_assignment_response(
    result: ORToolsSolveResult,
    original_request: SolveAssignmentProblemRequest,
) -> SolveAssignmentProblemResponse:
    """Convert a core CP-SAT result back to the assignment domain."""
    non_solution: tuple[SolverStatus, ...] = (
        "infeasible",
        "timeout_no_solution",
        "error",
    )

    if result.status in non_solution:
        return SolveAssignmentProblemResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=AssignmentExplanation(summary=f"Problem is {result.status}"),
        )

    if not result.solutions:
        return SolveAssignmentProblemResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=AssignmentExplanation(summary="No solution available"),
        )

    solution = result.solutions[0]
    var_values: dict[str, int] = {v.id: v.value for v in solution.variables}

    cost_matrix, _ = _build_cost_matrix(original_request)

    assignments: list[Assignment] = []
    agent_load: dict[str, int] = {a.id: 0 for a in original_request.agents}
    unassigned_tasks: list[str] = []
    total_cost = 0.0

    for task_idx, task in enumerate(original_request.tasks):
        assigned = False
        for agent_idx, agent in enumerate(original_request.agents):
            var_id = f"assign_t{task_idx}_a{agent_idx}"
            if var_values.get(var_id) == 1:
                cost = cost_matrix[task_idx][agent_idx]
                assignments.append(Assignment(task_id=task.id, agent_id=agent.id, cost=cost))
                agent_load[agent.id] += 1
                total_cost += cost
                assigned = True
                break
        if not assigned:
            unassigned_tasks.append(task.id)

    # --- Explanation --------------------------------------------------------
    num_agents_used = sum(1 for v in agent_load.values() if v > 0)
    summary_parts: list[str] = []

    if result.status == "optimal":
        summary_parts.append(
            f"Optimal assignment: {len(assignments)} tasks assigned to {num_agents_used} agents"
        )
    elif result.status in ("feasible", "timeout_best"):
        summary_parts.append(
            f"Feasible assignment: {len(assignments)} tasks assigned to {num_agents_used} agents"
        )
        if result.optimality_gap is not None:
            summary_parts.append(f"(gap: {result.optimality_gap:.2f}%)")
    else:
        summary_parts.append(f"Assignment: {len(assignments)} tasks assigned")

    if unassigned_tasks:
        summary_parts.append(f", {len(unassigned_tasks)} unassigned")

    overloaded: list[str] = []
    underutilized: list[str] = []
    loads = list(agent_load.values())
    if loads:
        avg_load = sum(loads) / len(loads)
        for agent in original_request.agents:
            load = agent_load[agent.id]
            if load > avg_load * 1.5:
                overloaded.append(agent.id)
            elif load < avg_load * 0.5 and agent.capacity > 0:
                underutilized.append(agent.id)

    return SolveAssignmentProblemResponse(
        status=result.status,
        assignments=assignments,
        unassigned_tasks=unassigned_tasks,
        agent_load=agent_load,
        total_cost=total_cost,
        solve_time_ms=result.solve_time_ms,
        optimality_gap=result.optimality_gap,
        explanation=AssignmentExplanation(
            summary=" ".join(summary_parts),
            overloaded_agents=overloaded,
            underutilized_agents=underutilized,
        ),
    )


def solve_assignment_problem(
    request: SolveAssignmentProblemRequest,
) -> SolveAssignmentProblemResponse:
    """Solve an assignment problem.

    Converts the high-level request to CP-SAT, solves, and converts back.
    """
    cpsat_request = convert_assignment_to_cpsat(request)
    result = solve_model(cpsat_request)
    return convert_cpsat_to_assignment_response(result, request)
