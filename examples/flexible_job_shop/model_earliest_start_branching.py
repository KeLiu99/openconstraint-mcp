"""Reference CP-SAT script: flexible job shop, earliest-start branching.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan. This is model_direct_optional_intervals.py with EXACTLY ONE change --
two `add_decision_strategy` calls -- so any difference between the two files'
results is attributable to the search order alone. Every model constraint, the
machine-load inequality, the greedy warm start and all solver parameters are
inherited verbatim.

THE CHANGE. The other files supply CP-SAT with a starting POINT (the greedy
`add_hint`) but never with a starting ORDER. This one tells the solver which
task to decide next and what to decide about it: repeatedly branch on the task
whose start can still happen earliest, and assign it exactly that earliest
start.

    add_decision_strategy(starts, CHOOSE_LOWEST_MIN, SELECT_MIN_VALUE)

CHOOSE_LOWEST_MIN picks the variable with the smallest remaining lower bound
(the task that could still go first); SELECT_MIN_VALUE then assigns that lower
bound (start it as early as allowed). In scheduling terms that is the textbook
non-delay dispatching rule, known in the literature as a serial schedule
generation scheme -- but the file is named for what the two constants DO, not
for the paper. It is a good FIRST DIVE for a makespan objective: every decision
left-shifts a task, so the dive bottoms out in a complete, tight schedule
instead of wandering. The second strategy (ends
and makespan, CHOOSE_FIRST/SELECT_MIN_VALUE) is bookkeeping -- it keeps the
union of strategies total, as `add_decision_strategy` requires, and never
actually branches, because propagation fixes an end as soon as its start and
presence literal are known.

WHAT IS DELIBERATELY *NOT* CHANGED: `solver.parameters.search_branching`. It
stays at the default AUTOMATIC_SEARCH, which per sat_parameters.proto fixes
literals with the SAT solver's own heuristics and then branches on integer
variables "using the fixed search specified by the user OR OUR DEFAULT ONE". So
this file replaces exactly one component -- the integer branching heuristic --
and leaves the clause learning, LP relaxation and pseudo-cost machinery in
charge of everything else. Two stronger settings were measured and both LOST:

    mk15, 60 s, single worker, seed 42, best incumbent (bound was 332 in all):

    configuration                                     makespan   conflicts
    no decision strategy (model_direct_optional...)        360       8,476
    this file (strategy + AUTOMATIC_SEARCH)                355       8,625
    strategy + PARTIAL_FIXED_SEARCH                        376      10,302
    strategy + FIXED_SEARCH                                570      60,852
    strategy + FIXED_SEARCH, greedy hint removed           615      63,031

FIXED_SEARCH is the instructive failure. Handing the solver a complete branching
order sounds like more guidance, but it switches OFF the LP- and pseudo-cost-
guided branching that is what actually drives the makespan down; the search then
spends its budget proving small things about a bad dive. At 60 s the lower bound
sat at 332 in every configuration above, which locates the effect: a decision
strategy is primarily a primal heuristic.

NO STRATEGY OVER THE PRESENCE LITERALS, for the same reason. Branching machine
choice first is the natural way to write this, but under AUTOMATIC_SEARCH the SAT
solver owns the literals and a user strategy over them never fires. That is
measured, not assumed: a variant adding
`add_decision_strategy(literals, CHOOSE_FIRST, SELECT_MAX_VALUE)` ahead of the
starts returned an identical 355 on the same mk15 run. Dead code, so it is not
here. It would start mattering under FIXED_SEARCH -- which is exactly the
configuration the table above rules out.

This file adds no variables and no constraints: mk01 stays at 210 / 212, the
same as model_direct_optional_intervals.py. A decision strategy changes only the
order decisions are taken in.

AT A LONGER BUDGET THE GAIN WIDENS RATHER THAN WASHING OUT. Both files rerun on
mk15 at 1200 s, submitted concurrently so they saw identical machine load:

    file                            makespan   bound   conflicts
    model_direct_optional_intervals      345     332      528,687
    this file                            339     333      468,087

So the head-start reading is wrong: 360 -> 355 at 60 s became 345 -> 339 at
1200 s. The 339 schedule was verified `accepted` by checker.py. Note the bound
column too: `best_objective_bound` is a LOWER bound, so this file proved "no
schedule beats 333" while the baseline proved only "no schedule beats 332" --
a strictly stronger statement from the same budget. That is why the claim above
is "primarily" a primal heuristic and not "only": a better incumbent found
sooner also prunes, and here it was worth the last unit of bound.

NEITHER RUN PROVED AN OPTIMUM, and the status field says so -- both report
`feasible`, not `optimal`. Proving optimality needs the two bounds to MEET: a
333 schedule in hand alongside the 333 bound. This run has a 339 schedule, so
what it established is 333 <= optimum <= 339 and nothing narrower. That the
optimum is exactly 333 is FJSPLib's result (recorded in problem.txt and in the
instance's `known_optimal_makespan`), not this run's. Closing the remaining 6
units is primal work -- constructing a better schedule -- not bound work.

OPEN QUESTION still outstanding: behnke, and replication. Every number here is
ONE run per configuration. These scripts are capped by WALL CLOCK, not
`max_deterministic_time`, so `random_seed = 42` fixes the search but not how much
of it fits in the budget -- a rerun under different machine load can land
elsewhere. Two paired runs agreeing in the same direction is encouraging, not a
confidence interval.

Runs standalone: python model_earliest_start_branching.py [data.json] [seconds] [results_dir]
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

# THE CHANGE. Branch on the task whose start can still happen earliest, and give
# it that earliest start -- a serial schedule generation scheme expressed as a
# CP-SAT decision strategy. The trailing stage on the ends and the makespan only
# keeps the strategy total; propagation fixes them once a start and its presence
# literal are known.
model.add_decision_strategy(
    [task.start for task in tasks.values()],
    cp_model.CHOOSE_LOWEST_MIN,
    cp_model.SELECT_MIN_VALUE,
)
model.add_decision_strategy(
    [task.end for task in tasks.values()] + [makespan],
    cp_model.CHOOSE_FIRST,
    cp_model.SELECT_MIN_VALUE,
)
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
    "formulation": "earliest_start_branching",
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
