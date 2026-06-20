"""Tests for the budget allocation domain tool."""

from openconstraint_mcp.cpsat.domains.allocation import (
    AllocationObjective,
    BudgetConstraint,
    Item,
    SolveBudgetAllocationRequest,
    _compute_scale,
    _scaled,
    convert_allocation_to_cpsat,
    solve_budget_allocation,
)


def test_compute_scale_covers_input_decimal_precision():
    """The scale is a power of ten covering the most-precise input (regression).

    Previously ``_compute_scale`` always returned 100, silently truncating any
    value with more than two decimal places.
    """
    request = SolveBudgetAllocationRequest(
        items=[Item(id="A", cost=1.125, value=3.14159)],
        budgets=[BudgetConstraint(resource="money", limit=5.0)],
    )
    # value 3.14159 → 5 decimal places → scale must be at least 1e5.
    assert _compute_scale(request) == 100_000


def test_compute_scale_floor_is_100_for_coarse_inputs():
    """Integer / two-decimal inputs still scale by the 100 floor."""
    request = SolveBudgetAllocationRequest(
        items=[Item(id="A", cost=4.0, value=6.5)],
        budgets=[BudgetConstraint(resource="money", limit=5.0)],
    )
    assert _compute_scale(request) == 100


def test_scaled_rounds_instead_of_truncating():
    """`_scaled` recovers the exact scaled value despite float error."""
    # 19.99 * 100 == 1998.9999999999998 → int() would give 1998.
    assert _scaled(19.99, 100) == 1999
    assert _scaled(4.35, 100) == 435


def test_knapsack_optimal_selection():
    """A basic knapsack picks the optimal item set."""
    items = [
        Item(id="A", cost=4, value=6),
        Item(id="B", cost=3, value=5),
        Item(id="C", cost=2, value=3),
    ]
    budgets = [BudgetConstraint(resource="money", limit=5)]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets)

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    assert response.total_cost <= 5.0
    # Optimal: pick B (cost 3, value 5) + C (cost 2, value 3) = cost 5, value 8
    assert response.total_value == 8.0
    assert set(response.selected_items) == {"B", "C"}


def test_knapsack_maximize_count():
    """MAXIMIZE_COUNT picks as many items as possible within budget."""
    items = [
        Item(id="A", cost=5, value=10),
        Item(id="B", cost=2, value=1),
        Item(id="C", cost=3, value=1),
    ]
    budgets = [BudgetConstraint(resource="money", limit=10)]
    request = SolveBudgetAllocationRequest(
        items=items, budgets=budgets, objective=AllocationObjective.MAXIMIZE_COUNT
    )

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    assert len(response.selected_items) == 3
    assert set(response.selected_items) == {"A", "B", "C"}


def test_knapsack_minimize_cost():
    """MINIMIZE_COST picks the cheapest items while meeting a value threshold."""
    items = [
        Item(id="A", cost=10, value=10),
        Item(id="B", cost=5, value=8),
        Item(id="C", cost=1, value=3),
    ]
    budgets = [BudgetConstraint(resource="money", limit=20)]
    request = SolveBudgetAllocationRequest(
        items=items,
        budgets=budgets,
        objective=AllocationObjective.MINIMIZE_COST,
        min_value_threshold=10,
    )

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    # To reach value 10: A alone (cost 10) or B+C (cost 6). B+C is cheaper.
    assert response.total_value >= 10.0
    assert response.total_cost == 6.0
    assert set(response.selected_items) == {"B", "C"}


def test_dependency_forces_co_selection():
    """A dependency forces the target to be selected together with the source."""
    items = [
        Item(id="A", cost=5, value=10, dependencies=["B"]),
        Item(id="B", cost=3, value=1),
    ]
    budgets = [BudgetConstraint(resource="money", limit=10)]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets)

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    # A requires B, so both must be selected or neither
    # optimal: pick both (cost 8, value 11)
    assert set(response.selected_items) == {"A", "B"}


def test_conflict_forbids_co_selection():
    """Conflicting items cannot be selected together."""
    items = [
        Item(id="A", cost=3, value=10, conflicts=["B"]),
        Item(id="B", cost=3, value=8),
        Item(id="C", cost=3, value=5),
    ]
    budgets = [BudgetConstraint(resource="money", limit=7)]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets)

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    # Cannot pick both A and B. Optimal: A + C = cost 6, value 15
    assert "A" in response.selected_items
    assert response.total_value >= 15.0
    assert "B" not in response.selected_items or response.total_cost == 7.0


def test_over_tight_budget_infeasible():
    """A budget that cannot even accommodate one item, with min_items, is infeasible."""
    items = [
        Item(id="A", cost=10, value=5),
    ]
    budgets = [BudgetConstraint(resource="money", limit=5)]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets, min_items=1)

    response = solve_budget_allocation(request)

    assert response.status == "infeasible"


def test_convert_allocation_to_cpsat_structure():
    """The converter produces a well-formed ORToolsSolveRequest."""
    items = [
        Item(id="X", cost=3, value=10),
        Item(id="Y", cost=4, value=15),
    ]
    budgets = [BudgetConstraint(resource="money", limit=6)]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets)

    cpsat_request = convert_allocation_to_cpsat(request)

    assert cpsat_request.mode == "optimize"
    assert len(cpsat_request.variables) == 2
    assert cpsat_request.variables[0].id == "select_X"
    assert cpsat_request.variables[0].domain == "bool"
    assert cpsat_request.variables[1].id == "select_Y"
    assert cpsat_request.objective is not None
    assert not isinstance(cpsat_request.objective, list)
    assert cpsat_request.objective.sense == "max"
    assert len(cpsat_request.constraints) >= 1  # at least the budget constraint


def test_non_money_resource_budget_ignores_missing_resource():
    """A non-money budget does not constrain items that lack that resource."""
    items = [
        Item(id="A", cost=10, value=5, resources_required={"hours": 3}),
        Item(id="B", cost=2, value=10),  # no hours requirement
    ]
    budgets = [
        BudgetConstraint(resource="money", limit=15),
        BudgetConstraint(resource="hours", limit=10),
    ]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets)

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    # Both items fit: cost 10+2=12 <= 15 money; hours 3+0=3 <= 10 hours
    assert set(response.selected_items) == {"A", "B"}


def test_fractional_resource_budget_respected():
    """Fractional non-money resources are scaled correctly, not truncated."""
    items = [
        Item(id="A", cost=1, value=1, resources_required={"hours": 0.6}),
        Item(id="B", cost=1, value=1, resources_required={"hours": 0.6}),
    ]
    budgets = [
        BudgetConstraint(resource="money", limit=10),
        BudgetConstraint(resource="hours", limit=1.0),
    ]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets)

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    # Two items at 0.6 hours each = 1.2 > 1.0 limit → can only pick one
    assert len(response.selected_items) == 1


def test_min_items_enforced():
    """min_items lower bound forces selecting at least N items."""
    items = [
        Item(id="A", cost=1, value=1),
        Item(id="B", cost=1, value=1),
        Item(id="C", cost=1, value=1),
    ]
    budgets = [BudgetConstraint(resource="money", limit=10)]
    request = SolveBudgetAllocationRequest(items=items, budgets=budgets, min_items=2)

    response = solve_budget_allocation(request)

    assert response.status == "optimal"
    assert len(response.selected_items) >= 2
