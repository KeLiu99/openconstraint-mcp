import itertools
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    payload = json.load(f)
solution = payload.get("solution") or payload.get("result", {}).get("solution") or {}
errors = []
weeks = [solution.get(f"week_{i}") for i in range(1, 11)]
if any(week is None for week in weeks):
    errors.append("missing week_1..week_10")
seen = set()
for index, groups in enumerate(weeks, start=1):
    if groups is None:
        continue
    players = sorted(player for group in groups for player in group)
    if players != list(range(1, 22)):
        errors.append(f"week_{index} is not a partition of 1..21")
    for group in groups:
        if len(group) != 3:
            errors.append(f"week_{index} has non-size-3 group {group}")
        for pair in itertools.combinations(sorted(group), 2):
            if pair in seen:
                errors.append(f"pair meets twice: {pair}")
            seen.add(pair)
if len(seen) != 210:
    errors.append(f"expected 210 unique pairs, saw {len(seen)}")
print(
    json.dumps(
        {
            "status": "rejected" if errors else "accepted",
            "errors": errors,
            "details": {"weeks": len(weeks), "pairs": len(seen)},
        }
    )
)
