"""Tests for the scheduling domain tool."""

import pytest

from openconstraint_mcp.cpsat.domains.scheduling import (
    Resource,
    SchedulingObjective,
    SolveSchedulingProblemRequest,
    Task,
    convert_scheduling_to_cpsat,
    solve_scheduling_problem,
)


def test_dependency_chain_schedules_in_order():
    """A chain of dependencies preserves precedence order and correct makespan."""
    tasks = [
        Task(id="A", duration=2),
        Task(id="B", duration=3, dependencies=["A"]),
        Task(id="C", duration=1, dependencies=["B"]),
    ]
    request = SolveSchedulingProblemRequest(tasks=tasks)

    response = solve_scheduling_problem(request)

    assert response.status == "optimal"
    assert response.makespan is not None
    assert response.makespan >= 6  # 2+3+1 = 6

    schedule = {t.task_id: t for t in response.schedule}
    assert schedule["A"].end_time <= schedule["B"].start_time
    assert schedule["B"].end_time <= schedule["C"].start_time


def test_resource_capacity_forces_serialization():
    """A resource capacity below concurrent demand forces serial execution."""
    tasks = [
        Task(id="A", duration=2, resources_required={"R": 1}),
        Task(id="B", duration=3, resources_required={"R": 1}),
    ]
    resources = [Resource(id="R", capacity=1)]
    request = SolveSchedulingProblemRequest(tasks=tasks, resources=resources)

    response = solve_scheduling_problem(request)

    assert response.status == "optimal"
    # With capacity 1, tasks cannot overlap
    schedule = {t.task_id: t for t in response.schedule}
    assert schedule["A"].end_time <= schedule["B"].start_time or (
        schedule["B"].end_time <= schedule["A"].start_time
    )
    assert response.makespan is not None
    assert response.makespan >= 5  # 2+3


def test_impossible_deadline_infeasible():
    """A deadline that cannot be met makes the problem infeasible."""
    tasks = [
        Task(id="A", duration=5, deadline=3),
    ]
    request = SolveSchedulingProblemRequest(tasks=tasks)

    response = solve_scheduling_problem(request)

    assert response.status == "infeasible"


def test_minimize_makespan_single_task():
    """A single task has makespan equal to its duration."""
    tasks = [Task(id="A", duration=5)]
    request = SolveSchedulingProblemRequest(tasks=tasks)

    response = solve_scheduling_problem(request)

    assert response.status == "optimal"
    assert response.makespan is not None
    assert response.makespan == 5


def test_earliest_start_respected():
    """A task with earliest_start cannot start before it."""
    tasks = [
        Task(id="A", duration=2, earliest_start=10),
    ]
    request = SolveSchedulingProblemRequest(tasks=tasks)

    response = solve_scheduling_problem(request)

    assert response.status == "optimal"
    schedule = {t.task_id: t for t in response.schedule}
    assert schedule["A"].start_time >= 10


def test_parallel_tasks_without_resource_limits():
    """Tasks without resource constraints can run in parallel."""
    tasks = [
        Task(id="A", duration=5),
        Task(id="B", duration=5),
    ]
    request = SolveSchedulingProblemRequest(tasks=tasks)

    response = solve_scheduling_problem(request)

    assert response.status == "optimal"
    # Without any constraints, they can overlap -> makespan = 5
    assert response.makespan is not None
    assert response.makespan == 5


def test_convert_scheduling_to_cpsat_structure():
    """The converter produces a well-formed ORToolsSolveRequest."""
    tasks = [Task(id="A", duration=3)]
    request = SolveSchedulingProblemRequest(tasks=tasks)

    cpsat_request = convert_scheduling_to_cpsat(request)

    assert cpsat_request.mode == "optimize"
    # start_A, end_A, makespan = 3 variables
    assert len(cpsat_request.variables) == 3
    var_ids = {v.id for v in cpsat_request.variables}
    assert var_ids == {"start_A", "end_A", "makespan"}
    assert cpsat_request.objective is not None
    assert not isinstance(cpsat_request.objective, list)


def test_unsupported_objective_raises():
    """An unsupported objective raises ValueError."""
    tasks = [Task(id="A", duration=1)]

    with pytest.raises(ValueError, match="minimize_cost"):
        solve_scheduling_problem(
            SolveSchedulingProblemRequest(
                tasks=tasks,
                objective=SchedulingObjective.MINIMIZE_COST,
            )
        )

    with pytest.raises(ValueError, match="minimize_lateness"):
        solve_scheduling_problem(
            SolveSchedulingProblemRequest(
                tasks=tasks,
                objective=SchedulingObjective.MINIMIZE_LATENESS,
            )
        )
