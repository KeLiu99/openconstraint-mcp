import json
import sys


def main() -> None:
    payload_path = sys.argv[1]
    with open(payload_path) as f:
        payload = json.load(f)

    solution = payload.get("solution") or {}
    objective = payload.get("objective")
    errors = []
    order = 12

    marks = None
    try:
        marks = [int(solution[f"mark_{i}"]) for i in range(order)]
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"solution missing/invalid mark keys: {exc}")

    if marks is not None:
        if marks[0] != 0:
            errors.append(f"mark_0 must be 0, got {marks[0]}")
        for i in range(order - 1):
            if not (marks[i] < marks[i + 1]):
                errors.append(
                    f"marks not strictly increasing at index {i}: {marks[i]} >= {marks[i + 1]}"
                )
        diffs = []
        for i in range(order):
            for j in range(i + 1, order):
                diffs.append(marks[j] - marks[i])
        if len(set(diffs)) != len(diffs):
            errors.append("pairwise differences are not all distinct (not a valid Golomb ruler)")
        if objective is None or int(objective) != marks[-1]:
            errors.append(f"reported objective {objective} does not match ruler length {marks[-1]}")

    status = "accepted" if not errors else "rejected"
    print(json.dumps({"status": status, "errors": errors, "details": {"order": order}}))


if __name__ == "__main__":
    main()
