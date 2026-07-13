"""Reference CP-SAT script: simple task-to-agent assignment.

Canonical emit snippet (see pyexec/core.py docstring for the contract).
Runs standalone: python assignment.py
"""

import json
import os

from ortools.sat.python import cp_model

model = cp_model.CpModel()

# 3 tasks, 2 agents; agent 0 handles tasks 0 and 2, agent 1 handles task 1.
num_tasks = 3
num_agents = 2

x = [[model.new_bool_var(f"x[{t}][{a}]") for a in range(num_agents)] for t in range(num_tasks)]

# Each task assigned to exactly one agent
for t in range(num_tasks):
    model.add_exactly_one(x[t][a] for a in range(num_agents))

# Minimize total cost (agent 0 costs 1/task, agent 1 costs 2/task)
costs = [1, 2]
total_cost = sum(x[t][a] * costs[a] for t in range(num_tasks) for a in range(num_agents))
model.minimize(total_cost)

solver = cp_model.CpSolver()
# Seed replay protocol: read the seed save_verified_cpsat_python injects via
# OPENCONSTRAINT_MCP_CPSAT_SEED; fall back to 42 for a plain run.
solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))
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
    for t in range(num_tasks):
        for a in range(num_agents):
            if solver.value(x[t][a]):
                solution[f"task_{t}"] = a

print(
    json.dumps(
        {
            "status": status_map.get(status, "error"),
            "objective": int(solver.objective_value) if model.has_objective() else None,
            "solution": solution,
        }
    )
)
