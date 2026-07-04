import json

from ortools.sat.python import cp_model

ORDER = 12
UPPER_BOUND = 91
TIME_LIMIT_S = 100.0

model = cp_model.CpModel()
marks = [model.NewIntVar(0, UPPER_BOUND, f"mark_{i}") for i in range(ORDER)]
model.Add(marks[0] == 0)
for i in range(ORDER - 1):
    model.Add(marks[i] < marks[i + 1])

diffs = []
for i in range(ORDER):
    for j in range(i + 1, ORDER):
        d = model.NewIntVar(1, UPPER_BOUND, f"diff_{i}_{j}")
        model.Add(d == marks[j] - marks[i])
        diffs.append(d)
model.AddAllDifferent(diffs)
model.Add(marks[1] - marks[0] < marks[ORDER - 1] - marks[ORDER - 2])
model.Minimize(marks[-1])

solver = cp_model.CpSolver()
solver.parameters.num_workers = 6
solver.parameters.max_time_in_seconds = TIME_LIMIT_S

status = solver.Solve(model)

_STATUS_MAP = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}
result_status = _STATUS_MAP.get(status, "unknown")

if result_status in ("optimal", "feasible"):
    solution = {f"mark_{i}": solver.Value(marks[i]) for i in range(ORDER)}
    objective = solver.Value(marks[-1])
else:
    solution = {}
    objective = None

print(json.dumps({"status": result_status, "objective": objective, "solution": solution}))
