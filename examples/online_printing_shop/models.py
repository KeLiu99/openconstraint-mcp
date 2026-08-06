"""Canonical data contract for Online Printing Shop instances."""

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

FORMAT_NAME = "openconstraint.ops.instance"
FORMAT_VERSION = "1.0"

Identifier = Annotated[str, StringConstraints(min_length=1)]
TimeTick = Annotated[int, Field(ge=0)]
Theta = Annotated[float, Field(gt=0, le=1)]


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

    format: Literal[FORMAT_NAME]
    format_version: Literal[FORMAT_VERSION]
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
            eligible = {
                operation_id
                for operation_id, operation in self.operations.items()
                if machine_id in operation.machine_options
            }
            if set(machine.setup_times.first) != eligible:
                raise ValueError(
                    f"machine {machine_id!r} first setup entries must match eligible operations"
                )
            expected_sources = eligible if len(eligible) > 1 else set()
            if set(machine.setup_times.transitions) != expected_sources:
                raise ValueError(
                    f"machine {machine_id!r} transition sources must match eligible operations"
                )
            for source_id, targets in machine.setup_times.transitions.items():
                if set(targets) != eligible - {source_id}:
                    raise ValueError(
                        f"machine {machine_id!r} transitions from {source_id!r} must cover "
                        "every other eligible operation"
                    )

        return self


def read_input(data_path: Path) -> dict[str, Any]:
    """Read one raw OPS instance object from a JSON file."""

    return json.loads(data_path.read_text(encoding="utf-8"))


def parse_input(raw: dict[str, Any]) -> OPSInstance:
    """Validate a raw object and return the typed solver input record."""

    return OPSInstance.model_validate(raw)
