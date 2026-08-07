"""Reference CP-SAT model and data contract for Online Printing Shop instances.

Loads a canonical OPS JSON file (default: ``data_sops1.json``), minimizes
makespan, and emits the openconstraint-mcp CP-SAT JSON envelope.
"""

import json
import os
import sys
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(min_length=1)]
TimeTick = Annotated[int, Field(ge=0)]
Theta = Annotated[float, Field(gt=0, le=1)]
CpsatIntVar = cp_model.IntVar


class ClosedModel(BaseModel):
    """Base for strict objects that reject misspelled or unsupported fields."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Provenance(ClosedModel):
    """Human-readable origin and stated license status for an instance."""

    source: Identifier
    license: Identifier


class UnavailabilityInterval(ClosedModel):
    """A machine outage delimited by nonnegative integer time ticks."""

    start: TimeTick
    end: TimeTick

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end <= self.start:
            raise ValueError("unavailability end must be greater than start")
        return self


class SetupTimes(ClosedModel):
    """Explicit setup durations for first and ordered subsequent operations."""

    first: dict[Identifier, TimeTick]
    transitions: dict[Identifier, dict[Identifier, TimeTick]]


class Machine(ClosedModel):
    """A machine calendar and its operation-specific setup durations."""

    unavailability: list[UnavailabilityInterval] = Field(
        description=(
            "Ordered machine outages. An operation may finish at an outage start and start "
            "at its end, but may not start at its start or finish at its end. Setups must not "
            "overlap an outage."
        )
    )
    setup_times: SetupTimes

    @model_validator(mode="after")
    def validate_unavailability(self) -> Self:
        for previous, current in zip(self.unavailability, self.unavailability[1:], strict=False):
            if current.start < previous.end:
                raise ValueError("unavailability intervals must be ordered and nonoverlapping")
        return self


class MachineOption(ClosedModel):
    """Processing time when an operation is assigned to this machine."""

    processing_time: TimeTick


class FixedOperation(ClosedModel):
    """A preassigned eligible machine and start time."""

    machine: Identifier
    start: TimeTick


class Operation(ClosedModel):
    """A mandatory OPS operation and its direct precedence successors."""

    job: Identifier
    successors: list[Identifier]
    machine_options: Annotated[dict[Identifier, MachineOption], Field(min_length=1)]
    release_time: TimeTick
    theta: Theta = Field(
        description=(
            "Fraction of this operation's processing required before a direct successor may "
            "start. A successor also may not finish before this operation finishes."
        )
    )
    fixed: FixedOperation | None = None


class OPSInstance(ClosedModel):
    """Versioned, solver-ready Online Printing Shop problem instance."""

    format: Literal["openconstraint.ops.instance"]
    format_version: Literal["1.0"]
    provenance: Provenance
    machines: Annotated[dict[Identifier, Machine], Field(min_length=1)]
    operations: Annotated[dict[Identifier, Operation], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        operation_ids = set(self.operations)
        machine_ids = set(self.machines)

        indegree = dict.fromkeys(operation_ids, 0)
        for operation_id, operation in self.operations.items():
            if len(operation.successors) != len(set(operation.successors)):
                raise ValueError(f"operation {operation_id!r} contains duplicate successors")
            for successor_id in operation.successors:
                successor = self.operations.get(successor_id)
                if successor is None:
                    raise ValueError(
                        f"operation {operation_id!r} references unknown successor {successor_id!r}"
                    )
                if successor.job != operation.job:
                    raise ValueError(
                        f"operation {operation_id!r} has successor outside job {operation.job!r}"
                    )
                indegree[successor_id] += 1

            unknown_machines = set(operation.machine_options) - machine_ids
            if unknown_machines:
                raise ValueError(
                    f"operation {operation_id!r} references unknown machines: "
                    f"{sorted(unknown_machines)}"
                )
            if operation.fixed is not None:
                if operation.fixed.machine not in operation.machine_options:
                    raise ValueError(f"operation {operation_id!r} fixed machine is not eligible")
                if operation.fixed.start < operation.release_time:
                    raise ValueError(
                        f"operation {operation_id!r} fixed start precedes its release time"
                    )

        ready = [operation_id for operation_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            operation_id = ready.pop()
            visited += 1
            for successor_id in self.operations[operation_id].successors:
                indegree[successor_id] -= 1
                if indegree[successor_id] == 0:
                    ready.append(successor_id)
        if visited != len(operation_ids):
            raise ValueError("operation precedence graph must be acyclic")

        for machine_id, machine in self.machines.items():
            eligible_operation_ids = {
                operation_id
                for operation_id, operation in self.operations.items()
                if machine_id in operation.machine_options
            }
            if set(machine.setup_times.first) != eligible_operation_ids:
                raise ValueError(
                    f"machine {machine_id!r} first setup entries must match eligible operations"
                )
            expected_sources = eligible_operation_ids if len(eligible_operation_ids) > 1 else set()
            if set(machine.setup_times.transitions) != expected_sources:
                raise ValueError(
                    f"machine {machine_id!r} transition sources must match eligible operations"
                )
            for source_id, targets in machine.setup_times.transitions.items():
                if set(targets) != eligible_operation_ids - {source_id}:
                    raise ValueError(
                        f"machine {machine_id!r} transitions from {source_id!r} must cover "
                        "every other eligible operation"
                    )

        return self


class ScheduledOperation(ClosedModel):
    """One complete operation decision in a solved OPS schedule."""

    operation: Identifier
    job: Identifier
    machine: Identifier
    predecessor: Identifier | None
    setup_start: TimeTick
    setup_duration: TimeTick
    start: TimeTick
    processing_time: TimeTick
    theta_completion_time: TimeTick
    end: TimeTick


class Solution(ClosedModel):
    """Typed boundary between the solver and the stdout serializer."""

    status: Literal["optimal", "feasible", "infeasible", "unknown", "error"]
    schedule: list[ScheduledOperation] | None = None
    objective: int | None = None
    best_objective_bound: float | None = None


def read_input(data_path: Path) -> dict[str, Any]:
    """Read one raw OPS instance object from a JSON file."""

    return json.loads(data_path.read_text(encoding="utf-8"))


def parse_input(raw: dict[str, Any]) -> OPSInstance:
    """Validate a raw object and return the typed solver input record."""

    return OPSInstance.model_validate(raw)


def _data_path() -> Path:
    filename = sys.argv[1] if len(sys.argv) > 1 else "data_sops1.json"
    return Path(__file__).parent / filename


def _horizon(instance: OPSInstance) -> int:
    anchor = max(
        [operation.release_time for operation in instance.operations.values()]
        + [
            operation.fixed.start
            for operation in instance.operations.values()
            if operation.fixed is not None
        ]
        + [gap.end for machine in instance.machines.values() for gap in machine.unavailability]
        + [0]
    )
    work = 0
    for operation_id, operation in instance.operations.items():
        processing = max(option.processing_time for option in operation.machine_options.values())
        setup = max(
            duration
            for machine_id in operation.machine_options
            for duration in (
                [instance.machines[machine_id].setup_times.first[operation_id]]
                + [
                    targets[operation_id]
                    for targets in instance.machines[machine_id].setup_times.transitions.values()
                    if operation_id in targets
                ]
            )
        )
        work += processing + setup
    return anchor + work


def _required_processing(theta: float, processing_time: int) -> int:
    return int((Decimal(str(theta)) * processing_time).to_integral_value(rounding=ROUND_CEILING))


def _add_resumable_duration(
    model: cp_model.CpModel,
    start: CpsatIntVar,
    finish: CpsatIntVar,
    duration: int,
    unavailability: list[UnavailabilityInterval],
    presence: CpsatIntVar,
    name: str,
) -> None:
    """Bind elapsed work to a calendar-aware finish when ``presence`` is true."""

    skipped_time = []
    for gap_index, gap in enumerate(unavailability):
        start_after = model.new_bool_var(f"{name}_start_after_{gap_index}")
        finish_after = model.new_bool_var(f"{name}_finish_after_{gap_index}")

        model.add(start >= gap.end).only_enforce_if(presence, start_after)
        model.add(start < gap.start).only_enforce_if(presence, start_after.Not())
        model.add(finish > gap.end).only_enforce_if(presence, finish_after)
        model.add(finish <= gap.start).only_enforce_if(presence, finish_after.Not())
        skipped_time.append((gap.end - gap.start) * (finish_after - start_after))

    model.add(finish - start == duration + sum(skipped_time)).only_enforce_if(presence)


def solve(instance: OPSInstance) -> Solution:
    """Build and solve the main CP-SAT model for OPS makespan minimization."""

    model = cp_model.CpModel()
    horizon = _horizon(instance)
    operation_ids = list(instance.operations)

    starts: dict[str, CpsatIntVar] = {}
    theta_completion_times: dict[str, CpsatIntVar] = {}
    ends: dict[str, CpsatIntVar] = {}
    setup_starts: dict[str, CpsatIntVar] = {}
    setup_durations: dict[str, CpsatIntVar] = {}
    assignments: dict[tuple[str, str], CpsatIntVar] = {}
    incoming_arcs: dict[tuple[str, str], list[tuple[str | None, CpsatIntVar]]] = {}
    machine_setup_intervals: dict[str, list[cp_model.IntervalVar]] = {
        machine_id: [] for machine_id in instance.machines
    }

    for operation_index, (operation_id, operation) in enumerate(instance.operations.items()):
        suffix = str(operation_index)
        start: CpsatIntVar = model.new_int_var(operation.release_time, horizon, f"start_{suffix}")
        theta_completion_time: CpsatIntVar = model.new_int_var(
            operation.release_time, horizon, f"theta_completion_time_{suffix}"
        )
        end: CpsatIntVar = model.new_int_var(operation.release_time, horizon, f"end_{suffix}")
        setup_start: CpsatIntVar = model.new_int_var(0, horizon, f"setup_start_{suffix}")
        setup_duration: CpsatIntVar = model.new_int_var(0, horizon, f"setup_duration_{suffix}")
        starts[operation_id] = start
        theta_completion_times[operation_id] = theta_completion_time
        ends[operation_id] = end
        setup_starts[operation_id] = setup_start
        setup_durations[operation_id] = setup_duration
        model.add(start <= theta_completion_time)
        model.add(theta_completion_time <= end)

        alternatives: list[CpsatIntVar] = []
        for machine_index, (machine_id, option) in enumerate(operation.machine_options.items()):
            is_assigned: CpsatIntVar = model.new_bool_var(f"is_assigned_{suffix}_{machine_index}")
            assignments[operation_id, machine_id] = is_assigned
            incoming_arcs[operation_id, machine_id] = []
            alternatives.append(is_assigned)

            machine = instance.machines[machine_id]
            _add_resumable_duration(
                model,
                start,
                theta_completion_time,
                _required_processing(operation.theta, option.processing_time),
                machine.unavailability,
                is_assigned,
                f"theta_completion_time_{suffix}_{machine_index}",
            )
            _add_resumable_duration(
                model,
                start,
                end,
                option.processing_time,
                machine.unavailability,
                is_assigned,
                f"end_{suffix}_{machine_index}",
            )
            machine_setup_intervals[machine_id].append(
                model.new_optional_interval_var(
                    setup_start,
                    setup_duration,
                    start,
                    is_assigned,
                    f"setup_{suffix}_{machine_index}",
                )
            )

        model.add_exactly_one(alternatives)
        if operation.fixed is not None:
            model.add(assignments[operation_id, operation.fixed.machine] == 1)
            model.add(start == operation.fixed.start)

    for operation_id, operation in instance.operations.items():
        for successor_id in operation.successors:
            model.add(starts[successor_id] >= theta_completion_times[operation_id])
            model.add(ends[successor_id] >= ends[operation_id])

    for machine_index, (machine_id, machine) in enumerate(instance.machines.items()):
        eligible_operation_ids = [
            operation_id
            for operation_id in operation_ids
            if machine_id in instance.operations[operation_id].machine_options
        ]
        node_index = {
            operation_id: index
            for index, operation_id in enumerate(eligible_operation_ids, start=1)
        }
        machine_assignments = [
            assignments[operation_id, machine_id] for operation_id in eligible_operation_ids
        ]
        machine_unused: CpsatIntVar = model.new_bool_var(f"machine_unused_{machine_index}")
        model.add(sum(machine_assignments) == 0).only_enforce_if(machine_unused)
        model.add(sum(machine_assignments) >= 1).only_enforce_if(machine_unused.Not())
        circuit_arcs: list[tuple[int, int, cp_model.LiteralT]] = [(0, 0, machine_unused)]

        for operation_id in eligible_operation_ids:
            is_assigned = assignments[operation_id, machine_id]
            operation_node = node_index[operation_id]
            circuit_arcs.append((operation_node, operation_node, is_assigned.Not()))

            first: CpsatIntVar = model.new_bool_var(f"first_{machine_index}_{operation_node}")
            circuit_arcs.append((0, operation_node, first))
            incoming_arcs[operation_id, machine_id].append((None, first))
            first_setup = machine.setup_times.first[operation_id]
            model.add(setup_durations[operation_id] == first_setup).only_enforce_if(first)
            model.add(
                setup_starts[operation_id] == starts[operation_id] - first_setup
            ).only_enforce_if(first)

            last: CpsatIntVar = model.new_bool_var(f"last_{machine_index}_{operation_node}")
            circuit_arcs.append((operation_node, 0, last))

        for predecessor_id in eligible_operation_ids:
            for operation_id in eligible_operation_ids:
                if predecessor_id == operation_id:
                    continue
                arc: CpsatIntVar = model.new_bool_var(
                    f"arc_{machine_index}_{node_index[predecessor_id]}_{node_index[operation_id]}"
                )
                circuit_arcs.append((node_index[predecessor_id], node_index[operation_id], arc))
                incoming_arcs[operation_id, machine_id].append((predecessor_id, arc))
                transition = machine.setup_times.transitions[predecessor_id][operation_id]
                model.add(setup_durations[operation_id] == transition).only_enforce_if(arc)
                model.add(
                    setup_starts[operation_id] == starts[operation_id] - transition
                ).only_enforce_if(arc)
                model.add(ends[predecessor_id] <= setup_starts[operation_id]).only_enforce_if(arc)

        model.add_circuit(circuit_arcs)
        outage_intervals = [
            model.new_fixed_size_interval_var(
                gap.start,
                gap.end - gap.start,
                f"outage_{machine_index}_{gap_index}",
            )
            for gap_index, gap in enumerate(machine.unavailability)
        ]
        model.add_no_overlap(machine_setup_intervals[machine_id] + outage_intervals)

    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, list(ends.values()))
    model.minimize(makespan)

    def extract_schedule(
        reader: cp_model.CpSolver | cp_model.CpSolverSolutionCallback,
    ) -> list[ScheduledOperation]:
        schedule = []
        for operation_id, operation in instance.operations.items():
            machine_id = next(
                machine_id
                for machine_id in operation.machine_options
                if reader.boolean_value(assignments[operation_id, machine_id])
            )
            predecessor = next(
                predecessor
                for predecessor, arc in incoming_arcs[operation_id, machine_id]
                if reader.boolean_value(arc)
            )
            schedule.append(
                ScheduledOperation(
                    operation=operation_id,
                    job=operation.job,
                    machine=machine_id,
                    predecessor=predecessor,
                    setup_start=reader.value(setup_starts[operation_id]),
                    setup_duration=reader.value(setup_durations[operation_id]),
                    start=reader.value(starts[operation_id]),
                    processing_time=operation.machine_options[machine_id].processing_time,
                    theta_completion_time=reader.value(theta_completion_times[operation_id]),
                    end=reader.value(ends[operation_id]),
                )
            )
        return schedule

    class _BestSolution(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self) -> None:
            write_output(
                serialize_solution(
                    Solution(
                        status="feasible",
                        schedule=extract_schedule(self),
                        objective=self.value(makespan),
                        best_objective_bound=float(self.best_objective_bound),
                    )
                )
            )

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))
    solver.parameters.num_workers = 1
    status_code = solver.solve(model, _BestSolution())
    status_map: dict[
        cp_model.CpSolverStatus, Literal["optimal", "feasible", "infeasible", "unknown", "error"]
    ] = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }
    has_solution = status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    bound_states = (cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN)
    return Solution(
        status=status_map.get(status_code, "error"),
        schedule=extract_schedule(solver) if has_solution else None,
        objective=solver.value(makespan) if has_solution else None,
        best_objective_bound=(
            float(solver.best_objective_bound) if status_code in bound_states else None
        ),
    )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    payload_solution: dict[str, Any] = {}
    if solution.schedule is not None:
        payload_solution = {
            "makespan": solution.objective,
            "schedule": [entry.model_dump(mode="json") for entry in solution.schedule],
        }
    return {
        "status": solution.status,
        "objective": solution.objective,
        "solution": payload_solution,
        "best_objective_bound": solution.best_objective_bound,
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    write_output(serialize_solution(solve(parse_input(read_input(_data_path())))))


if __name__ == "__main__":
    main()
