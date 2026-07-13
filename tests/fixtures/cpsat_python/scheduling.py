"""Reference CP-SAT script: minimize makespan for 3 tasks on 2 machines.

Canonical emit snippet (see pyexec/core.py docstring for the contract).
Runs standalone: python scheduling.py
"""

import json

from ortools.sat.python import cp_model

model = cp_model.CpModel()

# 3 tasks; durations = [3, 2, 4]; each must run on a single machine (no overlap)
durations = [3, 2, 4]
horizon = sum(durations)

starts = [model.new_int_var(0, horizon, f"start_{i}") for i in range(len(durations))]
ends = [model.new_int_var(0, horizon, f"end_{i}") for i in range(len(durations))]
intervals = [
    model.new_interval_var(starts[i], durations[i], ends[i], f"interval_{i}")
    for i in range(len(durations))
]

# No overlap on single machine
model.add_no_overlap(intervals)

makespan = model.new_int_var(0, horizon, "makespan")
model.add_max_equality(makespan, ends)
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
if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    solution = {f"task_{i}_start": solver.value(starts[i]) for i in range(len(durations))}
    solution["makespan"] = solver.value(makespan)

print(
    json.dumps(
        {
            "status": status_map.get(status, "error"),
            "objective": int(solver.objective_value) if model.has_objective() else None,
            "solution": solution,
        }
    )
)
