"""Reference CP-SAT script: 3-coloring of a small graph (satisfaction).

Assign one of 3 colors to each of 5 vertices so no two adjacent vertices
share a color. This is a pure satisfaction problem (no objective).

Runs standalone: python graph_coloring.py
Pair with graph_coloring_checker.py to demonstrate the checker gate of
save_verified_cpsat_python.
"""

import json

from ortools.sat.python import cp_model

# Pentagon graph: 0-1-2-3-4-0, plus diagonal 0-2.
VERTICES = 5
COLORS = 3
EDGES = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0), (0, 2)]

model = cp_model.CpModel()
color = [model.new_int_var(0, COLORS - 1, f"color_{v}") for v in range(VERTICES)]

for u, v in EDGES:
    model.add(color[u] != color[v])

solver = cp_model.CpSolver()
solver.parameters.random_seed = 42
solver.parameters.num_workers = 1
status_code = solver.solve(model)

status_map = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}

solution = {}
if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    solution = {f"color_{v}": solver.value(color[v]) for v in range(VERTICES)}

print(
    json.dumps(
        {
            "status": status_map.get(status_code, "error"),
            "objective": None,
            "solution": solution,
        }
    )
)
