"""Reference CP-SAT script: flexible job shop scheduling (optional intervals).

Loads a flexible job shop data file (default: data_mk01.json, the 10x6
Brandimarte instance whose proven optimum is 40) and minimizes makespan.

Formulation: the canonical OR-Tools encoding. Every task gets a start, an end
and a duration variable. Every *alternative* gets a presence literal and an
OPTIONAL interval whose start/end/duration are pinned to the task's own
variables while that literal is true; `add_exactly_one` over the literals
picks one machine. Each machine then gets a single `add_no_overlap` over the
optional intervals that could land on it, so the machine-exclusion constraint
is enforced by CP-SAT's disjunctive propagator and is automatically inactive
for alternatives that were not selected.

A task with exactly one alternative skips the optional machinery entirely and
uses a plain fixed-duration interval: there is no choice to model, and a
non-optional interval is cheaper to propagate. On mk01 and mk15 that covers
29% and 23% of tasks respectively; on the behnke instance it covers none.

Measured (single worker, seed 42, 600s cap; raw runs in results/ -- mk01
current, mk15 and behnke predating a later stdout change that added
num_tasks, kept rather than re-solved because no change since touched the
model itself):
- mk01: optimal 40 in 0.1s.
- mk15: best 347, bound 333. The bound REACHES the known optimum of 333, so
  the shortfall is finding the matching schedule, not proving it -- which is
  what num_workers=1 costs, since it drops the LNS workers that specialise in
  improving incumbents. Best incumbent of the three formulations.
- behnke lar04_1: best 504, bound 77. That bound is exactly the trivial
  max-job-min-length value, i.e. `no_overlap` contributed nothing globally
  across 60 machines. And 504 is 18% WORSE than a greedy dispatching
  heuristic's 427, so at this scale this model is not worth its runtime.

Runs standalone: python model.py [data_file.json] [time_limit_seconds] [results_dir]
"""

import collections
import json
import sys
import time
from pathlib import Path

from ortools.sat.python import cp_model

DATA_PATH = Path(__file__).parent / (sys.argv[1] if len(sys.argv) > 1 else "data_mk01.json")
TIME_LIMIT = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
# Saving the result is OPT-IN. These scripts are meant to be run through the MCP
# file tools against the user's own checkout, and a plain solve must not mutate
# it -- so nothing is written unless this third argument names a directory (the
# committed runs used `results`). A relative name resolves next to this script,
# so the write target never depends on the caller's cwd; an absolute path is
# taken as given.
RESULTS_DIR = (Path(__file__).parent / sys.argv[3]) if len(sys.argv) > 3 else None

data = json.loads(DATA_PATH.read_text())
# jobs -> job -> task -> alternative -> [machine, duration]
jobs: list[list[list[list[int]]]] = data["jobs"]
num_machines: int = data["num_machines"]

# Serializing every task at its slowest alternative is a valid upper bound.
horizon = sum(max(duration for _, duration in task) for job in jobs for task in job)

build_start = time.monotonic()
model = cp_model.CpModel()

Task = collections.namedtuple("Task", "start end duration")
tasks: dict[tuple[int, int], Task] = {}
machine_to_intervals: dict[int, list] = collections.defaultdict(list)
# Per task, the presence literal of each alternative -- empty for a task with a
# single alternative, whose machine is not a decision.
presences: dict[tuple[int, int], list] = {}

for job_id, job in enumerate(jobs):
    for task_id, alternatives in enumerate(job):
        suffix = f"_{job_id}_{task_id}"
        durations = [duration for _, duration in alternatives]
        start = model.new_int_var(0, horizon, "start" + suffix)
        end = model.new_int_var(0, horizon, "end" + suffix)

        if len(alternatives) == 1:
            machine, fixed_duration = alternatives[0]
            interval = model.new_interval_var(start, fixed_duration, end, "interval" + suffix)
            machine_to_intervals[machine].append(interval)
            tasks[job_id, task_id] = Task(start, end, model.new_constant(fixed_duration))
            presences[job_id, task_id] = []
            continue

        duration = model.new_int_var(min(durations), max(durations), "duration" + suffix)
        model.new_interval_var(start, duration, end, "interval" + suffix)

        literals = []
        for alt_id, (machine, alt_duration) in enumerate(alternatives):
            alt_suffix = f"{suffix}_{alt_id}"
            literal = model.new_bool_var("presence" + alt_suffix)
            alt_start = model.new_int_var(0, horizon, "alt_start" + alt_suffix)
            alt_end = model.new_int_var(0, horizon, "alt_end" + alt_suffix)
            alt_interval = model.new_optional_interval_var(
                alt_start, alt_duration, alt_end, literal, "alt_interval" + alt_suffix
            )
            # Pin the chosen alternative to the task's own timing.
            model.add(alt_start == start).only_enforce_if(literal)
            model.add(alt_end == end).only_enforce_if(literal)
            model.add(duration == alt_duration).only_enforce_if(literal)
            machine_to_intervals[machine].append(alt_interval)
            literals.append(literal)

        model.add_exactly_one(literals)
        tasks[job_id, task_id] = Task(start, end, duration)
        presences[job_id, task_id] = literals

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
build_seconds = time.monotonic() - build_start

solver = cp_model.CpSolver()
solver.parameters.random_seed = 42
solver.parameters.num_workers = 1
solver.parameters.max_time_in_seconds = TIME_LIMIT
status = solver.solve(model)

status_map = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}


def chosen_alternative(job_id: int, task_id: int) -> list[int]:
    """The [machine, duration] pair the solver selected for one task."""
    alternatives = jobs[job_id][task_id]
    literals = presences[job_id, task_id]
    if not literals:
        return alternatives[0]
    for alt_id, literal in enumerate(literals):
        if solver.boolean_value(literal):
            return alternatives[alt_id]
    raise AssertionError(f"no alternative selected for job {job_id} task {task_id}")


schedule: list[dict[str, int]] = []
objective = None
if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    for job_id, job in enumerate(jobs):
        for task_id in range(len(job)):
            machine, duration = chosen_alternative(job_id, task_id)
            schedule.append(
                {
                    "job": job_id,
                    "task": task_id,
                    "machine": machine,
                    "start": solver.value(tasks[job_id, task_id].start),
                    "duration": duration,
                    "end": solver.value(tasks[job_id, task_id].end),
                }
            )
    objective = solver.value(makespan)

stats = {
    "formulation": "optional_intervals",
    "instance": DATA_PATH.name,
    "time_limit": TIME_LIMIT,
    "build_seconds": round(build_seconds, 3),
    "wall_time": round(solver.wall_time, 3),
    "num_booleans": solver.num_booleans,
    "num_conflicts": solver.num_conflicts,
    "num_branches": solver.num_branches,
    "model_variables": len(model.proto.variables),
    "model_constraints": len(model.proto.constraints),
}
RESULT_PATH = (
    RESULTS_DIR / f"{stats['formulation']}__{DATA_PATH.stem}.json"
    if RESULTS_DIR is not None
    else None
)

# The result is ALWAYS printed verbatim, and written to a file only when the
# caller opted in. The printed `solution` must CONTAIN the schedule, not
# describe it: the checked MCP tools build the checker's payload from this
# stdout object, so a summary that merely points at a saved file leaves the
# checker with nothing to grade and it reports an ungradeable payload. The cost
# is real -- a 500-task behnke solution is ~40 KB of tool response -- and it is
# the price of an in-band verification pass. It carries no path to the saved
# file either: the name is derivable from the formulation and instance already
# in `stats`, and an absolute path would bake this machine's filesystem into
# every committed artifact under results/.
solution = (
    {
        "makespan": objective,
        "schedule": schedule,
        "instance": DATA_PATH.name,
        "num_tasks": len(schedule),
    }
    if objective is not None
    else {}
)

full = {
    "status": status_map.get(status, "error"),
    "objective": objective,
    "solution": solution,
    "best_objective_bound": solver.best_objective_bound,
    "stats": stats,
}
if RESULT_PATH is not None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(full), encoding="utf-8")

print(json.dumps(full))
