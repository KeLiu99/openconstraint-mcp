"""Reference CP-SAT script: flexible job shop with redundant global bounds.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan. The core encoding is IDENTICAL to model.py -- optional intervals per
alternative, `add_exactly_one` on the presence literals, `add_no_overlap` per
machine -- so the only difference between the two files is the block of
redundant constraints marked below.

Three implied-but-not-propagated facts are stated explicitly:

1. MACHINE LOAD. All work assigned to one machine runs sequentially inside
   [0, makespan], so `sum(d_a * lit_a for alternatives on m) <= makespan`.
2. JOB LENGTH. A job's tasks run in sequence, each taking at least its
   cheapest alternative, so `makespan >= sum(min duration)` over the job.
3. GLOBAL ENERGY. At most `num_machines` tasks can run at once, expressed as
   `add_cumulative(all task intervals, demands=1, capacity=num_machines)`.

None of these change the feasible set: every one is a consequence of
constraints model.py already has. They are here because CP-SAT's disjunctive
propagator reasons about machine exclusion COMBINATORIALLY, while (1) and (2)
are linear and feed the LP relaxation directly -- and on a minimization
problem it is usually the lower bound, not finding good solutions, that is
slow.

HONEST CAVEAT: this is a bundle of three changes, not an ablation. Its results
show that redundant bounds help on this instance family, but not which of the
three carried it. Separating them would need three more variants and three
times the benchmark budget.

Measured (single worker, seed 42, 600s cap; raw runs in results/): whether this
pays off depends entirely on scale.
- mk01: optimal 40 in 0.1s.
- mk15: best 363 against model.py's 347, bound 332 against 333 -- no gain and
  a small loss. Conflicts fell 4x (141k vs 588k) at equal bound quality, which
  says each node simply became more expensive: the add_cumulative over 284
  variable-duration intervals cost more propagation time than its reasoning
  returned. The textbook trick loses here.
- behnke lar04_1: bound 344 against model.py's 77 -- a 4.5x improvement, and
  the only nontrivial lower bound any of the three produced at this scale. The
  incumbent is simultaneously the worst of the three (624, versus a greedy
  heuristic's 427), because the added propagation crowds out search.

So the machine-load inequality buys BOUNDS, not SOLUTIONS -- and at 60
machines that trade is worth making, while at 15 it is not.

On the 344: the FJSPLib catalog lists 103 as this instance's best known lower
bound. All three constraints here are provably implied by FJSP, and this
model's bounds are sound wherever ground truth exists (mk01: 344's counterpart
is 40, exactly the optimum; mk15: 332 <= the true 333). Treat 344 as a
legitimate bound from this run, NOT as a claimed improvement on the
literature: that would require multi-seed replication, an independent solver,
and confirmation that the catalog figure is current.

Runs standalone: python model_redundant_bounds.py [data.json] [seconds] [results_dir]
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

horizon = sum(max(duration for _, duration in task) for job in jobs for task in job)

build_start = time.monotonic()
model = cp_model.CpModel()

Task = collections.namedtuple("Task", "start end duration interval")
tasks: dict[tuple[int, int], Task] = {}
machine_to_intervals: dict[int, list] = collections.defaultdict(list)
presences: dict[tuple[int, int], list] = {}
# machine -> linear terms for its total assigned processing time. A forced
# alternative contributes a plain int; a chosen one contributes d * literal.
machine_load_terms: dict[int, list] = collections.defaultdict(list)

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
            machine_load_terms[machine].append(fixed_duration)
            tasks[job_id, task_id] = Task(start, end, model.new_constant(fixed_duration), interval)
            presences[job_id, task_id] = []
            continue

        duration = model.new_int_var(min(durations), max(durations), "duration" + suffix)
        interval = model.new_interval_var(start, duration, end, "interval" + suffix)

        literals = []
        for alt_id, (machine, alt_duration) in enumerate(alternatives):
            alt_suffix = f"{suffix}_{alt_id}"
            literal = model.new_bool_var("presence" + alt_suffix)
            alt_start = model.new_int_var(0, horizon, "alt_start" + alt_suffix)
            alt_end = model.new_int_var(0, horizon, "alt_end" + alt_suffix)
            alt_interval = model.new_optional_interval_var(
                alt_start, alt_duration, alt_end, literal, "alt_interval" + alt_suffix
            )
            model.add(alt_start == start).only_enforce_if(literal)
            model.add(alt_end == end).only_enforce_if(literal)
            model.add(duration == alt_duration).only_enforce_if(literal)
            machine_to_intervals[machine].append(alt_interval)
            machine_load_terms[machine].append(alt_duration * literal)
            literals.append(literal)

        model.add_exactly_one(literals)
        tasks[job_id, task_id] = Task(start, end, duration, interval)
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

# --- redundant constraints: the ONLY difference from model.py ------------------

# (1) No machine can be busy longer than the makespan.
for machine in range(num_machines):
    terms = machine_load_terms[machine]
    if terms:
        model.add(sum(terms) <= makespan)

# (2) A job cannot finish faster than its cheapest route through its tasks.
for job in jobs:
    model.add(makespan >= sum(min(duration for _, duration in task) for task in job))

# (3) At most num_machines tasks can be in progress simultaneously.
model.add_cumulative(
    [task.interval for task in tasks.values()],
    [1] * len(tasks),
    num_machines,
)

# --- end redundant constraints -------------------------------------------------

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
    "formulation": "redundant_bounds",
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
# the price of an in-band verification pass.
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
if RESULT_PATH is not None and solution:
    solution["result_file"] = str(RESULT_PATH)

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
