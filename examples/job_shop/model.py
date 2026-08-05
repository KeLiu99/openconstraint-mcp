"""Reference CP-SAT script: job shop scheduling.

Loads a job shop data file (default: data_ft06.json, the 6x6 benchmark) and
minimizes makespan.
Run from the repository root:
    uv run examples/job_shop/model.py data_ft06.json
"""

import collections
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ortools.sat.python import cp_model


@dataclass(frozen=True)
class TaskSpec:
    machine: int
    duration: int


@dataclass(frozen=True)
class ProblemInstance:
    jobs: list[list[TaskSpec]]
    num_machines: int


@dataclass(frozen=True)
class ScheduleEntry:
    job: int
    task: int
    machine: int
    start: int
    duration: int
    end: int


@dataclass(frozen=True)
class Solution:
    status: str
    schedule: list[ScheduleEntry] | None = None
    objective: int | None = None
    best_objective_bound: float | None = None


def read_input() -> dict:
    data_path = Path(__file__).parent / (sys.argv[1] if len(sys.argv) > 1 else "data_ft06.json")
    return json.loads(data_path.read_text())


def parse_input(raw: dict) -> ProblemInstance:
    jobs = [
        [TaskSpec(machine=machine, duration=duration) for machine, duration in job]
        for job in raw["jobs"]
    ]
    return ProblemInstance(jobs=jobs, num_machines=raw["num_machines"])


def solve(instance: ProblemInstance) -> Solution:
    jobs = instance.jobs
    num_machines = instance.num_machines
    horizon = sum(task_spec.duration for job in jobs for task_spec in job)

    model = cp_model.CpModel()

    Task = collections.namedtuple("Task", "start end interval")
    tasks: dict[tuple[int, int], Task] = {}
    machine_to_intervals: dict[int, list] = collections.defaultdict(list)

    for job_id, job in enumerate(jobs):
        for task_id, task_spec in enumerate(job):
            suffix = f"_{job_id}_{task_id}"
            start = model.new_int_var(0, horizon, "start" + suffix)
            end = model.new_int_var(0, horizon, "end" + suffix)
            interval = model.new_interval_var(start, task_spec.duration, end, "interval" + suffix)
            tasks[job_id, task_id] = Task(start, end, interval)
            machine_to_intervals[task_spec.machine].append(interval)

    # A machine can only work on one task at a time.
    for machine in range(num_machines):
        model.add_no_overlap(machine_to_intervals[machine])

    # Tasks within a job run in the given order.
    for job_id, job in enumerate(jobs):
        for task_id in range(len(job) - 1):
            model.add(tasks[job_id, task_id + 1].start >= tasks[job_id, task_id].end)

    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(
        makespan, [tasks[job_id, len(job) - 1].end for job_id, job in enumerate(jobs)]
    )
    model.minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))
    solver.parameters.num_workers = 1
    status = solver.solve(model)

    status_map = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }

    schedule = None
    objective = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        schedule = [
            ScheduleEntry(
                job=job_id,
                task=task_id,
                machine=task_spec.machine,
                start=solver.value(tasks[job_id, task_id].start),
                duration=task_spec.duration,
                end=solver.value(tasks[job_id, task_id].end),
            )
            for job_id, job in enumerate(jobs)
            for task_id, task_spec in enumerate(job)
        ]
        objective = solver.value(makespan)

    bound_states = (cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN)
    best_objective_bound = float(solver.best_objective_bound) if status in bound_states else None

    return Solution(
        status=status_map.get(status, "error"),
        schedule=schedule,
        objective=objective,
        best_objective_bound=best_objective_bound,
    )


def serialize_solution(solution: Solution) -> dict:
    payload_solution: dict = {}
    if solution.schedule is not None:
        payload_solution = {
            "makespan": solution.objective,
            "schedule": [
                {
                    "job": entry.job,
                    "task": entry.task,
                    "machine": entry.machine,
                    "start": entry.start,
                    "duration": entry.duration,
                    "end": entry.end,
                }
                for entry in solution.schedule
            ],
        }
    return {
        "status": solution.status,
        "objective": solution.objective,
        "solution": payload_solution,
        "best_objective_bound": solution.best_objective_bound,
    }


def write_output(payload: dict) -> None:
    print(json.dumps(payload))


def main() -> None:
    write_output(serialize_solution(solve(parse_input(read_input()))))


if __name__ == "__main__":
    main()
