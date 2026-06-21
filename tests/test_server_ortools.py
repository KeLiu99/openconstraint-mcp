"""Server-level tests for the solve_ortools_model MCP tool and domain tools."""

from typing import Any

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from openconstraint_mcp.server import create_mcp_server


def _call(mcp, tool_name: str, arguments: dict) -> dict[str, Any]:
    """Synchronous wrapper around the async call_tool."""

    async def _inner() -> dict[str, Any]:
        result = await mcp.call_tool(tool_name, arguments)
        if isinstance(result, CallToolResult):
            assert result.structuredContent is not None
            return result.structuredContent  # type: ignore[return-value]
        return result[1]  # (content_blocks, structured_content)

    return anyio.run(_inner)


@pytest.fixture(name="mcp")
def _mcp():
    return create_mcp_server()


def test_tool_registered(mcp) -> None:
    """All five CP-SAT tools appear in tool list."""
    tools = {t.name for t in mcp._tool_manager.list_tools()}
    assert "solve_ortools_model" in tools
    assert "solve_budget_allocation" in tools
    assert "solve_assignment_problem" in tools
    assert "solve_scheduling_problem" in tools
    assert "solve_routing_problem" in tools


def test_satisfiable(mcp) -> None:
    """A trivial satisfiable model returns satisfied."""
    result = _call(
        mcp,
        "solve_ortools_model",
        {
            "model": {
                "mode": "satisfy",
                "variables": [{"id": "x", "domain": "integer", "lower": 0, "upper": 10}],
            }
        },
    )
    assert result["status"] == "satisfied"
    assert len(result["solutions"]) == 1
    assert 0 <= result["solutions"][0]["variables"][0]["value"] <= 10


def test_conflicting(mcp) -> None:
    """Conflicting constraints return infeasible."""
    result = _call(
        mcp,
        "solve_ortools_model",
        {
            "model": {
                "mode": "satisfy",
                "variables": [{"id": "x", "domain": "integer", "lower": 0, "upper": 10}],
                "constraints": [
                    {
                        "id": "c1",
                        "kind": "linear",
                        "params": {
                            "terms": [{"var": "x", "coef": 1}],
                            "sense": ">=",
                            "rhs": 5,
                        },
                    },
                    {
                        "id": "c2",
                        "kind": "linear",
                        "params": {
                            "terms": [{"var": "x", "coef": 1}],
                            "sense": "<=",
                            "rhs": 3,
                        },
                    },
                ],
            }
        },
    )
    assert result["status"] == "infeasible"


def test_invalid_mode_raises(mcp) -> None:
    """An invalid mode raises an MCP error."""
    with pytest.raises(ToolError):
        _call(
            mcp,
            "solve_ortools_model",
            {
                "model": {
                    "mode": "bogus",
                    "variables": [{"id": "x", "domain": "integer", "lower": 0, "upper": 10}],
                }
            },
        )


def test_optimize_without_objective_raises(mcp) -> None:
    """optimize mode without an objective raises."""
    with pytest.raises(ToolError):
        _call(
            mcp,
            "solve_ortools_model",
            {
                "model": {
                    "mode": "optimize",
                    "variables": [{"id": "x", "domain": "integer", "lower": 0, "upper": 10}],
                }
            },
        )


def test_knapsack_optimal(mcp) -> None:
    """A knapsack problem returns optimal with the correct objective_value."""
    result = _call(
        mcp,
        "solve_ortools_model",
        {
            "model": {
                "mode": "optimize",
                "variables": [
                    {"id": "a", "domain": "bool"},
                    {"id": "b", "domain": "bool"},
                    {"id": "c", "domain": "bool"},
                ],
                "constraints": [
                    {
                        "id": "cap",
                        "kind": "linear",
                        "params": {
                            "terms": [
                                {"var": "a", "coef": 3},
                                {"var": "b", "coef": 2},
                                {"var": "c", "coef": 1},
                            ],
                            "sense": "<=",
                            "rhs": 4,
                        },
                    },
                ],
                "objective": {
                    "sense": "max",
                    "terms": [
                        {"var": "a", "coef": 5},
                        {"var": "b", "coef": 2},
                        {"var": "c", "coef": 1},
                    ],
                },
            }
        },
    )
    assert result["status"] == "optimal"
    assert result["objective_value"] == 6
    assert isinstance(result["objective_value"], int)


# ---------------------------------------------------------------------------
# Domain tool MCP-level smoke tests
# ---------------------------------------------------------------------------


def test_budget_allocation_mcp(mcp) -> None:
    """Happy-path budget allocation via MCP call_tool."""
    result = _call(
        mcp,
        "solve_budget_allocation",
        {
            "request": {
                "items": [
                    {"id": "A", "cost": 4, "value": 6},
                    {"id": "B", "cost": 3, "value": 5},
                ],
                "budgets": [{"resource": "money", "limit": 5}],
            }
        },
    )
    assert result["status"] == "optimal"
    assert len(result["selected_items"]) >= 1


def test_assignment_problem_mcp(mcp) -> None:
    """Happy-path assignment via MCP call_tool."""
    result = _call(
        mcp,
        "solve_assignment_problem",
        {
            "request": {
                "agents": [{"id": "A", "skills": ["code"], "capacity": 2}],
                "tasks": [
                    {"id": "T1", "required_skills": ["code"]},
                    {"id": "T2", "required_skills": ["code"]},
                ],
            }
        },
    )
    assert result["status"] in ("optimal", "feasible")
    assert len(result["assignments"]) == 2


def test_scheduling_problem_mcp(mcp) -> None:
    """Happy-path scheduling via MCP call_tool."""
    result = _call(
        mcp,
        "solve_scheduling_problem",
        {
            "request": {
                "tasks": [
                    {"id": "A", "duration": 2},
                    {"id": "B", "duration": 3, "dependencies": ["A"]},
                ],
            }
        },
    )
    assert result["status"] == "optimal"
    assert result["makespan"] is not None
    assert result["makespan"] >= 5


def test_routing_problem_mcp(mcp) -> None:
    """Happy-path TSP routing via MCP call_tool."""
    result = _call(
        mcp,
        "solve_routing_problem",
        {
            "request": {
                "locations": [
                    {"id": "A", "coordinates": [0.0, 0.0]},
                    {"id": "B", "coordinates": [10.0, 0.0]},
                    {"id": "C", "coordinates": [10.0, 10.0]},
                ],
            }
        },
    )
    assert result["status"] == "optimal"
    assert len(result["routes"]) == 1
    assert len(result["routes"][0]["sequence"]) == 3
