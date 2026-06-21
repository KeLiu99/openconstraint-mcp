"""Tests for the assignment domain tool."""

import pytest
from pydantic import ValidationError

from openconstraint_mcp.cpsat.domains.assignment import (
    Agent,
    AssignmentObjective,
    AssignmentTask,
    SolveAssignmentProblemRequest,
    convert_assignment_to_cpsat,
    solve_assignment_problem,
)


def test_minimize_cost_assigns_all():
    """A balanced instance assigns all tasks at minimum cost."""
    agents = [Agent(id="A", skills=["code"], cost_multiplier=1.0, capacity=2)]
    tasks = [
        AssignmentTask(id="T1", required_skills=["code"], duration=1),
        AssignmentTask(id="T2", required_skills=["code"], duration=2),
    ]
    request = SolveAssignmentProblemRequest(
        agents=agents, tasks=tasks, objective=AssignmentObjective.MINIMIZE_COST
    )

    response = solve_assignment_problem(request)

    assert response.status in ("optimal", "feasible")
    assert len(response.assignments) == 2
    assert response.unassigned_tasks == []


def test_skill_mismatch_leaves_unassigned():
    """A task with skills no agent has is left unassigned (with force_assign_all=False)."""
    agents = [Agent(id="A", skills=["code"])]
    tasks = [
        AssignmentTask(id="T1", required_skills=["design"]),
    ]
    request = SolveAssignmentProblemRequest(agents=agents, tasks=tasks, force_assign_all=False)

    response = solve_assignment_problem(request)

    assert response.status == "optimal"
    assert len(response.assignments) == 0
    assert response.unassigned_tasks == ["T1"]


def test_skill_mismatch_infeasible_with_force():
    """Skill mismatch is infeasible when force_assign_all=True."""
    agents = [Agent(id="A", skills=["code"])]
    tasks = [
        AssignmentTask(id="T1", required_skills=["design"]),
    ]
    request = SolveAssignmentProblemRequest(agents=agents, tasks=tasks, force_assign_all=True)

    response = solve_assignment_problem(request)

    assert response.status == "infeasible"


def test_capacity_respected():
    """An agent's capacity cap is respected."""
    agents = [Agent(id="A", capacity=1)]
    tasks = [
        AssignmentTask(id="T1", duration=1),
        AssignmentTask(id="T2", duration=1),
    ]
    request = SolveAssignmentProblemRequest(agents=agents, tasks=tasks, force_assign_all=False)

    response = solve_assignment_problem(request)

    assert response.status in ("optimal", "feasible")
    assert len(response.assignments) <= 1
    assert response.agent_load.get("A", 0) <= 1


def test_maximize_assignments():
    """MAXIMIZE_ASSIGNMENTS picks as many tasks as possible."""
    agents = [Agent(id="A", capacity=2)]
    tasks = [
        AssignmentTask(id="T1"),
        AssignmentTask(id="T2"),
        AssignmentTask(id="T3"),
        AssignmentTask(id="T4"),
    ]
    request = SolveAssignmentProblemRequest(
        agents=agents,
        tasks=tasks,
        objective=AssignmentObjective.MAXIMIZE_ASSIGNMENTS,
        force_assign_all=False,
    )

    response = solve_assignment_problem(request)

    assert response.status in ("optimal", "feasible")
    assert len(response.assignments) == 2  # agent capacity is 2


def test_balance_load():
    """BALANCE_LOAD distributes tasks evenly across agents."""
    agents = [
        Agent(id="A", capacity=5),
        Agent(id="B", capacity=5),
    ]
    tasks = [
        AssignmentTask(id="T1"),
        AssignmentTask(id="T2"),
        AssignmentTask(id="T3"),
        AssignmentTask(id="T4"),
    ]
    request = SolveAssignmentProblemRequest(
        agents=agents,
        tasks=tasks,
        objective=AssignmentObjective.BALANCE_LOAD,
    )

    response = solve_assignment_problem(request)

    assert response.status in ("optimal", "feasible")
    assert len(response.assignments) == 4
    a_load = response.agent_load.get("A", 0)
    b_load = response.agent_load.get("B", 0)
    assert abs(a_load - b_load) <= 1  # evenly distributed


def test_convert_assignment_to_cpsat_structure():
    """The converter produces a well-formed ORToolsSolveRequest."""
    agents = [Agent(id="A", capacity=1)]
    tasks = [AssignmentTask(id="T1", duration=1)]

    request = SolveAssignmentProblemRequest(agents=agents, tasks=tasks)
    cpsat_request = convert_assignment_to_cpsat(request)

    assert cpsat_request.mode == "optimize"
    assert len(cpsat_request.variables) == 1  # 1 task × 1 agent
    assert cpsat_request.variables[0].id == "assign_t0_a0"
    assert cpsat_request.variables[0].domain == "bool"
    assert cpsat_request.objective is not None
    assert not isinstance(cpsat_request.objective, list)


def test_cost_matrix_skill_mismatch_enforced():
    """Custom cost_matrix still enforces skill incompatibilities."""
    agents = [Agent(id="A", skills=["code"]), Agent(id="B", skills=["design"])]
    tasks = [
        AssignmentTask(id="T1", required_skills=["code"]),
        AssignmentTask(id="T2", required_skills=["design"]),
    ]

    request = SolveAssignmentProblemRequest(
        agents=agents,
        tasks=tasks,
        cost_matrix=[
            [1.0, 999.0],  # T1→A cheap, T1→B expensive
            [999.0, 1.0],  # T2→A expensive, T2→B cheap
        ],
        force_assign_all=True,
    )

    response = solve_assignment_problem(request)

    assert response.status in ("optimal", "feasible")
    # T1 must go to A (only A has "code"); T2 must go to B (only B has "design")
    assignments_by_task = {a.task_id: a.agent_id for a in response.assignments}
    assert assignments_by_task["T1"] == "A"
    assert assignments_by_task["T2"] == "B"


def test_cost_matrix_provided():
    """A user-provided cost_matrix is used directly."""
    agents = [Agent(id="A", capacity=1), Agent(id="B", capacity=1)]
    tasks = [AssignmentTask(id="T1", duration=1)]

    request = SolveAssignmentProblemRequest(
        agents=agents,
        tasks=tasks,
        cost_matrix=[[5.0, 3.0]],
        objective=AssignmentObjective.MINIMIZE_COST,
    )

    response = solve_assignment_problem(request)

    assert response.status in ("optimal", "feasible")
    assert len(response.assignments) == 1
    # Should pick agent B (cost 3)
    assert response.assignments[0].agent_id == "B"
    assert response.assignments[0].cost == 3.0


def test_cost_matrix_wrong_row_count_rejected():
    """cost_matrix with fewer rows than tasks is rejected at construction."""
    agents = [Agent(id="A"), Agent(id="B")]
    tasks = [AssignmentTask(id="T1"), AssignmentTask(id="T2")]

    with pytest.raises(ValidationError, match="cost_matrix has 1 rows but there are 2 tasks"):
        SolveAssignmentProblemRequest(
            agents=agents,
            tasks=tasks,
            cost_matrix=[[1.0, 2.0]],  # only 1 row, need 2
        )


def test_cost_matrix_ragged_row_rejected():
    """cost_matrix with a short row is rejected at construction."""
    agents = [Agent(id="A"), Agent(id="B")]
    tasks = [AssignmentTask(id="T1"), AssignmentTask(id="T2")]

    with pytest.raises(ValidationError, match="row 1 has 1 columns"):
        SolveAssignmentProblemRequest(
            agents=agents,
            tasks=tasks,
            cost_matrix=[[1.0, 2.0], [3.0]],  # row 1 missing agent B
        )
