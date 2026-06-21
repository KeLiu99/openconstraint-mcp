"""Tests for the routing domain tool."""

import pytest

from openconstraint_mcp.cpsat.domains.routing import (
    Location,
    RoutingObjective,
    SolveRoutingProblemRequest,
    Vehicle,
    convert_routing_to_cpsat,
    solve_routing_problem,
)


def test_tsp_square_perimeter():
    """A 4-location square TSP returns the perimeter tour."""
    # Square of side 10
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(10.0, 0.0)),
        Location(id="C", coordinates=(10.0, 10.0)),
        Location(id="D", coordinates=(0.0, 10.0)),
    ]
    request = SolveRoutingProblemRequest(locations=locations)

    response = solve_routing_problem(request)

    assert response.status == "optimal"
    # Perimeter tour distance = 40
    assert response.total_distance == 40
    assert len(response.routes) == 1
    assert response.routes[0].total_distance == 40


def test_tsp_visits_all_once():
    """A TSP route visits every location exactly once."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(10.0, 0.0)),
        Location(id="C", coordinates=(10.0, 10.0)),
    ]
    request = SolveRoutingProblemRequest(locations=locations)

    response = solve_routing_problem(request)

    assert response.status == "optimal"
    seq = response.routes[0].sequence
    assert len(seq) == 3
    assert set(seq) == {"A", "B", "C"}


def test_vrp_unsupported():
    """Multi-vehicle VRP raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
        Location(id="C", coordinates=(0.0, 1.0)),
    ]
    vehicles = [
        Vehicle(id="V1", start_location="A"),
        Vehicle(id="V2", start_location="A"),
    ]

    with pytest.raises(ValueError, match="VRP with multiple vehicles"):
        solve_routing_problem(SolveRoutingProblemRequest(locations=locations, vehicles=vehicles))


def test_distance_matrix_used():
    """A user-provided distance matrix is used directly."""
    locations = [
        Location(id="A"),
        Location(id="B"),
        Location(id="C"),
    ]
    dm = [
        [0, 10, 20],
        [10, 0, 30],
        [20, 30, 0],
    ]
    request = SolveRoutingProblemRequest(locations=locations, distance_matrix=dm)

    response = solve_routing_problem(request)

    assert response.status == "optimal"
    # Total = A→B(10) + B→C(30) + C→A(20) = 60
    assert response.total_distance == 60


def test_unsupported_objective_raises():
    """Non-distance objectives raise ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        objective=RoutingObjective.MINIMIZE_TIME,
    )
    with pytest.raises(ValueError, match="MINIMIZE_TIME"):
        convert_routing_to_cpsat(request)


def test_force_visit_all_false_raises():
    """force_visit_all=False raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        force_visit_all=False,
    )
    with pytest.raises(ValueError, match="force_visit_all"):
        convert_routing_to_cpsat(request)


def test_max_route_distance_raises():
    """max_route_distance raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        max_route_distance=10,
    )
    with pytest.raises(ValueError, match="max_route_distance"):
        convert_routing_to_cpsat(request)


def test_vehicle_max_distance_raises():
    """vehicle.max_distance raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        vehicles=[Vehicle(id="V1", start_location="A", max_distance=5)],
    )
    with pytest.raises(ValueError, match="vehicle.max_distance"):
        convert_routing_to_cpsat(request)


def test_vehicle_max_time_raises():
    """vehicle.max_time raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        vehicles=[Vehicle(id="V1", start_location="A", max_time=10)],
    )
    with pytest.raises(ValueError, match="vehicle.max_time"):
        convert_routing_to_cpsat(request)


def test_vehicle_start_location_not_found_raises():
    """Vehicle start_location not in locations list raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        vehicles=[Vehicle(id="V1", start_location="Z")],
    )
    with pytest.raises(ValueError, match="start_location"):
        convert_routing_to_cpsat(request)


def test_vehicle_end_location_differs_raises():
    """Vehicle end_location != start_location raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        vehicles=[Vehicle(id="V1", start_location="A", end_location="B")],
    )
    with pytest.raises(ValueError, match="end.location"):
        convert_routing_to_cpsat(request)


def test_single_vehicle_with_start_location_ok():
    """Single vehicle with start_location matching a location is accepted."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
        Location(id="C", coordinates=(0.0, 1.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        vehicles=[Vehicle(id="V1", start_location="A")],
    )
    cpsat_request = convert_routing_to_cpsat(request)
    assert cpsat_request.mode == "optimize"


def test_convert_routing_to_cpsat_structure():
    """The converter produces a well-formed ORToolsSolveRequest."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
        Location(id="C", coordinates=(0.0, 1.0)),
    ]
    request = SolveRoutingProblemRequest(locations=locations)

    cpsat_request = convert_routing_to_cpsat(request)

    assert cpsat_request.mode == "optimize"
    # 3 locations, 6 arcs (no self-loops)
    assert len(cpsat_request.variables) == 6
    var_ids = {v.id for v in cpsat_request.variables}
    assert "arc_0_1" in var_ids
    assert "arc_0_0" not in var_ids  # no self-loop
    assert len(cpsat_request.constraints) == 1
    assert cpsat_request.constraints[0].kind == "circuit"
    assert cpsat_request.objective is not None
    assert not isinstance(cpsat_request.objective, list)


def test_route_sequence_starts_at_start_location():
    """The returned route sequence begins at the vehicle's start_location."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(10.0, 0.0)),
        Location(id="C", coordinates=(10.0, 10.0)),
        Location(id="D", coordinates=(0.0, 10.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        vehicles=[Vehicle(id="V1", start_location="B")],
    )

    response = solve_routing_problem(request)

    assert response.status == "optimal"
    assert response.routes[0].sequence[0] == "B"


def test_location_service_time_raises():
    """A location with non-zero service_time raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0), service_time=5),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(locations=locations)
    with pytest.raises(ValueError, match="service_time"):
        convert_routing_to_cpsat(request)


def test_location_time_window_raises():
    """A location with a time_window raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0), time_window=(0, 10)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(locations=locations)
    with pytest.raises(ValueError, match="time_window"):
        convert_routing_to_cpsat(request)


def test_location_demand_raises():
    """A location with non-zero demand raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0), demand=3),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(locations=locations)
    with pytest.raises(ValueError, match="demand"):
        convert_routing_to_cpsat(request)


def test_vehicle_capacity_raises():
    """A vehicle with non-default capacity raises ValueError."""
    locations = [
        Location(id="A", coordinates=(0.0, 0.0)),
        Location(id="B", coordinates=(1.0, 0.0)),
    ]
    request = SolveRoutingProblemRequest(
        locations=locations,
        vehicles=[Vehicle(id="V1", start_location="A", capacity=50)],
    )
    with pytest.raises(ValueError, match="capacity"):
        convert_routing_to_cpsat(request)
