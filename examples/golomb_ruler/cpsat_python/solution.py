import json

from ortools.sat.python import cp_model

ORDER = 12
UPPER_BOUND = 91
TIME_LIMIT_S = 100.0

model = cp_model.CpModel()
marks = [model.new_int_var(0, UPPER_BOUND, f"mark_{i}") for i in range(ORDER)]
model.add(marks[0] == 0)
for i in range(ORDER - 1):
    model.add(marks[i] < marks[i + 1])

diffs = []
for i in range(ORDER):
    for j in range(i + 1, ORDER):
        d = model.new_int_var(1, UPPER_BOUND, f"diff_{i}_{j}")
        model.add(d == marks[j] - marks[i])
        diffs.append(d)
model.add_all_different(diffs)
model.add(marks[1] - marks[0] < marks[ORDER - 1] - marks[ORDER - 2])
model.minimize(marks[-1])

solver = cp_model.CpSolver()
solver.parameters.num_workers = 8
solver.parameters.max_time_in_seconds = TIME_LIMIT_S

status = solver.solve(model)

_STATUS_MAP = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}
result_status = _STATUS_MAP.get(status, "unknown")

if result_status in ("optimal", "feasible"):
    solution = {f"mark_{i}": solver.value(marks[i]) for i in range(ORDER)}
    objective = solver.value(marks[-1])
else:
    solution = {}
    objective = None

print(json.dumps({"status": result_status, "objective": objective, "solution": solution}))
