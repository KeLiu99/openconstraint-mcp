"""Reference CP-SAT script: job shop scheduling.

Loads a job shop data file (default: data_ft06.json, the 6x6 benchmark) and
minimizes makespan.
Runs standalone: python model.py [data_file.json]
"""

import collections
import json
import sys
from pathlib import Path

from ortools.sat.python import cp_model

DATA_PATH = Path(__file__).parent / (sys.argv[1] if len(sys.argv) > 1 else "data_ft06.json")

data = json.loads(DATA_PATH.read_text())
jobs: list[list[list[int]]] = data["jobs"]
num_machines: int = data["num_machines"]
horizon = sum(duration for job in jobs for _, duration in job)

model = cp_model.CpModel()

Task = collections.namedtuple("Task", "start end interval")
tasks: dict[tuple[int, int], Task] = {}
machine_to_intervals: dict[int, list] = collections.defaultdict(list)

for job_id, job in enumerate(jobs):
    for task_id, (machine, duration) in enumerate(job):
        suffix = f"_{job_id}_{task_id}"
        start = model.new_int_var(0, horizon, "start" + suffix)
        end = model.new_int_var(0, horizon, "end" + suffix)
        interval = model.new_interval_var(start, duration, end, "interval" + suffix)
        tasks[job_id, task_id] = Task(start, end, interval)
        machine_to_intervals[machine].append(interval)

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
solver.parameters.random_seed = 42
solver.parameters.num_workers = 1
status = solver.solve(model)

status_map = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}

solution = {}
objective = None
if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    schedule = [
        {
            "job": job_id,
            "task": task_id,
            "machine": machine,
            "start": solver.value(tasks[job_id, task_id].start),
            "duration": duration,
            "end": solver.value(tasks[job_id, task_id].end),
        }
        for job_id, job in enumerate(jobs)
        for task_id, (machine, duration) in enumerate(job)
    ]
    objective = solver.value(makespan)
    solution = {"makespan": objective, "schedule": schedule}

print(
    json.dumps(
        {
            "status": status_map.get(status, "error"),
            "objective": objective,
            "solution": solution,
        }
    )
)
