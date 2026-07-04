"""Checker script for a 7-day urgent-care nurse roster CP-SAT script.

Validates a 7-day urgent-care nurse rota against the fixed instance data and
recomputes the objective independently.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str).
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {}}
- "accepted" with an empty errors list is the only passing verdict.

Runs standalone: python clinic_roster_checker.py <payload.json>
"""

import json
import sys
from pathlib import Path

DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SHIFTS: tuple[str, ...] = ("day", "night")
NURSES: tuple[str, ...] = ("alex", "blair", "casey", "devon", "elliot")

NIGHT_QUALIFIED: frozenset[str] = frozenset({"alex", "blair", "devon"})
WEEKEND_DAY_QUALIFIED: frozenset[str] = frozenset({"alex", "casey", "elliot"})
WEEKEND_DAYS: frozenset[str] = frozenset({"sat", "sun"})

UNAVAILABLE: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("alex", "wed", "night"),
        ("blair", "mon", "day"),
        ("casey", "fri", "day"),
        ("devon", "sat", "day"),
        ("elliot", "sun", "day"),
    }
)

NIGHT_PENALTY: dict[str, int] = {"alex": 1, "blair": 2, "devon": 1}
WEEKEND_PENALTY: dict[str, int] = {
    "alex": 3,
    "blair": 3,
    "casey": 1,
    "devon": 2,
    "elliot": 1,
}
REQUEST_PENALTIES: dict[tuple[str, str, str], int] = {
    ("alex", "sun", "night"): 4,
    ("blair", "thu", "night"): 3,
    ("casey", "mon", "day"): 2,
    ("devon", "fri", "night"): 2,
    ("elliot", "sat", "day"): 2,
}

MIN_SHIFTS_PER_NURSE = 2
MAX_SHIFTS_PER_NURSE = 4
FAIRNESS_WEIGHT = 2


def _shift_key(day: str, shift: str) -> str:
    return f"{day}_{shift}"


def _is_eligible(nurse: str, day: str, shift: str) -> bool:
    if (nurse, day, shift) in UNAVAILABLE:
        return False
    if shift == "night" and nurse not in NIGHT_QUALIFIED:
        return False
    return not (day in WEEKEND_DAYS and shift == "day" and nurse not in WEEKEND_DAY_QUALIFIED)


def _assignment_penalty(nurse: str, day: str, shift: str) -> int:
    penalty = REQUEST_PENALTIES.get((nurse, day, shift), 0)
    if shift == "night":
        penalty += NIGHT_PENALTY.get(nurse, 6)
    if day in WEEKEND_DAYS:
        penalty += WEEKEND_PENALTY[nurse]
    return penalty


def _check_number(label: str, actual: object, expected: int, errors: list[str]) -> None:
    if isinstance(actual, bool) or not isinstance(actual, int | float):
        errors.append(f"{label} is not numeric: {actual!r}")
    elif actual != expected:
        errors.append(f"{label} mismatch: expected {expected}, got {actual}")


def _solution_assignments(solution: object, errors: list[str]) -> dict[str, str]:
    if not isinstance(solution, dict):
        errors.append("solution is not a JSON object")
        return {}

    assignments = solution.get("assignments")
    if not isinstance(assignments, dict):
        errors.append("solution.assignments must be a JSON object")
        return {}
    if not all(
        isinstance(key, str) and isinstance(value, str) for key, value in assignments.items()
    ):
        errors.append("solution.assignments must map shift keys to nurse ids")
        return {}

    return dict(assignments)


def _validate_assignment_keys(assignments: dict[str, str], errors: list[str]) -> None:
    expected_keys = {_shift_key(day, shift) for day in DAYS for shift in SHIFTS}
    actual_keys = set(assignments)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if missing:
        errors.append(f"missing shifts: {', '.join(missing)}")
    if extra:
        errors.append(f"unknown shifts: {', '.join(extra)}")


def _validate_roster(assignments: dict[str, str], errors: list[str]) -> dict[str, int]:
    counts = {nurse: 0 for nurse in NURSES}
    nurse_day_shifts: dict[tuple[str, str], list[str]] = {}

    for day in DAYS:
        for shift in SHIFTS:
            key = _shift_key(day, shift)
            nurse = assignments.get(key)
            if nurse is None:
                continue
            if nurse not in counts:
                errors.append(f"{key} has unknown nurse: {nurse}")
                continue
            if not _is_eligible(nurse, day, shift):
                errors.append(f"{nurse} is not eligible for {key}")
            counts[nurse] += 1
            nurse_day_shifts.setdefault((nurse, day), []).append(shift)

    for (nurse, day), shifts in sorted(nurse_day_shifts.items()):
        if len(shifts) > 1:
            errors.append(f"{nurse} works multiple shifts on {day}: {', '.join(sorted(shifts))}")

    for nurse in NURSES:
        for index, day in enumerate(DAYS[:-1]):
            next_day = DAYS[index + 1]
            if assignments.get(_shift_key(day, "night")) == nurse and (
                assignments.get(_shift_key(next_day, "day")) == nurse
            ):
                errors.append(f"{nurse} works {day}_night then {next_day}_day without rest")

        for index in range(len(DAYS) - 2):
            night_run = [
                DAYS[index + offset]
                for offset in range(3)
                if assignments.get(_shift_key(DAYS[index + offset], "night")) == nurse
            ]
            if len(night_run) == 3:
                errors.append(f"{nurse} works three consecutive nights: {', '.join(night_run)}")

        if counts[nurse] < MIN_SHIFTS_PER_NURSE:
            errors.append(f"{nurse} works too few shifts: {counts[nurse]} < {MIN_SHIFTS_PER_NURSE}")
        if counts[nurse] > MAX_SHIFTS_PER_NURSE:
            errors.append(
                f"{nurse} works too many shifts: {counts[nurse]} > {MAX_SHIFTS_PER_NURSE}"
            )

    return counts


def main() -> None:
    if len(sys.argv) != 2:
        print(
            json.dumps(
                {"status": "error", "errors": ["expected payload path argument"], "details": {}}
            )
        )
        return

    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    solution = payload.get("solution", {})
    errors: list[str] = []
    assignments = _solution_assignments(solution, errors)
    _validate_assignment_keys(assignments, errors)
    shift_counts = _validate_roster(assignments, errors)

    total_preference_penalty = 0
    for day in DAYS:
        for shift in SHIFTS:
            nurse = assignments.get(_shift_key(day, shift))
            if nurse in NURSES:
                total_preference_penalty += _assignment_penalty(nurse, day, shift)
    fairness_penalty = FAIRNESS_WEIGHT * (max(shift_counts.values()) - min(shift_counts.values()))
    total_penalty = total_preference_penalty + fairness_penalty

    if isinstance(solution, dict):
        _check_number(
            "solution.total_preference_penalty",
            solution.get("total_preference_penalty"),
            total_preference_penalty,
            errors,
        )
        _check_number(
            "solution.fairness_penalty",
            solution.get("fairness_penalty"),
            fairness_penalty,
            errors,
        )
        _check_number(
            "solution.total_penalty",
            solution.get("total_penalty"),
            total_penalty,
            errors,
        )
        reported_counts = solution.get("shift_counts")
        if reported_counts != shift_counts:
            errors.append(
                f"solution.shift_counts mismatch: expected {shift_counts}, got {reported_counts}"
            )
    _check_number("objective", payload.get("objective"), total_penalty, errors)

    details = {
        "shift_count": len(DAYS) * len(SHIFTS),
        "shift_counts": shift_counts,
        "total_preference_penalty": total_preference_penalty,
        "fairness_penalty": fairness_penalty,
        "total_penalty": total_penalty,
    }
    status = "accepted" if not errors else "rejected"
    print(json.dumps({"status": status, "errors": errors, "details": details}))


if __name__ == "__main__":
    main()
