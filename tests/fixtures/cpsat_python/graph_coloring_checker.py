"""Checker script for graph_coloring.py.

Validates that no two adjacent vertices share the same color.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str).
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {}}
- "accepted" with an empty errors list is the only passing verdict.

Runs standalone: python graph_coloring_checker.py <payload.json>
"""

import json
import sys

EDGES = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0), (0, 2)]

payload = json.load(open(sys.argv[1]))
solution = payload.get("solution", {})

errors = []
for u, v in EDGES:
    cu = solution.get(f"color_{u}")
    cv = solution.get(f"color_{v}")
    if cu is None or cv is None:
        errors.append(f"missing color for vertex {u if cu is None else v}")
    elif cu == cv:
        errors.append(f"vertices {u} and {v} share color {cu} (adjacent)")

status = "accepted" if not errors else "rejected"
print(json.dumps({"status": status, "errors": errors, "details": {}}))
