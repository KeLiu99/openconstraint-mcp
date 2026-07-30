"""Reference CP-SAT script: flexible job shop, lean optional-interval encoding.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan. This is model_composite.py with EXACTLY ONE change -- how an
alternative's optional interval is attached -- so any difference between the two
files' results is attributable to the encoding alone.

THE CHANGE. model.py, model_redundant_bounds.py and model_composite.py all
follow the canonical OR-Tools `flexible_jobshop_sat.py` pattern: per alternative
they create a private `alt_start`/`alt_end` pair, wrap them in an optional
interval, and channel them back to the task's own variables:

    alt_start = new_int_var(...); alt_end = new_int_var(...)
    new_optional_interval_var(alt_start, d, alt_end, lit)
    add(alt_start == start).only_enforce_if(lit)
    add(alt_end   == end  ).only_enforce_if(lit)
    add(duration  == d    ).only_enforce_if(lit)

That costs 2 integer variables and 3 enforced constraints per alternative, plus
a main interval and a main duration variable per task. None of it is necessary
for pure FJSP: `add_exactly_one` already guarantees exactly one alternative is
present, so the optional interval can hang directly on the task's OWN start and
end. The present one enforces `end == start + its size`; the absent ones enforce
nothing and stay invisible to `add_no_overlap`.

    new_optional_interval_var(start, d, end, lit)

Measured model sizes (pre-presolve, `len(model.proto.{variables,constraints})`;
"canonical" is model_composite.py, the file this one was forked from, so the
encoding is the only difference between the columns):

    instance      canonical                direct                saved
    mk01            450 vars /   548 con     210 /   212     -53% / -61%
    mk15          3,183     / 3,972        1,365 / 1,365     -57% / -66%
    behnke       29,281     / 38,561      10,261 / 10,281    -65% / -73%

WHY THE CANONICAL FORM EXISTS ANYWAY: the private copies matter when
alternatives need different timing semantics -- machine-dependent setup times,
transfer lags, per-machine calendars -- because then the alternative's interval
is genuinely not the task's interval. problem.txt defers every one of those
extensions, so here the copies are pure overhead. Reintroduce them the moment
setup times arrive.

OPEN QUESTION this file is meant to answer: CP-SAT's presolve may already
collapse the channeling variables, in which case a 3x smaller INPUT model buys
nothing at search time. A smaller model is not automatically a faster one, and
the pre-presolve numbers above deliberately prove nothing about runtime.

Everything else is inherited unchanged from model_composite.py: the machine-load
inequality (the redundant constraint that measurably drove the bound) and the
greedy earliest-completion-time warm start. The global `add_cumulative` is
absent here for the same reason it is absent there -- it was measured to cost
far more than it returned.

Runs standalone: python model_direct_optional_intervals.py [data.json] [seconds] [results_dir]
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

Task = collections.namedtuple("Task", "start end")
tasks: dict[tuple[int, int], Task] = {}
machine_to_intervals: dict[int, list] = collections.defaultdict(list)
presences: dict[tuple[int, int], list] = {}
machine_load_terms: dict[int, list] = collections.defaultdict(list)

for job_id, job in enumerate(jobs):
    for task_id, alternatives in enumerate(job):
        suffix = f"_{job_id}_{task_id}"
        start = model.new_int_var(0, horizon, "start" + suffix)
        end = model.new_int_var(0, horizon, "end" + suffix)

        if len(alternatives) == 1:
            machine, fixed_duration = alternatives[0]
            interval = model.new_interval_var(start, fixed_duration, end, "interval" + suffix)
            machine_to_intervals[machine].append(interval)
            machine_load_terms[machine].append(fixed_duration)
            tasks[job_id, task_id] = Task(start, end)
            presences[job_id, task_id] = []
            continue

        literals = []
        for alt_id, (machine, alt_duration) in enumerate(alternatives):
            alt_suffix = f"{suffix}_{alt_id}"
            isPresented = model.new_bool_var("presence" + alt_suffix)
            # The direct form: the optional interval IS the task's interval while
            # this alternative is present, so it needs no private copies and no
            # channeling constraints. When absent it enforces nothing.
            alt_interval = model.new_optional_interval_var(
                start, alt_duration, end, isPresented, "alt_interval" + alt_suffix
            )
            machine_to_intervals[machine].append(alt_interval)
            machine_load_terms[machine].append(alt_duration * isPresented)
            literals.append(isPresented)

        model.add_exactly_one(literals)
        tasks[job_id, task_id] = Task(start, end)
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

# Inherited from model_composite.py: no machine can be busy longer than the
# makespan. Linear, so it reaches the LP relaxation that the disjunctive
# propagator cannot.
for machine in range(num_machines):
    terms = machine_load_terms[machine]
    if terms:
        model.add(sum(terms) <= makespan)

model.minimize(makespan)


def greedy_schedule() -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], int]]:
    """Earliest-completion-time list scheduling, used as a warm-start hint.

    Identical to the routines in model_pairwise_disjunctive.py and
    model_composite.py, so the hint is the same wherever it is used.
    """
    num_jobs = len(jobs)
    job_next_task = [0] * num_jobs
    job_ready = [0] * num_jobs
    machine_ready = [0] * num_machines
    starts: dict[tuple[int, int], int] = {}
    choices: dict[tuple[int, int], int] = {}
    remaining = sum(len(job) for job in jobs)

    while remaining:
        best = None
        for job_id in range(num_jobs):
            task_id = job_next_task[job_id]
            if task_id >= len(jobs[job_id]):
                continue
            for alt_id, (machine, duration) in enumerate(jobs[job_id][task_id]):
                start = max(job_ready[job_id], machine_ready[machine])
                completion = start + duration
                if best is None or completion < best[0]:
                    best = (completion, job_id, task_id, alt_id, machine, start)
        assert best is not None
        _completion, job_id, task_id, alt_id, machine, start = best
        _machine, duration = jobs[job_id][task_id][alt_id]
        starts[job_id, task_id] = start
        choices[job_id, task_id] = alt_id
        job_ready[job_id] = start + duration
        machine_ready[machine] = start + duration
        job_next_task[job_id] += 1
        remaining -= 1

    return starts, choices


greedy_starts, greedy_choices = greedy_schedule()
for key, task in tasks.items():
    model.add_hint(task.start, greedy_starts[key])
for key, task_literals in presences.items():
    for alt_id, isPresented in enumerate(task_literals):
        model.add_hint(isPresented, alt_id == greedy_choices[key])
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
    for alt_id, isPresented in enumerate(literals):
        if solver.boolean_value(isPresented):
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
    "formulation": "direct_optional_intervals",
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
