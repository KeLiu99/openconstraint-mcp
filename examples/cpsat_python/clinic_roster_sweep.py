"""Sweepable CP-SAT example: urgent-care clinic nurse rostering.

Build a 7-day rota with one day shift and one night shift per day. Respect skill
coverage, nurse time off, rest after nights, and workload bounds while minimizing
preference penalties plus a small fairness penalty.

Runs standalone: python clinic_roster_sweep.py
Pair with clinic_roster_checker.py to demonstrate run_cpsat_python_sweep with a
checker gate on a practical scheduling CSP.
"""

import json
import os

from ortools.sat.python import cp_model

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


def _sweep_seed() -> int:
    return int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))


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


def main() -> None:
    model = cp_model.CpModel()
    assigned = {
        (nurse, day, shift): model.new_bool_var(f"{nurse}_{day}_{shift}")
        for nurse in NURSES
        for day in DAYS
        for shift in SHIFTS
    }

    for day in DAYS:
        for shift in SHIFTS:
            model.add_exactly_one(assigned[nurse, day, shift] for nurse in NURSES)
            for nurse in NURSES:
                if not _is_eligible(nurse, day, shift):
                    model.add(assigned[nurse, day, shift] == 0)

    for nurse in NURSES:
        for day in DAYS:
            model.add_at_most_one(assigned[nurse, day, shift] for shift in SHIFTS)

        for index, day in enumerate(DAYS[:-1]):
            next_day = DAYS[index + 1]
            model.add(assigned[nurse, day, "night"] + assigned[nurse, next_day, "day"] <= 1)

        for index in range(len(DAYS) - 2):
            three_night_window = (
                assigned[nurse, DAYS[index + offset], "night"] for offset in range(3)
            )
            model.add(sum(three_night_window) <= 2)

    nurse_loads = []
    for nurse in NURSES:
        load = sum(assigned[nurse, day, shift] for day in DAYS for shift in SHIFTS)
        model.add(load >= MIN_SHIFTS_PER_NURSE)
        model.add(load <= MAX_SHIFTS_PER_NURSE)
        nurse_loads.append(load)

    max_load = model.new_int_var(MIN_SHIFTS_PER_NURSE, MAX_SHIFTS_PER_NURSE, "max_load")
    min_load = model.new_int_var(MIN_SHIFTS_PER_NURSE, MAX_SHIFTS_PER_NURSE, "min_load")
    model.add_max_equality(max_load, nurse_loads)
    model.add_min_equality(min_load, nurse_loads)

    preference_penalty = sum(
        _assignment_penalty(nurse, day, shift) * assigned[nurse, day, shift]
        for nurse in NURSES
        for day in DAYS
        for shift in SHIFTS
    )
    fairness_penalty = FAIRNESS_WEIGHT * (max_load - min_load)
    model.minimize(preference_penalty + fairness_penalty)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = _sweep_seed()
    solver.parameters.randomize_search = True
    solver.parameters.search_branching = cp_model.PORTFOLIO_WITH_QUICK_RESTART_SEARCH

    status_code = solver.solve(model)
    status_map = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }

    solution = {}
    objective = None
    if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = {}
        for day in DAYS:
            for shift in SHIFTS:
                for nurse in NURSES:
                    if solver.value(assigned[nurse, day, shift]):
                        assignments[_shift_key(day, shift)] = nurse

        shift_counts = {
            nurse: sum(
                1
                for day in DAYS
                for shift in SHIFTS
                if assignments[_shift_key(day, shift)] == nurse
            )
            for nurse in NURSES
        }
        total_preference_penalty = sum(
            _assignment_penalty(nurse, day, shift)
            for day in DAYS
            for shift in SHIFTS
            for nurse in (assignments[_shift_key(day, shift)],)
        )
        total_fairness_penalty = FAIRNESS_WEIGHT * (
            max(shift_counts.values()) - min(shift_counts.values())
        )
        total_penalty = total_preference_penalty + total_fairness_penalty
        solution = {
            "assignments": assignments,
            "shift_counts": shift_counts,
            "total_preference_penalty": total_preference_penalty,
            "fairness_penalty": total_fairness_penalty,
            "total_penalty": total_penalty,
        }
        objective = total_penalty

    print(
        json.dumps(
            {
                "status": status_map.get(status_code, "error"),
                "objective": objective,
                "solution": solution,
            }
        )
    )


if __name__ == "__main__":
    main()
