"""Routing problem converter (TSP — VRP deferred).

Portions adapted from chuk-mcp-solver (Apache-2.0).
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from openconstraint_mcp.cpsat.core import solve_model
from openconstraint_mcp.cpsat.schemas import (
    ORToolsCircuitParams,
    ORToolsConstraint,
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


class Location(BaseModel):
    """A location to visit."""

    id: str = Field(..., min_length=1)
    coordinates: tuple[float, float] | None = None
    service_time: int = Field(default=0, ge=0)
    time_window: tuple[int, int] | None = None
    demand: int = Field(default=0, ge=0)
    priority: int = Field(default=1, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Vehicle(BaseModel):
    """A vehicle for routing."""

    id: str = Field(..., min_length=1)
    capacity: int = Field(default=999999, ge=0)
    start_location: str = Field(..., min_length=1)
    end_location: str | None = None
    max_distance: int | None = Field(default=None, ge=0)
    max_time: int | None = Field(default=None, ge=0)
    cost_per_distance: float = Field(default=1.0, ge=0.0)
    fixed_cost: float = Field(default=0.0, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutingObjective(StrEnum):
    """Routing optimization objectives."""

    MINIMIZE_DISTANCE = "minimize_distance"
    MINIMIZE_TIME = "minimize_time"
    MINIMIZE_VEHICLES = "minimize_vehicles"
    MINIMIZE_COST = "minimize_cost"


class SolveRoutingProblemRequest(BaseModel):
    """High-level routing problem definition."""

    locations: list[Location] = Field(..., min_length=2)
    vehicles: list[Vehicle] = Field(default_factory=list)
    distance_matrix: list[list[int]] | None = None
    objective: RoutingObjective = RoutingObjective.MINIMIZE_DISTANCE
    force_visit_all: bool = True
    max_route_distance: int | None = Field(default=None, ge=0)
    timeout_ms: int = Field(default=60_000, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Route(BaseModel):
    """A vehicle route through locations."""

    vehicle_id: str
    sequence: list[str]
    total_distance: int = Field(..., ge=0)
    total_time: int = Field(..., ge=0)
    total_cost: float = Field(..., ge=0.0)
    load_timeline: list[tuple[str, int]] = Field(default_factory=list)


class RoutingExplanation(BaseModel):
    """Human-readable explanation of the routing solution."""

    summary: str
    bottlenecks: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class SolveRoutingProblemResponse(BaseModel):
    """Routing solution with domain-specific details."""

    status: SolverStatus
    routes: list[Route] = Field(default_factory=list)
    unvisited: list[str] = Field(default_factory=list)
    total_distance: int = Field(default=0, ge=0)
    total_time: int = Field(default=0, ge=0)
    total_cost: float = Field(default=0.0, ge=0.0)
    vehicles_used: int = Field(default=0, ge=0)
    solve_time_ms: int = Field(default=0, ge=0)
    optimality_gap: float | None = None
    explanation: RoutingExplanation


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

_BIG_SENTINEL = 10_000_000


def _build_distance_matrix(
    request: SolveRoutingProblemRequest,
) -> list[list[int]]:
    """Build or return the distance matrix."""
    if request.distance_matrix is not None:
        return request.distance_matrix

    locations = request.locations
    n = len(locations)
    matrix: list[list[int]] = []

    for i in range(n):
        row: list[int] = []
        for j in range(n):
            if i == j:
                row.append(0)
                continue
            ci = locations[i].coordinates
            cj = locations[j].coordinates
            if ci is None or cj is None:
                row.append(_BIG_SENTINEL)
                continue
            x1, y1 = ci
            x2, y2 = cj
            dist = math.hypot(x2 - x1, y2 - y1)
            row.append(round(dist))
        matrix.append(row)

    return matrix


def convert_routing_to_cpsat(
    request: SolveRoutingProblemRequest,
) -> ORToolsSolveRequest:
    """Compile a routing request into a core CP-SAT solve request.

    Currently only single-vehicle TSP with distance minimization is supported.
    Unsupported features raise ``ValueError`` rather than silently ignoring them.
    """
    if len(request.vehicles) > 1:
        raise ValueError("VRP with multiple vehicles is not yet supported")

    if request.objective != RoutingObjective.MINIMIZE_DISTANCE:
        raise ValueError(
            f"Routing objective {request.objective!r} is not yet supported; "
            f"only {RoutingObjective.MINIMIZE_DISTANCE!r} is available"
        )

    if not request.force_visit_all:
        raise ValueError("force_visit_all=False is not yet supported")

    if request.max_route_distance is not None:
        raise ValueError("max_route_distance is not yet supported")

    has_vehicle = len(request.vehicles) == 1
    if has_vehicle:
        vehicle = request.vehicles[0]
        if vehicle.max_distance is not None:
            raise ValueError("vehicle.max_distance is not yet supported")
        if vehicle.max_time is not None:
            raise ValueError("vehicle.max_time is not yet supported")
        if vehicle.capacity != Vehicle.model_fields["capacity"].default:
            raise ValueError("vehicle.capacity constraints are not yet supported")
        loc_ids = {loc.id for loc in request.locations}
        start_id = vehicle.start_location
        if start_id not in loc_ids:
            raise ValueError(
                f"Vehicle start_location {start_id!r} is not a location id; "
                f"available: {sorted(loc_ids)}"
            )
        if vehicle.end_location is not None and vehicle.end_location != start_id:
            raise ValueError(
                f"Different start/end locations ({start_id!r} -> "
                f"{vehicle.end_location!r}) are not yet supported; "
                f"only closed tours (start = end) are available"
            )

    for loc in request.locations:
        if loc.service_time != 0:
            raise ValueError(
                f"Location '{loc.id}': service_time is not yet supported"
            )
        if loc.time_window is not None:
            raise ValueError(
                f"Location '{loc.id}': time_window is not yet supported"
            )
        if loc.demand != 0:
            raise ValueError(
                f"Location '{loc.id}': demand is not yet supported"
            )

    locations = request.locations
    n = len(locations)
    dist = _build_distance_matrix(request)

    variables: list[ORToolsVariable] = []
    constraints: list[ORToolsConstraint] = []
    arcs: list[tuple[int, int, str]] = []
    arc_terms: list[ORToolsLinearTerm] = []

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            var_id = f"arc_{i}_{j}"
            variables.append(
                ORToolsVariable(
                    id=var_id,
                    domain="bool",
                    metadata={"from": locations[i].id, "to": locations[j].id},
                )
            )
            arcs.append((i, j, var_id))
            arc_terms.append(ORToolsLinearTerm(var=var_id, coef=dist[i][j]))

    constraints.append(
        ORToolsConstraint(
            id="tsp_circuit",
            kind="circuit",
            params=ORToolsCircuitParams(arcs=arcs),
            metadata={"description": "TSP tour circuit constraint"},
        )
    )

    objective = ORToolsObjective(sense="min", terms=arc_terms)
    return ORToolsSolveRequest(
        mode="optimize",
        variables=variables,
        constraints=constraints,
        objective=objective,
        search=ORToolsSearchConfig(timeout_ms=request.timeout_ms),
    )


def convert_cpsat_to_routing_response(
    result: ORToolsSolveResult,
    original_request: SolveRoutingProblemRequest,
) -> SolveRoutingProblemResponse:
    """Convert a core CP-SAT result back to the routing domain."""
    non_solution: tuple[SolverStatus, ...] = (
        "infeasible",
        "timeout_no_solution",
        "error",
    )

    if result.status in non_solution:
        return SolveRoutingProblemResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=RoutingExplanation(summary=f"Problem is {result.status}"),
        )

    if not result.solutions:
        return SolveRoutingProblemResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=RoutingExplanation(summary="No solution available"),
        )

    solution = result.solutions[0]
    var_values: dict[str, int] = {v.id: v.value for v in solution.variables}

    locations = original_request.locations
    n = len(locations)
    dist = _build_distance_matrix(original_request)

    # Reconstruct the tour from arc variables
    # Build adjacency: which arc (i->j) is active
    next_node: dict[int, int] = {}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if var_values.get(f"arc_{i}_{j}") == 1:
                next_node[i] = j
                break

    if not next_node:
        return SolveRoutingProblemResponse(
            status=result.status,
            solve_time_ms=result.solve_time_ms,
            explanation=RoutingExplanation(summary="No route found — circuit incomplete"),
        )

    # Walk the tour starting from the vehicle's start location
    sequence: list[str] = []
    total_dist = 0
    # Determine starting index: use the vehicle's start_location if provided,
    # otherwise default to index 0.
    start_idx = 0
    if original_request.vehicles:
        start_id = original_request.vehicles[0].start_location
        for idx, loc in enumerate(locations):
            if loc.id == start_id:
                start_idx = idx
                break
    cur = start_idx
    visited = set()
    for _ in range(n):
        sequence.append(locations[cur].id)
        visited.add(cur)
        nxt = next_node.get(cur)
        if nxt is None:
            break
        total_dist += dist[cur][nxt]
        if len(visited) == n:
            break
        cur = nxt

    # Only minimize_distance is supported in v0 (other objectives raise in the
    # converter), so total_time and total_cost mirror total_distance; they gain
    # independent meaning once time/cost objectives land.
    route = Route(
        vehicle_id=(original_request.vehicles[0].id if original_request.vehicles else "vehicle_0"),
        sequence=sequence,
        total_distance=total_dist,
        total_time=total_dist,
        total_cost=total_dist,
    )

    summary_parts: list[str] = []
    if result.status == "optimal":
        summary_parts.append(f"Found optimal tour with distance {total_dist}")
    elif result.status in ("feasible", "timeout_best"):
        summary_parts.append(f"Found feasible tour with distance {total_dist}")
        if result.optimality_gap is not None:
            summary_parts.append(f"(gap: {result.optimality_gap:.2f}%)")
    else:
        summary_parts.append("Tour found")

    summary_parts.append(f"visiting {len(sequence)} of {n} locations")

    return SolveRoutingProblemResponse(
        status=result.status,
        routes=[route],
        total_distance=total_dist,
        total_time=total_dist,
        total_cost=total_dist,
        vehicles_used=1,
        solve_time_ms=result.solve_time_ms,
        optimality_gap=result.optimality_gap,
        explanation=RoutingExplanation(summary=" ".join(summary_parts)),
    )


def solve_routing_problem(
    request: SolveRoutingProblemRequest,
) -> SolveRoutingProblemResponse:
    """Solve a routing problem.

    Converts the high-level request to CP-SAT, solves, and converts back.
    """
    cpsat_request = convert_routing_to_cpsat(request)
    result = solve_model(cpsat_request)
    return convert_cpsat_to_routing_response(result, request)
